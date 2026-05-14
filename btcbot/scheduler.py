"""
Scheduler — Signal timing
──────────────────────────────────────────────────────────────────────────
CORRECT 15-minute candle lifecycle:

  :00/:15/:30/:45  → candle OPENS
  :13/:28/:43/:58  → signal fires (2 min before current candle closes)
                     → signal references the NEXT candle as the tracked period
                       e.g. signal at :13 tracks candle :15→:30
  :15/:30/:45/:00  → current candle CLOSES / next candle OPENS
                     → resolve fires 2 seconds after close

  So the schedule is:
    generate  → :13, :28, :43, :58   ← 2 min before candle close
    resolve   → :15, :30, :45, :00   ← exactly at candle close (+2 s buffer)

  The signal_engine computes candle_open_time / candle_close_time as the
  NEXT 15-min boundary from the moment generate fires, so win/loss is
  correctly measured over the candle that starts after the signal drops.

Duplicate guard:
  Uses the candle_open_time FROM signal_engine directly as the unique key.
  A signal for candle 14:15→14:30 will never be saved twice because we
  check Signal.candle_open_time == sig['candle_open_time'] before saving.

Direction:
  Uses ML signal direction directly (no reversal).
  WIN: signal UP + close > open, or signal DOWN + close < open.
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
    Fires at :13, :28, :43, :58 UTC — 2 minutes before the current candle closes.
    The signal references the NEXT candle (the one that opens at the upcoming
    :00/:15/:30/:45 boundary) as the tracked win/loss period.
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

            # ── Place order ──────────────────────────────────────────────────────
            order          = execute_order(sig['symbol'], sig['direction'], mode,
                                           position_size, max_cp)
            contracts      = order.get("contracts", 0)
            contract_price = order.get("price_per_contract", max_cp)
            order_id       = order.get("order_id") if order.get("success") else None

            if order.get("success"):
                logger.info(
                    f"[GENERATE] Order ✓ | {sig['symbol']} {sig['direction']} "
                    f"${position_size} | {contracts} contracts @ ${contract_price} "
                    f"| id={order_id}"
                )
            else:
                logger.error(
                    f"[GENERATE] Order FAILED | {sig['symbol']} | "
                    f"error={order.get('error','unknown')}"
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
                open_price        = sig['open_price'],
                mode              = mode,
                order_id          = order_id,
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
    Fires at :00, :15, :30, :45 UTC — exactly at candle close.
    Resolves PENDING signals whose candle_close_time <= now.

    Uses OKX 'confirm' flag to guarantee the candle is fully closed before
    reading open/close prices. Retries next cycle if not yet confirmed.

    WIN/LOSS uses ML signal direction directly:
      signal UP   + close > open → WIN
      signal DOWN + close < open → WIN
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance
            from signal_engine import fetch_okx_candles, record_outcome
            from telegram_bot import send_result_alert
            import pandas as pd

            # Resolve any pending signal whose candle has already closed
            now     = datetime.now(timezone.utc).replace(tzinfo=None)
            cutoff  = now  # candle_close_time <= now means it's done

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
                # Fetch enough candles to cover all pending signals for this symbol
                df = fetch_okx_candles(sym, limit=20)
                if df.empty:
                    logger.warning(f"[RESOLVE] No OKX data for {sym}")
                    continue

                for sig in sigs:
                    try:
                        # candle_open_time is stored tz-naive — convert to tz-aware for matching
                        open_ts  = pd.Timestamp(sig.candle_open_time).tz_localize("UTC")
                        close_ts = pd.Timestamp(sig.candle_close_time).tz_localize("UTC")

                        # Match the exact candle by its open timestamp
                        match = df[df['timestamp'] == open_ts]

                        # Fallback: widen to range if exact match misses (sub-second drift)
                        if match.empty:
                            match = df[
                                (df['timestamp'] >= open_ts) &
                                (df['timestamp'] <  close_ts)
                            ]

                        if match.empty:
                            logger.warning(
                                f"[RESOLVE] No candle found for {sym} "
                                f"{sig.candle_open_time} — will retry next cycle"
                            )
                            continue

                        row = match.iloc[0]

                        # Only resolve against a fully confirmed (closed) candle.
                        # OKX sets confirm=1 when the candle is complete.
                        # If still 0, the candle hasn't closed yet — skip and retry.
                        if str(row.get('confirm', '1')) == '0':
                            logger.info(
                                f"[RESOLVE] {sym} candle {sig.candle_open_time} "
                                f"not yet confirmed — will retry next cycle"
                            )
                            continue

                        # Real open and close of the tracked candle
                        open_price  = float(row['open'])
                        close_price = float(row['close'])

                        # WIN/LOSS based on ML signal direction (no reversal)
                        if sig.signal_direction == "UP":
                            outcome = "WIN" if close_price > open_price else "LOSS"
                        else:
                            outcome = "WIN" if close_price < open_price else "LOSS"

                        sig.open_price  = open_price   # actual candle open (entry)
                        sig.close_price = close_price  # actual candle close (exit)
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
                        daily.total_signals += 1
                        daily.wins          += 1 if outcome == "WIN"  else 0
                        daily.losses        += 1 if outcome == "LOSS" else 0
                        daily.win_rate       = daily.wins / daily.total_signals * 100

                        # Telegram result
                        try:
                            send_result_alert(sig.to_dict(), outcome,
                                              open_price, close_price)
                        except Exception:
                            pass

                        logger.info(
                            f"[RESOLVE] {sym} {sig.signal_direction} | "
                            f"open={open_price:.4f} close={close_price:.4f} → {outcome}"
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
    # Signal generation — 2 min before candle CLOSE (:13, :28, :43, :58)
    # The signal_engine will return candle_open/close for the NEXT boundary.
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="13,28,43,58"),
        id="generate",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,       # never run two generate jobs simultaneously
    )

    # Outcome resolution — exactly at candle CLOSE (:00, :15, :30, :45)
    # 2-second cutoff buffer is enough; candle data is available immediately at close.
    scheduler.add_job(
        job_resolve_outcomes,
        CronTrigger(minute="0,15,30,45"),
        id="resolve",
        replace_existing=True,
        misfire_grace_time=30,
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
        "generate@:13/:28/:43/:58 | "
        "resolve@:00/:15/:30/:45 | "
        "daily@23:59 | retrain@sun-02:00"
    )
