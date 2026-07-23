import asyncio
import logging
import time
from .config import get_settings
from .db import session, Signal, Trade, BotEvent
from .deriv import DerivClient
from .strategy import Candle, R25Strategy
from sqlalchemy import select

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
        self._settlement_tasks: set[asyncio.Task] = set()

    async def log_event(self, level: str, event_type: str, message: str):
        async with session() as db:
            db.add(BotEvent(level=level, event_type=event_type, message=message))
            await db.commit()

    async def start(self):
        if self.running:
            return
        self.running = True
        self.status = "STARTING"
        self.last_error = None
        self._task = asyncio.create_task(self.run(), name="deriv-bot-engine")

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
        for task in list(self._settlement_tasks):
            task.cancel()
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
                await self.tick_loop()
                if self.running:
                    raise RuntimeError("Market data loop ended unexpectedly")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)
                self.status = "RECONNECTING"
                log.exception("Engine loop failure")
                await self.client.close()
                if self.running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def load_history(self):
        raw = await self.client.get_candles(self.settings.market_symbol, 120, self.settings.timeframe_seconds)
        unique = {}
        for x in raw:
            try:
                epoch = int(x["epoch"])
                unique[epoch] = Candle(epoch, float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]))
            except (KeyError, TypeError, ValueError):
                continue
        self.candles = sorted(unique.values(), key=lambda c: c.epoch)[-120:]
        if self.candles:
            self.last_candle_epoch = self.candles[-1].epoch

    async def tick_loop(self):
        async for tick in self.client.subscribe_ticks(self.settings.market_symbol):
            if not self.running:
                return
            tick_data = tick.get("tick") if isinstance(tick, dict) else None
            if not isinstance(tick_data, dict):
                continue
            try:
                epoch = int(tick_data["epoch"])
                spot = float(tick_data["quote"])
            except (KeyError, TypeError, ValueError):
                continue
            boundary = epoch - (epoch % self.settings.timeframe_seconds)
            if self.last_candle_epoch is None:
                self.last_candle_epoch = boundary
                continue
            if boundary > self.last_candle_epoch:
                completed_epoch = self.last_candle_epoch
                self.last_candle_epoch = boundary
                await self.on_exact_candle_open(boundary, spot, completed_epoch)

    async def on_exact_candle_open(self, candle_epoch: int, entry_spot: float, completed_epoch: int):
        async with session() as db:
            existing = (await db.execute(select(Signal).where(Signal.candle_epoch == candle_epoch))).scalar_one_or_none()
        if existing is not None:
            # candle_epoch is unique in the DB. Without this check, a
            # reconnect/restart that lands back on the same in-progress
            # candle would try to insert a duplicate Signal, raise an
            # unhandled IntegrityError, and crash the whole engine loop
            # (which would then just retry the same failure every backoff
            # cycle until the candle finally closed).
            self.current_signal = {
                "id": existing.id, "status": existing.status, "direction": existing.direction,
                "contract_type": existing.contract_type, "score": existing.score,
                "reason": existing.reason, "candle_epoch": candle_epoch,
                "entry_spot": entry_spot,
            }
            return
        raw = await self.client.get_candles(self.settings.market_symbol, 2, self.settings.timeframe_seconds)
        completed = next((x for x in raw if int(x.get("epoch", -1)) == completed_epoch), None)
        if completed is None and len(raw) >= 2:
            completed = raw[-2]
        if completed is None:
            return
        try:
            candle = Candle(int(completed["epoch"]), float(completed["open"]), float(completed["high"]), float(completed["low"]), float(completed["close"]))
        except (KeyError, TypeError, ValueError):
            return
        if not any(c.epoch == candle.epoch for c in self.candles):
            self.candles.append(candle)
            self.candles = sorted(self.candles, key=lambda c: c.epoch)[-120:]
        decision = self.strategy.evaluate(self.candles)
        if not decision:
            self.current_signal = {"status": "NO_SIGNAL", "candle_epoch": candle_epoch}
            return

        direction = decision.direction
        contract_type = "HIGHER" if direction == "UP" else "LOWER"
        async with session() as db:
            signal = Signal(
                candle_epoch=candle_epoch,
                symbol=self.settings.market_symbol,
                direction=direction,
                contract_type=contract_type,
                score=decision.score,
                status="QUALIFIED",
                reason=decision.reason,
            )
            db.add(signal)
            await db.commit()
            await db.refresh(signal)
            self.current_signal = {
                "id": signal.id, "status": "QUALIFIED", "direction": direction,
                "contract_type": contract_type, "score": decision.score,
                "reason": decision.reason, "candle_epoch": candle_epoch,
                "entry_spot": entry_spot,
            }
            if self.settings.auto_trade:
                await self.execute(signal.id, entry_spot)

    async def execute(self, signal_id: int, spot: float):
        async with session() as db:
            signal = await db.get(Signal, signal_id)
            if not signal:
                return
            try:
                proposal = await self.client.proposal(
                    self.settings.market_symbol, signal.direction, self.settings.stake,
                    self.settings.currency, self.settings.timeframe_seconds,
                )
                prop = proposal.get("proposal", {})
                proposal_id = prop.get("id")
                ask_price = float(prop.get("ask_price", self.settings.stake))
                if not proposal_id:
                    raise RuntimeError(f"Proposal did not contain an id: {proposal}")
                buy_response = await self.client.buy(str(proposal_id), ask_price)
                buy = buy_response.get("buy", {})
                contract_id = str(buy.get("contract_id", ""))
                if not contract_id:
                    raise RuntimeError(f"Buy response did not contain contract_id: {buy_response}")
                db.add(Trade(
                    signal_id=signal.id, contract_id=contract_id, symbol=signal.symbol,
                    mode=self.settings.bot_mode, direction=signal.direction,
                    stake=self.settings.stake, payout=float(prop.get("payout", 0) or 0),
                    status="OPEN", entry_spot=spot,
                ))
                signal.status = "EXECUTED"
                await db.commit()
                task = asyncio.create_task(self.settle(contract_id), name=f"settle-{contract_id}")
                self._settlement_tasks.add(task)
                task.add_done_callback(self._settlement_tasks.discard)
            except Exception as exc:
                signal.status = "EXECUTION_ERROR"
                await db.commit()
                self.last_error = str(exc)
                log.exception("Execution failed")

    async def settle(self, contract_id: str):
        deadline = time.monotonic() + self.settings.timeframe_seconds + 120  # + grace period for settlement lag
        while time.monotonic() < deadline and self.running:
            try:
                result = await self.client.proposal_open_contract(contract_id)
                poc = result.get("proposal_open_contract", {})
                if poc.get("is_sold") or poc.get("status") in ("won", "lost"):
                    profit = float(poc.get("profit", 0) or 0)
                    status = "WON" if profit > 0 else "LOST"
                    async with session() as db:
                        row = (await db.execute(select(Trade).where(Trade.contract_id == contract_id))).scalar_one_or_none()
                        if row:
                            row.profit = profit
                            row.status = status
                            await db.commit()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Settlement polling failed for %s: %s", contract_id, exc)
            await asyncio.sleep(3)
