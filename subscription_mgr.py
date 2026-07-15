"""
Tally Suite — Subscription Manager
Handles Razorpay plans, subscriptions, webhooks, and expiry logic.

Add to licence_server.py:
  from subscription_mgr import router as sub_router
  app.include_router(sub_router)

Razorpay setup:
  1. Create account at razorpay.com
  2. Settings → API Keys → generate Key ID + Secret
  3. Add to .env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET
  4. Dashboard → Webhooks → add https://yourdomain.com/api/payment/webhook
     Events: subscription.activated, subscription.charged, subscription.cancelled, payment.failed
"""

import os, hmac, hashlib, json, secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()

RAZORPAY_KEY_ID      = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET  = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
ADMIN_TOKEN          = os.getenv("ADMIN_TOKEN", "")
TRIAL_DAYS           = int(os.getenv("TRIAL_DAYS", "30"))

router = APIRouter(prefix="/api/payment", tags=["subscription"])

# ── Plans (create these in Razorpay dashboard too) ───────────────────────────
PLANS = {
    "starter_monthly": {
        "name":        "Starter Monthly",
        "price":       49900,   # paise = ₹499
        "interval":    "monthly",
        "features":    ["Bank import", "Tally push", "1 company"],
        "razorpay_plan_id": os.getenv("PLAN_STARTER_MONTHLY", ""),
    },
    "starter_yearly": {
        "name":        "Starter Yearly",
        "price":       399900,  # ₹3999
        "interval":    "yearly",
        "features":    ["Bank import", "Tally push", "1 company", "2 months free"],
        "razorpay_plan_id": os.getenv("PLAN_STARTER_YEARLY", ""),
    },
    "pro_monthly": {
        "name":        "Pro Monthly",
        "price":       99900,   # ₹999
        "interval":    "monthly",
        "features":    ["Bank import", "Bulk vouchers", "GST recon", "3 companies"],
        "razorpay_plan_id": os.getenv("PLAN_PRO_MONTHLY", ""),
    },
    "pro_yearly": {
        "name":        "Pro Yearly",
        "price":       799900,  # ₹7999
        "interval":    "yearly",
        "features":    ["Bank import", "Bulk vouchers", "GST recon", "3 companies", "2 months free"],
        "razorpay_plan_id": os.getenv("PLAN_PRO_YEARLY", ""),
    },
}

# ── DB helpers (uses pool from licence_server) ───────────────────────────────
async def get_pool():
    import licence_server
    return licence_server.pool

