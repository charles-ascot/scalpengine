"""Exercises the new auth + persistence + portfolio code without hitting Betfair."""
import os, sys, json, tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp()
os.environ.update({
    "STATE_FILE":    f"{TMP}/state.json",
    "SESSIONS_FILE": f"{TMP}/sessions.json",
    "API_KEYS_FILE": f"{TMP}/keys.json",
    "TRADES_FILE":   f"{TMP}/trades.json",
    "GCS_BUCKET": "", "BETFAIR_APP_KEY": "test_key", "DRY_RUN": "true",
    "OPS_API_KEY": "ops_test_key_value",
})
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "false")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from betfair_client import BetfairClient
FAKE = "FAKE_TOKEN_123"
def _login(self):
    self.session_token = FAKE
    self.session_expiry = datetime.now(timezone.utc) + timedelta(hours=4)
    return True
BetfairClient.login = _login
BetfairClient.ensure_session = lambda self: True
BetfairClient.get_account_balance = lambda self: 1234.56
BetfairClient.get_todays_win_markets = lambda self, countries=None: []

import main
from fastapi.testclient import TestClient
c = TestClient(main.app)

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")

print(f"\n=== REQUIRE_AUTH={REQUIRE_AUTH} ===")
print("\n[public endpoints]")
r = c.get("/api/health");    check("health 200", r.status_code == 200, r.text)
r = c.get("/api/keepalive"); check("keepalive 200", r.status_code == 200, r.text)
check("keepalive advertises require_auth",
      r.json().get("require_auth") == (REQUIRE_AUTH == "true"), r.text)

print("\n[protected endpoint, no credentials]")
r = c.get("/api/state")
if REQUIRE_AUTH == "true":
    check("state 401 without token", r.status_code == 401, f"got {r.status_code}")
else:
    check("state 200 (auth disabled = backward compatible)", r.status_code == 200, f"got {r.status_code}")

print("\n[login]")
r = c.post("/api/login", json={"username": "u", "password": "p"})
check("login 200", r.status_code == 200, r.text)
tok = r.json().get("session_token")
check("login returns session_token", bool(tok), r.text)
check("login returns balance", r.json().get("balance") == 1234.56, r.text)

print("\n[protected endpoint, with token]")
h = {"X-Session-Token": tok}
check("state 200 with token", c.get("/api/state", headers=h).status_code == 200)
r = c.post("/api/engine/start", headers=h)
check("engine start 200 with token", r.status_code == 200, r.text)
check("engine reports RUNNING/STARTING", r.json().get("status") in ("RUNNING", "STARTING"), r.text)
c.post("/api/engine/stop", headers=h)

print("\n[api keys]")
r = c.post("/api/keys", json={"label": "test"}, headers=h)
check("create key 200", r.status_code == 200, r.text)
raw = r.json().get("key", "")
check("key looks right", raw.startswith("scp_"), raw)
r = c.get("/api/keys", headers=h)
check("list keys omits key material",
      r.status_code == 200 and "key" not in r.json()["keys"][0], r.text)
if REQUIRE_AUTH == "true":
    check("api key also authenticates",
          c.get("/api/state", headers={"X-API-Key": raw}).status_code == 200)
    check("bogus token rejected",
          c.get("/api/state", headers={"X-Session-Token": "nope"}).status_code == 401)

print("\n[operator key]")
if REQUIRE_AUTH == "true":
    check("operator key authenticates", c.get("/api/state", headers={"X-API-Key": "ops_test_key_value"}).status_code == 200)
    check("wrong operator key rejected", c.get("/api/state", headers={"X-API-Key": "ops_test_key_valuX"}).status_code == 401)
    check("operator key cannot be minted via /api/keys listing",
          all("ops_test_key_value" not in str(k) for k in c.get("/api/keys", headers={"X-API-Key": "ops_test_key_value"}).json()["keys"]))

print("\n[logout revokes]")
c.post("/api/logout", headers=h)
if REQUIRE_AUTH == "true":
    check("token dead after logout", c.get("/api/state", headers=h).status_code == 401)

print(f"\n{len(P)} passed, {len(F)} failed")
if F:
    print("FAILED: " + ", ".join(F)); sys.exit(1)
