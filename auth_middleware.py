"""
Tally Suite — Auth Middleware
Protects HTML tool routes behind login.
Sessions stored in PostgreSQL.
"""

import os, hashlib, secrets, json
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from pydantic import BaseModel

router = APIRouter(tags=["auth"])

SESSION_HOURS = 8      # session expires after 8 hours
COOKIE_NAME   = "ts_session"

# ── helpers ───────────────────────────────────────────────────────────────────
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

async def get_pool():
    import licence_server
    return licence_server.pool

async def ensure_session_table():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                customer_id INT NOT NULL,
                email       TEXT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL,
                ip          TEXT
            );
        """)

async def create_session(customer_id: int, email: str, ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO sessions(token, customer_id, email, expires_at, ip)
            VALUES($1,$2,$3,$4,$5)
        """, token, customer_id, email, expires, ip)
    return token

async def validate_session(token: str) -> Optional[dict]:
    if not token: return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT s.*, sub.status, sub.trial_end, sub.current_period_end
            FROM sessions s
            JOIN customers c ON c.id = s.customer_id
            LEFT JOIN subscriptions sub ON sub.customer_id = s.customer_id
            WHERE s.token=$1 AND s.expires_at > NOW()
            ORDER BY sub.created_at DESC
            LIMIT 1
        """, token)
        if not row: return None
        # Check subscription is active or in trial
        status = row["status"]
        if status == "trial":
            if row["trial_end"] and row["trial_end"] < datetime.now(timezone.utc):
                return None  # trial expired
        elif status not in ("active",):
            return None  # cancelled/expired
        return dict(row)

async def delete_session(token: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token=$1", token)

# ── Login page HTML ───────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tally Suite — Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f7f6f2;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:'IBM Plex Sans',sans-serif}
.card{background:#fff;border:1px solid #dddbd4;border-radius:10px;padding:44px 48px;width:100%;max-width:400px;box-shadow:0 4px 24px rgba(0,0,0,.06)}
.logo{font-size:22px;font-weight:700;margin-bottom:4px}
.logo span{color:#1a6b3c}
.sub{font-size:13px;color:#4a5168;margin-bottom:28px}
label{display:block;font-size:12px;font-weight:500;color:#4a5168;margin-bottom:4px}
input{width:100%;padding:10px 13px;border:1px solid #dddbd4;border-radius:5px;font-size:14px;font-family:inherit;outline:none;margin-bottom:14px}
input:focus{border-color:#1a6b3c}
.btn{width:100%;padding:12px;background:#1a1f2e;color:#fff;border:none;border-radius:5px;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{background:#1a6b3c}
.msg{font-size:12px;text-align:center;margin-top:12px;min-height:18px;color:#b84c00}
.signup{text-align:center;margin-top:16px;font-size:13px;color:#4a5168}
.signup a{color:#1a6b3c;text-decoration:none;font-weight:500}
@keyframes sp{to{transform:rotate(360deg)}}
.sp{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite;vertical-align:middle;margin-right:6px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Tally<span>Suite</span></div>
  <div class="sub">Sign in to access your tools</div>
  <label>Email</label>
  <input type="email" id="email" placeholder="you@example.com" autofocus>
  <label>Password</label>
  <input type="password" id="pwd" placeholder="Your password">
  <button class="btn" id="btn" onclick="doLogin()">Sign In</button>
  <div class="msg" id="msg"></div>
  <div class="signup">Don't have an account? <a href="/">Sign up free</a></div>
</div>
<script>
async function doLogin(){
  var btn=document.getElementById('btn');
  var msg=document.getElementById('msg');
  var email=document.getElementById('email').value.trim();
  var pwd=document.getElementById('pwd').value;
  if(!email||!pwd){msg.textContent='Please enter email and password.';return;}
  btn.disabled=true;btn.innerHTML='<span class="sp"></span>Signing in…';
  try{
    var res=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:email,password:pwd})});
    var data=await res.json();
    if(res.ok){
      window.location.href=data.redirect||'/tool';
    }else{
      msg.textContent='✗ '+(data.detail||'Invalid email or password.');
      btn.disabled=false;btn.innerHTML='Sign In';
    }
  }catch(e){msg.textContent='✗ Cannot reach server.';btn.disabled=false;btn.innerHTML='Sign In';}
}
document.addEventListener('keydown',function(e){if(e.key==='Enter')doLogin();});
</script>
</body>
</html>"""

# ── Schemas ───────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email:    str
    password: str

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML)

@router.post("/auth/login")
async def auth_login(req: LoginRequest, request: Request, response: Response):
    await ensure_session_table()
    pool = await get_pool()
    pwd_hash = sha256(req.password)

    async with pool.acquire() as conn:
        cust = await conn.fetchrow(
            "SELECT * FROM customers WHERE email=$1 AND password_hash=$2",
            req.email.lower(), pwd_hash)

    if not cust:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    # Check subscription status
    async with pool.acquire() as conn:
        sub = await conn.fetchrow("""
            SELECT * FROM subscriptions
            WHERE customer_id=$1 ORDER BY created_at DESC LIMIT 1
        """, cust["id"])

    if not sub:
        raise HTTPException(status_code=403, detail="No subscription found. Please sign up.")

    now = datetime.now(timezone.utc)
    status = sub["status"]
    if status == "trial":
        if sub["trial_end"] and sub["trial_end"] < now:
            raise HTTPException(status_code=403,
                detail="Your trial has expired. Please subscribe to continue.")
    elif status not in ("active",):
        raise HTTPException(status_code=403,
            detail="Your subscription is inactive. Please renew.")

    # Create session
    ip = request.client.host if request.client else ""
    token = await create_session(cust["id"], cust["email"], ip)

    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True,   # not accessible via JS
        secure=True,     # HTTPS only
        samesite="lax",
        max_age=SESSION_HOURS * 3600
    )
    return {"ok": True, "redirect": "/tool", "email": cust["email"]}


@router.get("/auth/logout")
async def auth_logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME, "")
    if token: await delete_session(token)
    response.delete_cookie(COOKIE_NAME)
    return RedirectResponse("/login")


@router.get("/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    user = await validate_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return {"email": user["email"], "status": user["status"]}


# ── Middleware function to protect routes ─────────────────────────────────────
async def require_auth(request: Request) -> Optional[dict]:
    """Call this in any protected route to validate session."""
    token = request.cookies.get(COOKIE_NAME, "")
    user = await validate_session(token)
    return user
