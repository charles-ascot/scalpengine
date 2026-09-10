"""
CHIMERA Lay Engine — Betfair API Client
========================================
Handles all Betfair Exchange API interactions.
- Authentication (interactive login)
- Market discovery (UK/IE WIN markets)
- Price retrieval (identify favourites)
- Order placement (LAY bets)
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional
from rules import Runner

logger = logging.getLogger("betfair")

# ── Betfair API endpoints ──
LOGIN_URL = "https://identitysso.betfair.com/api/login"
CERT_LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"
API_URL = "https://api.betfair.com/exchange/betting/json-rpc/v1"

# ── Horse Racing event type ──
EVENT_TYPE_HORSE_RACING = "7"


class BetfairClient:
    """Minimal Betfair Exchange API client for lay betting."""

    def __init__(self, app_key: str, username: str, password: str):
        self.app_key = app_key
        self.username = username
        self.password = password
        self.session_token: Optional[str] = None
        self.session_expiry: Optional[datetime] = None
        self.last_login_error: Optional[str] = None

    # ──────────────────────────────────────────────
    #  AUTH
    # ──────────────────────────────────────────────

    def login(self) -> bool:
        """Authenticate with Betfair using interactive login."""
        try:
            resp = requests.post(
                LOGIN_URL,
                data={"username": self.username, "password": self.password},
                headers={
                    "X-Application": self.app_key,
                    "Accept": "application/json",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("status") == "SUCCESS":
                self.session_token = data["token"]
                self.session_expiry = datetime.now(timezone.utc) + timedelta(hours=4)
                self.last_login_error = None
                logger.info("Betfair login successful")
                return True
            else:
                self.last_login_error = data.get("error", "unknown")
                logger.error(f"Betfair login failed: {self.last_login_error}")
                return False
        except Exception as e:
            self.last_login_error = str(e)
            logger.error(f"Betfair login exception: {e}")
            return False

    def ensure_session(self) -> bool:
        """Ensure we have a valid session, re-authenticating if needed.

        Network-resilient: if a keepalive/login call fails due to a transient
        connection error (e.g. laptop briefly offline) AND the current token has
        not yet expired, we keep using the existing token rather than crashing.
        """
        now = datetime.now(timezone.utc)

        if self.session_token and self.session_expiry:
            # Token is fresh — no renewal needed
            if now < self.session_expiry - timedelta(minutes=30):
                return True

            # Token is near expiry — attempt keepalive renewal
            try:
                resp = requests.post(
                    KEEPALIVE_URL,
                    headers=self._headers(),
                    timeout=10,
                )
                data = resp.json()
                if data.get("status") == "SUCCESS":
                    self.session_expiry = now + timedelta(hours=4)
                    logger.info("Betfair session renewed via keepalive")
                    return True
            except requests.exceptions.ConnectionError:
                # Transient network issue — if token hasn't actually expired yet,
                # keep using it and try again next scan cycle.
                if now < self.session_expiry:
                    logger.warning(
                        "Network unreachable during keepalive — "
                        "token still valid until %s, continuing",
                        self.session_expiry.strftime("%H:%M:%S UTC"),
                    )
                    return True
                logger.error("Network offline and Betfair token has expired — cannot renew")
                return False
            except Exception as e:
                logger.warning(f"Keepalive failed ({e}) — attempting full re-login")

        # Full re-login with one retry on transient connection errors
        for attempt in range(1, 3):
            try:
                return self.login()
            except requests.exceptions.ConnectionError:
                if attempt < 2:
                    logger.warning(f"Login attempt {attempt} failed (network) — retrying in 5s")
                    time.sleep(5)
                else:
                    logger.error("Login failed after 2 attempts — network appears offline")
                    return False
        return False

    def _headers(self) -> dict:
        return {
            "X-Application": self.app_key,
            "X-Authentication": self.session_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _api_call(self, method: str, params: dict) -> Optional[dict]:
        """Make a JSON-RPC call to the Betfair API."""
        if not self.ensure_session():
            logger.error("No valid session for API call")
            return None

        payload = {
            "jsonrpc": "2.0",
            "method": f"SportsAPING/v1.0/{method}",
            "params": params,
            "id": 1,
        }

        try:
            resp = requests.post(
                API_URL,
                json=[payload],
                headers=self._headers(),
                timeout=30,
            )
            results = resp.json()
            if results and len(results) > 0:
                result = results[0]
                if "error" in result:
                    logger.error(f"API error on {method}: {result['error']}")
                    return None
                return result.get("result")
            return None
        except Exception as e:
            logger.error(f"API call {method} failed: {e}")
            return None

    # ──────────────────────────────────────────────
    #  MARKET DISCOVERY
    # ──────────────────────────────────────────────

    def get_todays_win_markets(self, countries: list[str] | None = None) -> list[dict]:
        """
        Get horse racing WIN markets for today.
        Returns list of market catalogue entries.
        """
        countries = countries or ["GB", "IE"]
        now = datetime.now(timezone.utc)
        end_of_day = now.replace(hour=23, minute=59, second=59)

        params = {
            "filter": {
                "eventTypeIds": [EVENT_TYPE_HORSE_RACING],
                "marketCountries": countries,
                "marketTypeCodes": ["WIN"],
                "marketStartTime": {
                    "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            },
            "maxResults": "200",
            "marketProjection": [
                "EVENT",
                "RUNNER_DESCRIPTION",
                "MARKET_START_TIME",
            ],
            "sort": "FIRST_TO_START",
        }

        result = self._api_call("listMarketCatalogue", params)
        if result is None:
            return []

        markets = []
        for m in result:
            markets.append({
                "market_id": m["marketId"],
                "market_name": m.get("marketName", ""),
                "venue": m.get("event", {}).get("venue", "Unknown"),
                "country": m.get("event", {}).get("countryCode", ""),
                "race_time": m.get("marketStartTime", ""),
                "runners": [
                    {
                        "selection_id": r["selectionId"],
                        "runner_name": r.get("runnerName", f"Runner {r['selectionId']}"),
                        "handicap": r.get("handicap", 0.0),
                        "sort_priority": r.get("sortPriority", 99),
                    }
                    for r in m.get("runners", [])
                ],
            })

        logger.info(f"Found {len(markets)} {'/'.join(countries)} WIN markets")
        return markets

    # ──────────────────────────────────────────────
    #  PRICE RETRIEVAL
    # ──────────────────────────────────────────────

    def get_market_prices(self, market_id: str) -> tuple[list["Runner"], bool]:
        """
        Get current best-available-to-lay prices for all runners in a market.
        Returns (runners, is_valid) where is_valid=False if market is
        closed, suspended, or in-play.
        """
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
            },
        }

        result = self._api_call("listMarketBook", params)
        if result is None or len(result) == 0:
            return [], False

        market = result[0]

        # Check market is still open
        if market.get("status") != "OPEN":
            logger.warning(f"Market {market_id} status: {market.get('status')} — skipping")
            return [], False

        # ── FIX: Check market is NOT in-play ──
        # Markets can be OPEN + inPlay=True simultaneously.
        # We only place pre-off bets.
        if market.get("inPlay", False):
            logger.warning(f"Market {market_id} is IN-PLAY — skipping (pre-off only)")
            return [], False

        runners = []
        for r in market.get("runners", []):
            runner = Runner(
                selection_id=r["selectionId"],
                runner_name=f"Selection {r['selectionId']}",  # Name comes from catalogue
                handicap=r.get("handicap", 0.0),
                status=r.get("status", "ACTIVE"),
            )

            # Get best available to lay (the lowest lay price)
            lay_prices = r.get("ex", {}).get("availableToLay", [])
            if lay_prices:
                runner.best_available_to_lay = lay_prices[0]["price"]

            # Get best available to back (the highest back price)
            back_prices = r.get("ex", {}).get("availableToBack", [])
            if back_prices:
                runner.best_available_to_back = back_prices[0]["price"]

            runners.append(runner)

        return runners, True

    def get_market_book(self, market_id: str) -> Optional[dict]:
        """Get full market book including status and inPlay flag."""
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
            },
        }
        result = self._api_call("listMarketBook", params)
        if result and len(result) > 0:
            return result[0]
        return None

    def get_market_book_full(self, market_id: str) -> Optional[dict]:
        """Get full market book with 3-level back/lay depth for display.

        Returns the complete market book with all runner prices
        at 3 depth levels, matching what Betfair shows in their UI.
        """
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS", "EX_TRADED"],
            },
        }
        result = self._api_call("listMarketBook", params)
        if result and len(result) > 0:
            market = result[0]
            return {
                "market_id": market_id,
                "status": market.get("status"),
                "in_play": market.get("inPlay", False),
                "total_matched": market.get("totalMatched", 0),
                "number_of_runners": market.get("numberOfRunners", 0),
                "runners": [
                    {
                        "selection_id": r.get("selectionId"),
                        "status": r.get("status", "ACTIVE"),
                        "last_price_traded": r.get("lastPriceTraded"),
                        "total_matched": r.get("totalMatched", 0),
                        "back": [
                            {"price": p["price"], "size": round(p["size"], 0)}
                            for p in r.get("ex", {}).get("availableToBack", [])[:3]
                        ],
                        "lay": [
                            {"price": p["price"], "size": round(p["size"], 0)}
                            for p in r.get("ex", {}).get("availableToLay", [])[:3]
                        ],
                    }
                    for r in market.get("runners", [])
                ],
            }
        return None

    def get_inplay_ltp(self, market_id: str) -> Optional[dict]:
        """Get in-play state and lastPriceTraded + actual SP for each runner.

        Used by the BSP Optimiser monitor loop.
        Returns None on API failure.
        """
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS", "SP_TRADED"],
            },
        }
        result = self._api_call("listMarketBook", params)
        if not result:
            return None
        market = result[0]
        runners = {}
        for r in market.get("runners", []):
            sp_data = r.get("sp", {})
            runners[r["selectionId"]] = {
                "ltp": r.get("lastPriceTraded"),
                "status": r.get("status", "ACTIVE"),
                "actual_sp": sp_data.get("actualSP"),   # set when bspReconciled
                "near_price": sp_data.get("nearPrice"),  # projected SP pre-race
            }
        return {
            "in_play": market.get("inPlay", False),
            "status": market.get("status", ""),
            "bsp_reconciled": market.get("bspReconciled", False),
            "runners": runners,
        }

    # ──────────────────────────────────────────────
    #  ORDER PLACEMENT
    # ──────────────────────────────────────────────

    def place_lay_order(self, market_id: str, selection_id: int, price: float, size: float, **refs) -> Optional[dict]:
        return self.place_order(market_id, selection_id, "LAY", price, size, **refs)

    # ──────────────────────────────────────────────
    #  ACCOUNT API
    # ──────────────────────────────────────────────

    ACCOUNT_API_URL = "https://api.betfair.com/exchange/account/json-rpc/v1"

    def _account_api_call(self, method: str, params: dict) -> Optional[dict]:
        """Make a JSON-RPC call to the Betfair Account API."""
        if not self.ensure_session():
            logger.error("No valid session for account API call")
            return None

        payload = {
            "jsonrpc": "2.0",
            "method": f"AccountAPING/v1.0/{method}",
            "params": params,
            "id": 1,
        }

        try:
            resp = requests.post(
                self.ACCOUNT_API_URL,
                json=[payload],
                headers=self._headers(),
                timeout=30,
            )
            results = resp.json()
            if results and len(results) > 0:
                result = results[0]
                if "error" in result:
                    logger.error(f"Account API error on {method}: {result['error']}")
                    return None
                return result.get("result")
            return None
        except Exception as e:
            logger.error(f"Account API call {method} failed: {e}")
            return None

    def get_account_balance(self) -> Optional[float]:
        """Get current account available balance."""
        try:
            result = self._account_api_call("getAccountFunds", {})
            if result:
                return result.get("availableToBetBalance")
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
        return None

    # ──────────────────────────────────────────────
    #  SETTLED BETS
    # ──────────────────────────────────────────────

    def get_race_result(self, market_id: str) -> Optional[dict]:
        """
        Check the result of a race market.

        Returns:
          { "settled": True,  "winner_selection_id": int|None }  — race settled
          { "settled": False, "market_status": str }              — race not settled yet
          None                                                     — market not found / API error

        A market is settled when its status is "CLOSED" and at least one
        runner has status "WINNER".  This typically happens 5–15 minutes
        after the race starts.
        """
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
            },
        }
        result = self._api_call("listMarketBook", params)
        if result is None or len(result) == 0:
            return None

        market = result[0]
        status = market.get("status")

        if status != "CLOSED":
            return {"settled": False, "market_status": status}

        winner_id = None
        for r in market.get("runners", []):
            if r.get("status") == "WINNER":
                winner_id = r["selectionId"]
                break

        return {
            "settled": True,
            "winner_selection_id": winner_id,
            "market_status": status,
        }

    def get_cleared_orders(
        self,
        settled_from: str = None,
        settled_to: str = None,
    ) -> list[dict]:
        """
        Fetch settled (cleared) orders from Betfair.
        Uses listClearedOrders on the betting API.

        settled_from/to: ISO 8601 datetime strings (e.g. "2026-02-20T00:00:00Z")
        Returns list of cleared order dicts.
        """
        params = {
            "betStatus": "SETTLED",
            "includeItemDescription": True,
            "fromRecord": 0,
            "recordCount": 1000,
        }

        if settled_from or settled_to:
            date_range = {}
            if settled_from:
                date_range["from"] = settled_from
            if settled_to:
                date_range["to"] = settled_to
            params["settledDateRange"] = date_range

        result = self._api_call("listClearedOrders", params)
        if result is None:
            return []

        return result.get("clearedOrders", [])

    # ──────────────────────────────────────────────
    #  SCALPING EXTENSIONS: BACK ORDERS, CANCEL, REPLACE, DEPTH
    # ──────────────────────────────────────────────

    def place_order(
        self,
        market_id: str,
        selection_id: int,
        side: str,
        price: float,
        size: float,
        customer_order_ref: Optional[str] = None,
        customer_strategy_ref: Optional[str] = None,
        customer_ref: Optional[str] = None,
    ) -> Optional[dict]:
        """Place one LIMIT order with LAPSE persistence.

        Returns None when the call itself failed. In that case the order may or
        may not exist at Betfair, so the caller must reconcile (by
        customer_order_ref) before assuming either — never blindly retry.
        customer_ref makes a mistaken re-submission a no-op at Betfair.

        Numbers must be sent as numbers: Betfair silently rejects string
        prices and sizes.
        """
        instruction = {
            "selectionId": int(selection_id),
            "handicap": 0,
            "side": side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": round(float(size), 2),
                "price": round(float(price), 2),
                "persistenceType": "LAPSE",
            },
        }
        if customer_order_ref:
            instruction["customerOrderRef"] = customer_order_ref
        params = {"marketId": market_id, "instructions": [instruction]}
        if customer_ref:
            params["customerRef"] = customer_ref
        if customer_strategy_ref:
            params["customerStrategyRef"] = customer_strategy_ref

        result = self._api_call("placeOrders", params)
        if result is None:
            return None

        reports = result.get("instructionReports") or [{}]
        report = reports[0]
        if result.get("status") == "SUCCESS":
            return {
                "status": "SUCCESS",
                "bet_id": report.get("betId"),
                "placed_date": report.get("placedDate"),
                "order_status": report.get("orderStatus"),
                "size_matched": float(report.get("sizeMatched", 0) or 0),
                "avg_price_matched": float(report.get("averagePriceMatched", 0) or 0),
            }
        return {
            "status": "FAILURE",
            "error_code": report.get("errorCode") or result.get("errorCode", "UNKNOWN"),
        }

    def place_back_order(self, market_id: str, selection_id: int, price: float, size: float, **refs) -> Optional[dict]:
        return self.place_order(market_id, selection_id, "BACK", price, size, **refs)

    def cancel_order(self, market_id: str, bet_id: str, size_reduction: float = None) -> dict:
        """Cancel or reduce an existing order."""
        instruction = {"betId": bet_id}
        if size_reduction is not None:
            instruction["sizeReduction"] = round(float(size_reduction), 2)

        params = {
            "marketId": market_id,
            "instructions": [instruction],
        }
        result = self._api_call("cancelOrders", params)
        if result is None:
            return {"status": "ERROR", "error_code": "API_CALL_FAILED"}

        status = result.get("status", "FAILURE")
        if status == "SUCCESS":
            reports = result.get("instructionReports", [{}])
            report = reports[0] if reports else {}
            return {
                "status": "SUCCESS",
                "size_cancelled": float(report.get("sizeCancelled", 0)),
                "cancelled_date": report.get("cancelledDate"),
            }
        return {
            "status": "ERROR",
            "error_code": result.get("errorCode", "UNKNOWN"),
        }

    def replace_order(self, market_id: str, bet_id: str, new_price: float) -> dict:
        """Replace an existing order with a new price (cancel+place)."""
        params = {
            "marketId": market_id,
            "instructions": [{
                "betId": bet_id,
                "newPrice": round(float(new_price), 2),
            }],
        }
        result = self._api_call("replaceOrders", params)
        if result is None:
            return {"status": "ERROR", "error_code": "API_CALL_FAILED"}

        status = result.get("status", "FAILURE")
        if status == "SUCCESS":
            reports = result.get("instructionReports", [{}])
            report = reports[0] if reports else {}
            cancel_report = report.get("cancelInstructionReport", {})
            place_report = report.get("placeInstructionReport", {})
            return {
                "status": "SUCCESS",
                "new_bet_id": place_report.get("betId"),
                "size_cancelled": float(cancel_report.get("sizeCancelled", 0)),
                "size_matched": float(place_report.get("sizeMatched", 0)),
            }
        return {
            "status": "ERROR",
            "error_code": result.get("errorCode", "UNKNOWN"),
        }

    def get_market_depth(self, market_id: str) -> Optional[dict]:
        """
        Fetch full order book depth (3-level back and lay).
        Returns enriched runner data with batb/batl arrays.
        """
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS", "EX_TRADED"],
                "exBestOffersOverrides": {
                    "bestPricesDepth": 3,
                },
            },
        }
        result = self._api_call("listMarketBook", params)
        if result is None or not result:
            return None

        market = result[0]
        return {
            "market_id": market_id,
            "status": market.get("status"),
            "in_play": market.get("inplay", False),
            "total_matched": market.get("totalMatched", 0),
            "runners": [
                {
                    "selection_id": r["selectionId"],
                    "status": r.get("status", "ACTIVE"),
                    "total_matched": r.get("totalMatched", 0),
                    "last_price_traded": r.get("lastPriceTraded"),
                    "ex": r.get("ex", {}),
                    # ex contains:
                    #   availableToBack: [{price, size}, ...]
                    #   availableToLay: [{price, size}, ...]
                    #   tradedVolume: [{price, size}, ...]
                }
                for r in market.get("runners", [])
            ],
        }

    def get_current_orders(
        self,
        market_ids: Optional[list[str]] = None,
        bet_ids: Optional[list[str]] = None,
        customer_order_refs: Optional[list[str]] = None,
    ) -> Optional[list[dict]]:
        """Current orders (executable, plus complete ones not yet settled).

        Returns None if the call failed. That must stay distinct from an empty
        list: a network blip is not evidence that every order has vanished.
        Follows moreAvailable so large books are not silently truncated.
        """
        params: dict = {"orderProjection": "ALL"}
        if market_ids:
            params["marketIds"] = list(market_ids)
        if bet_ids:
            params["betIds"] = list(bet_ids)
        if customer_order_refs:
            params["customerOrderRefs"] = list(customer_order_refs)

        orders: list[dict] = []
        from_record = 0
        while True:
            result = self._api_call("listCurrentOrders", {**params, "fromRecord": from_record})
            if result is None:
                return None
            page = result.get("currentOrders", [])
            orders.extend(page)
            if not result.get("moreAvailable") or not page:
                return orders
            from_record += len(page)
