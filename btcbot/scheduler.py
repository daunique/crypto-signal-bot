"""
APScheduler jobs:
- Every 15 min at candle open: generate signal, place order
- Every 15 min at candle close: resolve outcome (win/loss)
- Daily at 00:00 UTC: send daily summary
- Weekly: retrain models
"""
import logging
from datetime import datetime, timezone, timedelta, date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def _get_app_context():
    from app import app
    return app.app_context()


def job_generate_signal():
    """
    Runs at :00 of every 15-min candle (00:00, 00:15, 00:30, 00:45, 01:00...).
    Evaluates all 6 pairs, picks best signal, places order, notifies Telegram.
    """
    with _get_app_context():
        try:
            from extensions import db, socketio
            from models import Signal, Settings, ShadowBalance, DailyStats
            from signal_engine import pick_best_signal
            from limitless_executor import execute_order
            from telegram_bot import send_signal_alert

            settings = Settings.query.first()
            if not settings:
                settings = Settings()
                db.session.add(settings)
                db.session.commit()

            mode = settings.mode
            position_size = settings.position_size
            max_contract_price = settings.max_contract_price
            min_confidence = settings.min_confidence

            logger.info(f"[SCHEDULER] Generating signal | mode={mode} | "
                        f"size=${position_size} | conf>={min_confidence}")

            best = pick_best_signal(min_confidence=min_confidence)
            if not best:
                logger.info("[SCHEDULER] No qualifying signal this candle")
                return

            # Execute order
            order_result = execute_order(
                symbol=best['symbol'],
                direction=best['direction'],
                mode=mode,
                position_size_usd=position_size,
                max_contract_price=max_contract_price,
            )

            contracts = order_result.get("contracts", 0)
            contract_price = order_result.get("price_per_contract", max_contract_price)
            order_id = order_result.get("order_id") if order_result.get("success") else None

            # Save signal to DB
            signal = Signal(
                symbol=best['symbol'],
                candle_open_time=best['candle_open_time'],
                candle_close_time=best['candle_close_time'],
                signal_direction=best['direction'],
                ml_confidence=best['confidence'],
                rsi_14=best['rsi_14'],
                macd_hist=best['macd_hist'],
                adx=best['adx'],
                vol_ratio=best['vol_ratio'],
                tier=best['tier'],
                open_price=best['open_price'],
                mode=mode,
                order_id=order_id,
                position_size=position_size,
                contracts_bought=contracts,
                contract_price=contract_price,
                outcome="PENDING",
            )
            db.session.add(signal)
            db.session.commit()

            # Shadow balance deduction
            if mode == "shadow":
                shadow = ShadowBalance.query.first()
                if not shadow:
                    shadow = ShadowBalance(balance=1000.0)
                    db.session.add(shadow)
                shadow.balance = max(0, shadow.balance - position_size)
                db.session.commit()

            # Telegram notification
            sent = send_signal_alert(
                signal=best,
                mode=mode,
                position_size=position_size,
                contracts=contracts,
                contract_price=contract_price,
            )
            signal.telegram_sent = sent
            db.session.commit()

            # Push to dashboard via WebSocket
            # Use socketio.emit with to='/' for broadcast from background thread
            try:
                from extensions import socketio as sio
                sio.emit("new_signal", signal.to_dict())
            except Exception as ws_err:
                logger.warning(f"[WS] emit failed (HTTP polling will compensate): {ws_err}")

            logger.info(f"[SCHEDULER] Signal saved: {best['symbol']} {best['direction']} "
                        f"conf={best['confidence']:.2f} tier={best['tier']}")

        except Exception as e:
            logger.error(f"[SCHEDULER] job_generate_signal error: {e}", exc_info=True)


