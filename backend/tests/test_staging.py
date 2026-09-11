"""Stage 2 — staged entry (A.7), re-underwriting (A.8), and the trade state machine (B.9.1).

All orders go to the simulated venue against Betfair-shaped market books the
test controls. Betfair is never called.
"""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp()
os.environ.update({
    "STATE_FILE": f"{TMP}/s.json", "SESSIONS_FILE": f"{TMP}/se.json",
    "API_KEYS_FILE": f"{TMP}/k.json", "TRADES_FILE": f"{TMP}/t.json",
    "GCS_BUCKET": "", "BETFAIR_APP_KEY": "test", "DRY_RUN": "true",
    "LIVE_ORDERS_ENABLED": "false",
})
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from betfair_client import BetfairClient
BOOK = {"b": None}
BetfairClient.login = lambda self: (setattr(self, "session_token", "T") or
                                    setattr(self, "session_expiry", datetime.now(timezone.utc) + timedelta(hours=4)) or True)
BetfairClient.get_account_balance = lambda self: 1000.0
BetfairClient.get_todays_win_markets = lambda self, countries=None: []
BetfairClient.get_market_book = lambda self, market_id: BOOK["b"]

from engine import ScalpEngine
from trade_planner import build_trade_plan
from state_machines import TradeState, validate_trade_transition

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}{'' if c else '  <- ' + str(d)}")

SEL = 777
def book(back=None, back_size=500.0, lay=None, status="OPEN", inplay=False):
    atb = [{"price": back, "size": back_size}] if back else []
    atl = [{"price": lay or round(back + 0.1, 2), "size": 500.0}] if back else []
    return {"marketId": "1.1", "status": status, "inplay": inplay,
            "runners": [{"selectionId": SEL, "status": "ACTIVE",
                         "ex": {"availableToBack": atb, "availableToLay": atl}}]}

def engine():
    # Each scenario gets a clean engine: no trades, orders or risk state
    # inherited from the previous one through the persistence files.
    for f in ("s.json", "se.json", "k.json", "t.json"):
        try:
            os.remove(f"{TMP}/{f}")
        except FileNotFoundError:
            pass
    e = ScalpEngine(); e.login("u", "p"); e.dry_run = True
    return e

def make_trade(e, tid, mins, entry=4.0, quality=0.75):
    now = datetime.now(timezone.utc)
    plan = build_trade_plan(tid, "r1", "1.1", SEL, "Horse", quality, 0.7, 0.2, 0.2, entry)
    e.trade_plans[tid] = plan.to_dict()
    e.trades[tid] = {"trade_id": tid, "market_id": "1.1", "selection_id": SEL, "horse_name": "Horse",
                     "venue": "Test", "race_time": (now + timedelta(minutes=mins)).isoformat(),
                     "state": "PLANNED", "control_mode": "AUTO", "entry_odds": entry,
                     "created_at": now.isoformat(),
                     "scenario_envelope": {"first_lay_only": {"min_pnl": -5.0}}}
    return e.trades[tid], e.trade_plans[tid]

