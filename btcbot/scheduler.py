"""
Scheduler — Signal timing
──────────────────────────────────────────────────────────────────────────
15-minute candle lifecycle:

  :00/:15/:30/:45  → candle OPENS
  :13/:28/:43/:58  → signal fires (2 min before current candle closes)
                     signal_engine returns the NEXT candle as the tracked period
                     e.g. signal at :13 → candle_open=:15, candle_close=:30
  :15/:30/:45/:00  → tracked candle OPENS  (entry = this candle's open price)
  :30/:45/:00/:15  → tracked candle CLOSES (exit  = this candle's close price)
                     → resolve fires 1 min later, reads OKX open+close → WIN/LOSS

Schedule:
  generate        → :13, :28, :43, :58
  resolve_primary → :01, :16, :31, :46  (1 min after candle close)
  resolve_safety  → every 2 minutes     (catches any missed/stuck signals)

Win/Loss rule:
  open_price  = actual candle OPEN  from OKX  (entry at candle start)
  close_price = actual candle CLOSE from OKX  (exit  at candle end)
  UP   signal: WIN if close > open
  DOWN signal: WIN if close < open
"""
import logging
from datetime import datetime, timezone, timedelta, date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def _ctx():
    from app import app
    return app.app_context()


# ── Signal generation ─────────────────────────────────────────────────────────

