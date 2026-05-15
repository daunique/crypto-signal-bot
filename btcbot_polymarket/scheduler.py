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
    Generates ONE signal, places FAK limit order on Polymarket, saves to DB.
    Duplicate guard: skips if a signal for this candle_open_time already exists.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, Settings, ShadowBalance
            from signal_engine import pick_best_signal
            from polymarket_executor import execute_order   # ← Polymarket
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

            sig = pick_best_signal(min_confidence=min_conf)
            if not sig:
                logger.info("[GENERATE] No qualifying signal this candle")
                return

            candle_open  = sig['candle_open_time']
            candle_close = sig['candle_close_time']

            # Duplicate guard
            existing = Signal.query.filter(
                Signal.candle_open_time == candle_open
            ).first()
            if existing:
                logger.info(f"[GENERATE] Duplicate guard — signal already exists for {candle_open}")
                return

            # Place FAK limit order on Polymarket
            order          = execute_order(sig['symbol'], sig['direction'], mode,
                                           position_size, max_cp)
            contracts      = order.get("contracts", 0)
            contract_price = order.get("price_per_contract", max_cp)
            order_id       = order.get("order_id") if order.get("success") else None

            if order.get("success"):
                logger.info(
                    f"[GENERATE] FAK order ✓ | {sig['symbol']} {sig['direction']} "
                    f"${position_size} | {contracts} contracts @ ${contract_price} "
                    f"| matched={order.get('size_matched', 0)} | id={order_id}"
                )
            else:
                logger.error(
                    f"[GENERATE] Order FAILED | {sig['symbol']} | "
                    f"error={order.get('error','unknown')}"
                )

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
            )
            db.session.add(signal_obj)
            db.session.commit()
            logger.info(f"[GENERATE] Signal saved | id={signal_obj.id}")

            # Shadow balance update
            if mode == "shadow":
                shadow = ShadowBalance.query.first()
                if shadow:
                    shadow.balance -= position_size
                    shadow.updated_at = datetime.utcnow()
                    db.session.commit()

            # Telegram alert
            try:
                send_signal_alert(sig, mode, position_size, contracts, contract_price)
                signal_obj.telegram_sent = True
                db.session.commit()
            except Exception as te:
                logger.warning(f"[GENERATE] Telegram failed: {te}")

            # WebSocket push
            try:
                socketio.emit("new_signal", signal_obj.to_dict(), namespace="/")
            except Exception as we:
                logger.warning(f"[GENERATE] WebSocket failed: {we}")

        except Exception as e:
            logger.error(f"[GENERATE] Unhandled error: {e}", exc_info=True)


def job_resolve_signal():
    """
    Fires at :00, :15, :30, :45 UTC — candle CLOSE (same tick, runs first).
    Resolves PENDING signals whose candle_close_time <= now.
    Fetches OKX close price and marks WIN/LOSS.
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, Settings, ShadowBalance, DailyStats
            from signal_engine import fetch_okx_candles
            from telegram_bot import send_result_alert

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            tolerance = timedelta(seconds=5)

            pending = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.candle_close_time <= now + tolerance,
            ).all()

            if not pending:
                return

            logger.info(f"[RESOLVE] Resolving {len(pending)} signal(s)")

            for sig in pending:
                symbol = sig.symbol

                # Retry up to 3x for OKX to publish the close candle
                close_price = None
                for attempt in range(3):
                    df = fetch_okx_candles(symbol, limit=5)
                    if not df.empty:
                        # Find the candle that matches candle_open_time
                        close_row = df[df['timestamp'].dt.replace(tzinfo=None) == sig.candle_open_time]
                        if not close_row.empty:
                            close_price = float(close_row.iloc[-1]['close'])
                            break
                        # Fallback: use the second-to-last candle's close
                        if len(df) >= 2:
                            close_price = float(df.iloc[-2]['close'])
                            break
                    if attempt < 2:
                        import time; time.sleep(1)

                if close_price is None:
                    logger.warning(f"[RESOLVE] Could not fetch close price for signal {sig.id}")
                    continue

                sig.close_price = close_price

                if sig.signal_direction == "UP":
                    sig.outcome = "WIN" if close_price > sig.open_price else "LOSS"
                else:
                    sig.outcome = "WIN" if close_price < sig.open_price else "LOSS"

                # Shadow balance: return position on WIN (simplified P&L)
                if sig.mode == "shadow":
                    shadow = ShadowBalance.query.first()
                    if shadow:
                        if sig.outcome == "WIN":
                            pnl = sig.position_size * 0.9  # ~90% net gain estimate
                            shadow.balance          += sig.position_size + pnl
                            shadow.total_profit_loss += pnl
                        else:
                            shadow.total_profit_loss -= sig.position_size
                        shadow.updated_at = datetime.utcnow()

                db.session.commit()
                logger.info(f"[RESOLVE] Signal {sig.id} {sig.symbol} → {sig.outcome} "
                            f"open={sig.open_price} close={close_price}")

                # Telegram result
                try:
                    send_result_alert(sig)
                except Exception as te:
                    logger.warning(f"[RESOLVE] Telegram result failed: {te}")

                # WebSocket push
                try:
                    socketio.emit("signal_resolved", sig.to_dict(), namespace="/")
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[RESOLVE] Unhandled error: {e}", exc_info=True)


def job_daily_summary():
    """Fires at 23:59 UTC — record daily stats."""
    with _ctx():
        try:
            from extensions import db
            from models import Signal, DailyStats

            today       = date.today()
            today_start = datetime.combine(today, datetime.min.time())
            sigs        = Signal.query.filter(
                Signal.candle_open_time >= today_start
            ).all()

            wins   = sum(1 for s in sigs if s.outcome == "WIN")
            losses = sum(1 for s in sigs if s.outcome == "LOSS")
            total  = len(sigs)
            wr     = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0

            ds = DailyStats.query.filter_by(date=today).first()
            if not ds:
                ds = DailyStats(date=today)
                db.session.add(ds)

            ds.total_signals = total
            ds.wins          = wins
            ds.losses        = losses
            ds.win_rate      = wr
            db.session.commit()
            logger.info(f"[DAILY] {today} | W={wins} L={losses} WR={wr}%")

        except Exception as e:
            logger.error(f"[DAILY] {e}", exc_info=True)


def job_retrain():
    """Fires Sunday 02:00 UTC — retrain all ML models."""
    with _ctx():
        try:
            from signal_engine import retrain_all
            logger.info("[RETRAIN] Weekly retrain starting...")
            retrain_all(limit=500)
            logger.info("[RETRAIN] Complete")
        except Exception as e:
            logger.error(f"[RETRAIN] {e}", exc_info=True)


def start_scheduler():
    scheduler.add_job(
        job_resolve_signal,
        CronTrigger(minute="0,15,30,45", second="0"),
        id="resolve",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
    )
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="0,15,30,45", second="2"),
        id="generate",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
    )
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=23, minute=59),
        id="daily",
        replace_existing=True,
    )
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
