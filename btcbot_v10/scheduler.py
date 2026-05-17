"""
Scheduler — Signal timing
──────────────────────────────────────────────────────────────────────────
CORRECT 15-minute candle lifecycle:

  :00/:15/:30/:45  → candle OPENS  → generate signal, place order
  :15/:30/:45/:00  → candle CLOSES → resolve fires at SAME tick

  Both jobs fire at the same boundary. The RESOLVE job runs first:
    1. Resolve: look for PENDING signals whose candle_close_time <= now
       (within 5-second tolerance). Retry up to 3x with 1s sleep to let
       OKX publish the just-closed candle, then give up until next tick.
    2. Generate: place a new order for the candle that just opened.

  This eliminates the 2-minute delay that was causing duplicate signals
  (the old :02/:17/:32/:47 schedule was still running when the :00/:15
  generate job fired, leaving the previous signal unresolved and allowing
  the duplicate guard to fail on boundary edge cases).

  Example:
    14:00 UTC → RESOLVE: closes 13:45–14:00 candle signal (if any)
                GENERATE: opens new signal for 14:00–14:15 candle
    14:15 UTC → RESOLVE: closes 14:00–14:15 candle signal
                GENERATE: opens new signal for 14:15–14:30 candle

  Duplicate guard:
    generate: Signal.candle_open_time == candle_open must not exist.
    resolve:  Signal.outcome == "PENDING" AND
              candle_close_time <= (now + 5s tolerance).

  Prices:
    open_price  = the OKX 15-min candle OPEN price at candle_open_time
    close_price = the OKX 15-min candle CLOSE price at candle_close_time
    Both are fetched from OKX at resolve time so they are exact OHLC values.

  WIN/LOSS:
    signal UP   + close_price > open_price → WIN
    signal DOWN + close_price < open_price → WIN
"""
import logging
from datetime import datetime, timezone, timedelta, date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def _ctx():
    from app import app
    return app.app_context()


