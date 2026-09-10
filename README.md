# CHIMERA Scalping Engine

Pre-race scalping engine for the Betfair Exchange. A FastAPI backend runs the
strategy loop and holds state; a React dashboard drives it.

Built from **CHI-SPC-002** (Chimera Scalping Strategy, April 2026) — Part A
(strategy & business rules), Part B (global technical spec), Part C (bookmaker
trigger extension).

> **The engine cannot place bets.** It scans markets, scores candidates and
> plans trades, but the execution loop is not built — nothing places, cancels
> or closes an order, in dry run or live. Several dashboard controls are
> therefore cosmetic. Read [Control status](#control-status) before relying
> on any of them.

## Control status

`_manage_active_trades()` is a `TODO Phase 2` stub. Until the execution loop
exists, any control that depends on it does nothing beyond changing a label.

| Control | Claims to | Actually does |
|---|---|---|
| **Flatten** (Trades tab) | Close a position | Relabels the trade `STOPPING_OUT`. Cancels nothing, places nothing. |
| **Dry run / Live** toggle | Switch to real money | Flips a flag nothing reads. No orders are placed in either mode. |
| **Lock / Assisted** (Trades tab) | Take manual control | Changes the control-mode label. No execution code reads it. |
| **Confirm** (Triggers tab) | Register a bookmaker bet and start its lay ladder | Records the confirmation. Creates no trade. |
| **Kill switch** | Block new trades | Works — blocks new trade *plans*. Cannot cancel orders; none exist. |
| **Start / Stop** | Run the scan loop | Works. |
| **Risk limits, point value, ladder profile** | Size trades | Work, at planning time only. |
| **Countries, process window** | Scope scanning | Work. |

The four cosmetic controls are disabled in the dashboard, drawn with a dashed
border, and carry a tooltip saying why. Each is re-enabled when the execution
stage that backs it is built.

**No control in this dashboard can get you out of a position.** Betfair's own
site is the only way to cancel or close a bet.

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
| `BETFAIR_APP_KEY` | — | Betfair application key, mounted from Secret Manager (`scalpengine-betfair-app-key`). Required. |
| `FRONTEND_URL` | `https://scalping.thync.online` | CORS origin. **Must** be set to the real frontend domain. |
| `GCS_BUCKET` | — | Bucket for state persistence (production: `chimera-scalping-state`). Without it, state dies with the container. |
| `DRY_RUN` | `true` | Intended to gate real orders. **Currently gates nothing** — see [Control status](#control-status). **Overridden by persisted state — see [Configuration precedence](#configuration-precedence).** |
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
| `POST` | `/api/engine/dry-run` | Toggle the dry-run flag. No effect until execution exists. |
| `POST` | `/api/engine/point-value` \| `/countries` \| `/process-window` \| `/ladder-profile` | Tuning. |
| `GET` | `/api/markets` \| `/api/candidates` | Discovery and scoring. |
| `GET` | `/api/trades` \| `/api/trades/all` \| `/api/trades/{id}` | Trades. |
| `POST` | `/api/trades/{id}/control` \| `/flatten` | **Cosmetic.** Change the trade's label only; no orders cancelled or placed. |
| `GET` | `/api/alerts` \| `/api/alerts/history` | Bookmaker trigger alerts. |
| `POST` | `/api/alerts/{id}/confirm` \| `/dismiss` | Confirm records the bet but creates no trade. |
| `GET` | `/api/risk` | Risk config + live portfolio exposure. |
| `POST` | `/api/risk/config` \| `/risk/kill-switch` | Risk limits and kill switch. Both gate new trade plans only. |
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

- **Backend** → Cloud Run, via Cloud Build running [`cloudbuild.yaml`](cloudbuild.yaml):
  test → build → push → deploy → verify. A failing, crashing or empty test
  suite stops the build before anything ships.
- **Frontend** → Cloudflare Pages, from `frontend/`. Typically live within a minute.

The service lives in project **`chimera-v4`** (project number `950990732577`),
region `europe-west2`.

### Every Cloud Run setting lives in `cloudbuild.yaml`

Scaling, CPU, env vars and the secret reference are all deploy flags in
`cloudbuild.yaml`. Change them there. A setting applied by hand with
`gcloud run services update` is overwritten by the next deploy.

The trigger must be created with `--build-config=cloudbuild.yaml`. **Never use
the Cloud Run "continuous deployment" wizard.** It runs
`services update --image --labels` and nothing else, so every flag in this
file goes inert.

The trigger is `scalpengine-deploy`. It replaced the wizard trigger on
10 September 2026.

The last build step, `verify`, runs
[`scripts/verify_deploy.py`](scripts/verify_deploy.py) against the live service
and fails the build if any flag differs from this file. By then the deploy has
already happened, so a failed `verify` means the live service needs attention —
do not just retry.

To deploy by hand during an incident:

```bash
gcloud builds submit --project=chimera-v4 --config=cloudbuild.yaml
```

### Why the flags are what they are

| Flag | Why |
|---|---|
| `--min-instances=1`, `--no-cpu-throttling` | The scan loop is a background thread. Without CPU always allocated and a warm instance, Cloud Run throttles it between requests and it stalls whenever nobody is watching the dashboard. It also keeps the Betfair session alive. |
| `--max-instances=1` | The engine is a singleton holding trade state in memory. A second instance runs a second scan loop and races the first on the GCS state files — and once execution exists, would place duplicate orders. |
| `REQUIRE_AUTH=true` | Without it the kill switch, risk config and dry-run toggle are reachable by anyone holding the Cloud Run URL. |
| `--set-secrets` | Mounts the Betfair app key from Secret Manager. See below. |

### Secret handling

`BETFAIR_APP_KEY` is held in Secret Manager as `scalpengine-betfair-app-key`,
replicated in `europe-west2` only, and readable only by the runtime service
account `scalpengine-cloudrun@chimera-v4.iam.gserviceaccount.com`.
`cloudbuild.yaml` mounts it at `:latest`, so rotating the key is a new secret
version followed by a deploy.

No secret belongs in this repository. `.env` is gitignored.

## Known limitations

- **No execution loop.** See [Control status](#control-status). This is the
  gap between the current engine and a tradeable one.

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
