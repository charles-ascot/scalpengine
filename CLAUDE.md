# CHIMERA Scalping Engine

## Architecture
- **Backend**: FastAPI/Python on Google Cloud Run (europe-west2), GCP project chimera-v4
- **Frontend**: React/Vite on Cloudflare Pages
- **State**: GCS bucket persistence (survives cold starts)
- **Auth**: Betfair SSO interactive login
- **Data Sources**: Betfair Exchange (direct), FSU-1X (The Odds API), FSU-1Y (The Racing API)

## Specification Reference
Built from CHI-SPC-002 (Chimera Scalping Strategy, April 2026):
- Part A: Strategy & Business Rules (A.1–A.18)
- Part B: Global Technical Specification (B.1–B.27)
- Part C: Bookmaker Trigger Extension Module (C.1–C.30)

## Key Files
- `backend/engine.py` — Core ScalpEngine (orchestration, loop, trade management)
- `backend/state_machines.py` — Trade/Order/Control state machines (B.9)
- `backend/candidate.py` — Candidate scoring model (A.6)
- `backend/trade_planner.py` — Staged entry + lay ladders (A.7, A.9)
- `backend/scenario_engine.py` — Pre-trade simulation (A.10, A.11)
- `backend/risk_engine.py` — 4-level risk controls (B.13)
- `backend/position.py` — Position & P&L tracking (B.15)
- `backend/bookmaker_trigger.py` — Bookmaker trigger extension (Part C)
- `backend/betfair_client.py` — Extended Betfair API client (back/lay/cancel/replace/depth)
- `backend/main.py` — FastAPI server

## Deploy
- Backend: Push to GitHub → Cloud Run auto-deploys
- Frontend: Push to GitHub → Cloudflare Pages auto-deploys
- Set VITE_API_URL in Cloudflare Pages env vars to point at Cloud Run URL

## Environment Variables (Cloud Run)
- BETFAIR_APP_KEY
- FRONTEND_URL (Cloudflare Pages domain — must match the real frontend origin or CORS fails)
- GCS_BUCKET (state persistence bucket)
- DRY_RUN (true/false)
- POLL_INTERVAL (seconds, default 15)
- REQUIRE_AUTH (true/false, default false — closes the API to unauthenticated callers)

## Cloud Run Settings
The scan loop is a background thread, so the service needs CPU always
allocated and a warm instance or it stalls between requests:
`--no-cpu-throttling --min-instances=1`

## Tests
`./backend/tests/run_all.sh` — Betfair is stubbed, so no network or
credentials are needed. Run it before pushing backend changes.

## Claude Code Rules
- Do NOT touch wrangler.jsonc or platform config
- Scope: VSCode + GitHub only
- Cloudflare Pages and Cloud Run handle their own deploys
