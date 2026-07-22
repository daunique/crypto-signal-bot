import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
import websockets

from .config import get_settings

log = logging.getLogger(__name__)

API_BASE = "https://api.derivws.com"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"


class DerivClient:
    """Current Deriv API client.

    Public market data uses the public WebSocket. Trading uses a separate
    authenticated WebSocket URL obtained through the PAT + App ID REST OTP flow.
    This separation prevents concurrent market-data and trading requests from
    consuming each other's WebSocket responses.
    """

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

    async def connect(self):
        await self.close()
        self.account_id = await self._select_account()
        otp_url = await self._get_otp_url(self.account_id)

        self.public_ws = await websockets.connect(PUBLIC_WS, ping_interval=20, ping_timeout=20)
        self.trade_ws = await websockets.connect(otp_url, ping_interval=20, ping_timeout=20)
        self._public_reader_task = asyncio.create_task(self._reader("public"))
        self._trade_reader_task = asyncio.create_task(self._reader("trade"))
        log.info("Connected to Deriv current API: account=%s mode=%s", self.account_id, self.settings.bot_mode)

    async def _select_account(self) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{API_BASE}/trading/v1/options/accounts",
                headers=self._headers(),
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Deriv accounts request failed ({response.status_code}): {response.text[:500]}")

        body = response.json()
        data = body.get("data", [])
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected Deriv accounts response: {body}")

        if self.settings.deriv_account_id:
            for account in data:
                if str(account.get("account_id")) == self.settings.deriv_account_id:
                    return self.settings.deriv_account_id
            raise RuntimeError(f"DERIV_ACCOUNT_ID={self.settings.deriv_account_id} was not returned for this token")

        wanted = "demo" if self.settings.bot_mode.lower() == "demo" else "real"
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
            if task:
                task.cancel()
        for task in (self._public_reader_task, self._trade_reader_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._public_reader_task = None
        self._trade_reader_task = None
        for ws in (self.public_ws, self.trade_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        self.public_ws = None
        self.trade_ws = None
        self._fail_waiters(RuntimeError("Deriv connection closed"))

    def _fail_waiters(self, exc: Exception):
        for waiters in (self._public_waiters, self._trade_waiters):
            for future in waiters.values():
                if not future.done():
                    future.set_exception(exc)
            waiters.clear()

    async def _reader(self, channel: str):
        ws = self.public_ws if channel == "public" else self.trade_ws
        waiters = self._public_waiters if channel == "public" else self._trade_waiters
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Ignoring non-JSON Deriv message")
                    continue

                req_id = message.get("req_id")
                if req_id is not None and req_id in waiters:
                    future = waiters.pop(req_id)
                    if message.get("error"):
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        future.set_result(message)
                    continue

                if channel == "public" and message.get("msg_type") == "tick":
                    try:
                        self._public_ticks.put_nowait(message)
                    except asyncio.QueueFull:
                        _ = self._public_ticks.get_nowait()
                        self._public_ticks.put_nowait(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Deriv %s WebSocket reader stopped: %s", channel, exc)
            self._fail_waiters(exc)

    async def request(self, payload: dict[str, Any], channel: str = "trade") -> dict[str, Any]:
        ws = self.trade_ws if channel == "trade" else self.public_ws
        waiters = self._trade_waiters if channel == "trade" else self._public_waiters
        if not ws:
            raise RuntimeError(f"Deriv {channel} WebSocket is not connected")

        if channel == "trade":
            self._trade_req_id += 1
            req_id = self._trade_req_id
        else:
            self._public_req_id += 1
            req_id = self._public_req_id

        future = asyncio.get_running_loop().create_future()
        waiters[req_id] = future
        message = dict(payload)
        message["req_id"] = req_id
        await ws.send(json.dumps(message))
        try:
            return await asyncio.wait_for(future, timeout=20)
        finally:
            waiters.pop(req_id, None)

    async def subscribe_ticks(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        await self.request({"ticks": symbol, "subscribe": 1}, channel="public")
        while True:
            yield await self._public_ticks.get()

    async def get_candles(self, symbol: str, count: int, granularity: int) -> list[dict[str, Any]]:
        response = await self.request({
            "ticks_history": symbol,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity,
        }, channel="public")
        return response.get("candles", [])

    async def proposal(self, symbol: str, direction: str, amount: float, currency: str,
                       duration: int, barrier_distance: float) -> dict[str, Any]:
        contract_type = "HIGHER" if direction == "UP" else "LOWER"
        barrier = f"+{barrier_distance}" if direction == "UP" else f"-{barrier_distance}"
        return await self.request({
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency,
            "duration": duration,
            "duration_unit": "s",
            "underlying_symbol": symbol,
            "barrier": barrier,
        }, channel="trade")

    async def buy(self, proposal_id: str, price: float) -> dict[str, Any]:
        return await self.request({"buy": proposal_id, "price": price}, channel="trade")

    async def proposal_open_contract(self, contract_id: str) -> dict[str, Any]:
        return await self.request({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 0,
        }, channel="trade")
