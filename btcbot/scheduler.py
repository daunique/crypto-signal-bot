"""
Scheduler
─────────────────────────────────────────────────────────────────────────
Signal timing — exact 15-min candle boundaries (UTC):
  Generate:  :00, :15, :30, :45  — one signal fired per candle
  Resolve:   :01, :16, :31, :46  — after candle fully closes
  Daily:     23:59 UTC
  Retrain:   Sunday 02:00 UTC

ONE signal per candle enforced by pick_best_signal() in signal_engine.
"""
import logging
from datetime import datetime, timezone, date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def _ctx():
    from app import app
    return app.app_context()


def job_generate_signal():
    """
    Runs at exactly :00, :15, :30, :45 UTC.
    Picks the single best signal across all 6 pairs.
    Places order immediately (live or shadow).
    Sends Telegram notification.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, Settings, ShadowBalance
            from signal_engine import pick_best_signal
            from limitless_executor import execute_order
            from telegram_bot import send_signal_alert
            from datetime import datetime, timezone
            import math

            settings = Settings.query.first()
            if not settings:
                settings = Settings()
                db.session.add(settings)
                db.session.commit()

            mode          = settings.mode
            position_size = settings.position_size
            max_cp        = settings.max_contract_price
            min_conf      = settings.min_confidence

            # ── Duplicate guard ───────────────────────────────────────────────
            # Calculate the current candle's exact open time (floor to 15-min boundary)
            now_utc    = datetime.now(timezone.utc)
            total_mins = now_utc.hour * 60 + now_utc.minute
            boundary   = (total_mins // 15) * 15
            candle_h   = boundary // 60
            candle_m   = boundary % 60
            candle_open = now_utc.replace(
                hour=candle_h, minute=candle_m, second=0,
                microsecond=0, tzinfo=None
            )
            candle_close = candle_open.replace(
                hour=(candle_h + (candle_m + 15) // 60) % 24,
                minute=(candle_m + 15) % 60
            )

            # Check if we already saved a signal for this exact candle window
            existing = Signal.query.filter(
                Signal.candle_open_time == candle_open,
            ).first()
            if existing:
                logger.info(
                    f"[SCHEDULER] Signal already exists for candle "
                    f"{candle_open} → {candle_close} (id={existing.id}) — skipping duplicate"
                )
                return

            logger.info(f"[SCHEDULER] Generating signal | candle={candle_open}→{candle_close} "
                        f"| mode={mode} | min_conf={min_conf}")

            sig = pick_best_signal(min_confidence=min_conf)
            if not sig:
                logger.info("[SCHEDULER] No signal this candle — nothing sent")
                return

            # Place order
            order          = execute_order(sig['symbol'], sig['direction'], mode,
                                           position_size, max_cp)
            contracts      = order.get("contracts", 0)
            contract_price = order.get("price_per_contract", max_cp)
            order_id       = order.get("order_id") if order.get("success") else None

            if not order.get("success"):
                logger.error(f"[SCHEDULER] Order failed: {order.get('error')}")

            signal_obj = Signal(
                symbol            = sig['symbol'],
                candle_open_time  = sig['candle_open_time'],
                candle_close_time = sig['candle_close_time'],
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

            # Shadow balance deduction
            if mode == "shadow":
                shadow = ShadowBalance.query.first()
                if shadow:
                    shadow.balance = max(0, shadow.balance - position_size)
                    db.session.commit()

            # Telegram
            sent = send_signal_alert(
                signal     = sig,
                mode       = mode,
                position_size   = position_size,
                contracts  = contracts,
                contract_price  = contract_price,
            )
            signal_obj.telegram_sent = sent
            db.session.commit()

            # WebSocket push
            try:
                socketio.emit("new_signal", signal_obj.to_dict())
            except Exception as e:
                logger.warning(f"[WS] emit: {e}")

            logger.info(f"[SCHEDULER] Signal saved: {sig['symbol']} "
                        f"{sig['direction']} conf={sig['confidence']:.3f} "
                        f"candle={sig['candle_open_time']}→{sig['candle_close_time']}")

        except Exception as e:
            logger.error(f"[SCHEDULER] generate error: {e}", exc_info=True)


def job_resolve_outcomes():
    """
    Runs at :01, :16, :31, :46 UTC — 1 minute after candle close.
    Resolves all PENDING signals whose candle_close_time <= now.
    WIN/LOSS determined strictly by candle close price vs entry price.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance
            from signal_engine import fetch_okx_candles, record_outcome
            from telegram_bot import send_result_alert
            import pandas as pd

            now = datetime.now(timezone.utc).replace(tzinfo=None)

            pending = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.candle_close_time <= now,
            ).all()

            if not pending:
                logger.debug("[RESOLVE] No pending signals to resolve")
                return

            # Group by symbol to minimise API calls
            by_sym = {}
            for s in pending:
                by_sym.setdefault(s.symbol, []).append(s)

            for sym, sigs in by_sym.items():
                df = fetch_okx_candles(sym, limit=10)
                if df.empty:
                    continue

                for sig in sigs:
                    try:
                        # Find the candle that corresponds to this signal's window
                        close_ts = pd.Timestamp(sig.candle_close_time, tz="UTC")
                        open_ts  = pd.Timestamp(sig.candle_open_time,  tz="UTC")

                        # Match candle whose open timestamp == signal candle_open_time
                        match = df[
                            (df['timestamp'] >= open_ts) &
                            (df['timestamp'] <  close_ts)
                        ]
                        if match.empty:
                            # fallback: use first candle after open
                            match = df[df['timestamp'] >= open_ts]
                        if match.empty:
                            logger.warning(f"[RESOLVE] No candle match for {sym} {sig.candle_open_time}")
                            continue

                        close_price = float(match.iloc[0]['close'])
                        open_price  = sig.open_price

                        # WIN/LOSS based on TRADE direction (opposite of ML signal)
                        # signal UP → trade DOWN → WIN if price went DOWN
                        # signal DOWN → trade UP → WIN if price went UP
                        if sig.signal_direction == "UP":
                            # traded DOWN
                            outcome = "WIN" if close_price < open_price else "LOSS"
                        else:
                            # traded UP
                            outcome = "WIN" if close_price > open_price else "LOSS"

                        sig.close_price = close_price
                        sig.outcome     = outcome
                        db.session.flush()

                        # Per-pair tracker
                        try:
                            record_outcome(sym, outcome)
                        except Exception:
                            pass

                        # Shadow P&L
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
                        daily.wins   += 1 if outcome == "WIN"  else 0
                        daily.losses += 1 if outcome == "LOSS" else 0
                        daily.win_rate = daily.wins / daily.total_signals * 100

                        # Telegram result
                        try:
                            send_result_alert(sig.to_dict(), outcome, open_price, close_price)
                        except Exception:
                            pass

                        logger.info(f"[RESOLVE] {sym} {sig.signal_direction} "
                                    f"open={open_price:.4f} close={close_price:.4f} → {outcome}")

                    except Exception as e:
                        logger.error(f"[RESOLVE] signal {sig.id}: {e}")

            db.session.commit()

            # WebSocket push
            try:
                for sigs in by_sym.values():
                    for s in sigs:
                        if s.outcome != "PENDING":
                            socketio.emit("signal_resolved", s.to_dict())
            except Exception as e:
                logger.warning(f"[WS] resolve emit: {e}")

        except Exception as e:
            logger.error(f"[SCHEDULER] resolve error: {e}", exc_info=True)


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
            logger.error(f"[SCHEDULER] daily summary: {e}")


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
    # Signal generation — exact 15-min candle open boundaries
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="0,15,30,45"),
        id="generate", replace_existing=True, misfire_grace_time=20,
    )
    # Outcome resolution — 1 min after candle close
    scheduler.add_job(
        job_resolve_outcomes,
        CronTrigger(minute="1,16,31,46"),
        id="resolve", replace_existing=True, misfire_grace_time=20,
    )
    # Daily summary
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=23, minute=59),
        id="daily", replace_existing=True,
    )
    # Weekly retrain
    scheduler.add_job(
        job_retrain,
        CronTrigger(day_of_week="sun", hour=2, minute=0),
        id="retrain", replace_existing=True,
    )
    scheduler.start()
    logger.info("[SCHEDULER] Started — signals@:00/:15/:30/:45, resolve@:01/:16/:31/:46")