def job_generate_signal():
    """
    Fires at :13, :28, :43, :58 UTC — 2 min before current candle closes.
    signal_engine returns candle_open_time/candle_close_time for the NEXT boundary.
    open_price stored here is a preview only — overwritten at resolve time with
    the actual OKX candle open.
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

            sig = pick_best_signal(min_confidence=min_conf)
            if not sig:
                logger.info("[GENERATE] No qualifying signal this candle")
                return

            candle_open  = sig['candle_open_time']
            candle_close = sig['candle_close_time']

            existing = Signal.query.filter(
                Signal.entry_time == candle_open
            ).first()
            if existing:
                logger.info(
                    f"[GENERATE] Duplicate blocked — signal already exists for "
                    f"candle {candle_open}→{candle_close} "
                    f"(existing id={existing.id} symbol={existing.symbol})"
                )
                return

            order          = execute_order(sig['symbol'], sig['direction'], mode,
                                           position_size, max_cp)
            contracts      = order.get("contracts", 0)
            contract_price = order.get("price_per_contract", max_cp)
            order_id       = order.get("order_id") if order.get("success") else None

            if order.get("success"):
                logger.info(
                    f"[GENERATE] Order OK | {sig['symbol']} {sig['direction']} "
                    f"${position_size} | {contracts} contracts @ ${contract_price} "
                    f"| id={order_id}"
                )
            else:
                logger.error(
                    f"[GENERATE] Order FAILED | {sig['symbol']} | "
                    f"error={order.get('error','unknown')}"
                )

            signal_obj = Signal(
                symbol            = sig['symbol'],
                entry_time       = candle_open,
                close_time       = candle_close,
                signal_direction  = sig['direction'],
                ml_confidence     = sig['confidence'],
                rsi_14            = sig['rsi_14'],
                macd_hist         = sig['macd_hist'],
                adx               = sig['adx'],
                vol_ratio         = sig['vol_ratio'],
                tier              = sig['tier'],
                entry_price      = None,   # filled at resolve with real candle open
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

            if mode == "shadow":
                shadow = ShadowBalance.query.first()
                if shadow:
                    shadow.balance = max(0, shadow.balance - position_size)
                    db.session.commit()

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

            try:
                socketio.emit("new_signal", signal_obj.to_dict())
            except Exception as e:
                logger.warning(f"[GENERATE] WS emit: {e}")

            logger.info(
                f"[GENERATE] Saved id={signal_obj.id} | "
                f"{sig['symbol']} {sig['direction']} conf={sig['confidence']:.3f} | "
                f"candle {candle_open}→{candle_close}"
            )

        except Exception as e:
            logger.error(f"[GENERATE] Unhandled error: {e}", exc_info=True)


# ── Outcome resolution ────────────────────────────────────────────────────────

def job_resolve_outcomes():
    """
    Resolves all PENDING signals whose candle_close_time <= (now - 60s).

    For each signal:
      1. Fetches the last 20 OKX 15m candles for that symbol
      2. Matches the candle by its exact open timestamp (= candle_open_time)
      3. open_price  = matched candle's OPEN  → true entry price
         close_price = matched candle's CLOSE → true exit price
      4. WIN/LOSS = direction vs open→close movement
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance
            from signal_engine import fetch_okx_candles, record_outcome
            from telegram_bot import send_result_alert
            import pandas as pd

            now    = datetime.now(timezone.utc).replace(tzinfo=None)
            cutoff = now - timedelta(seconds=60)   # candle must have closed >= 60s ago

            pending = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.close_time <= cutoff,
            ).all()

            if not pending:
                logger.debug("[RESOLVE] No pending signals to resolve")
                return

            logger.info(f"[RESOLVE] {len(pending)} signal(s) to resolve")

            by_sym = {}
            for s in pending:
                by_sym.setdefault(s.symbol, []).append(s)

            for sym, sigs in by_sym.items():
                df = fetch_okx_candles(sym, limit=20)
                if df.empty:
                    logger.warning(f"[RESOLVE] No OKX data for {sym} — will retry")
                    continue

                logger.debug(
                    f"[RESOLVE] {sym} OKX candles: "
                    f"{df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}"
                )

                for sig in sigs:
                    try:
                        # Localize tz-naive DB timestamps to UTC for DataFrame comparison
                        open_ts  = pd.Timestamp(sig.entry_time).tz_localize("UTC")
                        close_ts = pd.Timestamp(sig.close_time).tz_localize("UTC")

                        # Exact match on candle open timestamp
                        match = df[df['timestamp'] == open_ts]

                        # Fallback: any candle within the window (handles sub-second drift)
                        if match.empty:
                            match = df[
                                (df['timestamp'] >= open_ts) &
                                (df['timestamp'] <  close_ts)
                            ]

                        if match.empty:
                            logger.warning(
                                f"[RESOLVE] {sym} candle {sig.entry_time} "
                                f"not in OKX response "
                                f"(got: {df['timestamp'].tolist()}) — will retry"
                            )
                            continue

                        row = match.iloc[0]

                        # Entry = candle open, Exit = candle close — exact 15-min values
                        open_price  = float(row['open'])
                        close_price = float(row['close'])

                        if sig.signal_direction == "UP":
                            outcome = "WIN" if close_price > open_price else "LOSS"
                        else:
                            outcome = "WIN" if close_price < open_price else "LOSS"

                        sig.entry_price  = open_price    # overwrite preview with real entry
                        sig.close_price = close_price
                        sig.outcome     = outcome
                        db.session.flush()

                        try:
                            record_outcome(sym, outcome)
                        except Exception:
                            pass

                        if sig.mode == "shadow":
                            shadow = ShadowBalance.query.first()
                            if shadow and sig.position_size:
                                if outcome == "WIN":
                                    payout = sig.position_size * 1.8
                                    shadow.balance           += payout
                                    shadow.total_profit_loss += payout - sig.position_size
                                else:
                                    shadow.total_profit_loss -= sig.position_size

                        sig_date = sig.entry_time.date()
                        daily    = DailyStats.query.filter_by(date=sig_date).first()
                        if not daily:
                            daily = DailyStats(date=sig_date, mode=sig.mode)
                            db.session.add(daily)
                        daily.total_signals += 1
                        daily.wins          += 1 if outcome == "WIN"  else 0
                        daily.losses        += 1 if outcome == "LOSS" else 0
                        daily.win_rate       = daily.wins / daily.total_signals * 100

                        try:
                            send_result_alert(sig.to_dict(), outcome,
                                              open_price, close_price)
                        except Exception:
                            pass

                        logger.info(
                            f"[RESOLVE] OK {sym} {sig.signal_direction} | "
                            f"{sig.entry_time} → {sig.close_time} | "
                            f"entry={open_price:.4f}  close={close_price:.4f}  {outcome}"
                        )

                    except Exception as e:
                        logger.error(
                            f"[RESOLVE] Error signal id={sig.id}: {e}", exc_info=True
                        )

            db.session.commit()

            try:
                for sigs in by_sym.values():
                    for s in sigs:
                        if s.outcome != "PENDING":
                            socketio.emit("signal_resolved", s.to_dict())
            except Exception as e:
                logger.warning(f"[RESOLVE] WS emit: {e}")

        except Exception as e:
            logger.error(f"[RESOLVE] Unhandled error: {e}", exc_info=True)


# ── Supporting jobs ───────────────────────────────────────────────────────────

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


# ── Scheduler startup ─────────────────────────────────────────────────────────

def start_scheduler():
    # Signal generation — 2 min before candle close
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="13,28,43,58"),
        id="generate",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
    )

    # Primary resolve — 1 full minute after candle close
    # :01/:16/:31/:46 — OKX candle data is always finalized by then
    scheduler.add_job(
        job_resolve_outcomes,
        CronTrigger(minute="1,16,31,46"),
        id="resolve_primary",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
    )

    # Safety-net resolve — every 2 minutes
    # Catches signals missed by the primary (OKX timeout, restart, etc.)
    scheduler.add_job(
        job_resolve_outcomes,
        IntervalTrigger(minutes=2),
        id="resolve_safety",
        replace_existing=True,
        misfire_grace_time=60,
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
        "resolve_primary@:01/:16/:31/:46 | "
        "resolve_safety@every-2min | "
        "daily@23:59 | retrain@sun-02:00"
    )
