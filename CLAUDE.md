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
- Backend: Push to `main` → trigger `scalpengine-deploy` runs `cloudbuild.yaml`
  (test → build → push → deploy → verify). `verify` fails the build if the live
  service differs from the yaml — the deploy has already happened by then
- Frontend: Push to `main` → Cloudflare Pages auto-deploys
- Set VITE_API_URL in Cloudflare Pages env vars to point at Cloud Run URL
- Every Cloud Run flag lives in `cloudbuild.yaml`. Never set one by hand, and
  never create a trigger with the Cloud Run wizard — it deploys image and
  labels only. Triggers use `--build-config=cloudbuild.yaml`.
- Project `chimera-v4`, region `europe-west2`. Pass `--project=chimera-v4`.

## Environment Variables (Cloud Run)
- BETFAIR_APP_KEY (Secret Manager: `scalpengine-betfair-app-key` — never a plain env var)
- FRONTEND_URL (Cloudflare Pages domain — must match the real frontend origin or CORS fails)
- GCS_BUCKET (state persistence bucket)
- DRY_RUN (true/false)
- POLL_INTERVAL (seconds, default 15)
- REQUIRE_AUTH (true/false, default false — closes the API to unauthenticated callers)

## Cloud Run Settings
Locked in `cloudbuild.yaml`: `--min-instances=1 --max-instances=1
--no-cpu-throttling`. The scan loop is a background thread (stalls if
throttled) and the engine is a singleton (a second instance races it).

## Tests
`bash backend/tests/run_all.sh` — Betfair is stubbed; no network or
credentials. Runs as the first Cloud Build step; a failing, crashing or empty
suite stops the deploy. No local builds — verify in Cloud Build.

## Claude Code Rules
- Do NOT touch wrangler.jsonc
- `main` only, never branch. README and CHANGELOG updated in every commit
- Check the diff for secrets before every push
- Never create a cloud resource without Charles approving its name first
- Never start the Betfair session or place a real bet without asking