async def ensure_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id           SERIAL PRIMARY KEY,
                email        TEXT UNIQUE NOT NULL,
                name         TEXT,
                phone        TEXT,
                gstin        TEXT,
                password_hash TEXT,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                machine_id   TEXT
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id                  SERIAL PRIMARY KEY,
                customer_id         INT REFERENCES customers(id),
                plan_key            TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'trial',
                trial_start         TIMESTAMPTZ DEFAULT NOW(),
                trial_end           TIMESTAMPTZ,
                current_period_start TIMESTAMPTZ,
                current_period_end   TIMESTAMPTZ,
                razorpay_sub_id     TEXT,
                razorpay_plan_id    TEXT,
                cancelled_at        TIMESTAMPTZ,
                created_at          TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS payments (
                id              SERIAL PRIMARY KEY,
                customer_id     INT REFERENCES customers(id),
                subscription_id INT REFERENCES subscriptions(id),
                razorpay_payment_id TEXT,
                amount          INT,
                currency        TEXT DEFAULT 'INR',
                status          TEXT,
                paid_at         TIMESTAMPTZ DEFAULT NOW(),
                raw_event       JSONB
            );
        """)


# ── Razorpay API helper ───────────────────────────────────────────────────────
async def razorpay_post(endpoint: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.razorpay.com/v1/{endpoint}",
            json=payload,
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Razorpay error: {resp.text}")
    return resp.json()


def verify_razorpay_signature(body: bytes, signature: str) -> bool:
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Schemas ───────────────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    email:    str
    name:     str
    phone:    Optional[str] = None
    password: str
    machine_id: Optional[str] = None

class LoginRequest(BaseModel):
    email:    str
    password: str
    machine_id: Optional[str] = None

class SubscribeRequest(BaseModel):
    customer_id: int
    plan_key:    str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans():
    """Return available plans (used by landing page)."""
    return [
        {
            "key":       k,
            "name":      v["name"],
            "price":     v["price"],
            "price_fmt": f"₹{v['price']//100}",
            "interval":  v["interval"],
            "features":  v["features"],
        }
        for k, v in PLANS.items()
    ]


@router.post("/signup")
async def signup(req: SignupRequest):
    """Register a new customer and start trial."""
    await ensure_tables()
    pool = await get_pool()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    try:
        async with pool.acquire() as conn:
            cust = await conn.fetchrow("""
                INSERT INTO customers(email, name, phone, password_hash, machine_id)
                VALUES($1,$2,$3,$4,$5)
                RETURNING id, email, name
            """, req.email.lower(), req.name, req.phone, pwd_hash, req.machine_id)

            trial_end = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
            sub = await conn.fetchrow("""
                INSERT INTO subscriptions(customer_id, plan_key, status, trial_end)
                VALUES($1,'trial','trial',$2)
                RETURNING id
            """, cust["id"], trial_end)

        return {
            "customer_id":   cust["id"],
            "email":         cust["email"],
            "name":          cust["name"],
            "status":        "trial",
            "trial_days":    TRIAL_DAYS,
            "trial_end":     trial_end.isoformat(),
        }
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Email already registered.")


@router.post("/login")
async def login(req: LoginRequest):
    """Login and return subscription status."""
    pool = await get_pool()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    async with pool.acquire() as conn:
        cust = await conn.fetchrow(
            "SELECT * FROM customers WHERE email=$1 AND password_hash=$2",
            req.email.lower(), pwd_hash)
        if not cust:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        # Update machine_id if provided
        if req.machine_id:
            await conn.execute(
                "UPDATE customers SET machine_id=$1 WHERE id=$2",
                req.machine_id, cust["id"])

        sub = await conn.fetchrow("""
            SELECT * FROM subscriptions
            WHERE customer_id=$1
            ORDER BY created_at DESC LIMIT 1
        """, cust["id"])

        now = datetime.now(timezone.utc)
        status = sub["status"] if sub else "none"
        days_remain = 0
        if status == "trial" and sub["trial_end"]:
            days_remain = max(0, (sub["trial_end"] - now).days)
            if days_remain == 0:
                status = "expired"

        return {
            "customer_id":     cust["id"],
            "email":           cust["email"],
            "name":            cust["name"],
            "status":          status,
            "days_remain":     days_remain,
            "plan_key":        sub["plan_key"] if sub else None,
            "period_end":      sub["current_period_end"].isoformat() if sub and sub["current_period_end"] else None,
        }


@router.post("/subscribe")
async def create_subscription(req: SubscribeRequest):
    """Create a Razorpay subscription for the customer."""
    if req.plan_key not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan.")
    plan = PLANS[req.plan_key]
    if not plan["razorpay_plan_id"]:
        raise HTTPException(status_code=503, detail="Razorpay plan not configured yet.")

    pool = await get_pool()
    async with pool.acquire() as conn:
        cust = await conn.fetchrow("SELECT * FROM customers WHERE id=$1", req.customer_id)
        if not cust:
            raise HTTPException(status_code=404, detail="Customer not found.")

    rp_sub = await razorpay_post("subscriptions", {
        "plan_id":        plan["razorpay_plan_id"],
        "total_count":    120,   # months/years to allow
        "quantity":       1,
        "customer_notify": 1,
        "notes":          {"customer_id": str(req.customer_id), "plan": req.plan_key},
    })

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subscriptions(customer_id, plan_key, status, razorpay_sub_id, razorpay_plan_id)
            VALUES($1,$2,'created',$3,$4)
        """, req.customer_id, req.plan_key, rp_sub["id"], plan["razorpay_plan_id"])

    return {
        "razorpay_sub_id": rp_sub["id"],
        "razorpay_key":    RAZORPAY_KEY_ID,
        "plan_name":       plan["name"],
        "amount":          plan["price"],
        "short_url":       rp_sub.get("short_url", ""),
    }


