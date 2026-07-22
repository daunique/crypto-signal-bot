import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
import websockets

from .config import get_settings

log = logging.getLogger(__name__)

REST_BASE = "https://api.derivws.com"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"


class DerivAPIError(RuntimeError):
    pass


class DerivClient:
    """Deriv client for the current PAT application API.

    Market data uses the public WebSocket. Account/trading operations use an
    authenticated WebSocket URL obtained from the PAT + App ID OTP flow.
    This deliberately does not use the legacy `?app_id=...` authorize flow.
    """

    def __init__(self):
        self.settings = get_settings()
        self.market_ws = None
        self.trade_ws = None
        self.req_id = 0
        self.request_lock = asyncio.Lock()
        self.account_id: str | None = None
        self._closed = False

    def _headers(self) -> dict[str, str]:
        if not self.settings.deriv_app_id:
            raise DerivAPIError("DERIV_APP_ID is missing")
        if not self.settings.auth_token:
            raise DerivAPIError("No Deriv PAT/API token found. Set DERIV_PAT or DERIV_API_TOKEN.")
        return {
            "Deriv-App-ID": self.settings.deriv_app_id,
            "Authorization": f"Bearer {self.settings.auth_token}",
            "Accept": "application/json",
        }

    async def _rest(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=REST_BASE, timeout=20.0) as client:
            response = await client.request(method, path, headers=self._headers(), **kwargs)
            try:
                payload = response.json()
            except Exception:
                payload = {"raw": response.text}
            if response.is_error:
                raise DerivAPIError(f"Deriv REST {response.status_code}: {payload}")
            return payload

    async def resolve_account(self) -> str:
        if self.settings.deriv_account_id:
            self.account_id = self.settings.deriv_account_id.strip()
            return self.account_id

        response = await self._rest("GET", "/trading/v1/options/accounts")
        data = response.get("data", [])
        if isinstance(data, dict):
            data = [data]
        accounts = [x for x in data if isinstance(x, dict)]
        matching = [x for x in accounts if x.get("account_type") == self.settings.account_type]
        if not matching:
            raise DerivAPIError(
                f"No {self.settings.account_type} Options account found for this PAT. "
                "Set DERIV_ACCOUNT_ID explicitly if the account list contains the required account."
            )
        self.account_id = str(matching[0].get("account_id"))
        if not self.account_id or self.account_id == "None":
            raise DerivAPIError("Deriv account response did not contain account_id")
        return self.account_id

    async def _get_authenticated_ws_url(self) -> str:
        account_id = await self.resolve_account()
        response = await self._rest(
            "POST", f"/trading/v1/options/accounts/{account_id}/otp"
        )
        url = (response.get("data") or {}).get("url")
        if not url:
            raise DerivAPIError(f"OTP response did not contain data.url: {response}")
        return url

    async def connect(self):
        self._closed = False
        self.market_ws = await websockets.connect(
            PUBLIC_WS, ping_interval=20, ping_timeout=20, close_timeout=5
        )
        trade_url = await self._get_authenticated_ws_url()
        self.trade_ws = await websockets.connect(
            trade_url, ping_interval=20, ping_timeout=20, close_timeout=5
        )
        log.info("Connected to Deriv current API in %s mode, account=%s", self.settings.bot_mode, self.account_id)

    async def close(self):
        self._closed = True
        for ws in (self.market_ws, self.trade_ws):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    log.exception("Error closing Deriv websocket")
        self.market_ws = None
        self.trade_ws = None

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.trade_ws:
            raise DerivAPIError("Authenticated Deriv websocket is not connected")
        async with self.request_lock:
            self.req_id += 1
            request_id = self.req_id
            message = dict(payload)
            message["req_id"] = request_id
            await self.trade_ws.send(json.dumps(message))
            while True:
                raw = await self.trade_ws.recv()
                response = json.loads(raw)
                if response.get("req_id") != request_id:
                    continue
                if response.get("error"):
                    raise DerivAPIError(str(response["error"]))
                return response

    async def subscribe_ticks(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        if not self.market_ws:
            raise DerivAPIError("Public market websocket is not connected")
        await self.market_ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 1}))
        async for raw in self.market_ws:
            try:
                response = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if response.get("msg_type") == "tick" and isinstance(response.get("tick"), dict):
                yield response

    async def get_candles(self, symbol: str, count: int, granularity: int) -> list[dict[str, Any]]:
        response = await self.request({
            "ticks_history": symbol,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity,
        })
        return response.get("candles", [])

    async def proposal(
        self, symbol: str, direction: str, amount: float, currency: str,
        duration: int, barrier_distance: float
    ) -> dict[str, Any]:
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
        })

    async def buy(self, proposal_id: str, price: float) -> dict[str, Any]:
        return await self.request({"buy": proposal_id, "price": price})

    async def proposal_open_contract(self, contract_id: str) -> dict[str, Any]:
        return await self.request({"proposal_open_contract": 1, "contract_id": int(contract_id)})
