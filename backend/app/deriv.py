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
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class DerivClient:
    """Current Deriv API client.

    PAT authentication is performed only through REST using:
      Deriv-App-ID + Authorization: Bearer <PAT>

    The REST OTP endpoint then returns a short-lived authenticated WebSocket URL.
    No legacy `authorize(token)` call is used anywhere.
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
        app_id = self.settings.deriv_app_id.strip()
        token = self.settings.auth_token
        if not app_id:
            raise DerivAPIError("DERIV_APP_ID is missing")
        if not token:
            raise DerivAPIError(
                "No PAT token found. Set DERIV_PAT to the full Personal Access Token created for this PAT app."
            )
        return {
            "Deriv-App-ID": app_id,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
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
                errors = payload.get("errors") if isinstance(payload, dict) else None
                detail = errors[0] if isinstance(errors, list) and errors else payload
                raise DerivAPIError(
                    f"Deriv REST {response.status_code}: {detail}",
                    status_code=response.status_code,
                    code=(detail.get("code") if isinstance(detail, dict) else None),
                )
            return payload

    async def resolve_account(self) -> str:
        if self.settings.deriv_account_id.strip():
            self.account_id = self.settings.deriv_account_id.strip()
            return self.account_id

        response = await self._rest("GET", "/trading/v1/options/accounts")
        data = response.get("data", [])
        if isinstance(data, dict):
            data = [data]
        accounts = [x for x in data if isinstance(x, dict)]
        matching = [x for x in accounts if str(x.get("account_type", "")).lower() == self.settings.account_type]
        if not matching:
            available = [x.get("account_type") for x in accounts]
            raise DerivAPIError(
                f"No {self.settings.account_type} Options account found. Available account types: {available}. "
                "Set DERIV_ACCOUNT_ID to the exact Options account ID if needed."
            )
        self.account_id = str(matching[0].get("account_id"))
        if not self.account_id or self.account_id == "None":
            raise DerivAPIError("Deriv account response did not contain account_id")
        return self.account_id

    async def _get_authenticated_ws_url(self) -> str:
        account_id = await self.resolve_account()
        response = await self._rest("POST", f"/trading/v1/options/accounts/{account_id}/otp")
        data = response.get("data") or {}
        url = data.get("url")
        if not url:
            raise DerivAPIError(f"OTP response did not contain data.url: {response}")
        return url

    async def connect(self):
        await self.close()
        self._closed = False

        # Public market data needs no authentication.
        self.market_ws = await websockets.connect(
            PUBLIC_WS, ping_interval=20, ping_timeout=20, close_timeout=5
        )

        # PAT -> REST OTP -> authenticated WS. No authorize request is sent.
        trade_url = await self._get_authenticated_ws_url()
        self.trade_ws = await websockets.connect(
            trade_url, ping_interval=20, ping_timeout=20, close_timeout=5
        )
        log.info(
            "Deriv authenticated WebSocket connected: mode=%s account=%s",
            self.settings.bot_mode,
            self.account_id,
        )

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
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    response = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if response.get("req_id") != request_id:
                    continue
                if response.get("error"):
                    err = response["error"]
                    raise DerivAPIError(str(err), code=err.get("code") if isinstance(err, dict) else None)
                return response

    async def subscribe_ticks(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        if not self.market_ws:
            raise DerivAPIError("Public market websocket is not connected")
        await self.market_ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 1}))
        async for raw in self.market_ws:
            try:
                response = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            # Ignore authorize/error/subscription/other messages safely.
            if response.get("msg_type") != "tick":
                continue
            tick = response.get("tick")
            if not isinstance(tick, dict) or "epoch" not in tick:
                continue
            yield response

    async def get_candles(self, symbol: str, count: int, granularity: int) -> list[dict[str, Any]]:
        response = await self.request({
            "ticks_history": symbol,
            "count": count,
            "end": "latest",
            "style": "candles",
            "granularity": granularity,
        })
        candles = response.get("candles", [])
        return candles if isinstance(candles, list) else []

    async def proposal(
        self, symbol: str, direction: str, amount: float, currency: str,
        duration: int, barrier_distance: float
    ) -> dict[str, Any]:
        contract_type = "HIGHER" if direction == "UP" else "LOWER"
        # Relative barrier is signed: + for HIGHER, - for LOWER.
        barrier = f"+{barrier_distance:g}" if direction == "UP" else f"-{barrier_distance:g}"
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
