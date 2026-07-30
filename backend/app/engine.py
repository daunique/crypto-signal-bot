import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from .config import get_settings
from .db import (
    session, Signal, Trade, BotEvent, VolSample, get_effective_bot_mode, prune_old_vol_samples,
    load_pnl_track_state, save_pnl_track_state, set_bot_mode_override,
)
from .deriv import DerivClient
from .strategy import RollingVolatility, VolatilityTimingStrategy
from .pnl_tracker import apply_trade_outcome
from sqlalchemy import select

log = logging.getLogger(__name__)


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (matches numpy's default `percentile`
    method), implemented by hand rather than pulling in numpy for one
    function in an otherwise numpy-free dependency list."""
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


class VolatilityTracker:
    """Owns the rolling realized-volatility estimate and the trailing-window
    percentile threshold that VolatilityTimingStrategy fires against. This is
    the stateful counterpart to strategy.py's stateless decision function --
    matching this project's existing split (see strategy.py's module
    docstring / the README's original "pure, stateless" note about
    strategy.py) between "what does the data say right now" (owned here) and
    "given that, what do we do" (owned by VolatilityTimingStrategy).

    Threshold is recomputed continuously (every RECOMPUTE_EVERY_N_SAMPLES new
    samples, roughly every 5 minutes) from `vol_samples` rows in the trailing
    `vol_trailing_days`, using an absolute time cutoff rather than calendar-day
    boundaries. This is still fully causal -- every sample used was recorded
    from a tick strictly before the one currently being evaluated, so it's the
    same non-lookahead property the backtest validated (Section 11), just
    updated continuously instead of once a day.

    (An earlier version of this recomputed once per UTC day and explicitly
    excluded "today"'s own samples, to mirror the backtest's once-daily
    threshold exactly. That turned out to have a real practical cost: on a
    brand-new deployment there is no prior day at all, so the threshold
    stayed None -- and the bot fired zero signals -- for the entire first
    calendar day. Excluding "today" was never necessary for correctness
    (a continuously-updated threshold that only ever looks at
    already-elapsed ticks is exactly as non-lookahead as a once-daily one --
    "today" isn't special, only "not yet happened" is), so this switches to
    the continuous version, which reaches its first threshold within about
    RECOMPUTE_EVERY_N_SAMPLES x SAMPLE_EVERY_N_TICKS ticks (roughly 3-4 hours
    from a cold start) instead of a full day.)
    """

    SAMPLE_EVERY_N_TICKS = 30
    MIN_SAMPLES_FOR_THRESHOLD = 200  # below this, treat the threshold as unknown rather than noisy
    RECOMPUTE_EVERY_N_SAMPLES = 10   # ~5 minutes at the default 30-tick/~2s-tick sampling rate

    def __init__(self, client: DerivClient, settings):
        self.client = client
        self.settings = settings
        self.rolling = RollingVolatility(settings.vol_window_ticks)
        self._tick_count = 0
        self._samples_since_recompute = 0
        self._today: date | None = None
        self._threshold: float | None = None

    async def bootstrap(self):
        """Best-effort seed of the rolling window from recent tick history,
        so the strategy doesn't need to wait `vol_window_ticks` live ticks
        after every restart before it can even compute a volatility reading.
        Any failure is logged and swallowed: the bot falls back to building
        its rolling window from live ticks from scratch, which only delays
        when it starts evaluating signals, not whether it starts at all.

        This does NOT backfill `vol_samples` (the trailing-window history
        used for the percentile threshold) -- doing that properly needs
        paginating Deriv's `ticks_history` back `vol_trailing_days` days,
        which depends on this account's actual per-request count cap and
        rate limits that this environment has no way to verify against the
        live API. Until that backfill is added, a brand-new deployment
        accumulates its own trailing history from live ticks (see
        `current_threshold()`, which returns None -- and so fires no
        signals -- until MIN_SAMPLES_FOR_THRESHOLD samples exist, roughly
        3-4 hours from a cold start with the defaults). That is a deliberate
        fail-safe, not an oversight: trading on a fabricated or too-thin
        threshold would silently not be the strategy that was backtested.
        """
        try:
            history = await self.client.get_tick_history(
                self.settings.market_symbol, count=self.settings.vol_window_ticks + 5,
            )
        except Exception as exc:
            log.warning("Volatility bootstrap tick fetch failed; will accumulate live instead: %s", exc)
            return
        for row in history:
            try:
                self.rolling.update(float(row["quote"]))
            except (KeyError, TypeError, ValueError):
                continue
        log.info("Volatility bootstrap seeded rolling window with %d historical ticks", len(history))

    async def on_tick(self, epoch: int, price: float) -> float | None:
        """Feed one tick in. Returns the current rolling volatility reading
        (None until the rolling window has filled)."""
        current_vol = self.rolling.update(price)
        today = datetime.fromtimestamp(epoch, tz=timezone.utc).date()
        if self._today is None:
            self._today = today
        elif today != self._today:
            self._today = today
            await prune_old_vol_samples(self.settings.market_symbol, self.settings.vol_trailing_days)
        if current_vol is None:
            return None
        self._tick_count += 1
        if self._tick_count % self.SAMPLE_EVERY_N_TICKS == 0:
            async with session() as db:
                db.add(VolSample(symbol=self.settings.market_symbol, day=today, epoch=epoch, vol=current_vol))
                await db.commit()
            self._samples_since_recompute += 1
        if self._threshold is None or self._samples_since_recompute >= self.RECOMPUTE_EVERY_N_SAMPLES:
            await self._recompute_threshold(epoch)
            self._samples_since_recompute = 0
        return current_vol

    async def _recompute_threshold(self, current_epoch: int):
        cutoff_epoch = current_epoch - self.settings.vol_trailing_days * 86400
        async with session() as db:
            rows = (await db.execute(
                select(VolSample.vol).where(
                    VolSample.symbol == self.settings.market_symbol,
                    VolSample.epoch >= cutoff_epoch,
                    VolSample.epoch <= current_epoch,
                )
            )).scalars().all()
        if len(rows) < self.MIN_SAMPLES_FOR_THRESHOLD:
            self._threshold = None
            return
        self._threshold = _percentile(list(rows), self.settings.vol_target_percentile)

    def current_threshold(self) -> float | None:
        return self._threshold


class BotEngine:
    def __init__(self):
        self.settings = get_settings()
        self.client = DerivClient()
        self.vol_tracker = VolatilityTracker(self.client, self.settings)
        self.strategy = VolatilityTimingStrategy(self.settings.bet_direction, self.settings.vol_target_percentile)
        self.running = False
        self.status = "STOPPED"
        self.current_signal = None
        self.last_error = None
        self.trade_in_flight = False
        self._task: asyncio.Task | None = None
        self._settlement_tasks: set[asyncio.Task] = set()
        self._settling_contract_ids: set[str] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_reconnect = False

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
        for task in list(self._background_tasks):
            task.cancel()
        await self.client.close()

    async def run(self):
        backoff = 2
        while self.running:
            try:
                mode = await get_effective_bot_mode(self.settings)
                await self.client.connect(mode)
                await self.vol_tracker.bootstrap()
                await self._resume_open_trades()
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

    async def _resume_open_trades(self):
        """On (re)connect, pick back up any trade this process lost track of
        in-memory across a restart (self.trade_in_flight is only ever an
        in-process flag). Without this, a restart mid-trade would forget the
        open position entirely: the non-overlap guarantee in tick_loop()
        would be silently violated (a new signal could fire while the old
        contract is technically still live), and that trade's outcome would
        never be recorded.

        run() calls this on every reconnect, not just the first startup --
        guarded by `_settling_contract_ids` so a trade whose settlement task
        is already running (spawned either by _execute_leg or by an earlier
        call to this same method) never gets a second, duplicate poller.
        """
        async with session() as db:
            open_trades = (await db.execute(select(Trade).where(Trade.status == "OPEN"))).scalars().all()
        for trade in open_trades:
            if not trade.contract_id or trade.contract_id in self._settling_contract_ids:
                continue
            self.trade_in_flight = True
            self._start_settlement_task(trade.contract_id, name_prefix="settle-resume")
            log.info("Resumed settlement polling for open contract %s after restart", trade.contract_id)

    def _start_settlement_task(self, contract_id: str, name_prefix: str = "settle") -> asyncio.Task:
        self._settling_contract_ids.add(contract_id)
        task = asyncio.create_task(self.settle(contract_id), name=f"{name_prefix}-{contract_id}")
        self._settlement_tasks.add(task)
        task.add_done_callback(self._settlement_tasks.discard)
        return task

    async def tick_loop(self):
        tick_stream = self.client.subscribe_ticks(self.settings.market_symbol)
        while self.running:
            if self._pending_reconnect:
                self._pending_reconnect = False
                raise RuntimeError("Reconnect requested (PnL-track mode switch)")
            try:
                tick = await asyncio.wait_for(tick_stream.__anext__(), timeout=5.0)
            except asyncio.TimeoutError:
                # Nothing arrived in the last 5s -- normal when a mode
                # switch's client.close() just tore down the connection (see
                # _apply_mode_switch): the loop above re-checks
                # _pending_reconnect on the very next iteration instead of
                # hanging forever on a queue nothing feeds anymore.
                continue
            except StopAsyncIteration:
                return
            # See the equivalent comment in the previous candle-based version
            # of this loop (still applies verbatim): the public tick stream
            # can keep flowing after the separate trade connection has
            # silently died, and without this check that would go undetected
            # until a trade attempt's error got swallowed downstream.
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
            await self.on_tick(epoch, spot)

    async def on_tick(self, epoch: int, spot: float):
        current_vol = await self.vol_tracker.on_tick(epoch, spot)
        if self.trade_in_flight:
            # Enforces the same non-overlapping-trades assumption the
            # backtest used throughout (see report Section 3 onward): never
            # more than one contract open at a time. Volatility tracking
            # above still runs every tick regardless, so the rolling window
            # and trailing samples stay current while a trade is open.
            return
        threshold = self.vol_tracker.current_threshold()
        decision = self.strategy.evaluate(current_vol, threshold)
        if not decision:
            self.current_signal = {"status": "NO_SIGNAL", "candle_epoch": epoch}
            return

        try:
            await self._handle_qualified_signal(epoch, spot, decision)
        except Exception as exc:
            # A qualifying signal is rare and valuable (that's the whole
            # point of the volatility filter) -- losing this one tick's
            # opportunity to an unexpected DB/execution error is much
            # better than what an uncaught exception here does: propagate
            # out of on_tick() -> tick_loop() -> run(), which reads as a
            # connection failure and drops the engine into a reconnect
            # loop that re-hits the same error on the next qualifying
            # signal. (This is exactly what a stale NOT NULL column with
            # no default did on 2026-07-30 -- see db.py's
            # _drop_unknown_columns -- before it was caught here too.)
            self.last_error = str(exc)
            log.exception("Failed to handle qualified signal at epoch %s", epoch)
            await self.log_event("error", "signal_handling_error", str(exc))

    async def _handle_qualified_signal(self, epoch: int, spot: float, decision):
        async with session() as db:
            # Only checked once a signal actually qualifies (not on every
            # tick) -- candle_epoch is unique in the DB, and this only
            # matters at a reconnect boundary where the same tick could
            # otherwise be re-evaluated and re-inserted, raising an
            # unhandled IntegrityError and crashing the engine loop.
            existing = (await db.execute(select(Signal).where(Signal.candle_epoch == epoch))).scalar_one_or_none()
            if existing is not None:
                return
        contract_type = decision.direction  # "HIGHER" or "LOWER" -- see strategy.py; direction is fixed, not computed
        wire_direction = "UP" if decision.direction == "HIGHER" else "DOWN"
        async with session() as db:
            signal = Signal(
                candle_epoch=epoch,
                symbol=self.settings.market_symbol,
                direction=wire_direction,
                contract_type=contract_type,
                status="QUALIFIED",
                reason=decision.reason,
                barrier_offset=self.settings.barrier,
                current_vol=decision.current_vol,
                vol_threshold=decision.threshold,
            )
            db.add(signal)
            await db.commit()
            await db.refresh(signal)
            self.current_signal = {
                "id": signal.id, "status": "QUALIFIED", "direction": wire_direction,
                "contract_type": contract_type, "reason": decision.reason,
                "candle_epoch": epoch, "entry_spot": spot,
                "barrier_offset": self.settings.barrier,
                "current_vol": decision.current_vol, "vol_threshold": decision.threshold,
            }
            if self.settings.auto_trade:
                self.trade_in_flight = True
                await self.execute(signal.id, spot, self.settings.barrier, wire_direction)

    async def execute(self, signal_id: int, spot: float, barrier_offset: float, direction: str):
        """Places a single Higher/Lower trade in the configured direction."""
        ok = await self._execute_leg(signal_id, spot, barrier_offset, direction)
        async with session() as db:
            signal = await db.get(Signal, signal_id)
            if signal:
                signal.status = "EXECUTED" if ok else "EXECUTION_ERROR"
                await db.commit()
        if not ok:
            self.trade_in_flight = False  # nothing was actually opened; don't block future signals on it

    async def _execute_leg(self, signal_id: int, spot: float, barrier_offset: float, direction: str) -> bool:
        """Builds, proposes, and buys one Higher/Lower contract. Returns
        True on success; on failure, logs it (both to the app logger and
        the persisted BotEvent feed) and returns False rather than raising."""
        try:
            duration = self.settings.contract_duration_ticks
            barrier_str = self.client.build_proposal_payload(
                self.settings.market_symbol, direction, self.settings.stake,
                self.settings.currency, duration, barrier_offset, duration_unit="t",
            )["barrier"]
            proposal = await self.client.proposal(
                self.settings.market_symbol, direction, self.settings.stake,
                self.settings.currency, duration, barrier_offset, duration_unit="t",
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
            # Read once per trade (not cached) so a track switch that just
            # happened in settle() for the previous trade is picked up
            # immediately, matching "the next trade should switch" wording.
            pnl_state = await load_pnl_track_state()
            async with session() as db:
                db.add(Trade(
                    signal_id=signal_id, contract_id=contract_id, symbol=self.settings.market_symbol,
                    mode=self.client.current_mode or self.settings.bot_mode, direction=direction,
                    stake=self.settings.stake, payout=float(prop.get("payout", 0) or 0),
                    status="OPEN", entry_spot=spot, barrier=barrier_str,
                    pnl_track=pnl_state.track,
                ))
                await db.commit()
            self._start_settlement_task(contract_id)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            log.exception("Execution failed")
            await self.log_event("error", "execution_error", str(exc))
            return False

    async def settle(self, contract_id: str):
        # 10 ticks at R_25's ~2s tick rate is roughly 20s; this deadline is
        # generously long relative to that (not derived from it) precisely
        # because tick spacing isn't perfectly guaranteed in real time --
        # see README/report for the tick-rate figures this assumes.
        deadline = time.monotonic() + 180
        try:
            while time.monotonic() < deadline and self.running:
                try:
                    result = await self.client.proposal_open_contract(contract_id)
                    poc = result.get("proposal_open_contract", {})
                    if poc.get("is_sold") or poc.get("status") in ("won", "lost"):
                        profit = float(poc.get("profit", 0) or 0)
                        status = "WON" if profit > 0 else "LOST"
                        was_live_mode = False
                        async with session() as db:
                            row = (await db.execute(select(Trade).where(Trade.contract_id == contract_id))).scalar_one_or_none()
                            if row:
                                row.profit = profit
                                row.status = status
                                was_live_mode = (row.mode == "live")
                                await db.commit()
                        await self._advance_pnl_track(profit, was_live_mode)
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Settlement polling failed for %s: %s", contract_id, exc)
                await asyncio.sleep(3)
            if self.running:
                msg = f"Gave up polling settlement for contract {contract_id} after the deadline; outcome unknown"
                log.warning(msg)
                await self.log_event("warning", "settlement_timeout", msg)
        finally:
            # Always clear both the in-flight flag and the settling-tracker
            # entry once this contract is no longer being tracked (won,
            # lost, or gave up waiting) -- a settlement timeout should not
            # permanently wedge the bot into never trading again, nor leave
            # a stale entry that would block a legitimate future resume.
            self.trade_in_flight = False
            self._settling_contract_ids.discard(contract_id)

    async def _advance_pnl_track(self, profit: float, was_live_mode: bool):
        """Runs the main/sub PnL-track state machine (pnl_tracker.py) against
        one just-settled trade's outcome, persists the result, and -- if it
        calls for a mode switch -- applies it. See pnl_tracker.py's module
        docstring for the exact rule."""
        state = await load_pnl_track_state()
        new_state, mode_switch = apply_trade_outcome(
            state, profit, was_live_mode,
            profit_target=self.settings.pnl_track_profit_target,
            loss_streak_limit=self.settings.pnl_track_loss_streak_limit,
        )
        await save_pnl_track_state(new_state)
        if new_state.track != state.track:
            await self.log_event(
                "info", "pnl_track_switch",
                f"PnL track switched {state.track} -> {new_state.track}"
                + (f" (mode -> {mode_switch})" if mode_switch else ""),
            )
        if mode_switch:
            # Spawned as an independent task (not awaited here) so this
            # method -- called from inside settle(), itself one of
            # self._settlement_tasks -- never calls anything that could
            # cancel its own task from within itself. See
            # _apply_mode_switch's docstring for why it forces a reconnect
            # via client.close() rather than self.stop()/self.start() for
            # the same reason.
            task = asyncio.create_task(self._apply_mode_switch(mode_switch), name=f"pnl-track-mode-switch-{mode_switch}")
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _apply_mode_switch(self, mode: str):
        """Persists the new BOT_MODE override, then forces a reconnect --
        setting _pending_reconnect (picked up within 5s by tick_loop's
        timeout-based poll, see there) and closing the client so the stale
        connection isn't left dangling. Deliberately does NOT call
        self.stop()/self.start(): this runs from settle() -> 
        _advance_pnl_track() -> here, i.e. from inside one of
        self._settlement_tasks, and stop() cancels every task in that set --
        including, in that call chain, the very settlement task this call
        originated from. Signaling run()'s existing reconnect loop instead
        (same path a dropped connection already takes) sidesteps that
        entirely.
        """
        try:
            await set_bot_mode_override(mode)
            await self.log_event("info", "pnl_track_mode_switch", f"Auto-switching bot mode to {mode}")
            self._pending_reconnect = True
            await self.client.close()
        except Exception as exc:
            log.exception("Failed to apply PnL-track mode switch to %s", mode)
            await self.log_event("error", "pnl_track_mode_switch_failed", str(exc))
