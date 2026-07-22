import asyncio
import logging
import time
from datetime import datetime, timezone
from sqlalchemy import select, func
from .config import get_settings
from .db import session, Signal, Trade, BotEvent
from .deriv import DerivClient
from .strategy import Candle, R25Strategy

log = logging.getLogger(__name__)


class BotEngine:
    def __init__(self):
        self.settings = get_settings()
        self.client = DerivClient()
        self.strategy = R25Strategy(self.settings.min_confluence_score)
        self.running = False
        self.status = "STOPPED"
        self.last_candle_epoch = None
        self.candles: list[Candle] = []
        self.current_signal = None
        self.last_error = None
        self._task: asyncio.Task | None = None

    async def log_event(self, level: str, event_type: str, message: str):
        async with SessionContext() as db:
            db.add(BotEvent(level=level, event_type=event_type, message=message))
            await db.commit()

    async def start(self):
        if self.running:
            return
        self.running = True
        self.status = "STARTING"
        self.last_error = None
        self._task = asyncio.create_task(self.run(), name="deriv-bot-engine")
        log.info("Bot engine started")

    async def stop(self):
        self.running = False
        self.status = "STOPPED"
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self.client.close()

    async def run(self):
        backoff = 2
        while self.running:
            try:
                await self.client.connect()
                await self.load_history()
                self.status = "RUNNING"
                self.last_error = None
                backoff = 2
                log.info("ENGINE_READY symbol=%s timeframe=%ss mode=%s", self.settings.market_symbol, self.settings.timeframe_seconds, self.settings.bot_mode)
                await self.tick_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)
                self.status = "ERROR"
                log.exception("Engine error")
                await self.client.close()
                if self.running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)

    async def load_history(self):
        raw = await self.client.get_candles(
            self.settings.market_symbol, 120, self.settings.timeframe_seconds
        )
        candles = []
        seen = set()
        for x in raw:
            epoch = int(x["epoch"])
            if epoch in seen:
                continue
            seen.add(epoch)
            candles.append(Candle(
                epoch=epoch,
                open=float(x["open"]),
                high=float(x["high"]),
                low=float(x["low"]),
                close=float(x["close"]),
            ))
        self.candles = sorted(candles, key=lambda c: c.epoch)[-120:]
        if self.candles:
            self.last_candle_epoch = self.candles[-1].epoch
            log.info("Loaded %d completed candles; last_epoch=%s", len(self.candles), self.last_candle_epoch)

    async def tick_loop(self):
        async for tick in self.client.subscribe_ticks(self.settings.market_symbol):
            if not self.running:
                break
            tick_data = tick.get("tick") if isinstance(tick, dict) else None
            if not isinstance(tick_data, dict):
                continue
            try:
                epoch = int(tick_data["epoch"])
                spot = float(tick_data.get("quote"))
            except (KeyError, TypeError, ValueError):
                continue

            boundary = epoch - (epoch % self.settings.timeframe_seconds)
            if self.last_candle_epoch is None:
                self.last_candle_epoch = boundary
                continue

            if boundary > self.last_candle_epoch:
                # This is the first observed tick of the new candle. The signal
                # is evaluated from the candle that just closed, and execution
                # uses the current candle's opening spot, not the previous candle's open.
                previous_boundary = self.last_candle_epoch
                self.last_candle_epoch = boundary
                await self.on_exact_candle_open(boundary, spot, previous_boundary)

    async def on_exact_candle_open(self, candle_epoch: int, entry_spot: float, completed_epoch: int):
        raw = await self.client.get_candles(
            self.settings.market_symbol, 2, self.settings.timeframe_seconds
        )
        if not raw:
            return

        completed = None
        for item in raw:
            if int(item.get("epoch", -1)) == completed_epoch:
                completed = item
                break
        if completed is None:
            # Fallback only if the API response has shifted by one tick.
            completed = raw[-2] if len(raw) >= 2 else raw[-1]

        candle = Candle(
            epoch=int(completed["epoch"]),
            open=float(completed["open"]),
            high=float(completed["high"]),
            low=float(completed["low"]),
            close=float(completed["close"]),
        )
        if not any(c.epoch == candle.epoch for c in self.candles):
            self.candles.append(candle)
            self.candles = sorted(self.candles, key=lambda c: c.epoch)[-120:]

        decision = self.strategy.evaluate(self.candles)
        if not decision:
            self.current_signal = {"status": "NO_SIGNAL", "candle_epoch": candle_epoch}
            return

        direction = decision.direction
        contract_type = "HIGHER" if direction == "UP" else "LOWER"
        barrier = self.settings.barrier_for(self.settings.market_symbol)

        async with SessionContext() as db:
            signal = Signal(
                candle_epoch=candle_epoch,
                symbol=self.settings.market_symbol,
                direction=direction,
                contract_type=contract_type,
                barrier=barrier,
                score=decision.score,
                status="QUALIFIED",
                reason=decision.reason,
            )
            db.add(signal)
            await db.commit()
            await db.refresh(signal)

            self.current_signal = {
                "id": signal.id,
                "status": "QUALIFIED",
                "direction": direction,
                "contract_type": contract_type,
                "barrier": barrier,
                "score": decision.score,
                "reason": decision.reason,
                "candle_epoch": candle_epoch,
                "entry_spot": entry_spot,
            }

            if self.settings.auto_trade:
                await self.execute(signal, entry_spot)

    async def execute(self, signal: Signal, spot: float):
        try:
            proposal = await self.client.proposal(
                self.settings.market_symbol,
                signal.direction,
                self.settings.stake,
                self.settings.currency,
                self.settings.timeframe_seconds,
                signal.barrier,
            )
            prop = proposal.get("proposal", {})
            proposal_id = prop.get("id")
            ask_price = float(prop.get("ask_price", self.settings.stake))
            if not proposal_id:
                raise RuntimeError(f"Proposal did not contain an id: {proposal}")

            buy_response = await self.client.buy(proposal_id, ask_price)
            buy = buy_response.get("buy", {})
            contract_id = str(buy.get("contract_id", ""))
            if not contract_id:
                raise RuntimeError(f"Buy response did not contain contract_id: {buy_response}")

            async with SessionContext() as db:
                db.add(Trade(
                    signal_id=signal.id,
                    contract_id=contract_id,
                    symbol=signal.symbol,
                    mode=self.settings.bot_mode,
                    direction=signal.direction,
                    stake=self.settings.stake,
                    payout=float(prop.get("payout", 0) or 0),
                    status="OPEN",
                    entry_spot=spot,
                    barrier=signal.barrier,
                ))
                signal.status = "EXECUTED"
                await db.commit()

            log.info("TRADE_OPEN contract_id=%s direction=%s entry_spot=%s barrier=%s", contract_id, signal.direction, spot, signal.barrier)
            asyncio.create_task(self.settle(contract_id))
        except Exception:
            log.exception("Execution failed")
            async with SessionContext() as db:
                signal.status = "EXECUTION_ERROR"
                await db.commit()

    async def settle(self, contract_id: str):
        deadline = time.time() + self.settings.timeframe_seconds + 90
        while time.time() < deadline and self.running:
            try:
                result = await self.client.proposal_open_contract(contract_id)
                poc = result.get("proposal_open_contract", {})
                if poc.get("is_sold") or poc.get("status") in ("won", "lost"):
                    profit = float(poc.get("profit", 0) or 0)
                    status = "WON" if profit > 0 else "LOST"
                    async with SessionContext() as db:
                        row = (await db.execute(select(Trade).where(Trade.contract_id == contract_id))).scalar_one_or_none()
                        if row:
                            row.profit = profit
                            row.status = status
                            await db.commit()
                    log.info("TRADE_SETTLED contract_id=%s status=%s profit=%s", contract_id, status, profit)
                    return
            except Exception:
                log.exception("Settlement polling failed")
            await asyncio.sleep(3)


class SessionContext:
    async def __aenter__(self):
        self.db = session()
        return await self.db

    async def __aexit__(self, exc_type, exc, tb):
        await self.db.close()
