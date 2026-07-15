"""
Tally Suite — Licence Server
FastAPI + PostgreSQL
"""

import os, hashlib, secrets, string
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:admin%40123@localhost:5432/tallydb")
ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN",  "change-this-admin-token")
TRIAL_DAYS   = int(os.getenv("TRIAL_DAYS", "30"))

app = FastAPI(title="Tally Suite Licence Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DB pool ───────────────────────────────────────────────────────────────────
pool: asyncpg.Pool = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS machines (
                machine_id   TEXT PRIMARY KEY,
                first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                licensed     BOOLEAN NOT NULL DEFAULT FALSE,
                licence_key  TEXT,
                activated_at TIMESTAMPTZ,
                note         TEXT
            );
            CREATE TABLE IF NOT EXISTS licence_keys (
                key_hash   TEXT PRIMARY KEY,
                label      TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                used_by    TEXT,
                used_at    TIMESTAMPTZ,
                revoked    BOOLEAN NOT NULL DEFAULT FALSE
            );
        """)

@app.on_event("shutdown")
async def shutdown():
    await pool.close()

# ── Include routers (AFTER app and pool are defined) ──────────────────────────
from tally_proxy    import router as tally_router
from subscription_mgr import router as sub_router
from auth_middleware  import router as auth_router, require_auth
from converter_api    import router as conv_router

app.include_router(tally_router)
app.include_router(sub_router)
app.include_router(auth_router)
app.include_router(conv_router)

# ── Static file routes ────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
async def serve_portal():
    return FileResponse(os.path.join(BASE, "portal.html"))

@app.get("/tool")
@app.get("/bank_to_tally.html")
async def serve_tool(request: Request):
    user = await require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return FileResponse(os.path.join(BASE, "bank_to_tally.html"))

@app.get("/tally_bridge.exe")
async def serve_bridge():
    f = os.path.join(BASE, "dist", "tally_bridge.exe")
    if os.path.exists(f):
        return FileResponse(f, media_type="application/octet-stream")
    return JSONResponse({"detail": "Bridge not built yet."}, status_code=404)

@app.get("/health")
async def health():
    return {"ok": True}

# ── Helpers ───────────────────────────────────────────────────────────────────
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def days_since(ts: datetime) -> int:
    return (datetime.now(timezone.utc) - ts).days

def require_admin(token: str = Header(None, alias="X-Admin-Token")):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")

# ── Schemas ───────────────────────────────────────────────────────────────────
class MachineRequest(BaseModel):
    machine_id: str

class ActivateRequest(BaseModel):
    machine_id:  str
    licence_key: str

class KeygenRequest(BaseModel):
    label: Optional[str] = None

class RevokeRequest(BaseModel):
    key_hash: str

# ── Licence endpoints ─────────────────────────────────────────────────────────
@app.post("/api/licence/init")
async def init_machine(req: MachineRequest):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM machines WHERE machine_id=$1", req.machine_id)
        if not row:
            await conn.execute("INSERT INTO machines(machine_id) VALUES($1)", req.machine_id)
            row = await conn.fetchrow("SELECT * FROM machines WHERE machine_id=$1", req.machine_id)
        else:
            await conn.execute("UPDATE machines SET last_seen=NOW() WHERE machine_id=$1", req.machine_id)
        days_used   = days_since(row["first_seen"])
        days_remain = max(0, TRIAL_DAYS - days_used)
        return {
            "status":      "licensed" if row["licensed"] else ("trial" if days_remain > 0 else "expired"),
            "licensed":    row["licensed"],
            "days_used":   days_used,
            "days_remain": days_remain,
            "trial_days":  TRIAL_DAYS,
            "first_seen":  row["first_seen"].isoformat(),
        }

@app.post("/api/licence/check")
async def check_licence(req: MachineRequest):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM machines WHERE machine_id=$1", req.machine_id)
        if not row:
            raise HTTPException(status_code=404, detail="Machine not registered.")
        await conn.execute("UPDATE machines SET last_seen=NOW() WHERE machine_id=$1", req.machine_id)
        days_used   = days_since(row["first_seen"])
        days_remain = max(0, TRIAL_DAYS - days_used)
        return {
            "status":      "licensed" if row["licensed"] else ("trial" if days_remain > 0 else "expired"),
            "licensed":    row["licensed"],
            "days_used":   days_used,
            "days_remain": days_remain,
            "trial_days":  TRIAL_DAYS,
        }

@app.post("/api/licence/activate")
async def activate(req: ActivateRequest):
    key_hash = sha256(req.licence_key.strip().upper())
    async with pool.acquire() as conn:
        key_row = await conn.fetchrow("SELECT * FROM licence_keys WHERE key_hash=$1", key_hash)
        if not key_row:
            raise HTTPException(status_code=400, detail="Invalid licence key.")
        if key_row["revoked"]:
            raise HTTPException(status_code=400, detail="This key has been revoked.")
        if key_row["used_by"] and key_row["used_by"] != req.machine_id:
            raise HTTPException(status_code=400, detail="Key already used on another machine.")
        await conn.execute("UPDATE licence_keys SET used_by=$1, used_at=NOW() WHERE key_hash=$2",
                           req.machine_id, key_hash)
        await conn.execute("UPDATE machines SET licensed=TRUE, licence_key=$1, activated_at=NOW() WHERE machine_id=$2",
                           key_hash, req.machine_id)
        return {"status": "activated", "message": "Licence activated successfully."}

@app.get("/api/licence/keys")
async def list_keys(x_admin_token: str = Header(None)):
    require_admin(x_admin_token)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM licence_keys ORDER BY created_at DESC")
        return [dict(r) for r in rows]

@app.post("/api/licence/keygen")
async def generate_key(req: KeygenRequest, x_admin_token: str = Header(None)):
    require_admin(x_admin_token)
    chars = string.ascii_uppercase + string.digits
    parts = [''.join(secrets.choice(chars) for _ in range(5)) for _ in range(3)]
    key   = "TALLY-" + "-".join(parts)
    key_hash = sha256(key)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO licence_keys(key_hash, label) VALUES($1,$2)",
                           key_hash, req.label or key)
    return {"key": key, "key_hash": key_hash, "label": req.label or key}

@app.delete("/api/licence/key")
async def revoke_key(req: RevokeRequest, x_admin_token: str = Header(None)):
    require_admin(x_admin_token)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE licence_keys SET revoked=TRUE WHERE key_hash=$1", req.key_hash)
    return {"revoked": True}
