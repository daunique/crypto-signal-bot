import asyncio
import logging
import time
from .config import get_settings
from .db import session, Signal, Trade, BotEvent
from .deriv import DerivClient, DerivAPIError
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
        self.run_task = None
        self.settlement_tasks: set[asyncio.Task] = set()
        self._restart_delay = 5

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
        self.run_task = asyncio.create_task(self.run(), name="deriv-bot-engine")
        log.info("Bot engine started: version=%s mode=%s symbol=%s", self.settings.app_version, self.settings.bot_mode, self.settings.market_symbol)

    async def stop(self):
        self.running = False
        self.status = "STOPPING"
        await self.client.close()
        if self.run_task and self.run_task is not asyncio.current_task():
            self.run_task.cancel()
        for task in list(self.settlement_tasks):
            task.cancel()
        self.settlement_tasks.clear()
        self.status = "STOPPED"

    async def run(self):
        while self.running:
            try:
                self.last_error = None
                await self.client.connect()
                await self.load_history()
                self.status = "RUNNING"
                self._restart_delay = 5
                await self.tick_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)
                self.status = "ERROR"
                log.exception("Engine error")
                await self.client.close()

                # Authentication/configuration failures should not hammer the API.
                delay = min(self._restart_delay, 60)
                if isinstance(exc, DerivAPIError) and exc.status_code in (401, 403):
                    log.error("Deriv authentication/permission failure. Fix PAT/App ID/scopes before restarting.")
                    delay = 60
                if self.running:
                    await asyncio.sleep(delay)
                    self._restart_delay = min(delay * 2, 60)

    async def load_history(self):
        raw = await self.client.get_candles(
            self.settings.market_symbol, self.settings.history_count, self.settings.timeframe_seconds
        )
        self.candles = [
            Candle(
                epoch=int(x["epoch"]), open=float(x["open"]), high=float(x["high"]),
                low=float(x["low"]), close=float(x["close"])
            )
            for x in raw
            if all(k in x for k in ("epoch", "open", "high", "low", "close"))
        ]
        if self.candles:
            self.last_candle_epoch = self.candles[-1].epoch

    async def tick_loop(self):
        async for tick in self.client.subscribe_ticks(self.settings.market_symbol):
            if not self.running:
                break
            tick_data = tick.get("tick") if isinstance(tick, dict) else None
            if not isinstance(tick_data, dict):
                continue
            try:
                epoch = int(tick_data["epoch"])
            except (KeyError, TypeError, ValueError):
                continue

            boundary = epoch - (epoch % self.settings.timeframe_seconds)
            if self.last_candle_epoch is None:
                self.last_candle_epoch = boundary
                continue

            if boundary > self.last_candle_epoch:
                # The first tick in the new interval is the boundary trigger.
                await self.on_exact_candle_open(boundary)
                self.last_candle_epoch = boundary

    async def on_exact_candle_open(self, candle_epoch: int):
        raw = await self.client.get_candles(
            self.settings.market_symbol, 2, self.settings.timeframe_seconds
        )
        if not raw:
            return
        completed = raw[-2] if len(raw) >= 2 else raw[-1]
        candle = Candle(
            epoch=int(completed["epoch"]), open=float(completed["open"]),
            high=float(completed["high"]), low=float(completed["low"]), close=float(completed["close"])
        )
        self.candles = [c for c in self.candles if c.epoch != candle.epoch]
        self.candles.append(candle)
        self.candles.sort(key=lambda c: c.epoch)
        self.candles = self.candles[-self.settings.history_count:]

        decision = self.strategy.evaluate(self.candles)
        if not decision:
            self.current_signal = {"status": "NO_SIGNAL", "candle_epoch": candle_epoch}
            return

        barrier = self.settings.barrier_for_symbol(self.settings.market_symbol)
        contract_type = "HIGHER" if decision.direction == "UP" else "LOWER"

        async with SessionContext() as db:
            signal = Signal(
                candle_epoch=candle_epoch,
                symbol=self.settings.market_symbol,
                direction=decision.direction,
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
                "id": signal.id, "status": "QUALIFIED", "direction": decision.direction,
                "contract_type": contract_type, "barrier": barrier, "score": decision.score,
                "reason": decision.reason, "candle_epoch": candle_epoch,
            }

            if self.settings.auto_trade:
                await self.execute(signal)

    async def execute(self, signal: Signal):
        try:
            proposal = await self.client.proposal(
                self.settings.market_symbol, signal.direction, self.settings.stake,
                self.settings.currency, self.settings.timeframe_seconds, signal.barrier
            )
            prop = proposal.get("proposal") or {}
            proposal_id = prop.get("id")
            if not proposal_id:
                raise RuntimeError(f"Proposal did not contain id: {proposal}")
            ask_price = float(prop.get("ask_price") or self.settings.stake)

            buy_response = await self.client.buy(proposal_id, ask_price)
            buy = buy_response.get("buy") or {}
            contract_id = str(buy.get("contract_id", ""))
            if not contract_id:
                raise RuntimeError(f"Buy response did not contain contract_id: {buy_response}")

            async with SessionContext() as db:
                db.add(Trade(
                    signal_id=signal.id, contract_id=contract_id, symbol=signal.symbol,
                    mode=self.settings.bot_mode, direction=signal.direction,
                    stake=self.settings.stake, payout=float(prop.get("payout") or 0),
                    status="OPEN", entry_spot=prop.get("spot"), barrier=signal.barrier,
                ))
                signal.status = "EXECUTED"
                await db.commit()

            task = asyncio.create_task(self.settle(contract_id), name=f"settle-{contract_id}")
            self.settlement_tasks.add(task)
            task.add_done_callback(self.settlement_tasks.discard)
        except Exception:
            log.exception("Execution failed")
            async with SessionContext() as db:
                row = (await db.execute(select(Signal).where(Signal.id == signal.id))).scalar_one_or_none()
                if row:
                    row.status = "EXECUTION_ERROR"
                    await db.commit()

    async def settle(self, contract_id: str):
        deadline = time.time() + self.settings.timeframe_seconds + 60
        while time.time() < deadline:
            try:
                result = await self.client.proposal_open_contract(contract_id)
                poc = result.get("proposal_open_contract") or {}
                if poc.get("is_sold") or poc.get("status") in ("won", "lost"):
                    profit = float(poc.get("profit") or 0)
                    status = "WON" if profit > 0 else "LOST"
                    async with SessionContext() as db:
                        row = (await db.execute(select(Trade).where(Trade.contract_id == contract_id))).scalar_one_or_none()
                        if row:
                            row.profit = profit
                            row.status = status
                            await db.commit()
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
