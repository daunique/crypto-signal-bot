import asyncio
import logging
import time
from .config import get_settings
from .db import session, Signal, Trade, BotEvent, get_effective_bot_mode
from .deriv import DerivClient
from .strategy import Tick, TickEMAStrategy
from sqlalchemy import select

log = logging.getLogger(__name__)


class BotEngine:
    def __init__(self):
        self.settings = get_settings()
        self.client = DerivClient()
        self.strategy = TickEMAStrategy(vol_window=self.settings.tick_vol_window)
        self.running = False
        self.status = "STOPPED"
        self.current_signal = None
        self.last_error = None
        self.ticks_since_decision = 0
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
                mode = await get_effective_bot_mode(self.settings)
                await self.client.connect(mode)
                # A fresh strategy instance on every (re)connect, rather
                # than reusing one across a gap: ticks missed during a
                # disconnect would otherwise leave a silent discontinuity
                # inside the EMA/volatility window. Re-warming from live
                # ticks (see WARMING_UP below) is simpler and safer than
                # trying to detect and patch a gap.
                self.strategy = TickEMAStrategy(vol_window=self.settings.tick_vol_window)
                self.ticks_since_decision = 0
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
                await self.log_event("error", "engine_loop_failure", str(exc))
                await self.client.close()
                if self.running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def tick_loop(self):
        async for tick in self.client.subscribe_ticks(self.settings.market_symbol):
            if not self.running:
                return
            # The public tick stream can keep flowing even after the
            # separate *trade* connection has silently died (e.g. a brief
            # network blip severs one but not the other). Previously that
            # went undetected until a trade was actually attempted, and
            # even then execute() swallowed the resulting "not connected"
            # error into EXECUTION_ERROR and returned normally -- so
            # tick_loop() never raised, run()'s reconnect/backoff never
            # triggered, and every future signal would just repeat the same
            # silent failure until the process was restarted by hand.
            if not self.client.trade_connected:
                raise RuntimeError("Trade WebSocket is down; reconnecting")
            tick_data = tick.get("tick") if isinstance(tick, dict) else None
            if not isinstance(tick_data, dict):
                continue
            try:
                epoch = int(tick_data["epoch"])
                spot = float(tick_data["quote"])
            except (KeyError, TypeError, ValueError):
                continue

            self.strategy.push_tick(Tick(epoch, spot))

            if not self.strategy.ready:
                self.current_signal = {
                    "status": "WARMING_UP",
                    "tick_count": self.strategy.tick_count,
                    "min_ticks_required": self.strategy.MIN_TICKS,
                }
                continue

            self.ticks_since_decision += 1
            if self.ticks_since_decision < self.settings.trade_duration_ticks:
                continue
            # A decision point: the previous trade (if any) has had exactly
            # trade_duration_ticks ticks to run its course, so the next one
            # is non-overlapping -- matching how the backtest behind this
            # strategy's numbers was run (see strategy.py's docstring).
            self.ticks_since_decision = 0
            await self.on_decision_tick(epoch, spot)

    async def on_decision_tick(self, decision_epoch: int, entry_spot: float):
        async with session() as db:
            existing = (await db.execute(select(Signal).where(Signal.candle_epoch == decision_epoch))).scalar_one_or_none()
        if existing is not None:
            # candle_epoch is unique in the DB (column name predates the
            # tick strategy -- see db.py -- it now holds the decision
            # tick's epoch rather than a candle-boundary epoch). Without
            # this check, a reconnect/restart landing back on the exact
            # same tick epoch would try to insert a duplicate Signal, raise
            # an unhandled IntegrityError, and crash the whole engine loop
            # (which would then just retry the same failure every backoff
            # cycle).
            self.current_signal = {
                "id": existing.id, "status": existing.status, "direction": existing.direction,
                "contract_type": existing.contract_type, "score": existing.score,
                "reason": existing.reason, "candle_epoch": decision_epoch,
                "entry_spot": entry_spot,
            }
            return

        decision = self.strategy.evaluate()
        if not decision:
            self.current_signal = {"status": "NO_SIGNAL", "candle_epoch": decision_epoch}
            return

        # Traded direction matches the strategy's raw reading directly (no
        # inversion). An earlier revision of this bot, for the old candle
        # strategy, deliberately traded the *opposite* of the detected
        # direction -- that was a decision specific to the old strategy and
        # was never validated for this one. This tick strategy's backtested
        # win rates (see strategy.py's docstring) were measured trading the
        # raw direction; inverting here would silently make the live bot
        # trade something different from what was actually backtested.
        direction = decision.direction
        reason = decision.reason
        contract_type = "HIGHER" if direction == "UP" else "LOWER"
        # Deriv's Higher/Lower barrier is sized as a fraction of the
        # rolling tick volatility, so it scales with actual current
        # volatility instead of a fixed point value going stale. See
        # config.py's barrier_vol_fraction and README.
        barrier_offset = decision.vol * self.settings.barrier_vol_fraction
        if barrier_offset <= 0:
            self.current_signal = {"status": "NO_SIGNAL", "candle_epoch": decision_epoch}
            return
        async with session() as db:
            signal = Signal(
                candle_epoch=decision_epoch,
                symbol=self.settings.market_symbol,
                direction=direction,
                contract_type=contract_type,
                score=decision.score,
                status="QUALIFIED",
                reason=reason,
                barrier_offset=barrier_offset,
            )
            db.add(signal)
            await db.commit()
            await db.refresh(signal)
            self.current_signal = {
                "id": signal.id, "status": "QUALIFIED", "direction": direction,
                "contract_type": contract_type, "score": decision.score,
                "reason": reason, "candle_epoch": decision_epoch,
                "entry_spot": entry_spot, "barrier_offset": barrier_offset,
            }
            if self.settings.auto_trade:
                await self.execute(signal.id, entry_spot, barrier_offset)

    async def execute(self, signal_id: int, spot: float, barrier_offset: float):
        async with session() as db:
            signal = await db.get(Signal, signal_id)
            if not signal:
                return
            try:
                # Always the exact barrier computed at signal opening (from
                # the tick volatility at that moment) -- no substitution.
                # If Deriv rejects it, that's reported honestly
                # (EXECUTION_ERROR, visible in diagnostics) instead of
                # silently trying a different value than what the strategy
                # actually computed.
                barrier_str = self.client.build_proposal_payload(
                    self.settings.market_symbol, signal.direction, self.settings.stake,
                    self.settings.currency, self.settings.trade_duration_ticks, barrier_offset,
                )["barrier"]
                proposal = await self.client.proposal(
                    self.settings.market_symbol, signal.direction, self.settings.stake,
                    self.settings.currency, self.settings.trade_duration_ticks, barrier_offset,
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
                    mode=self.client.current_mode or self.settings.bot_mode, direction=signal.direction,
                    stake=self.settings.stake, payout=float(prop.get("payout", 0) or 0),
                    status="OPEN", entry_spot=spot, barrier=barrier_str,
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
                await self.log_event("error", "execution_error", str(exc))

    async def settle(self, contract_id: str):
        # Tick contracts settle fast (trade_duration_ticks ticks, typically
        # well under a minute at R_25's observed ~2s/tick rate) but the
        # deadline stays generous: ticks aren't guaranteed to arrive at any
        # particular real-time rate, and settlement itself can lag the
        # underlying contract expiry. 120s alone comfortably covers a slow
        # feed for a 10-tick contract; the floor is kept in case
        # trade_duration_ticks is configured lower and ticks are unusually
        # slow.
        deadline = time.monotonic() + max(120, self.settings.trade_duration_ticks * 10 + 60)
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
            await asyncio.sleep(1)
        if self.running:
            msg = f"Gave up polling settlement for contract {contract_id} after the deadline; outcome unknown"
            log.warning(msg)
            await self.log_event("warning", "settlement_timeout", msg)
