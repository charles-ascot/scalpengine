"""Stage 1 — order status, fills -> position, and a dry run that genuinely simulates.

Betfair is never called. Market books and order reports below are shaped like
Betfair's own listMarketBook / listCurrentOrders responses.
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

from execution import (BetfairVenue, OrderManager, SimulatedVenue, build_position,
                       is_valid_price, LIVE_ORDERS_ENABLED)

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}{'' if c else '  <- ' + str(d)}")

SEL = 12345
def book(atb=(), atl=(), status="OPEN", inplay=False, runner_status="ACTIVE"):
    return {"marketId": "1.1", "status": status, "inplay": inplay, "runners": [{
        "selectionId": SEL, "status": runner_status,
        "ex": {"availableToBack": [{"price": p, "size": s} for p, s in atb],
               "availableToLay": [{"price": p, "size": s} for p, s in atl]}}]}

class Market:
    """A market whose book the test changes between polls."""
    def __init__(self, b): self.b = b
    def get(self, market_id): return self.b

def sim(b):
    m = Market(b)
    return OrderManager({"SIMULATED": SimulatedVenue(m.get)}), m

def order(om, side, price, size, trade="trd_a"):
    return om.submit(trade, "1.1", SEL, side, price, size, simulated=True)


print("\n[price ladder]")
check("valid ticks accepted", all(is_valid_price(p) for p in (1.01, 2.0, 2.02, 3.65, 4.8, 7.2, 19.5, 30, 1000)))
check("off-ladder prices refused", not any(is_valid_price(p) for p in (1.0, 2.01, 3.66, 4.85, 7.3, 1001)))


print("\n[simulated venue — submission]")
om, m = sim(book(atb=[(3.7, 5), (3.65, 20)], atl=[(3.75, 30)]))
o = order(om, "BACK", 3.65, 10)
check("BACK crossing fills immediately", o.status == "FILLED" and o.matched_stake == 10, (o.status, o.matched_stake))
check("fills at offered prices, best first", abs(o.avg_matched_price - 3.675) < 1e-9, o.avg_matched_price)
check("bet id marked simulated", o.bet_id.startswith("SIM-"), o.bet_id)

om, m = sim(book(atb=[(3.5, 50)], atl=[(3.55, 50)]))
o = order(om, "BACK", 3.65, 10)
check("BACK not crossing rests", o.status == "ACK" and o.matched_stake == 0, o.status)

om, m = sim(book(atb=[(3.7, 4)]))
o = order(om, "BACK", 3.7, 10)
check("partial fill capped by displayed size", o.status == "PARTIAL" and o.matched_stake == 4 and o.remaining == 6,
      (o.status, o.matched_stake, o.remaining))

om, m = sim(book(atl=[(3.8, 10), (3.85, 10), (3.9, 10)]))
o = order(om, "LAY", 3.85, 12)
check("LAY crossing takes lowest prices first", o.matched_stake == 12 and abs(o.avg_matched_price - (10*3.8 + 2*3.85)/12) < 1e-4,
      (o.matched_stake, o.avg_matched_price))

for label, b, side, price, size, code in [
    ("off-ladder price", book(atb=[(3.7, 9)]), "BACK", 3.66, 10, "INVALID_ODDS"),
    ("stake below minimum", book(atb=[(3.7, 9)]), "BACK", 3.7, 0.5, "INVALID_BET_SIZE"),
    ("no market data", None, "BACK", 3.7, 10, "NO_MARKET_DATA"),
    ("suspended market", book(status="SUSPENDED"), "BACK", 3.7, 10, "MARKET_SUSPENDED"),
    ("in-play market", book(inplay=True), "BACK", 3.7, 10, "SIM_INPLAY_NOT_SIMULATED"),
    ("removed runner", book(runner_status="REMOVED"), "BACK", 3.7, 10, "RUNNER_NOT_ACTIVE"),
]:
    om, m = sim(b)
    o = order(om, side, price, size)
    check(f"refused: {label}", o.status == "REJECTED" and o.error_code == code, (o.status, o.error_code))


print("\n[simulated venue — reconciliation]")
om, m = sim(book(atb=[(3.5, 50)], atl=[(3.55, 50)]))
o = order(om, "BACK", 3.65, 10)
m.b = book(atb=[(3.7, 20)], atl=[(3.75, 20)])        # market trades through our price
fills = om.reconcile()
check("resting order fills when the market trades through", o.status == "FILLED" and o.matched_stake == 10, o.status)
check("resting fill is at the order's own price", len(fills) == 1 and fills[0].price == 3.65, [f.price for f in fills])
check("idempotent: a second poll adds nothing", om.reconcile() == [] and len(om.fills) == 1, len(om.fills))

om, m = sim(book(atb=[(3.5, 50)]))
o = order(om, "BACK", 3.65, 10)
m.b = book(atb=[(3.7, 4)])
om.reconcile(); om.reconcile(); om.reconcile()
check("static crossing liquidity is taken once, not every poll", o.matched_stake == 4, o.matched_stake)

om, m = sim(book(atb=[(3.6, 50)], atl=[(3.65, 50)]))
o = order(om, "BACK", 3.65, 10)
om.reconcile()
check("queue position never assumed at our own price", o.matched_stake == 0, o.matched_stake)

om, m = sim(book(atb=[(3.5, 50)]))
a, b = order(om, "BACK", 3.65, 10), order(om, "BACK", 3.65, 10)
m.b = book(atb=[(3.7, 12)])
om.reconcile()
check("our own orders share displayed liquidity", a.matched_stake + b.matched_stake == 12,
      (a.matched_stake, b.matched_stake))

om, m = sim(book(atb=[(3.7, 4)]))
o = order(om, "BACK", 3.7, 10)
m.b = book(atb=[(3.7, 100)], inplay=True)
om.reconcile()
check("unmatched stake lapses at the off; matched part stands",
      o.status == "EXPIRED" and o.matched_stake == 4 and o.size_lapsed == 6, (o.status, o.matched_stake, o.size_lapsed))

om, m = sim(book(atb=[(3.5, 50)]))
o = order(om, "BACK", 3.65, 10)
m.b = book(atb=[(3.9, 100)], status="SUSPENDED")
om.reconcile()
check("suspension changes nothing", o.status == "ACK" and o.matched_stake == 0, o.status)

m.b = None
om.reconcile()
check("unreadable market: unchanged and not flagged", o.status == "ACK" and not om.flagged, (o.status, om.flagged))

om, m = sim(book(atb=[(3.5, 50)]))
o = order(om, "BACK", 3.65, 10)
r = om.cancel(o.order_id, "test")
check("simulated cancel completes", o.status == "CANCELLED" and o.size_cancelled == 10 and r["ok"], o.status)


print("\n[positions from fills]")
om, m = sim(book(atb=[(4.0, 10)], atl=[(3.0, 10)]))
order(om, "BACK", 4.0, 10)
order(om, "LAY", 3.0, 10)
pos = build_position("trd_a", om.fills_for("trd_a"))
check("back 10 @ 4.0, lay 10 @ 3.0 matches A.11", (pos.pnl_if_win, pos.pnl_if_lose) == (9.5, 0.0), (pos.pnl_if_win, pos.pnl_if_lose))
pos = build_position("t", [f for f in om.fills if f.side == "LAY"])
check("lay-only position is no longer reported as flat", (pos.pnl_if_win, pos.pnl_if_lose) == (-20.0, 9.5),
      (pos.pnl_if_win, pos.pnl_if_lose))


print("\n[real venue — the live-orders interlock]")
check("LIVE_ORDERS_ENABLED defaults to off", LIVE_ORDERS_ENABLED is False)

class FakeClient:
    """Betfair-shaped responses; records every call."""
    def __init__(self): self.placed, self.current, self.cancels = [], [], []
    def place_order(self, market_id, selection_id, side, price, size, **refs):
        self.placed.append(dict(market_id=market_id, side=side, price=price, size=size, **refs))
        return self.place_result
    def get_current_orders(self, market_ids=None, **kw): return self.current
    def cancel_order(self, market_id, bet_id): self.cancels.append(bet_id); return self.cancel_result

fc = FakeClient()
om = OrderManager({"BETFAIR": BetfairVenue(lambda: fc, live_enabled=False)})
o = om.submit("trd_b", "1.1", SEL, "BACK", 4.0, 10, simulated=False)
check("real submit refused while live orders are disabled", o.status == "REJECTED" and o.error_code == "LIVE_ORDERS_DISABLED", o.error_code)
check("Betfair was never called", fc.placed == [], fc.placed)

fc = FakeClient()
fc.place_result = {"status": "SUCCESS", "bet_id": "B1", "size_matched": 4.0, "avg_price_matched": 4.1, "order_status": "EXECUTABLE"}
om = OrderManager({"BETFAIR": BetfairVenue(lambda: fc, live_enabled=True)})
o = om.submit("trd_b", "1.1", SEL, "BACK", 4.0, 10, simulated=False)
check("placement match recorded as a fill", o.status == "PARTIAL" and o.matched_stake == 4 and len(om.fills) == 1, o.status)
sent = fc.placed[0]
check("order carries trade id (A.12)", sent["customer_order_ref"] == "trd_b-1" and len(sent["customer_order_ref"]) <= 32, sent)
check("order carries strategy version (A.12)", sent["customer_strategy_ref"] == "scalp-1.0.0" and len(sent["customer_strategy_ref"]) <= 15, sent)
check("customer_ref makes re-submission idempotent", sent["customer_ref"] == o.order_id, sent)

fc.current = [{"betId": "B1", "customerOrderRef": "trd_b-1", "status": "EXECUTION_COMPLETE",
               "sizeMatched": 10.0, "averagePriceMatched": 4.04, "sizeRemaining": 0.0,
               "sizeLapsed": 0.0, "sizeCancelled": 0.0, "sizeVoided": 0.0}]
new = om.reconcile()
check("incremental fill priced from cumulative averages", len(new) == 1 and new[0].stake == 6 and abs(new[0].price - 4.0) < 1e-6,
      [(f.stake, f.price) for f in new])
check("order FILLED from venue report", o.status == "FILLED", o.status)

fc = FakeClient()
fc.place_result = {"status": "SUCCESS", "bet_id": "B2", "size_matched": 0.0, "avg_price_matched": 0.0, "order_status": "EXECUTABLE"}
om = OrderManager({"BETFAIR": BetfairVenue(lambda: fc, live_enabled=True)})
o = om.submit("trd_c", "1.1", SEL, "BACK", 4.0, 10, simulated=False)
fc.current = None
om.reconcile()
check("failed order poll changes nothing and flags nothing", o.status == "ACK" and not om.flagged, (o.status, om.flagged))
fc.current = []
om.reconcile()
check("open order missing at venue is flagged for review", "trd_c" in om.flagged, om.flagged)

fc = FakeClient()
fc.place_result = {"status": "SUCCESS", "bet_id": "B3", "size_matched": 0.0, "avg_price_matched": 0.0, "order_status": "EXECUTABLE"}
fc.cancel_result = {"status": "ERROR", "error_code": "ERROR_IN_ORDER"}
om = OrderManager({"BETFAIR": BetfairVenue(lambda: fc, live_enabled=True)})
o = om.submit("trd_d", "1.1", SEL, "BACK", 4.0, 10, simulated=False)
r = om.cancel(o.order_id)
check("failed cancel leaves the order visibly live", o.status == "ACK" and not r["ok"], o.status)


print("\n[real venue — unconfirmed submission]")
fc = FakeClient(); fc.place_result = None          # transport failure on placeOrders
om = OrderManager({"BETFAIR": BetfairVenue(lambda: fc, live_enabled=True)})
o = om.submit("trd_e", "1.1", SEL, "BACK", 4.0, 10, simulated=False)
check("unconfirmed submit is neither accepted nor rejected", o.status == "NEW" and o.submit_uncertain, o.status)
fc.current = [{"betId": "B9", "customerOrderRef": "trd_e-1", "status": "EXECUTABLE",
               "sizeMatched": 0.0, "averagePriceMatched": 0.0, "sizeRemaining": 10.0}]
om.reconcile()
check("found at venue by customerOrderRef and adopted", o.bet_id == "B9" and o.status == "ACK" and not o.submit_uncertain,
      (o.bet_id, o.status))

fc = FakeClient(); fc.place_result = None; fc.current = []
om = OrderManager({"BETFAIR": BetfairVenue(lambda: fc, live_enabled=True)})
o = om.submit("trd_f", "1.1", SEL, "BACK", 4.0, 10, simulated=False)
for _ in range(3): om.reconcile()
check("never found after 3 polls -> REJECTED NOT_PLACED", o.status == "REJECTED" and o.error_code == "NOT_PLACED", (o.status, o.error_code))


print("\n[Betfair client — wire format]")
from betfair_client import BetfairClient
bc = BetfairClient("k", "u", "p")
calls = []
def fake_api(method, params):
    calls.append((method, params))
    if method == "placeOrders":
        return {"status": "SUCCESS", "instructionReports": [{"betId": "X", "sizeMatched": 0, "averagePriceMatched": 0, "orderStatus": "EXECUTABLE"}]}
    if method == "listCurrentOrders":
        page = [{"betId": "p1"}] if params["fromRecord"] == 0 else [{"betId": "p2"}]
        return {"currentOrders": page, "moreAvailable": params["fromRecord"] == 0}
bc._api_call = fake_api
bc.place_back_order("1.1", "123", 4.0, 10, customer_order_ref="trd_x-1", customer_strategy_ref="scalp-1.0.0", customer_ref="ord_1")
ins = calls[0][1]["instructions"][0]
check("BACK sends numbers, not strings", isinstance(ins["selectionId"], int) and isinstance(ins["limitOrder"]["price"], float)
      and isinstance(ins["limitOrder"]["size"], float) and ins["handicap"] == 0, ins)
check("refs reach the wire", ins.get("customerOrderRef") == "trd_x-1" and calls[0][1].get("customerStrategyRef") == "scalp-1.0.0"
      and calls[0][1].get("customerRef") == "ord_1", calls[0][1])
calls.clear()
got = bc.get_current_orders(market_ids=["1.1"])
check("listCurrentOrders follows moreAvailable", [o["betId"] for o in got] == ["p1", "p2"], got)
bc._api_call = lambda m, p: None
check("listCurrentOrders failure is None, not []", bc.get_current_orders(market_ids=["1.1"]) is None)


print("\n[engine integration]")
from betfair_client import BetfairClient as BC
BC.login = lambda self: (setattr(self, "session_token", "T") or
                         setattr(self, "session_expiry", datetime.now(timezone.utc) + timedelta(hours=4)) or True)
BC.get_account_balance = lambda self: 100.0
BC.get_todays_win_markets = lambda self, countries=None: []
LIVE_BOOK = {"b": book(atb=[(4.0, 10)], atl=[(4.1, 10)])}
BC.get_market_book = lambda self, market_id: LIVE_BOOK["b"]

from engine import ScalpEngine
e = ScalpEngine(); e.login("u", "p"); e.dry_run = True
past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
e.trades["trd_z"] = {"trade_id": "trd_z", "market_id": "1.1", "selection_id": SEL, "state": "PLANNED",
                     "race_time": past, "created_at": past}
o, err = e.submit_order("trd_z", "BACK", 4.0, 10, source="MANUAL")
check("dry run routes to the simulated venue", o.venue == "SIMULATED" and o.status == "FILLED", (o.venue, o.status))
check("position rebuilt from the fill", e.positions["trd_z"]["gross_back_stake"] == 10.0, e.positions.get("trd_z"))
check("order audit lands in the trade log", any(t["entity_type"] == "order" for t in e.state_transitions))
e._purge_stale_trades()
check("purge keeps a trade with venue history even after its race", "trd_z" in e.trades)

e2 = ScalpEngine()
check("orders and fills survive a restart", len(e2.order_manager.orders) == 1 and len(e2.order_manager.fills) == 1,
      (len(e2.order_manager.orders), len(e2.order_manager.fills)))
e2._rebuild_position("trd_z")
check("rebuilt after restart without double-counting", e2.positions["trd_z"]["gross_back_stake"] == 10.0,
      e2.positions["trd_z"]["gross_back_stake"])

e.dry_run = False
o, _ = e.submit_order("trd_z", "BACK", 4.0, 10)
check("dry run off still cannot reach Betfair without the env interlock",
      o.venue == "BETFAIR" and o.error_code == "LIVE_ORDERS_DISABLED", (o.venue, o.error_code))

print("\n[API]")
import main
from fastapi.testclient import TestClient
c = TestClient(main.app)
main.engine.login("u", "p")
main.engine.trades["trd_api"] = {"trade_id": "trd_api", "market_id": "1.1", "selection_id": SEL,
                                 "state": "PLANNED", "race_time": past, "created_at": past}
body = {"side": "BACK", "price": 4.0, "size": 5}
main.engine.dry_run, main.engine.status = False, "RUNNING"
check("manual order refused when not in dry run", c.post("/api/trades/trd_api/manual-order", json=body).status_code == 409)
main.engine.dry_run, main.engine.status = True, "STOPPED"
check("manual order refused when engine not running", c.post("/api/trades/trd_api/manual-order", json=body).status_code == 409)
main.engine.status = "RUNNING"
r = c.post("/api/trades/trd_api/manual-order", json=body)
check("manual order in dry run is simulated", r.status_code == 200 and r.json()["order"]["venue"] == "SIMULATED", r.text)
check("orders endpoint lists it", any(o["trade_id"] == "trd_api" for o in c.get("/api/orders").json()["orders"]))
check("fills endpoint lists it", any(f["trade_id"] == "trd_api" for f in c.get("/api/fills?trade_id=trd_api").json()["fills"]))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
