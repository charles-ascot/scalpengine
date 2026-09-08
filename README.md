# CHIMERA Scalping Engine

Pre-race scalping engine for the Betfair Exchange. A FastAPI backend runs the
strategy loop and holds state; a React dashboard drives it.

Built from **CHI-SPC-002** (Chimera Scalping Strategy, April 2026) — Part A
(strategy & business rules), Part B (global technical spec), Part C (bookmaker
trigger extension).

> **Trading software.** It places real money bets when `DRY_RUN=false`. Read
> [Operational requirements](#operational-requirements) and
> [Known limitations](#known-limitations) before going live.

---

## Architecture

```
┌─────────────────────┐         ┌──────────────────────┐
│  React + Vite UI    │  HTTPS  │  FastAPI backend     │
│  Cloudflare Pages   │ ──────► │  Cloud Run           │
│  scalpengine.       │         │  europe-west2        │
│  thync.online       │         │  GCP: chimera-v4     │
└─────────────────────┘         └──────────┬───────────┘
                                           │
                        ┌──────────────────┼──────────────────┐
                        ▼                  ▼                  ▼
                 Betfair Exchange     GCS bucket         FSU feeds
                 (auth, prices,       (state across      (odds / racing
                  bet placement)       cold starts)       reference data)
```

State lives in a GCS bucket rather than the container, so it survives the
cold starts that Cloud Run's scale-to-zero causes.

## Repository layout

| Path | Purpose |
|---|---|
| `backend/main.py` | FastAPI app, routes, auth |
| `backend/engine.py` | `ScalpEngine` — orchestration, scan loop, trade management |
| `backend/state_machines.py` | Trade / order / control state machines (B.9) |
| `backend/candidate.py` | Candidate scoring model (A.6) |
| `backend/trade_planner.py` | Staged entry + lay ladders (A.7, A.9) |
| `backend/scenario_engine.py` | Pre-trade simulation (A.10, A.11) |
| `backend/risk_engine.py` | Four-level risk controls (B.13) |
| `backend/position.py` | Position and P&L tracking (B.15) |
| `backend/bookmaker_trigger.py` | Bookmaker trigger extension (Part C) |
| `backend/betfair_client.py` | Betfair API client (back/lay/cancel/replace/depth) |
| `backend/fsu_client.py` | FSU-1X / FSU-1Y data clients |
| `frontend/src/App.jsx` | Entire dashboard UI |

## Quick start

Backend (Python 3.12, matching the container):

```bash
cd backend && pip install -r requirements.txt && uvicorn main:app --port 8080 --reload
```

Frontend, in a second shell:

```bash
cd frontend && npm install && npm run dev
```

Vite proxies `/api` to `localhost:8080`, so no `VITE_API_URL` is needed
locally. The backend's CORS allow-list already includes `localhost:5173`.

## Environment variables

### Backend (Cloud Run)

| Variable | Default | Purpose |
|---|---|---|
| `BETFAIR_APP_KEY` | — | Betfair application key. Required. |
| `FRONTEND_URL` | `https://scalping.thync.online` | CORS origin. **Must** be set to the real frontend domain. |
| `GCS_BUCKET` | — | Bucket for state persistence (production: `chimera-scalping-state`). Without it, state dies with the container. |
| `DRY_RUN` | `true` | `false` places real bets. **Overridden by persisted state — see [Configuration precedence](#configuration-precedence).** |
| `POLL_INTERVAL` | `15` | Seconds between scan cycles. |
| `PROCESS_WINDOW_MINUTES` | `120` | How far ahead of the off to consider races. |
| `REQUIRE_AUTH` | `false` | `true` closes the API to unauthenticated callers. See [Authentication](#authentication). |

### Frontend (Cloudflare Pages)

| Variable | Purpose |
|---|---|
| `VITE_API_URL` | Cloud Run backend URL. Baked in at build time — changing it needs a rebuild. |

## Configuration precedence

Several settings are persisted to GCS and **take precedence over the
environment variables** when the engine boots. This is the single most
surprising thing about operating this service.

`_load_state()` compares the stored `day_started` against the current UTC date:

- **Same UTC day, state exists** — the stored values win. `dry_run`,
  `countries`, `process_window`, `point_value` and `ladder_profile` are all
  restored from GCS, and the corresponding environment variables are ignored.
- **New UTC day, or no state** — stored state is discarded wholesale and the
  environment variables and code defaults apply.

The practical consequence: **toggling DRY RUN off in the dashboard persists.**
It survives redeploys and container recycles, and setting `DRY_RUN=true` on
Cloud Run will *not* override it within the same UTC day. To return to dry
run, toggle it back in the dashboard or:

```bash
curl -X POST https://<backend-url>/api/engine/dry-run
```

Always confirm the live value before trading rather than inferring it from
Cloud Run's configuration:

```bash
curl -s https://<backend-url>/api/keepalive
```

`day_started` is set once at construction and never advanced by the scan
loop, so an instance that stays warm across midnight keeps the old date until
it is recycled.

## Authentication

Two layers, which are easy to confuse:

1. **Betfair session** — the engine's own login to the Exchange, established
   via `POST /api/login`. Without it `POST /api/engine/start` returns 401 and
   the engine cannot trade. The session token is persisted to GCS and restored
   on cold start; the password is never written to disk.
2. **API access** — who may call this API at all. Controlled by `REQUIRE_AUTH`
   and **off by default**.

With `REQUIRE_AUTH=true`, every endpoint except `/api/health`,
`/api/keepalive` and `/api/login` requires either:

- `X-Session-Token` — issued by `/api/login`, valid 12 hours, for the browser; or
- `X-API-Key` — a long-lived key from `POST /api/keys`, for machine callers.

The dashboard stores its session token in `localStorage` and sends it
automatically. Turn `REQUIRE_AUTH` on before going live: the kill switch,
risk configuration and flatten controls are all mutating endpoints.

## API reference

Interactive docs at `/docs`; schema at `/openapi.json`.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | Liveness. Public. |
| `GET` | `/api/keepalive` | Status, Betfair auth state, `require_auth`. Public. |
| `POST` | `/api/login` | Betfair login. Returns balance + session token. Public. |
| `POST` | `/api/logout` | Revokes session token, clears Betfair client. |
| `GET` | `/api/state` | Full dashboard state. |
| `POST` | `/api/engine/start` \| `/stop` | Engine lifecycle. Start needs a Betfair session. |
| `POST` | `/api/engine/dry-run` | Toggle dry-run. |
| `POST` | `/api/engine/point-value` \| `/countries` \| `/process-window` \| `/ladder-profile` | Tuning. |
| `GET` | `/api/markets` \| `/api/candidates` | Discovery and scoring. |
| `GET` | `/api/trades` \| `/api/trades/all` \| `/api/trades/{id}` | Trades. |
| `POST` | `/api/trades/{id}/control` \| `/flatten` | Manual override, emergency flatten. |
| `GET` | `/api/alerts` \| `/api/alerts/history` | Bookmaker trigger alerts. |
| `POST` | `/api/alerts/{id}/confirm` \| `/dismiss` | Alert handling. |
| `GET` | `/api/risk` | Risk config + live portfolio exposure. |
| `POST` | `/api/risk/config` \| `/risk/kill-switch` | Risk controls. |
| `GET` | `/api/positions` \| `/api/settlements` \| `/api/audit` \| `/api/sessions` | Reporting. |
| `GET`/`POST` | `/api/keys` | Machine API keys. Key material returned once, on create. |

## Tests

```bash
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
./backend/tests/run_all.sh
```

Betfair is stubbed throughout — the suite makes no network calls, needs no
credentials, and places no bets. It covers the auth layer in both
`REQUIRE_AUTH` modes, session persistence across simulated cold starts, and
portfolio aggregation including a regression test that Level-4 risk actually
blocks on a drawdown breach.

## Deployment

Both halves deploy on push to `main`:

- **Backend** → Cloud Run, from the root `Dockerfile`.
- **Frontend** → Cloudflare Pages, from `frontend/`. Typically live within a minute.

## Operational requirements

The scan loop is a background thread. Cloud Run throttles CPU to near zero
between requests, so with the default settings the loop stalls whenever
traffic goes quiet. For continuous operation the service needs CPU always
allocated and at least one warm instance:

The service lives in project **`chimera-v4`** (project number
`950990732577`), region `europe-west2`. `--project` is required unless that
is your active gcloud project — omitting it fails with
`Service [scalpengine] could not be found`.

```bash
gcloud run services update scalpengine \
  --project=chimera-v4 \
  --region=europe-west2 \
  --no-cpu-throttling \
  --min-instances=1
```

Without this the engine appears to run but scans only while someone is
watching the dashboard. `--min-instances=1` also keeps the Betfair session
alive continuously, since the loop's keepalive keeps renewing it.

### Before trading live

Close the API to anonymous callers. Until this is set, `POST
/api/risk/kill-switch`, `POST /api/risk/config` and `POST /api/engine/dry-run`
are reachable by anyone holding the Cloud Run URL:

```bash
gcloud run services update scalpengine \
  --project=chimera-v4 \
  --region=europe-west2 \
  --update-env-vars=REQUIRE_AUTH=true
```

Anyone already on the dashboard is bounced to the login screen once, and
receives a session token on logging back in.

### Secret handling

`BETFAIR_APP_KEY` is currently a plain environment variable, readable by
anyone with Cloud Run viewer access on `chimera-v4`. Consider moving it to
Secret Manager and referencing it with `--set-secrets` before this service
handles real money.

## Known limitations

- **Settlements are never recorded.** `settlements.append` is not called
  anywhere, so `/api/settlements` always returns empty and rolling P&L is
  derived from per-trade realised P&L rather than a true N-day window.
- **Re-login after a long outage needs a human.** Only the session token is
  persisted, never the password. If the instance is down past the token's
  expiry, someone must log in through the dashboard again.
- **Portfolio aggregation is unverified against the spec.** The Level-4
  inputs are computed from trade and position records; the exact definitions
  in CHI-SPC-002 B.13 have not been cross-checked.

## Spec reference

`CHI-SPC-002` — Chimera Scalping Strategy, April 2026. Part A (A.1–A.18),
Part B (B.1–B.27), Part C (C.1–C.30).
