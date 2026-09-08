# Changelog

All notable changes to the CHIMERA Scalping Engine are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