def job_resolve_outcomes():
    """
    Runs at :01 past every 15-min mark (00:01, 00:16, 00:31, 00:46...).
    Checks all PENDING signals whose candle_close_time <= now, fetches close price,
    marks WIN/LOSS. Only counts candles that have fully closed.
    """
    with _get_app_context():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance, Settings
            from signal_engine import fetch_okx_candles
            from telegram_bot import send_result_alert
            import pandas as pd

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            pending = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.candle_close_time <= now,
            ).all()

            if not pending:
                return

            settings = Settings.query.first()
            mode = settings.mode if settings else "shadow"

            for sig in pending:
                try:
                    # Fetch 2 candles to get the closed candle price
                    df = fetch_okx_candles(sig.symbol, limit=5)
                    if df.empty:
                        continue

                    # Find the candle that matches the close time
                    close_ts = pd.Timestamp(sig.candle_close_time, tz="UTC")
                    # The candle open == close_ts - 15min
                    candle_open_ts = close_ts - pd.Timedelta(minutes=15)
                    match = df[df['timestamp'] >= candle_open_ts]
                    if match.empty:
                        continue

                    close_price = float(match.iloc[0]['close'])
                    open_price = sig.open_price

                    if sig.signal_direction == "UP":
                        outcome = "WIN" if close_price > open_price else "LOSS"
                    else:
                        outcome = "WIN" if close_price < open_price else "LOSS"

                    sig.close_price = close_price
                    sig.outcome = outcome
                    db.session.commit()

                    # Record outcome in per-pair live tracker
                    try:
                        from signal_engine import record_outcome
                        record_outcome(sig.symbol, outcome)
                    except Exception:
                        pass

                    # Update shadow balance
                    if sig.mode == "shadow":
                        shadow = ShadowBalance.query.first()
                        if shadow and sig.position_size:
                            if outcome == "WIN":
                                # Win pays ~2x on prediction market
                                payout = sig.position_size * 1.8
                                shadow.balance += payout
                                shadow.total_profit_loss += (payout - sig.position_size)
                            else:
                                shadow.total_profit_loss -= sig.position_size
                            db.session.commit()

                    # Update daily stats
                    sig_date = sig.candle_open_time.date()
                    daily = DailyStats.query.filter_by(date=sig_date).first()
                    if not daily:
                        daily = DailyStats(date=sig_date, mode=sig.mode)
                        db.session.add(daily)
                    daily.total_signals += 1
                    if outcome == "WIN":
                        daily.wins += 1
                    else:
                        daily.losses += 1
                    daily.win_rate = daily.wins / daily.total_signals * 100
                    db.session.commit()

                    # Telegram result
                    send_result_alert(sig.to_dict(), outcome, open_price, close_price)

                    # Push update to dashboard
                    try:
                        from extensions import socketio as sio
                        sio.emit("signal_resolved", sig.to_dict())
                    except Exception as ws_err:
                        logger.warning(f"[WS] resolve emit failed: {ws_err}")

                    logger.info(f"[RESOLVE] {sig.symbol} {sig.signal_direction} -> {outcome} "
                                f"open={open_price:.4f} close={close_price:.4f}")

                except Exception as e:
                    logger.error(f"[RESOLVE] Error resolving signal {sig.id}: {e}")

        except Exception as e:
            logger.error(f"[SCHEDULER] job_resolve_outcomes error: {e}", exc_info=True)


def job_daily_summary():
    """Sends daily summary at 23:59 UTC."""
    with _get_app_context():
        try:
            from models import DailyStats, Settings
            from telegram_bot import send_daily_summary

            settings = Settings.query.first()
            mode = settings.mode if settings else "shadow"

            today = date.today()
            daily = DailyStats.query.filter_by(date=today).first()
            if daily:
                send_daily_summary(
                    date_str=str(today),
                    wins=daily.wins,
                    losses=daily.losses,
                    total=daily.total_signals,
                    mode=mode,
                )
        except Exception as e:
            logger.error(f"[SCHEDULER] daily summary error: {e}")


def job_retrain_models():
    """Retrains ML models weekly with fresh data."""
    with _get_app_context():
        try:
            from signal_engine import retrain_all
            logger.info("[SCHEDULER] Starting weekly model retrain...")
            retrain_all(limit=500)
            logger.info("[SCHEDULER] Weekly retrain complete")
        except Exception as e:
            logger.error(f"[SCHEDULER] retrain error: {e}")


def start_scheduler():
    """Configure and start all scheduled jobs."""

    # Signal generation: every 15 minutes at :00 (candle open)
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="0,15,30,45"),
        id="generate_signal",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Outcome resolution: 1 minute after candle close to ensure data is available
    scheduler.add_job(
        job_resolve_outcomes,
        CronTrigger(minute="1,16,31,46"),
        id="resolve_outcomes",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Daily summary at 23:59 UTC
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=23, minute=59),
        id="daily_summary",
        replace_existing=True,
    )

    # Weekly retrain on Sunday at 02:00 UTC
    scheduler.add_job(
        job_retrain_models,
        CronTrigger(day_of_week="sun", hour=2, minute=0),
        id="retrain_models",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("[SCHEDULER] Started: signal@:00, resolve@:01, daily@23:59, retrain weekly")
