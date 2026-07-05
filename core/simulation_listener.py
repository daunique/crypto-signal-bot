"""
POLYBOT — Simulation WebSocket Listener
A deliberately MINIMAL WebSocket listener used only by run_simulation.py.

Unlike core/websocket_listener.py (the real trading listener), this
one has NO OrderExecutor, NO CapitalEngine, NO CircuitBreaker, and
NO PositionManager wired in — it cannot place a real order even by
accident, because it never holds a reference to anything capable of
doing so. It reuses the same real-time price-parsing logic (the
best_bid_ask / book / price_change event handling) since that part
is purely read-only market data with no execution risk, then feeds
every price update into core/simulation_engine.py's SimulationEngine
instead of the live trading pipeline.
"""
import asyncio
import json
import time
import websockets
from config import Config


class SimulationListener:
    def __init__(self, discovery, sim_engine):
        self.discovery = discovery
        self.sim = sim_engine
        self.book = {}
        self.ws = None
        self.ws_connected = False
        self.pending_subs = []
        self._reconnect_attempt = 0

    async def run(self):
        while True:
            try:
                await self._connect_and_listen()
            except Exception as e:
                self.ws_connected = False
                delay = min(30, 2 ** self._reconnect_attempt)
                self._reconnect_attempt += 1
                print(f"[SIM-WS] Disconnected: {e} — "
                      f"reconnecting in {delay}s")
                await asyncio.sleep(delay)

    async def _connect_and_listen(self):
        print("[SIM-WS] Connecting (read-only, simulation mode)...")
        async with websockets.connect(
            Config.WS_URL, ping_interval=20, ping_timeout=10,
            close_timeout=5, max_size=10_000_000
        ) as ws:
            self.ws = ws
            self.ws_connected = True
            self._reconnect_attempt = 0
            print("[SIM-WS] Connected — simulating trades against "
                  "live prices (NO real orders will ever be placed)")

            if self.pending_subs:
                await self._send_subscription(self.pending_subs)
                self.pending_subs = []

            async for raw_message in ws:
                await self._on_message(raw_message)

    async def _on_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = data.get("event_type") or data.get("type")
        if not event_type:
            return

        if event_type == "best_bid_ask":
            token_id = data.get("asset_id")
            best_ask = data.get("best_ask")
            if not token_id or best_ask is None:
                return
            if token_id not in self.book:
                self.book[token_id] = {}
            self.book[token_id]["bid"] = float(data.get("best_bid") or 0)
            self.book[token_id]["ask"] = float(best_ask)
            await self._check_simulated_opportunity(token_id)

        elif event_type == "book":
            token_id = data.get("asset_id")
            if token_id and data.get("asks"):
                best_ask = float(data["asks"][0]["price"]) if data["asks"] else None
                best_bid = float(data["bids"][0]["price"]) if data.get("bids") else None
                if best_ask:
                    self.book[token_id] = {"bid": best_bid or 0, "ask": best_ask}

        elif event_type == "price_change":
            token_id = data.get("asset_id")
            if token_id:
                await self._check_simulated_opportunity(token_id)

    async def _check_simulated_opportunity(self, updated_token_id: str):
        market = self.discovery.get_market_by_token(updated_token_id)
        if not market:
            return

        yes_token = market["yes_token"]
        no_token = market["no_token"]
        yes_data = self.book.get(yes_token)
        no_data = self.book.get(no_token)
        if not yes_data or not no_data:
            return
        yes_ask = yes_data.get("ask")
        no_ask = no_data.get("ask")
        if not yes_ask or not no_ask:
            return

        trade = self.sim.evaluate_tick(
            pair_id=market["pair_id"],
            slug=market.get("slug", ""),
            yes_ask=yes_ask, no_ask=no_ask,
            expiry=market["expiry"],
        )
        if trade:
            print(f"[SIM TRADE] {trade.pair_id} | "
                  f"combined={trade.combined_cost:.4f} | "
                  f"shares={trade.shares:.2f} | "
                  f"net_profit=${trade.net_profit:.4f} | "
                  f"balance=${self.sim.capital.balance:.2f}")

    async def subscribe_tokens(self, token_ids: list):
        if self.ws and self.ws_connected:
            await self._send_subscription(token_ids)
        else:
            self.pending_subs.extend(token_ids)

    async def unsubscribe_tokens(self, token_ids: list):
        if self.ws and self.ws_connected:
            try:
                await self.ws.send(json.dumps({
                    "assets_ids": token_ids, "operation": "unsubscribe",
                }))
            except Exception:
                pass
        for tid in token_ids:
            self.book.pop(tid, None)

    async def _send_subscription(self, token_ids: list):
        try:
            await self.ws.send(json.dumps({
                "assets_ids": token_ids, "type": "market",
                "custom_feature_enabled": True,
            }))
        except Exception as e:
            print(f"[SIM-WS] Subscription error: {e}")

    async def reconnect(self):
        if self.ws:
            await self.ws.close()
        self.ws_connected = False

    async def finalize_market(self, pair_id: str, slug: str):
        """No-op for simulation — SimulationEngine settles trades
        immediately at trade time rather than waiting for market
        resolution (see simulation_engine.py's evaluate_tick
        docstring for why that's a reasonable simplification for
        the two-sided guaranteed-hedge pattern specifically)."""
        pass