def at(trade, mins):
    trade["race_time"] = (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat()

def stage(plan, n):
    return next((s for s in plan["entry_stages"] if s["stage_no"] == n), None)

def orders(e, tid):
    return sorted(e.order_manager.orders_for(tid), key=lambda o: o.created_at)

def tick(e):
    e._reconcile_orders()
    e._manage_active_trades()


print("\n[stage 1 — probe at planning]")
e = engine(); BOOK["b"] = book(back=4.0, lay=4.1)
t, plan = make_trade(e, "t1", 40)
e._manage_active_trades()
os_ = orders(e, "t1")
check("probe placed on a planned AUTO trade", len(os_) == 1 and t["state"] == "STAGE1_PENDING", (len(os_), t["state"]))
check("entry at the best back price, not the lay side", os_[0].price == 4.0 and os_[0].side == "BACK", os_[0].price)
check("probe is Stage 1's planned stake", os_[0].requested_stake == stage(plan, 1)["planned_stake"],
      (os_[0].requested_stake, stage(plan, 1)["planned_stake"]))
check("probe is simulated in dry run", os_[0].venue == "SIMULATED")
tick(e)
check("matched probe -> STAGE1_ENTERED -> AWAITING_CONFIRMATION", t["state"] == "AWAITING_CONFIRMATION", t["state"])
check("stage records the fill", stage(plan, 1)["actual_price"] == 4.0 and stage(plan, 1)["actual_stake"] > 0)

e = engine(); BOOK["b"] = book(back=4.0)
t, _ = make_trade(e, "t2", 20)
e._manage_active_trades()
check("no probe once the confirmation window has begun", not orders(e, "t2") and t["state"] == "PLANNED", t["state"])

e = engine(); BOOK["b"] = book(back=None)
t, _ = make_trade(e, "t3", 40)
e._manage_active_trades()
check("no back price: nothing placed, retried later", not orders(e, "t3") and t["state"] == "PLANNED", t["state"])

e = engine(); BOOK["b"] = book(back=4.0)
t, _ = make_trade(e, "t4", 40); t["control_mode"] = "MANUAL_LOCK"
e._manage_active_trades()
check("MANUAL_LOCK trade is never touched", not orders(e, "t4") and t["state"] == "PLANNED")
t["control_mode"] = "ASSISTED"
e._manage_active_trades()
check("ASSISTED trade is never touched", not orders(e, "t4"))

e = engine(); BOOK["b"] = book(back=4.0)
t, plan = make_trade(e, "t5", 40); e.risk_config.kill_switch_enabled = True
e._manage_active_trades()
check("kill switch blocks the probe", not orders(e, "t5") and t["state"] == "PLANNED", t["state"])
check("block reason recorded on the stage", "Kill switch" in (stage(plan, 1).get("last_block") or ""), stage(plan, 1).get("last_block"))

e = engine(); BOOK["b"] = book(back=4.0); e.dry_run = False
t, _ = make_trade(e, "t6", 40)
for _ in range(3):
    e._manage_active_trades()
check("live mode without the interlock: rejected, abandoned after 3 tries",
      t["state"] == "CANCELLED" and all(o.error_code == "LIVE_ORDERS_DISABLED" for o in orders(e, "t6")),
      (t["state"], [o.error_code for o in orders(e, "t6")]))
check("an abandoned runner is not re-planned this race", e._has_trade_for("1.1", SEL))


print("\n[stage 1 — resolution]")
e = engine(); BOOK["b"] = book(back=4.0, back_size=0.0)    # price shown, nothing behind it
t, _ = make_trade(e, "t7", 40)
e._manage_active_trades()
BOOK["b"] = book(back=4.0, inplay=True)
tick(e)
check("probe that never matched and lapsed -> CANCELLED", t["state"] == "CANCELLED", t["state"])

e = engine(); BOOK["b"] = book(back=4.0, back_size=1.0)    # only £1 available
t, plan = make_trade(e, "t8", 40)
e._manage_active_trades()
at(t, 25); tick(e)
o = orders(e, "t8")[0]
check("part-matched probe: remainder cancelled at the confirmation window",
      o.status == "CANCELLED" and o.matched_stake == 1.0, (o.status, o.matched_stake))
check("carries on with what matched", t["state"] in ("AWAITING_CONFIRMATION", "STAGE2_PENDING", "LIVE_EXPOSED"), t["state"])


print("\n[stage 2 — re-underwriting at the confirmation window]")
def to_awaiting(tid, entry=4.0):
    e = engine(); BOOK["b"] = book(back=entry)
    t, plan = make_trade(e, tid, 40, entry=entry)
    e._manage_active_trades(); tick(e)
    assert t["state"] == "AWAITING_CONFIRMATION", t["state"]
    return e, t, plan

e, t, plan = to_awaiting("g1")
BOOK["b"] = book(back=3.8)
e._manage_active_trades()
check("nothing added before the window", len(orders(e, "g1")) == 1 and t["state"] == "AWAITING_CONFIRMATION")
at(t, 25); e._manage_active_trades()
o2 = orders(e, "g1")[-1]
check("GREEN (shortened): full stage 2 added at the new best back",
      t["open_classification"] == "GREEN" and o2.price == 3.8 and o2.requested_stake == stage(plan, 2)["planned_stake"],
      (t.get("open_classification"), o2.price, o2.requested_stake))
check("-> STAGE2_PENDING", t["state"] == "STAGE2_PENDING", t["state"])
tick(e)
check("filled stage 2 -> LIVE_EXPOSED", t["state"] == "LIVE_EXPOSED", t["state"])
check("position holds both stages", e.positions["g1"]["gross_back_stake"] ==
      round(stage(plan, 1)["planned_stake"] + stage(plan, 2)["planned_stake"], 2), e.positions["g1"]["gross_back_stake"])

e, t, plan = to_awaiting("a1")
BOOK["b"] = book(back=4.1)                  # drifted 2.5% — modest
at(t, 25); e._manage_active_trades()
o2 = orders(e, "a1")[-1]
check("AMBER (modest drift): half stage 2", t["open_classification"] == "AMBER"
      and o2.requested_stake == round(stage(plan, 2)["planned_stake"] * 0.5, 2), (t.get("open_classification"), o2.requested_stake))

e, t, plan = to_awaiting("r1")
BOOK["b"] = book(back=4.6)                  # drifted 15%
at(t, 25); e._manage_active_trades()
check("RED (material drift): INVALIDATED, nothing added",
      t["state"] == "INVALIDATED" and len(orders(e, "r1")) == 1, (t["state"], len(orders(e, "r1"))))

e, t, plan = to_awaiting("k1")
e.risk_config.kill_switch_enabled = True
at(t, 25); e._manage_active_trades()
check("risk block at stage 2: no add, position carried as LIVE_EXPOSED",
      t["state"] == "LIVE_EXPOSED" and len(orders(e, "k1")) == 1 and stage(plan, 2).get("skipped") == "blocked",
      (t["state"], stage(plan, 2).get("skipped")))


print("\n[stage 3 — late add]")
def to_live(tid, s2_price=3.8):
    e, t, plan = to_awaiting(tid)
    BOOK["b"] = book(back=s2_price)
    at(t, 25); e._manage_active_trades(); tick(e)
    assert t["state"] == "LIVE_EXPOSED", t["state"]
    return e, t, plan

e, t, plan = to_live("s3a")
check("plan includes a late add at this quality", stage(plan, 3) is not None)
at(t, 15); e._manage_active_trades()
check("no late add before its window", len(orders(e, "s3a")) == 2)
BOOK["b"] = book(back=3.7)
at(t, 8); e._manage_active_trades()
check("still GREEN at the window: late add placed", len(orders(e, "s3a")) == 3 and orders(e, "s3a")[-1].price == 3.7,
      [o.price for o in orders(e, "s3a")])
check("state unchanged by the late add", t["state"] == "LIVE_EXPOSED")
e._manage_active_trades(); e._manage_active_trades()
check("late add placed once only", len(orders(e, "s3a")) == 3, len(orders(e, "s3a")))

e, t, plan = to_awaiting("s3b")
BOOK["b"] = book(back=4.1); at(t, 25); e._manage_active_trades(); tick(e)
at(t, 8); BOOK["b"] = book(back=3.9); e._manage_active_trades()
check("AMBER confirmation: no late add", len(orders(e, "s3b")) == 2 and "not GREEN" in (stage(plan, 3).get("skipped") or ""),
      stage(plan, 3).get("skipped"))

e, t, plan = to_live("s3c")
at(t, 0.5); e._manage_active_trades()
check("no late add inside the pre-off flatten window", len(orders(e, "s3c")) == 2
      and "flatten window" in (stage(plan, 3).get("skipped") or ""), stage(plan, 3).get("skipped"))


print("\n[state machine audit]")
e, t, plan = to_live("audit")
hist = [x for x in e.state_transitions if x["entity_type"] == "trade" and x["entity_id"] == "audit"]
path = [x["to_state"] for x in hist]
check("full path followed", path == ["STAGE1_PENDING", "STAGE1_ENTERED", "AWAITING_CONFIRMATION", "STAGE2_PENDING", "LIVE_EXPOSED"], path)
check("every transition is one B.9.1 allows",
      all(validate_trade_transition(TradeState(x["from_state"]), TradeState(x["to_state"])) for x in hist))
check("no trade flagged for review on the happy path", "needs_review" not in t)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