@router.post("/webhook")
async def razorpay_webhook(request: Request):
    """Handle Razorpay webhook events."""
    body      = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if RAZORPAY_WEBHOOK_SECRET and not verify_razorpay_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    event = json.loads(body)
    pool  = await get_pool()

    evt_type = event.get("event", "")
    payload  = event.get("payload", {})

    async with pool.acquire() as conn:
        if evt_type == "subscription.activated":
            sub_data = payload.get("subscription", {}).get("entity", {})
            rp_sub_id = sub_data.get("id")
            start = datetime.fromtimestamp(sub_data.get("current_start", 0), tz=timezone.utc)
            end   = datetime.fromtimestamp(sub_data.get("current_end",   0), tz=timezone.utc)
            await conn.execute("""
                UPDATE subscriptions
                SET status='active', current_period_start=$1, current_period_end=$2
                WHERE razorpay_sub_id=$3
            """, start, end, rp_sub_id)

        elif evt_type == "subscription.charged":
            sub_data = payload.get("subscription", {}).get("entity", {})
            pmt_data = payload.get("payment",      {}).get("entity", {})
            rp_sub_id = sub_data.get("id")
            end = datetime.fromtimestamp(sub_data.get("current_end", 0), tz=timezone.utc)
            row = await conn.fetchrow(
                "SELECT id, customer_id FROM subscriptions WHERE razorpay_sub_id=$1", rp_sub_id)
            if row:
                await conn.execute("""
                    UPDATE subscriptions
                    SET status='active', current_period_end=$1
                    WHERE razorpay_sub_id=$2
                """, end, rp_sub_id)
                await conn.execute("""
                    INSERT INTO payments(customer_id, subscription_id, razorpay_payment_id, amount, status, raw_event)
                    VALUES($1,$2,$3,$4,'captured',$5)
                """, row["customer_id"], row["id"],
                    pmt_data.get("id"), pmt_data.get("amount"), json.dumps(event))

        elif evt_type in ("subscription.cancelled", "subscription.halted"):
            rp_sub_id = payload.get("subscription", {}).get("entity", {}).get("id")
            await conn.execute("""
                UPDATE subscriptions
                SET status='cancelled', cancelled_at=NOW()
                WHERE razorpay_sub_id=$1
            """, rp_sub_id)

        elif evt_type == "payment.failed":
            rp_sub_id = payload.get("subscription", {}).get("entity", {}).get("id", "")
            if rp_sub_id:
                await conn.execute("""
                    UPDATE subscriptions SET status='payment_failed'
                    WHERE razorpay_sub_id=$1
                """, rp_sub_id)

    return {"ok": True}


@router.get("/status/{customer_id}")
async def subscription_status(customer_id: int):
    """Get current subscription status for a customer."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        sub = await conn.fetchrow("""
            SELECT s.*, c.email, c.name
            FROM subscriptions s
            JOIN customers c ON c.id = s.customer_id
            WHERE s.customer_id=$1
            ORDER BY s.created_at DESC LIMIT 1
        """, customer_id)
        if not sub:
            raise HTTPException(status_code=404, detail="No subscription found.")

        now = datetime.now(timezone.utc)
        status = sub["status"]
        days_remain = 0
        if status == "trial" and sub["trial_end"]:
            days_remain = max(0, (sub["trial_end"] - now).days)
            if days_remain == 0: status = "expired"
        elif status == "active" and sub["current_period_end"]:
            days_remain = max(0, (sub["current_period_end"] - now).days)

        return {
            "customer_id":  customer_id,
            "email":        sub["email"],
            "name":         sub["name"],
            "status":       status,
            "plan_key":     sub["plan_key"],
            "days_remain":  days_remain,
            "period_end":   sub["current_period_end"].isoformat() if sub["current_period_end"] else None,
            "trial_end":    sub["trial_end"].isoformat() if sub["trial_end"] else None,
        }


# ── Admin ─────────────────────────────────────────────────────────────────────
@router.get("/admin/customers")
async def admin_customers(x_admin_token: str = Header(None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.id, c.email, c.name, c.phone, c.created_at,
                   s.status, s.plan_key, s.current_period_end, s.trial_end
            FROM customers c
            LEFT JOIN subscriptions s ON s.customer_id = c.id
            ORDER BY c.created_at DESC
        """)
    return [dict(r) for r in rows]
