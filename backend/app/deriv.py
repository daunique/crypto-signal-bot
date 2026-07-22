import asyncio
import json
import logging
from typing import Any, AsyncIterator
import websockets
from .config import get_settings

log = logging.getLogger(__name__)


class DerivClient:
    def __init__(self):
        self.settings = get_settings()
        self.ws = None
        self.req_id = 0

    @property
    def endpoint(self) -> str:
        app_id = self.settings.deriv_app_id or "1089"
        return f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"

    async def connect(self):
        self.ws = await websockets.connect(self.endpoint, ping_interval=20, ping_timeout=20)
        if self.settings.deriv_token:
            await self.request({"authorize": self.settings.deriv_token})
        log.info("Connected to Deriv in %s mode", self.settings.bot_mode)

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ws:
            raise RuntimeError("Deriv websocket is not connected")
        self.req_id += 1
        payload = dict(payload)
        payload["req_id"] = self.req_id
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await self.ws.recv()
            response = json.loads(raw)
            if response.get("req_id") == self.req_id:
                if response.get("error"):
                    raise RuntimeError(response["error"])
                return response

    async def subscribe_ticks(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        if not self.ws:
            raise RuntimeError("Deriv websocket is not connected")
        self.req_id += 1
        await self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": self.req_id}))
        async for raw in self.ws:
            response = json.loads(raw)
            if response.get("msg_type") == "tick":
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

    async def proposal(self, symbol: str, direction: str, amount: float, currency: str,
                       duration: int, barrier_distance: float, spot: float) -> dict[str, Any]:
        # Deriv Higher/Lower contract request. Exact contract parameters should
        # be verified against the account's current API contract schema.
        contract_type = "CALL" if direction == "UP" else "PUT"
        barrier = f"+{barrier_distance}" if direction == "UP" else f"-{barrier_distance}"
        return await self.request({
            "proposal": 1,
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency,
            "duration": duration,
            "duration_unit": "s",
            "symbol": symbol,
            "barrier": barrier,
        })

    async def buy(self, proposal_id: str, price: float) -> dict[str, Any]:
        return await self.request({"buy": proposal_id, "price": price})

    async def proposal_open_contract(self, contract_id: str) -> dict[str, Any]:
        return await self.request({"proposal_open_contract": 1, "contract_id": contract_id})
