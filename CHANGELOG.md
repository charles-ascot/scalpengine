# Changelog

All notable changes to the CHIMERA Scalping Engine are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added — execution loop, Stage 2

- **Staged entry (A.7), wired to the trade state machine (B.9.1).** The
  engine now enters planned trades itself. Stage 1 probes at planning; at 30
  minutes before the off the position is re-underwritten against the probe
  price (A.8) — green adds the full Stage 2, amber adds half, red invalidates
  and adds nothing; at 10 minutes an optional Stage 3 late add goes in only if
  still green. The spec times stages off a morning bookmaker price and the
  exchange opening; FB7 is exchange-only, so they are timed against the off,
  and both windows are configurable.
- Every stage passes the risk engine before it is placed (B.13: block stage
  advance), and only `AUTO` trades are automated.
- Trade cards show each stage's progress, the live position, the
  re-underwriting verdict, and a warning when a trade needs review.

### Changed

- **Entries back at the best available back price.** `entry_odds` had been
  read from the lay side of the spread — one tick above the market — so a
  back placed there would have filled mainly when the horse drifted, just as
  the favourite thesis weakened. Planning now uses the price that can
  actually be backed.
- **Lock and Auto re-enabled.** Now that automation exists, Lock genuinely
  stops automated entries on a trade — the spec's "manual override locks the
  trade out of automatic control". Assisted stays disabled: its approval flow
  is not built.

### Fixed

- **An invented entry price.** With no lay price, `entry_odds` fell back to
  `2.0`. Harmless while nothing placed orders; with Stage 2 it would have
  staked on a made-up number. No price now means no trade.
- **Abandoned trades would have been re-planned every scan.** The one-trade-
  per-runner guard ignored cancelled trades, so an entry abandoned for any
  reason would have been planned afresh 15 seconds later, indefinitely. The
  guard now holds for the whole race.

### Added — operator key

- **Operator API key** in Secret Manager (`scalpengine-ops-api-key`), mounted
  as `OPS_API_KEY` and accepted as `X-API-Key`. Operating the locked API had
  meant pulling a session token out of a browser's `localStorage` or typing a
  Betfair password into a terminal. Compared in constant time; if the secret
  is not mounted, no operator key exists — it never falls back to a default.

### Added — execution loop, Stage 1

- **Orders, fills and reconciliation** (`backend/execution.py`, B.14). The
  engine can now place an order, poll its state, turn changes in matched stake
  into fill records, and rebuild the trade's position from those fills. Orders
  and fills persist with the trades.
- **A dry run that genuinely simulates.** Orders placed in dry run go to a
  simulated venue that matches against real Betfair prices and places
  nothing. Before this, dry run gated nothing because nothing placed orders.
  The fill model is conservative by design — no assumed queue position, no
  reused liquidity, resting fills at the order's own price — so a dry run
  cannot look better than the market would have allowed.
- **`LIVE_ORDERS_ENABLED` interlock**, set `false` in `cloudbuild.yaml`. While
  false the Betfair venue refuses every order regardless of the dashboard's
  dry-run flag, so going live is a reviewed commit rather than a click.
- **Unconfirmed submissions are reconciled, not guessed.** A placement that
  times out may or may not exist at Betfair. It is now looked up by
  `customerOrderRef` and declared not placed only after three polls, and
  `customerRef` makes a mistaken re-submission a no-op at Betfair.
- `GET /api/orders`, `GET /api/fills`, and `POST
  /api/trades/{id}/manual-order` — dry run only for now, as the harness for
  exercising the simulated venue against live prices.
- `backend/tests/test_execution.py`: 57 assertions; the suite is now 119.

### Fixed — found while building Stage 1

- **`place_back_order` sent numbers as strings.** `place_lay_order`'s own
  docstring records that Betfair silently rejects string prices and sizes; it
  had been fixed for lays and never for backs. Every automated entry would
  have been refused. Cancel and replace had the same fault. Placement is now
  one `place_order` implementation.
- **No order carried a trade ID or strategy version**, which spec A.12
  requires and which reconciliation after a timeout depends on.
- **`get_current_orders` returned `[]` on failure**, so a network blip looked
  identical to every order vanishing. It now returns `None`, and follows
  `moreAvailable` rather than silently truncating.
- **Lay-only positions were reported as flat.** `recalculate()` returned
  early whenever no back had matched.
- **Portfolio exposure had the wrong sign.** `max_open_loss` is negative for a
  loss and was summed as-is, so real exposure would have read negative and
  the capital utilisation cap could never fire. Hedge completion, a 0–1
  fraction, was compared against 100. Neither showed because no position had
  ever held stake; the portfolio test had used hand-written values with the
  wrong sign, and now builds positions from real fills.

### Changed

- **Daily drawdown now uses each position's worst case**, not
  `PositionSnapshot`'s midpoint proxy. Under the midpoint, a naked £10 back
  counted as roughly +£9 unrealised — offsetting real losses and making the
  cap fire late. It now counts as −£10. The cap will fire earlier than before.


- **`cloudbuild.yaml`** — tests, build and deploy, with every Cloud Run flag
  locked in the repo. The service was deployed by a Cloud Run wizard trigger
  that runs `services update --image --labels` and nothing else, so its
  configuration existed only on the service and would have been lost on any
  rebuild. Paths are inlined and images tagged with `$BUILD_ID` so the file
  also runs by hand under `gcloud builds submit` during an incident. The
  wizard trigger was replaced by `scalpengine-deploy` on 10 September; its
  first run deployed revision `scalpengine-00019-8qg`, and all fourteen live
  flags matched this file.
- **`verify` build step** — `scripts/verify_deploy.py` compares the live
  service with the deploy flags in `cloudbuild.yaml` after every deploy and
  fails the build on any difference, so no deploy is assumed to have taken
  effect. It reports unexpected env vars by name only, so a secret that leaks
  into a plain env var cannot leak into the build log too. Tested against the
  live service with `maxScale` tampered to 20: it fails.
- **`.dockerignore` and `.gcloudignore`**, deliberately different: tests must
  reach Cloud Build but never ship. Previously `COPY backend/ .` put the test
  suite in the production image. Verified in Cloud Build: the image now holds
  only application files.

### Security

- **Betfair app key moved to Secret Manager** as
  `scalpengine-betfair-app-key`, replicated in `europe-west2` only and
  readable only by the runtime service account. It had been a plain env var,
  readable by anyone with Cloud Run viewer access. Created from the live value
  without printing it, and verified by hash. Live since revision
  `scalpengine-00019-8qg`, with no plain value left on the service. Revisions
  before that keep the plain value in their configuration until deleted.

### Changed

- **`--max-instances` 20 → 1**, live since revision `scalpengine-00019-8qg`. The
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
