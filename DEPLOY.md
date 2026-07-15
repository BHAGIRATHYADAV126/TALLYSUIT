# Tally Suite — Deployment Guide

## Security Model

| File | Location | Customer can see? |
|------|----------|-------------------|
| licence_server.py | Render server | ❌ No |
| subscription_mgr.py | Render server | ❌ No |
| tally_proxy.py | Render server | ❌ No |
| auth_middleware.py | Render server | ❌ No |
| bank_to_tally.html | Served by FastAPI after login | ⚠️ HTML only (Python logic is base64) |
| portal.html | Vercel (public landing page) | ✅ Yes (no sensitive logic) |

## Deploy Steps

### 1. Neon (Free PostgreSQL)
- Sign up at neon.tech
- Create project: `tallysuite`, database: `tallydb`
- Copy connection string

### 2. GitHub (Private repo)
- Create PRIVATE repo: `tally-suite`
- Upload all .py files + HTML files + render.yaml

### 3. Render (Free backend)
- Connect GitHub repo
- Add environment variables (never hardcode):
  - DATABASE_URL (from Neon)
  - ADMIN_TOKEN (your secret)
  - TRIAL_DAYS=30
  - RAZORPAY_KEY_ID
  - RAZORPAY_KEY_SECRET

### 4. Vercel (Free frontend)  
- Deploy only portal.html (public landing page)
- Update SERVER URL to your Render URL

## What customers can NOT see
- Your Python business logic
- Database credentials
- Admin token
- Razorpay keys
- Other customers' data
