import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from .config import get_settings

log = logging.getLogger(__name__)

API_BASE = "https://api.derivws.com"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"


class DerivAPIError(RuntimeError):
    """A structured error response from Deriv (the `error` object of a reply).

    Carries `.code`/`.subcode` so callers can react to specific failures
    (e.g. retrying with an adjusted barrier on InvalidBarrier) without
    parsing the stringified exception message.
    """

    def __init__(self, error: dict[str, Any]):
        self.code = error.get("code")
        self.subcode = error.get("subcode")
        self.message = error.get("message")
        self.details = error.get("details")
        super().__init__(str(error))


class DerivClient:
    """Resilient Deriv current API client using PAT authentication.

    Market data and trading use separate WebSocket connections. Trades are
    genuine Higher/Lower contracts: a signed, relative `barrier` offset is
    always included (see build_proposal_payload).
    """

    # This account's own live `contracts_for` response (2026-07-24, via the
    # /api/diagnostics/contracts-for endpoint -- see README) confirmed
    # "HIGHER"/"LOWER" is the correct contract_type, and found an "intraday"
    # duration tier (15s-1d) for R_25's "higherlower" category. That check
    # was run before this bot moved to tick-duration contracts (2026-07-29) --
    # it did NOT specifically confirm a tick-count duration tier (duration_unit
    # "t") is offered for this contract type/symbol/account. Deriv's tick
    # Higher/Lower contracts commonly allow 5-10 ticks, but "commonly" is not
    # "confirmed for this account": re-run /api/diagnostics/contracts-for
    # after connecting and check the response for a duration_unit of "t"
    # before trading this live. If it isn't offered, `proposal()` below will
    # fail with a DerivAPIError (InvalidContractType or a duration error)
    # rather than silently trading something else -- it will not fall back to
    # a different duration on its own.
    #
    # An earlier version of this mapped UP/DOWN to CALL/PUT instead, based
    # on Deriv's general Higher/Lower docs (developers.deriv.com/docs/
    # higherlower, legacy-docs.deriv.com/docs/higherlower), which describe
    # CALL/PUT for this product -- reasonable given that source, but wrong
    # for what this account's API actually accepts. That's also why every
    # barrier value was rejected as InvalidBarrier regardless of magnitude
    # or sign (2026-07-23/24 entries below): CALL/PUT was never going to
    # accept a barrier here, no matter its value.
    DIRECTION_TO_CONTRACT_TYPE = {"UP": "HIGHER", "DOWN": "LOWER"}

    def __init__(self):
        self.settings = get_settings()
        self.public_ws = None
        self.trade_ws = None
        self._public_reader_task = None
        self._trade_reader_task = None
        self._public_req_id = 0
        self._trade_req_id = 0
        self._public_waiters: dict[int, asyncio.Future] = {}
        self._trade_waiters: dict[int, asyncio.Future] = {}
        self._public_ticks: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.account_id: str | None = None
        self.current_mode: str | None = None

    @property
    def trade_connected(self) -> bool:
        """Whether the trade (authenticated) WebSocket is actually usable.

        A brief network blip can sever just this connection while the
        separate public/tick connection stays alive -- previously nothing
        checked for that independently of an actual trade attempt, so a
        dead trade connection could go unnoticed indefinitely (see
        BotEngine.tick_loop in engine.py and the README).
        """
        return (
            self.trade_ws is not None
            and self._trade_reader_task is not None
            and not self._trade_reader_task.done()
        )

    def _headers(self) -> dict[str, str]:
        if not self.settings.deriv_app_id:
            raise RuntimeError("DERIV_APP_ID is not configured")
        if not self.settings.deriv_pat:
            raise RuntimeError("DERIV_PAT is not configured")
        return {
            "Deriv-App-ID": self.settings.deriv_app_id,
            "Authorization": f"Bearer {self.settings.deriv_pat}",
            "Content-Type": "application/json",
        }

    async def _connect_ws(self, uri: str):
        last_error = None
        for attempt in range(3):
            try:
                return await asyncio.wait_for(
                    websockets.connect(
                        uri,
                        open_timeout=15,
                        close_timeout=5,
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=2**20,
                    ),
                    timeout=20,
                )
            except (asyncio.TimeoutError, TimeoutError, OSError, InvalidStatus, InvalidURI, ConnectionClosed) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"WebSocket handshake failed after retries: {last_error}")

    async def connect(self, mode: str):
        await self.close()
        self.public_ws = await self._connect_ws(PUBLIC_WS)
        try:
            # OTPs are short-lived and must be used immediately after being
            # minted, so the account/OTP lookup happens right before the
            # trade WS connection, not before the (potentially slow, with
            # up to 3 retries) public WS handshake above.
            self.account_id = await self._select_account(mode)
            otp_url = await self._get_otp_url(self.account_id)
            self.trade_ws = await self._connect_ws(otp_url)
        except Exception:
            await self.close()
            raise
        self._public_reader_task = asyncio.create_task(self._reader("public"), name="deriv-public-reader")
        self._trade_reader_task = asyncio.create_task(self._reader("trade"), name="deriv-trade-reader")
        log.info("DERIV_CONNECTED account=%s mode=%s", self.account_id, self.current_mode)

    async def _select_account(self, mode: str) -> str:
        # `mode` is resolved by the caller (engine.py, via db.py's
        # get_effective_bot_mode/RuntimeSetting -- see README) since it can
        # be changed from the dashboard without a redeploy. Kept as an
        # explicit parameter here, rather than this module importing the
        # DB-backed resolver itself, so DerivClient stays a decoupled,
        # independently-testable Deriv API client with no knowledge of this
        # app's specific settings-persistence mechanism. Stored so callers
        # (logging, Trade.mode) show what was actually used, even if
        # DERIV_ACCOUNT_ID below bypasses using it for selection.
        self.current_mode = mode
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{API_BASE}/trading/v1/options/accounts", headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(f"Deriv accounts request failed ({response.status_code}): {response.text[:500]}")
        body = response.json()
        data = body.get("data", [])
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected Deriv accounts response: {body}")
        if self.settings.deriv_account_id:
            if any(str(a.get("account_id")) == self.settings.deriv_account_id for a in data):
                return self.settings.deriv_account_id
            raise RuntimeError(f"DERIV_ACCOUNT_ID={self.settings.deriv_account_id} was not returned for this token")
        wanted = "demo" if mode == "demo" else "real"
        candidates = [a for a in data if str(a.get("account_type", "")).lower() == wanted]
        active = [a for a in candidates if str(a.get("status", "active")).lower() == "active"]
        selected = active[0] if active else (candidates[0] if candidates else None)
        if not selected or not selected.get("account_id"):
            raise RuntimeError(f"No active {wanted} Options account was returned by Deriv")
        return str(selected["account_id"])

    async def _get_otp_url(self, account_id: str) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{API_BASE}/trading/v1/options/accounts/{account_id}/otp",
                headers=self._headers(),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Deriv OTP request failed ({response.status_code}): {response.text[:500]}")
        body = response.json()
        url = (body.get("data") or {}).get("url")
        if not url:
            raise RuntimeError(f"Deriv OTP response did not contain data.url: {body}")
        return url

    async def close(self):
        for task in (self._public_reader_task, self._trade_reader_task):
            if task and not task.done():
                task.cancel()
        for task in (self._public_reader_task, self._trade_reader_task):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._public_reader_task = self._trade_reader_task = None
        for ws in (self.public_ws, self.trade_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        self.public_ws = self.trade_ws = None
        self._fail_waiters(RuntimeError("Deriv connection closed"))

    def _fail_waiters(self, exc: Exception):
        for waiters in (self._public_waiters, self._trade_waiters):
            for future in list(waiters.values()):
                if not future.done():
                    future.set_exception(exc)
            waiters.clear()

    async def _reader(self, channel: str):
        ws = self.public_ws if channel == "public" else self.trade_ws
        waiters = self._public_waiters if channel == "public" else self._trade_waiters
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        log.warning("Ignoring non-UTF8 WebSocket message")
                        continue
                if not isinstance(raw, str):
                    log.warning("Ignoring non-text WebSocket message")
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Ignoring non-JSON Deriv message")
                    continue
                if not isinstance(message, dict):
                    continue
                req_id = message.get("req_id")
                if req_id is not None and req_id in waiters:
                    future = waiters.pop(req_id)
                    if not future.done():
                        if message.get("error"):
                            future.set_exception(DerivAPIError(message["error"]))
                        else:
                            future.set_result(message)
                    continue
                if channel == "public" and message.get("msg_type") == "tick":
                    try:
                        self._public_ticks.put_nowait(message)
                    except asyncio.QueueFull:
                        try:
                            self._public_ticks.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        self._public_ticks.put_nowait(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Deriv %s reader stopped: %s", channel, exc)
            self._fail_waiters(exc)

    async def request(self, payload: dict[str, Any], channel: str = "trade") -> dict[str, Any]:
        ws = self.trade_ws if channel == "trade" else self.public_ws
        reader = self._trade_reader_task if channel == "trade" else self._public_reader_task
        if ws is None or reader is None or reader.done():
            raise RuntimeError(f"Deriv {channel} WebSocket is not connected")
        if channel == "trade":
            self._trade_req_id += 1
            req_id = self._trade_req_id
            waiters = self._trade_waiters
        else:
            self._public_req_id += 1
            req_id = self._public_req_id
            waiters = self._public_waiters
        future = asyncio.get_running_loop().create_future()
        waiters[req_id] = future
        try:
            await asyncio.wait_for(ws.send(json.dumps({**payload, "req_id": req_id})), timeout=5)
            return await asyncio.wait_for(future, timeout=self.settings.request_timeout_seconds)
        finally:
            waiters.pop(req_id, None)

    async def subscribe_ticks(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        await self.request({"ticks": symbol, "subscribe": 1}, channel="public")
        while True:
            yield await self._public_ticks.get()

    async def get_candles(self, symbol: str, count: int, granularity: int) -> list[dict[str, Any]]:
        response = await self.request({
            "ticks_history": symbol, "count": count, "end": "latest",
            "style": "candles", "granularity": granularity,
        }, channel="public")
        return response.get("candles", [])

    async def get_tick_history(self, symbol: str, count: int, end: int | str = "latest") -> list[dict[str, Any]]:
        """Raw tick history (epoch/quote pairs), for bootstrapping the
        volatility-percentile trailing window on first startup -- see
        engine.py's VolatilityTracker.bootstrap(). `end` can be an epoch
        (int) to page backward past `count`, or "latest" for the most recent
        `count` ticks. Deriv's `ticks_history` request caps `count` per call
        (commonly up to 5000; not independently re-verified for this
        account/symbol) -- the caller is responsible for paging across
        multiple calls if more than one page of history is needed, using
        each response's earliest epoch minus one as the next `end`.
        """
        response = await self.request({
            "ticks_history": symbol, "count": count, "end": end,
            "style": "ticks",
        }, channel="public")
        history = response.get("history", {})
        times = history.get("times", [])
        prices = history.get("prices", [])
        return [{"epoch": t, "quote": p} for t, p in zip(times, prices)]

    def build_proposal_payload(self, symbol: str, direction: str, amount: float, currency: str, duration: int, barrier_offset: float, duration_unit: str = "t") -> dict[str, Any]:
        try:
            contract_type = self.DIRECTION_TO_CONTRACT_TYPE[direction]
        except KeyError:
            raise ValueError(f"Unknown signal direction: {direction!r}") from None
        if barrier_offset <= 0:
            raise ValueError(f"barrier_offset must be > 0 for a Higher/Lower contract, got {barrier_offset!r}")
        if duration_unit not in ("t", "s"):
            raise ValueError(f"duration_unit must be 't' (ticks) or 's' (seconds), got {duration_unit!r}")
        # Deriv's Higher/Lower barrier must be a signed, relative offset --
        # confirmed directly against this account's own contracts_for
        # response (see README and the class docstring above). Positive/
        # HIGHER = barrier above the entry spot; negative/LOWER = barrier
        # below the entry spot.
        signed_offset = barrier_offset if direction == "UP" else -barrier_offset
        return {
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency,
            "duration": duration,
            "duration_unit": duration_unit,
            "underlying_symbol": symbol,
            "barrier": f"{signed_offset:+.3f}",
        }

    async def proposal(self, symbol: str, direction: str, amount: float, currency: str, duration: int, barrier_offset: float, duration_unit: str = "t") -> dict[str, Any]:
        payload = self.build_proposal_payload(symbol, direction, amount, currency, duration, barrier_offset, duration_unit)
        return await self.request(payload, channel="trade")

    async def buy(self, proposal_id: str, price: float) -> dict[str, Any]:
        return await self.request({"buy": proposal_id, "price": price}, channel="trade")

    def build_proposal_open_contract_payload(self, contract_id: str) -> dict[str, Any]:
        # Per Deriv's schema, `subscribe` is optional but its *only* legal
        # value is the integer 1 -- there is no "0" for a one-shot check,
        # you simply omit the field. Sending 0 (as this used to) is
        # rejected with InputValidationFailed on every single poll attempt,
        # meaning contract settlement was never being recorded.
        return {"proposal_open_contract": 1, "contract_id": int(contract_id)}

    async def proposal_open_contract(self, contract_id: str) -> dict[str, Any]:
        payload = self.build_proposal_open_contract_payload(contract_id)
        return await self.request(payload, channel="trade")

    def build_contracts_for_payload(self, symbol: str) -> dict[str, Any]:
        # Deriv's error response was explicit and did NOT complain about the
        # `contracts_for` key itself: "Properties not allowed: currency,
        # underlying_symbol." Unlike proposal/proposal_open_contract, this
        # endpoint does not use the flag=1 + separate underlying_symbol
        # pattern -- the symbol is the value of contracts_for directly
        # (matching the old API's shape), with no other properties allowed.
        return {"contracts_for": symbol}

    async def contracts_for(self, symbol: str) -> dict[str, Any]:
        """Live diagnostic query: ask Deriv what contract types, barrier
        limits, and duration limits actually exist for this symbol on this
        account. This is Deriv's own documented way to determine valid
        barrier ranges (see README) -- used here because repeatedly guessing
        barrier magnitudes (a 100x range, both signs) was conclusively ruled
        out by empirical retries, and this app's own research couldn't
        confidently pin down the exact current response shape in advance.
        Returns the raw response so a human/Claude can read the real field
        names directly instead of trusting a possibly-wrong parse of them.
        """
        payload = self.build_contracts_for_payload(symbol)
        return await self.request(payload, channel="trade")
