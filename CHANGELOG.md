# Changelog

All notable changes to the CHIMERA Scalping Engine are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **`cloudbuild.yaml`** — tests, build and deploy, with every Cloud Run flag
  locked in the repo. The service was deployed by a Cloud Run wizard trigger
  that runs `services update --image --labels` and nothing else, so its
  configuration existed only on the service and would have been lost on any
  rebuild. Paths are inlined and images tagged with `$BUILD_ID` so the file
  also runs by hand under `gcloud builds submit` during an incident.
  **Takes effect when the wizard trigger is replaced — pending.**
- **`.dockerignore` and `.gcloudignore`**, deliberately different: tests must
  reach Cloud Build but never ship. Previously `COPY backend/ .` put the test
  suite in the production image. Verified in Cloud Build: the image now holds
  only application files.

### Security

- **Betfair app key moved to Secret Manager** as
  `scalpengine-betfair-app-key`, replicated in `europe-west2` only and
  readable only by the runtime service account. It had been a plain env var,
  readable by anyone with Cloud Run viewer access. Created from the live value
  without printing it, and verified by hash. The service switches to it on the
  first `cloudbuild.yaml` deploy; older revisions keep the plain value in
  their configuration until deleted.

### Changed

- **`--max-instances` 20 → 1**, on the first `cloudbuild.yaml` deploy. The
  engine is a singleton holding trade state in memory. A second instance
  would run a second scan loop, race the first on the GCS state files, and —
  once execution exists — place duplicate orders.
- **Test runner fails loudly.** `run_all.sh` now fails any suite that
  crashes or reports zero assertions, and prints its output on failure, since
  CI has no other way to show the traceback. Verified in Cloud Build: a
  runner pointed at `/bin/true` (finds nothing) and `/bin/false` (crashes
  silently) both fail the build.

- **Disabled the four controls that do nothing.** Flatten, Lock / Assisted /
  Auto, Confirm Bet and the Dry run / Live toggle looked fully functional in
  the dashboard while doing nothing beyond changing a label. They are now
  disabled, drawn with a dashed border, and carry a tooltip explaining why —
  Flatten's reads "Close positions on Betfair directly". They stay visible
  rather than hidden so an operator knows the control exists and is not
  surprised by its absence. Each is re-enabled when the execution stage that
  backs it lands.

### Documentation

- **Corrected documentation of controls that do nothing.** The README and
  the operator guide described Flatten as an emergency control. It is not:
  `flatten_trade()` relabels the trade `STOPPING_OUT` and stops there, with
  a `TODO Phase 2` where the cancel and closing orders should be. An audit
  of every documented control found three more of the same kind:
  - **Dry run / Live** — `dry_run` is read only by a log line and a session
    label. Nothing places orders, so "Live" never meant real money.
  - **Lock / Assisted** — `control_mode` is validated on change and read by
    nothing else.
  - **Confirm** on a trigger alert — records the confirmation, and a `TODO`
    stands where the trade should be created.

  The README now opens with a Control status table stating what each control
  actually does, and the API reference marks the cosmetic endpoints. The
  header claim that the engine "places real money bets when `DRY_RUN=false`"
  was false and is removed.
- Folded the previous Unreleased documentation entry into 1.2.0, where it
  was committed; it had been left below that release.

---

## [1.2.0] — 2026-09-09

### Fixed

- **Race times displayed in UTC, an hour behind UK racing.** Every timestamp
  in the dashboard was rendered by slicing characters out of the raw ISO
  string (`race_time.slice(11, 16)`), which skips timezone conversion
  entirely. Betfair returns `marketStartTime` in UTC, so a 13:51 Redcar card
  showed as 12:51 through BST. The "mins to off" column was computed
  correctly, which made the display look internally consistent and hid the
  problem. Times are now parsed and formatted in `Europe/London`, which also
  covers Irish meetings, and correctly shows no shift under GMT in winter.
- **Trades accumulated forever.** `_load_trades()` has no day filter, unlike
  `_load_state()`, so trades were restored across days without bound —
  yesterday's `PLANNED` trades for races that had already run stayed in the
  active list and inflated the trade count. `_purge_stale_trades()` now drops
  `PLANNED` trades whose race has passed and closed trades from previous
  days, on boot and on every scan. Anything still holding exposure is kept
  regardless of age.

### Added

- **Editable risk limits in the dashboard.** The Risk tab could display
  bankroll, per-trade loss and the daily cap but never change them, so sizing
  a live run meant hand-rolled `curl` calls carrying a session token pulled
  out of `localStorage`. An Adjust Limits panel now covers bankroll, max loss
  per trade, daily drawdown cap and point value. Because every Level-4 cap is
  a percentage of bankroll, a mismatch between bankroll and the real Betfair
  balance sizes the caps against a number that does not exist — the panel
  warns on any mismatch and offers a one-click correction.
- `backend/tests/test_purge.py`, bringing the suite to 60 assertions.

### Documentation

- **Configuration precedence.** Documented that `dry_run`, `countries`,
  `process_window`, `point_value` and `ladder_profile` are restored from GCS
  and take precedence over their environment variables within the same UTC
  day. `_load_state()` discards stored state only when `day_started` differs
  from the current UTC date, so `DRY_RUN=true` on Cloud Run does not override
  a dry-run toggle made through the dashboard. This was found in production:
  the service reported `dry_run: false` while `DRY_RUN` was unset on Cloud Run
  and the Dockerfile default was `true` — the value had been toggled in the UI
  and persisted through subsequent deploys.
