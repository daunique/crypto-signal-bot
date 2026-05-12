"""
Scheduler — AB testing mode
Each pair's signal is evaluated independently every 15 minutes.
All 4 pairs run in parallel. No Telegram (AB test mode).
Win/loss resolved 1 minute after candle close.
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


def job_generate_signals():
    """
    Runs at :00, :15, :30, :45 UTC.
    Evaluates all 4 pairs independently.
    Each pair's signal saved separately — no cross-pair filtering.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, Settings, ShadowBalance
            from signal_engine import get_all_signals
            from limitless_executor import execute_order

            settings = Settings.query.first()
            if not settings:
                settings = Settings(); db.session.add(settings); db.session.commit()

            mode             = settings.mode
            position_size    = settings.position_size
            max_cp           = settings.max_contract_price

            logger.info(f"[SCHEDULER] Evaluating all pairs | mode={mode}")

            all_sigs = get_all_signals()
            saved = []

            for sym, sig in all_sigs.items():
                if sig is None:
                    logger.info(f"[{sym}] No signal this candle")
                    continue

                # Execute order (shadow or live)
                order = execute_order(sym, sig['direction'], mode, position_size, max_cp)
                contracts    = order.get("contracts", 0)
                contract_price = order.get("price_per_contract", max_cp)
                order_id     = order.get("order_id") if order.get("success") else None

                signal = Signal(
                    symbol           = sym,
                    candle_open_time = sig['candle_open_time'],
                    candle_close_time= sig['candle_close_time'],
                    signal_direction = sig['direction'],
                    trade_direction  = order.get('trade_direction', ''),  # reversed direction
                    ml_confidence    = sig['confidence'],
                    rsi_14           = sig['rsi_14'],
                    macd_hist        = sig['macd_hist'],
                    adx              = sig['adx'],
                    vol_ratio        = sig['vol_ratio'],
                    tier             = sig['tier'],
                    open_price       = sig['open_price'],
                    mode             = mode,
                    order_id         = order_id,
                    position_size    = position_size,
                    contracts_bought = contracts,
                    contract_price   = contract_price,
                    outcome          = "PENDING",
                    telegram_sent    = False,
                )
                db.session.add(signal)
                db.session.flush()
                saved.append(signal)

                logger.info(f"[{sym}] Signal saved: {sig['direction']} "
                            f"conf={sig['confidence']:.3f} tier={sig['tier']}")

            db.session.commit()

            # Shadow balance deduction
            if mode == "shadow" and saved:
                shadow = ShadowBalance.query.first()
                if shadow:
                    total_spent = position_size * len(saved)
                    shadow.balance = max(0, shadow.balance - total_spent)
                    db.session.commit()

            # Push all signals to dashboard via WebSocket
            try:
                for sig_obj in saved:
                    socketio.emit("new_signal", sig_obj.to_dict())
            except Exception as e:
                logger.warning(f"[WS] emit error: {e}")

        except Exception as e:
            logger.error(f"[SCHEDULER] job_generate_signals error: {e}", exc_info=True)


def job_resolve_outcomes():
    """
    Runs at :01, :16, :31, :46 UTC.
    Resolves all PENDING signals whose candle has fully closed.
    Win/Loss counted only after full 15-min candle closes.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance
            from signal_engine import fetch_okx_candles, record_outcome
            import pandas as pd

            now     = datetime.now(timezone.utc).replace(tzinfo=None)
            pending = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.candle_close_time <= now,
            ).all()

            if not pending:
                return

            # Group by symbol to minimise OKX calls
            by_sym = {}
            for s in pending:
                by_sym.setdefault(s.symbol, []).append(s)

            for sym, sigs in by_sym.items():
                df = fetch_okx_candles(sym, limit=10)
                if df.empty:
                    continue

                for sig in sigs:
                    try:
                        close_ts = pd.Timestamp(sig.candle_close_time, tz="UTC")
                        open_ts  = close_ts - pd.Timedelta(minutes=15)
                        match    = df[df['timestamp'] >= open_ts]
                        if match.empty:
                            continue

                        close_price = float(match.iloc[0]['close'])
                        open_price  = sig.open_price

                        # Outcome based on TRADE direction (reversed), not signal direction
                        effective_dir = sig.trade_direction or sig.signal_direction
                        if effective_dir == "UP":
                            outcome = "WIN" if close_price > open_price else "LOSS"
                        else:
                            outcome = "WIN" if close_price < open_price else "LOSS"

                        sig.close_price = close_price
                        sig.outcome     = outcome
                        db.session.flush()

                        # Per-pair live tracker
                        try:
                            record_outcome(sym, outcome)
                        except Exception:
                            pass

                        # Shadow balance update
                        if sig.mode == "shadow":
                            shadow = ShadowBalance.query.first()
                            if shadow and sig.position_size:
                                if outcome == "WIN":
                                    payout = sig.position_size * 1.8
                                    shadow.balance          += payout
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
                        daily.wins   += 1 if outcome=="WIN"  else 0
                        daily.losses += 1 if outcome=="LOSS" else 0
                        daily.win_rate = daily.wins / daily.total_signals * 100

                        logger.info(f"[{sym}] {sig.signal_direction} → {outcome} "
                                    f"open={open_price:.4f} close={close_price:.4f}")

                    except Exception as e:
                        logger.error(f"[RESOLVE] {sym} signal {sig.id}: {e}")

            db.session.commit()

            # Push updates to dashboard
            try:
                for sym_sigs in by_sym.values():
                    for s in sym_sigs:
                        if s.outcome != "PENDING":
                            socketio.emit("signal_resolved", s.to_dict())
            except Exception as e:
                logger.warning(f"[WS] resolve emit: {e}")

        except Exception as e:
            logger.error(f"[SCHEDULER] resolve error: {e}", exc_info=True)


def job_daily_summary():
    """23:59 UTC — log daily stats per pair."""
    with _ctx():
        try:
            from models import DailyStats, Signal
            from datetime import date
            today  = date.today()
            logger.info(f"[DAILY] Summary for {today}")
            for sym in ["BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT"]:
                wins   = Signal.query.filter_by(symbol=sym, outcome="WIN").count()
                losses = Signal.query.filter_by(symbol=sym, outcome="LOSS").count()
                total  = wins+losses
                wr     = wins/total*100 if total>0 else 0
                logger.info(f"  {sym}: W={wins} L={losses} WR={wr:.1f}%")
        except Exception as e:
            logger.error(f"[DAILY] summary error: {e}")


def job_retrain():
    """Sunday 02:00 UTC — retrain all models."""
    with _ctx():
        try:
            from signal_engine import retrain_all
            logger.info("[RETRAIN] Weekly model retrain starting...")
            retrain_all(limit=500)
            logger.info("[RETRAIN] Done")
        except Exception as e:
            logger.error(f"[RETRAIN] error: {e}")


def start_scheduler():
    scheduler.add_job(job_generate_signals, CronTrigger(minute="0,15,30,45"),
                      id="gen_signals", replace_existing=True, misfire_grace_time=30)
    scheduler.add_job(job_resolve_outcomes, CronTrigger(minute="1,16,31,46"),
                      id="resolve",     replace_existing=True, misfire_grace_time=30)
    scheduler.add_job(job_daily_summary,   CronTrigger(hour=23, minute=59),
                      id="daily_sum",   replace_existing=True)
    scheduler.add_job(job_retrain, CronTrigger(day_of_week="sun", hour=2),
                      id="retrain",     replace_existing=True)
    scheduler.start()
    logger.info("[SCHEDULER] Started: signals@:00, resolve@:01, daily@23:59, retrain weekly")