def job_generate_signal():
    """
    Fires at :00, :15, :30, :45 UTC — candle OPEN.
    Generates ONE signal, places order, saves to DB.
    Duplicate guard: skips if a signal for this candle_open_time already exists.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, Settings, ShadowBalance
            from signal_engine import pick_best_signal
            from limitless_executor import execute_order
            from telegram_bot import send_signal_alert

            settings = Settings.query.first()
            if not settings:
                settings = Settings()
                db.session.add(settings)
                db.session.commit()

            mode          = settings.mode
            position_size = settings.position_size
            max_cp        = settings.max_contract_price
            min_conf      = settings.min_confidence

            logger.info(f"[GENERATE] Starting | mode={mode} min_conf={min_conf}")

            # Get signal from engine first — it computes the correct candle boundary
            sig = pick_best_signal(min_confidence=min_conf)
            if not sig:
                logger.info("[GENERATE] No qualifying signal this candle")
                return

            candle_open  = sig['candle_open_time']
            candle_close = sig['candle_close_time']

            # ── Duplicate guard using signal_engine's candle_open_time ──────────
            # This is the authoritative boundary — derived from floor(now / 15min)
            existing = Signal.query.filter(
                Signal.candle_open_time == candle_open
            ).first()
            if existing:
                logger.info(
                    f"[GENERATE] Duplicate blocked — signal already exists for "
                    f"candle {candle_open}→{candle_close} "
                    f"(existing id={existing.id} symbol={existing.symbol})"
                )
                return

            # ── Place order — retry up to 5 attempts, 30s apart, stop on first success ──
            import time as _time
            ORDER_MAX_ATTEMPTS = 5
            ORDER_RETRY_DELAY  = 30   # seconds
            order = {"success": False, "error": "not attempted"}

            # Invert direction — signal engine picks UP/DOWN but live results
            # show the opposite side wins, so we trade the inverse.
            _inverted_direction = "DOWN" if sig['direction'] == "UP" else "UP"

            for _attempt in range(1, ORDER_MAX_ATTEMPTS + 1):
                order = execute_order(sig['symbol'], _inverted_direction, mode,
                                      position_size, max_cp)
                if order.get("success"):
                    # Order placed — do NOT retry regardless of remaining attempts
                    break
                logger.warning(
                    f"[GENERATE] Order attempt {_attempt}/{ORDER_MAX_ATTEMPTS} FAILED | "
                    f"{sig['symbol']} | error={order.get('error','unknown')}"
                )
                if _attempt < ORDER_MAX_ATTEMPTS:
                    logger.info(
                        f"[GENERATE] Retrying in {ORDER_RETRY_DELAY}s "
                        f"(attempt {_attempt + 1}/{ORDER_MAX_ATTEMPTS})…"
                    )
                    _time.sleep(ORDER_RETRY_DELAY)

            contracts      = order.get("contracts", 0)
            contract_price = order.get("price_per_contract", max_cp)
            order_id       = order.get("order_id") if order.get("success") else None

            if order.get("success"):
                logger.info(
                    f"[GENERATE] Order ✓ | {sig['symbol']} {_inverted_direction} (signal={sig['direction']}) "
                    f"${position_size} | {contracts} contracts @ ${contract_price} "
                    f"| id={order_id}"
                )
            else:
                logger.error(
                    f"[GENERATE] Order FAILED after {ORDER_MAX_ATTEMPTS} attempts | "
                    f"{sig['symbol']} | last_error={order.get('error','unknown')}"
                )

            # ── Save signal ──────────────────────────────────────────────────────
            signal_obj = Signal(
                symbol            = sig['symbol'],
                candle_open_time  = candle_open,
                candle_close_time = candle_close,
                signal_direction  = sig['direction'],
                ml_confidence     = sig['confidence'],
                rsi_14            = sig['rsi_14'],
                macd_hist         = sig['macd_hist'],
                adx               = sig['adx'],
                vol_ratio         = sig['vol_ratio'],
                tier              = sig['tier'],
                open_price        = sig['open_price'],  # provisional; overwritten by resolve with exact OKX OHLC
                mode              = mode,
                order_id          = order_id,
                market_slug       = order.get("slug") if order.get("success") else None,
                position_size     = position_size,
                contracts_bought  = contracts,
                contract_price    = contract_price,
                outcome           = "PENDING",
                telegram_sent     = False,
            )
            db.session.add(signal_obj)
            db.session.commit()

            # ── Shadow balance deduction ─────────────────────────────────────────
            if mode == "shadow":
                shadow = ShadowBalance.query.first()
                if shadow:
                    shadow.balance = max(0, shadow.balance - position_size)
                    db.session.commit()

            # ── Telegram ─────────────────────────────────────────────────────────
            try:
                sent = send_signal_alert(
                    signal        = sig,
                    mode          = mode,
                    position_size = position_size,
                    contracts     = contracts,
                    contract_price= contract_price,
                )
                signal_obj.telegram_sent = sent
                db.session.commit()
            except Exception as e:
                logger.warning(f"[GENERATE] Telegram failed: {e}")

            # ── WebSocket push ───────────────────────────────────────────────────
            try:
                socketio.emit("new_signal", signal_obj.to_dict())
            except Exception as e:
                logger.warning(f"[GENERATE] WS emit: {e}")

            logger.info(
                f"[GENERATE] Saved signal id={signal_obj.id} | "
                f"{sig['symbol']} {sig['direction']} conf={sig['confidence']:.3f} | "
                f"candle {candle_open}→{candle_close}"
            )

        except Exception as e:
            logger.error(f"[GENERATE] Unhandled error: {e}", exc_info=True)


def job_resolve_outcomes():
    """
    Fires at :00, :15, :30, :45 UTC — the SAME tick as the candle boundary.
    Resolves PENDING signals whose candle_close_time is within 5 seconds of now.

    The resolve job runs BEFORE generate in the same tick so the previous
    candle is always closed before the new signal is written.

    Retries fetching the OKX candle up to 3 times with 1-second gaps to give
    OKX time to publish the just-closed candle — no hard delay needed.

    Uses exact OKX OHLC prices:
      open_price  = candle row 'open'  at candle_open_time
      close_price = candle row 'close' at candle_open_time
    WIN: signal UP  → close > open
    WIN: signal DOWN → close < open
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance
            from signal_engine import fetch_okx_candles, record_outcome
            from telegram_bot import send_result_alert
            import pandas as pd
            import time as _time

            # Resolve any PENDING signal whose candle_close_time <= now + 5s tolerance.
            # The +5s window catches signals fired right at the boundary.
            now     = datetime.now(timezone.utc).replace(tzinfo=None)
            cutoff  = now + timedelta(seconds=5)

            pending = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.candle_close_time <= cutoff,
            ).all()

            if not pending:
                logger.debug("[RESOLVE] No pending signals ready to resolve")
                return

            logger.info(f"[RESOLVE] Resolving {len(pending)} pending signal(s)")

            # Group by symbol to minimise OKX API calls
            by_sym = {}
            for s in pending:
                by_sym.setdefault(s.symbol, []).append(s)

            for sym, sigs in by_sym.items():
                # Retry fetching OKX data up to 3 times (1s apart) to let
                # OKX publish the just-closed candle before we read it.
                df = pd.DataFrame()
                # Retry until OKX has published the just-closed candle (up to 5s)
                target_opens = {pd.Timestamp(s.candle_open_time, tz="UTC") for s in sigs}
                df = pd.DataFrame()
                for attempt in range(5):
                    df = fetch_okx_candles(sym, limit=10)
                    if not df.empty and target_opens.issubset(set(df["timestamp"])):
                        break
                    logger.debug(f"[RESOLVE] OKX waiting for candle data, attempt {attempt+1}/5 for {sym}")
                    _time.sleep(1)

                if df.empty:
                    logger.warning(f"[RESOLVE] No OKX data for {sym} after 5 attempts")
                    continue

                for sig in sigs:
                    try:
                        open_ts  = pd.Timestamp(sig.candle_open_time,  tz="UTC")
                        close_ts = pd.Timestamp(sig.candle_close_time, tz="UTC")

                        # Find the EXACT candle whose open timestamp == candle_open_time.
                        # This gives us the true OKX open AND close for that 15-min bar.
                        match = df[df['timestamp'] == open_ts]

                        # Fallback: candle opened within the [open_ts, close_ts) window
                        if match.empty:
                            match = df[
                                (df['timestamp'] >= open_ts) &
                                (df['timestamp'] <  close_ts)
                            ]

                        if match.empty:
                            logger.warning(
                                f"[RESOLVE] No matching candle for {sym} "
                                f"open={sig.candle_open_time} — will retry next boundary"
                            )
                            continue

                        candle_row  = match.iloc[0]
                        open_price  = float(candle_row['open'])   # exact OKX candle open
                        close_price = float(candle_row['close'])  # exact OKX candle close

                        # WIN/LOSS based on ML signal direction (no reversal)
                        if sig.signal_direction == "UP":
                            outcome = "WIN" if close_price > open_price else "LOSS"
                        else:
                            outcome = "WIN" if close_price < open_price else "LOSS"

                        sig.open_price  = open_price   # update to exact OKX candle open
                        sig.close_price = close_price
                        sig.outcome     = outcome
                        db.session.flush()

                        # Per-pair tracker
                        try:
                            record_outcome(sym, outcome)
                        except Exception:
                            pass

                        # Shadow P&L (prediction market pays ~1.8x on win)
                        if sig.mode == "shadow":
                            shadow = ShadowBalance.query.first()
                            if shadow and sig.position_size:
                                if outcome == "WIN":
                                    payout = sig.position_size * 1.8
                                    shadow.balance           += payout
                                    shadow.total_profit_loss += payout - sig.position_size
                                else:
                                    shadow.total_profit_loss -= sig.position_size

                        # Daily stats
                        sig_date = sig.candle_open_time.date()
                        daily    = DailyStats.query.filter_by(date=sig_date).first()
                        if not daily:
                            daily = DailyStats(date=sig_date, mode=sig.mode)
                            db.session.add(daily)
                        daily.total_signals  = (daily.total_signals or 0) + 1
                        daily.wins           = (daily.wins or 0) + (1 if outcome == "WIN"  else 0)
                        daily.losses         = (daily.losses or 0) + (1 if outcome == "LOSS" else 0)
                        daily.win_rate       = daily.wins / daily.total_signals * 100

                        # ── Auto-claim winnings on live WIN trades ────────────
                        # 5 attempts, 24s apart = completes within 2 minutes.
                        # Stops immediately on first success.
                        if outcome == "WIN" and sig.mode == "live" and sig.market_slug:
                            try:
                                import time as _claim_time
                                from limitless_executor import claim_winnings
                                CLAIM_MAX_ATTEMPTS = 5
                                CLAIM_RETRY_DELAY  = 24   # seconds (5 × 24s = 2 min total)
                                claim_result = {"success": False, "error": "not attempted"}

                                for _cattempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
                                    claim_result = claim_winnings(
                                        sig.market_slug,
                                        sig.signal_direction,
                                        sym,
                                    )
                                    if claim_result.get("success"):
                                        logger.info(
                                            f"[RESOLVE] CLAIM ✓ {sym} (attempt {_cattempt}/{CLAIM_MAX_ATTEMPTS}) | "
                                            f"redeemed={claim_result.get('redeemed_amount')} "
                                            f"tx={claim_result.get('tx_hash')}"
                                        )
                                        break  # success — do not retry
                                    logger.warning(
                                        f"[RESOLVE] CLAIM attempt {_cattempt}/{CLAIM_MAX_ATTEMPTS} FAILED {sym} | "
                                        f"error={claim_result.get('error')}"
                                    )
                                    if _cattempt < CLAIM_MAX_ATTEMPTS:
                                        logger.info(
                                            f"[RESOLVE] Retrying claim in {CLAIM_RETRY_DELAY}s "
                                            f"(attempt {_cattempt + 1}/{CLAIM_MAX_ATTEMPTS})…"
                                        )
                                        _claim_time.sleep(CLAIM_RETRY_DELAY)

                                if not claim_result.get("success"):
                                    logger.error(
                                        f"[RESOLVE] CLAIM EXHAUSTED {sym} after {CLAIM_MAX_ATTEMPTS} attempts | "
                                        f"last_error={claim_result.get('error')} | "
                                        f"slug={sig.market_slug} — check Limitless manually"
                                    )
                            except Exception as ce:
                                logger.error(f"[RESOLVE] claim_winnings exception: {ce}")

                        # Telegram result
                        try:
                            send_result_alert(sig.to_dict(), outcome,
                                              open_price, close_price)
                        except Exception:
                            pass

                        logger.info(
                            f"[RESOLVE] {sym} {sig.signal_direction} | "
                            f"entry={open_price:.4f} close={close_price:.4f} → {outcome}"
                        )

                    except Exception as e:
                        logger.error(f"[RESOLVE] Error on signal id={sig.id}: {e}")

            db.session.commit()

            # WebSocket push resolved signals
            try:
                for sigs in by_sym.values():
                    for s in sigs:
                        if s.outcome != "PENDING":
                            socketio.emit("signal_resolved", s.to_dict())
            except Exception as e:
                logger.warning(f"[RESOLVE] WS emit: {e}")

        except Exception as e:
            logger.error(f"[RESOLVE] Unhandled error: {e}", exc_info=True)


