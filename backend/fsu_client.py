"""
CHIMERA FSU Client
==================
Drop-in replacement for BetfairClient that fetches data from the FSU
(Fractional Services Unit) instead of the live Betfair Exchange API.

Used by the Lay Engine when running in BACKTEST mode.

The interface deliberately mirrors BetfairClient so that engine.py
needs minimal changes to support backtesting:

  client = FSUClient(base_url=FSU_URL, date="2025-07-13")
  client.set_virtual_time("2025-07-13T12:00:00Z")

  markets = client.get_todays_win_markets(countries=["GB", "IE"])
  runners, valid = client.get_market_prices(market_id)
  book = client.get_market_book_full(market_id)

The virtual_time property controls which point in the historic
timeline the FSU reconstructs state for.  Advance it to replay
market evolution through a race day.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from rules import Runner

logger = logging.getLogger("fsu_client")

FSU_BASE_URL = os.getenv("FSU_URL", "https://fsu.thync.online")


class FSUClient:
    """
    Fetches historic Betfair market data from the FSU service.
    Mirrors the BetfairClient interface used by LayEngine.
    """

    def __init__(
        self,
        base_url: str = FSU_BASE_URL,
        date: Optional[str] = None,
        virtual_time: Optional[str] = None,
        timeout: int = 30,
    ):
        """
        Args:
            base_url:     FSU service root URL, e.g. "https://fsu.thync.online"
            date:         The backtest date (YYYY-MM-DD). Defaults to today.
            virtual_time: ISO 8601 datetime representing "now" in the replay.
                          If None, defaults to the current wallclock time (UTC).
            timeout:      HTTP request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._virtual_time: Optional[str] = virtual_time
        self.timeout = timeout
        self._session = requests.Session()

    # ──────────────────────────────────────────────
    #  VIRTUAL CLOCK
    # ──────────────────────────────────────────────

    @property
    def virtual_time(self) -> str:
        """
        The current virtual timestamp used for all FSU queries.
        If not explicitly set, returns the current UTC wallclock time.
        """
        if self._virtual_time:
            return self._virtual_time
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def set_virtual_time(self, iso_timestamp: str) -> None:
        """
        Set the virtual clock to a specific point in the replay timeline.
        iso_timestamp: e.g. "2025-07-13T12:30:00Z"
        """
        self._virtual_time = iso_timestamp
        logger.info(f"FSU virtual time → {iso_timestamp}")

    def advance_virtual_time(self, seconds: int) -> None:
        """Advance the virtual clock by the given number of seconds."""
        ts = self._parse_ts(self.virtual_time)
        new_ts = ts + seconds * 1000
        iso = datetime.fromtimestamp(new_ts / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.set_virtual_time(iso)

    # ──────────────────────────────────────────────
    #  GCP IDENTITY TOKEN  (Cloud Run → Cloud Run)
    # ──────────────────────────────────────────────

    def _fetch_identity_token(self) -> Optional[str]:
        """
        Fetch a GCP OIDC identity token from the metadata server.
        Only available when running on Cloud Run / GCE / GKE.
        Returns None in local dev so requests still go through unauthenticated.
        """
        meta_url = (
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={self.base_url}"
        )
        try:
            resp = requests.get(
                meta_url,
                headers={"Metadata-Flavor": "Google"},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception:
            pass
        return None

    def _refresh_auth_header(self) -> None:
        """Set (or clear) the Authorization header on the session."""
        token = self._fetch_identity_token()
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})
            logger.info("FSUClient: GCP identity token set on session")
        else:
            logger.info("FSUClient: no metadata server — running unauthenticated (local dev)")

    # ──────────────────────────────────────────────
    #  AUTH STUBS  (mirrors BetfairClient interface)
    # ──────────────────────────────────────────────

    def login(self) -> bool:
        """Fetch GCP identity token and attach to session."""
        self._refresh_auth_header()
        logger.info(f"FSUClient: backtest mode for date={self.date}")
        return True

    def logout(self) -> None:
        pass

    def ensure_session(self) -> bool:
        return True

    def get_account_balance(self) -> float:
        """Returns a fixed virtual balance for backtest sessions."""
        return 10_000.0

    # ──────────────────────────────────────────────
    #  MARKET DISCOVERY
    # ──────────────────────────────────────────────

    def get_todays_win_markets(
        self, countries: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Fetch WIN markets for the backtest date from the FSU.
        Mirrors BetfairClient.get_todays_win_markets().
        """
        countries = countries or ["GB", "IE"]
        params = {
            "date": self.date,
            "market_type": "WIN",
            "countries": ",".join(countries),
        }
        resp = self._get("/api/markets", params=params)
        if resp is None:
            return []

        markets = []
        for m in resp.get("markets", []):
            markets.append({
                "market_id": m["market_id"],
                "market_name": m["market_name"],
                "venue": m["venue"],
                "country": m["country"],
                "race_time": m["race_time"],
                "runners": [
                    {
                        "selection_id": r["selection_id"],
                        "runner_name": r["runner_name"],
                        "handicap": r.get("handicap", 0.0),
                        "sort_priority": r["sort_priority"],
                    }
                    for r in m.get("runners", [])
                ],
            })

        logger.info(
            f"FSU: {len(markets)} WIN markets for {self.date} ({'/'.join(countries)})"
        )
        return markets

    # ──────────────────────────────────────────────
    #  PRICE RETRIEVAL
    # ──────────────────────────────────────────────

    def get_market_prices(self, market_id: str) -> tuple[list[Runner], bool]:
        """
        Get best-available lay/back prices for all runners at virtual_time.
        Mirrors BetfairClient.get_market_prices().
        Returns (runners, is_valid) where is_valid=False if market is
        closed, suspended, or in-play.
        """
        params = {
            "timestamp": self.virtual_time,
            "date": self.date,
        }
        resp = self._get(f"/api/markets/{market_id}/prices", params=params)
        if resp is None:
            return [], False

        status = resp.get("status", "OPEN")
        in_play = resp.get("in_play", False)

        if status != "OPEN":
            logger.warning(f"FSU: market {market_id} status={status} — skipping")
            return [], False

        if in_play:
            logger.warning(f"FSU: market {market_id} is IN-PLAY — skipping (pre-off only)")
            return [], False

        runners = []
        for r in resp.get("runners", []):
            runner = Runner(
                selection_id=r["selection_id"],
                runner_name=r.get("runner_name", f"Runner {r['selection_id']}"),
                handicap=r.get("handicap", 0.0),
                status=r.get("status", "ACTIVE"),
            )
            runner.best_available_to_lay = r.get("best_available_to_lay")
            runner.best_available_to_back = r.get("best_available_to_back")
            runners.append(runner)

        return runners, True

    def get_market_book(self, market_id: str) -> Optional[dict]:
        """
        Get market book (status + inPlay flag) at virtual_time.
        Mirrors BetfairClient.get_market_book().
        """
        book = self.get_market_book_full(market_id)
        return book

    def get_market_book_full(self, market_id: str) -> Optional[dict]:
        """
        Get full market book with 3-level depth at virtual_time.
        Mirrors BetfairClient.get_market_book_full().
        """
        params = {
            "timestamp": self.virtual_time,
            "date": self.date,
        }
        resp = self._get(f"/api/markets/{market_id}/book", params=params)
        if resp is None:
            return None

        return {
            "market_id": market_id,
            "status": resp.get("status"),
            "in_play": resp.get("in_play", False),
            "total_matched": resp.get("total_matched", 0),
            "number_of_runners": resp.get("number_of_runners", 0),
            "runners": [
                {
                    "selection_id": r.get("selection_id"),
                    "status": r.get("status", "ACTIVE"),
                    "last_price_traded": r.get("last_price_traded"),
                    "total_matched": r.get("total_matched", 0),
                    "back": r.get("back", []),
                    "lay": r.get("lay", []),
                }
                for r in resp.get("runners", [])
            ],
        }

    # ──────────────────────────────────────────────
    #  ORDER PLACEMENT  (no-op in backtest)
    # ──────────────────────────────────────────────

    def place_lay_order(
        self,
        market_id: str,
        selection_id: int,
        price: float,
        size: float,
    ) -> Optional[dict]:
        """
        In backtest mode, order placement is a no-op that returns a
        synthetic confirmation so the engine's bet tracking still works.
        """
        logger.info(
            f"FSU [backtest]: LAY {selection_id} @ {price} £{size:.2f} on {market_id} "
            f"(virtual time: {self.virtual_time})"
        )
        return {
            "status": "SUCCESS",
            "instruction_reports": [{
                "status": "SUCCESS",
                "order_status": "EXECUTABLE",
                "instruction": {
                    "selection_id": selection_id,
                    "limit_order": {"price": price, "size": size},
                },
                "bet_id": f"BACKTEST-{market_id[-6:]}-{selection_id}",
            }],
        }

    # ──────────────────────────────────────────────
    #  RACE RESULT  (derived from marketDefinition)
    # ──────────────────────────────────────────────

    def get_race_result(self, market_id: str) -> Optional[dict]:
        """
        Determine the race result by inspecting the final market state.
        Returns { settled: bool, winner_selection_id: int | None }.
        """
        # Use the last available timestamp by not providing a time bound
        # (reconstruct_at with a very large value gives the final state)
        far_future_ms = int(datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        params = {
            "timestamp": "2099-01-01T00:00:00Z",
            "date": self.date,
        }
        resp = self._get(f"/api/markets/{market_id}/prices", params=params)
        if resp is None:
            return None

        md_status = resp.get("status", "OPEN")
        settled = md_status == "CLOSED"

        # The winner is the runner whose status is WINNER
        winner_id = None
        for r in resp.get("runners", []):
            if r.get("status") == "WINNER":
                winner_id = r["selection_id"]
                break

        return {"settled": settled, "winner_selection_id": winner_id}

    # ──────────────────────────────────────────────
    #  TIMELINE  (backtest-specific)
    # ──────────────────────────────────────────────

    def get_market_timeline(self, market_id: str) -> Optional[dict]:
        """
        Return all MCM timestamps for a market file.
        Use this to drive a replay loop:

            tl = client.get_market_timeline(market_id)
            for ts_ms in tl["timestamps"]:
                iso = ms_to_iso(ts_ms)
                client.set_virtual_time(iso)
                runners, valid = client.get_market_prices(market_id)
                ...
        """
        resp = self._get(f"/api/markets/{market_id}/timeline", params={"date": self.date})
        return resp

    # ──────────────────────────────────────────────
    #  INTERNALS
    # ──────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"FSU HTTP error {e.response.status_code} for {url}: {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"FSU request failed for {url}: {e}")
            return None

    @staticmethod
    def _parse_ts(iso: str) -> int:
        """Convert ISO 8601 string to epoch milliseconds."""
        ts = iso.strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