- **Corrected the Cloud Run commands.** They omitted `--project=chimera-v4`
  and failed with `Service [scalpengine] could not be found` for anyone whose
  active gcloud project was not `chimera-v4` (project number `950990732577`).
- **Added instructions for enabling `REQUIRE_AUTH`** on Cloud Run, with the
  list of mutating endpoints that are anonymous until it is set.
- **Flagged `BETFAIR_APP_KEY` secret handling.** It is stored as a plain
  environment variable, readable by anyone with Cloud Run viewer access on
  `chimera-v4`; Secret Manager is the safer option before live trading.
- Recorded the production `GCS_BUCKET` (`chimera-scalping-state`), confirming
  that the 1.1.0 session persistence has somewhere to write.

---

## [1.1.0] — 2026-09-08

Hardening pass following the 1.0.1 investigation. Everything here is covered
by tests in `backend/tests/`.

### Fixed

- **Level-4 portfolio risk was inert.** `portfolio_state` was initialised to
  zeros, read by `check_portfolio_risk`, serialised to the dashboard — and
  never written to. Every Level-4 control therefore evaluated against zero
  exposure and could not fire: the daily drawdown cap, the rolling loss cap
  and the capital utilisation cap were all decorative. Added
  `ScalpEngine._recompute_portfolio()`, called at the top of each scan cycle
  (so risk decisions see live exposure) and from `get_state()` (so the
  dashboard stops reporting £0.00 against open positions).
- **The Betfair session did not survive a cold start.** `is_authenticated`
  depended on a `BetfairClient` held only in memory, and `_save_state()` never
  persisted it. Because Cloud Run scales to zero, every recycle silently
  de-authenticated the engine and `POST /api/engine/start` began returning 401.
  The session token and expiry are now persisted alongside the rest of the
  state and restored on boot; expired tokens are discarded rather than
  restored. The password is never written to disk.
- **A running engine stayed stopped after a recycle.** The `running` flag is
  now persisted, and a FastAPI startup hook resumes the scan loop when a valid
  session was restored.

### Added

- **Optional API authentication.** `REQUIRE_AUTH=true` closes every endpoint
  except `/api/health`, `/api/keepalive` and `/api/login` to unauthenticated
  callers. Two credential types are accepted: `X-Session-Token` (issued by
  `/api/login`, 12-hour lifetime, used by the dashboard) and `X-API-Key`
  (long-lived, for machine callers). **Defaults to `false`**, so existing
  deployments are unaffected until the flag is set.
- `GET`/`POST /api/keys` for managing machine API keys. Key material is
  returned only once, at creation; the list endpoint exposes metadata only.
- `/api/keepalive` now reports `require_auth`, letting the dashboard tell the
  difference between "no Betfair session" and "no API credential".
- `README.md` and this changelog.

### Changed

- The dashboard sends its session token on every request and clears it on
  logout.
- `require_api_key` — previously defined but attached to no route — is now a
  machine-only gate, always enforced where it is used, independent of
  `REQUIRE_AUTH`.

### Security

- Before 1.1.0 every endpoint was reachable anonymously by anyone with the
  Cloud Run URL, including `POST /api/risk/kill-switch`, `POST
  /api/risk/config` and `POST /api/trades/{id}/flatten`. Set `REQUIRE_AUTH=true`
  before running with `DRY_RUN=false`.

### Known issues

- `settlements.append` is never called, so `/api/settlements` is always empty
  and rolling P&L falls back to per-trade realised P&L rather than a true
  N-day window.
- The scan loop still needs `--no-cpu-throttling --min-instances=1` on Cloud
  Run to run continuously; this is platform configuration, not code.
- Portfolio aggregation has not been cross-checked against CHI-SPC-002 B.13.

---

## [1.0.1] — 2026-09-08

### Fixed

- **The login screen was unreachable.** The dashboard decided it was
  authenticated by testing `state.status !== undefined` against `/api/state` —
  a public endpoint that always returns a `status` field. The condition was
  therefore always true, so the UI marked itself authenticated on load and
  skipped the login form. With no way to reach it, no Betfair session could be
  established, and `POST /api/engine/start` returned
  `401 {"status":"error","message":"Not authenticated"}` on every attempt. The
  dashboard now polls `/api/keepalive`, which reports real session state, and
  holds rendering until the first check resolves so the dashboard cannot flash
  before the login screen.

---

## [1.0.0] — 2026-09-02

### Added

- Initial release, built from CHI-SPC-002: candidate scoring (A.6), staged
  entry and lay ladders (A.7, A.9), pre-trade scenario simulation (A.10, A.11),
  four-level risk controls (B.13), trade/order/control state machines (B.9),
  position and P&L tracking (B.15), and the bookmaker trigger extension
  (Part C).
- FastAPI backend on Cloud Run, React/Vite dashboard on Cloudflare Pages, GCS
  state persistence, Betfair SSO login.

[1.1.0]: https://github.com/charles-ascot/scalpengine/releases/tag/v1.1.0
[1.0.1]: https://github.com/charles-ascot/scalpengine/releases/tag/v1.0.1
[1.0.0]: https://github.com/charles-ascot/scalpengine/releases/tag/v1.0.0