def job_daily_summary():
    with _ctx():
        try:
            from models import DailyStats, Settings
            from telegram_bot import send_daily_summary
            settings = Settings.query.first()
            mode     = settings.mode if settings else "shadow"
            today    = date.today()
            daily    = DailyStats.query.filter_by(date=today).first()
            if daily:
                send_daily_summary(
                    date_str = str(today),
                    wins     = daily.wins,
                    losses   = daily.losses,
                    total    = daily.total_signals,
                    mode     = mode,
                )
        except Exception as e:
            logger.error(f"[DAILY] {e}")


def job_retrain():
    with _ctx():
        try:
            from signal_engine import retrain_all
            logger.info("[RETRAIN] Weekly retrain starting...")
            retrain_all(limit=500)
            logger.info("[RETRAIN] Done")
        except Exception as e:
            logger.error(f"[RETRAIN] {e}")


def start_scheduler():
    # RESOLVE first — fires at the candle boundary second=0.
    # Closes the previous candle signal before the new one is generated.
    # Retries OKX fetch internally (up to 3x with 1s sleep) — no extra delay needed.
    scheduler.add_job(
        job_resolve_outcomes,
        CronTrigger(minute="0,15,30,45", second="0"),
        id="resolve",
        replace_existing=True,
        misfire_grace_time=10,
        max_instances=1,
    )

    # GENERATE — fires 2 seconds after the candle boundary.
    # At :00:02, :15:02, :30:02, :45:02 UTC.
    # The 2s gap lets RESOLVE commit first. The new Limitless market opens
    # AT the boundary — so firing at :00:02 places into the fresh :00→:15
    # market which has ~15 minutes before its deadline.
    # The expired-market filter in limitless_executor ensures we never
    # accidentally submit to the just-closed market.
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="0,15,30,45", second="2"),
        id="generate",
        replace_existing=True,
        misfire_grace_time=10,
        max_instances=1,
    )

    # Daily summary at 23:59 UTC
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=23, minute=59),
        id="daily",
        replace_existing=True,
    )

    # Weekly model retrain — Sunday 02:00 UTC
    scheduler.add_job(
        job_retrain,
        CronTrigger(day_of_week="sun", hour=2, minute=0),
        id="retrain",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "[SCHEDULER] Started | "
        "resolve@:00/:15/:30/:45+0s | "
        "generate@:00/:15/:30/:45+2s | "
        "daily@23:59 | retrain@sun-02:00"
    )
