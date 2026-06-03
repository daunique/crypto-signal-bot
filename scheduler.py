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

# ── Family rotation tracker ───────────────────────────────────────────────────
# Persisted in memory across candles. Updated immediately after a signal fires.
# A = BTC-USDT + ETH-USDT | B = DOGE-USDT + SOL-USDT | C = XRP-USDT + BNB-USDT
_FAMILY_MAP = {
    "BTC-USDT": "A", "ETH-USDT": "A",
    "DOGE-USDT": "B", "SOL-USDT": "B",
    "XRP-USDT": "C", "BNB-USDT": "C",
}
_last_fired_family = None  # set immediately after every signal insert


def _ctx():
    from app import app
    return app.app_context()


def _models_ready():
    """Check if models are trained by inspecting signal_engine._models dict."""
    try:
        from signal_engine import _models, SYMBOLS
        return len(_models) >= len(SYMBOLS)
    except Exception:
        return False


def job_generate_signal():
    """
    Fires at :00, :15, :30, :45 UTC — candle OPEN.
    Generates ONE signal, places order, saves to DB.
    """
    logger.info("[GENERATE] Job fired")
    if not _models_ready():
        logger.info("[GENERATE] Models not ready yet — skipping this candle")
        return
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
            max_cp        = settings.max_contract_price
            min_conf      = settings.min_confidence

            # ── Incremental Martingale position sizing ───────────────────
            # Uses the user-defined sequence stored in settings.martingale_sequence
            # (comma-separated, e.g. "1,1.5,2,3,4.5"). streak index into that list.
            base_size = settings.position_size
            if settings.use_martingale:
                streak = int(settings.martingale_streak or 0)
                cap    = int(settings.martingale_cap or 10)
                streak = min(streak, cap - 1)

                # Parse custom sequence — fall back to default if malformed
                _raw_seq = settings.martingale_sequence or "1,1.5,2,3,4.5,6.7"
                try:
                    _MARTINGALE_SEQUENCE = [round(float(x.strip()), 1)
                                            for x in _raw_seq.split(",") if x.strip()]
                    if not _MARTINGALE_SEQUENCE:
                        raise ValueError("empty")
                except Exception:
                    _MARTINGALE_SEQUENCE = [1.0, 1.5, 2.0, 3.0, 4.5, 6.7]

                if streak < len(_MARTINGALE_SEQUENCE):
                    position_size = _MARTINGALE_SEQUENCE[streak]
                else:
                    position_size = _MARTINGALE_SEQUENCE[-1]

                logger.info(
                    f"[GENERATE] Martingale ON | streak={streak}/{cap} "
                    f"→ stake=${position_size:.1f} (seq={_raw_seq})"
                )
            else:
                position_size = base_size

            # ── 2-candle cooldown after 2 consecutive losses — DISABLED ────────
            # cd_remaining = int(settings.cooldown_remaining or 0)
            # if cd_remaining > 0:
            #     settings.cooldown_remaining = cd_remaining - 1
            #     db.session.commit()
            #     logger.info(
            #         f"[GENERATE] COOLDOWN active — sitting out this candle "
            #         f"({cd_remaining} candle(s) remaining after this one). "
            #         f"Resuming in {cd_remaining - 1} more candle(s)."
            #     )
            #     return

            logger.info(f"[GENERATE] Starting | mode={mode} min_conf={min_conf}")

            # Clear slug/market cache at start of each candle so we always
            # discover the freshest market, not a stale one from 15min ago.
            try:
                from limitless_executor import _slug_cache, _market_cache
                _slug_cache.clear()
                _market_cache.clear()
            except Exception:
                pass

            # ── SOL-USDT 2-hour cooldown ────────────────────────────────────
            # After any SOL signal fires, suppress SOL from the candidate pool
            # for 8 candles (2 hours). SOL is only re-eligible if 2 hours have
            # passed since the last SOL signal AND it qualifies at that time.
            _sol_blocked = False
            try:
                _last_sol = Signal.query.filter(
                    Signal.symbol == "SOL-USDT"
                ).order_by(Signal.candle_open_time.desc()).first()
                if _last_sol:
                    _now_utc   = datetime.now(timezone.utc).replace(tzinfo=None)
                    _sol_age   = _now_utc - _last_sol.candle_open_time
                    if _sol_age < timedelta(hours=2):
                        _sol_blocked = True
                        _sol_mins_left = int((timedelta(hours=2) - _sol_age).total_seconds() / 60)
                        logger.info(
                            f"[GENERATE] SOL-USDT on 2-hour cooldown — "
                            f"{_sol_mins_left}m remaining since last signal"
                        )
            except Exception as _se:
                logger.warning(f"[GENERATE] SOL cooldown check error: {_se}")

            # ── Pair family rotation ──────────────────────────────────────────
            # Uses module-level _last_fired_family (set immediately when a signal
            # is inserted). Falls back to DB query only on cold start (when the
            # variable is None after a redeploy).
            global _last_fired_family
            _excluded_families = None
            try:
                _current_family = _last_fired_family
                if _current_family is None:
                    # Cold start — seed from DB
                    _seed_sig = Signal.query.order_by(Signal.created_at.desc()).first()
                    if _seed_sig:
                        _seed_sym = _seed_sig.symbol or _seed_sig.pair or ""
                        _current_family = _FAMILY_MAP.get(_seed_sym)
                        _last_fired_family = _current_family
                        logger.info(f"[GENERATE] Cold-start: seeded last family={_current_family} from DB ({_seed_sym})")

                if _current_family:
                    _excluded_families = [_current_family]
                    logger.info(f"[GENERATE] Excluding family {_current_family} — must rotate to another family")
                else:
                    logger.info("[GENERATE] No family exclusion — first signal ever")
            except Exception as _fe:
                logger.warning(f"[GENERATE] Family rotation check error: {_fe}")

            # Get signal from engine first — it computes the correct candle boundary
            sig = pick_best_signal(
                min_confidence=min_conf,
                exclude=["SOL-USDT"] if _sol_blocked else None,
                excluded_families=_excluded_families,
            )
            if not sig:
                logger.info("[GENERATE] No qualifying signal this candle")
                return

            candle_open  = sig['candle_open_time']
            candle_close = sig['candle_close_time']

            # ── Global duplicate guard — no new signal while any is PENDING ──
            # Safety valve: if a signal has been PENDING for >30 minutes it's
            # stale (missed resolution window due to a redeploy or crash).
            # Force-resolve it as UNKNOWN so it doesn't block forever.
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            stale   = Signal.query.filter(
                Signal.outcome == "PENDING",
                Signal.candle_close_time <= now_utc - timedelta(minutes=30),
            ).all()
            for _stale in stale:
                _stale.outcome = "UNKNOWN"
                logger.warning(
                    f"[GENERATE] Force-resolved stale PENDING signal "
                    f"id={_stale.id} ({_stale.symbol}) — "
                    f"candle closed at {_stale.candle_close_time}, >30min ago"
                )
            if stale:
                db.session.commit()

            any_pending = Signal.query.filter(Signal.outcome == "PENDING").first()
            if any_pending:
                _pending_symbol = any_pending.symbol or any_pending.pair or ""
                _pending_family = {
                    "BTC-USDT": "A", "ETH-USDT": "A",
                    "DOGE-USDT": "B", "SOL-USDT": "B",
                    "XRP-USDT": "C", "BNB-USDT": "C",
                }.get(_pending_symbol)
                if _pending_family:
                    # Add pending family to exclusion list (don't fire same family twice)
                    _excluded_families = list(set((_excluded_families or []) + [_pending_family]))
                    logger.info(
                        f"[GENERATE] Signal id={any_pending.id} "
                        f"({_pending_symbol}) still PENDING — "
                        f"excluding its family {_pending_family} from candidates"
                    )
                else:
                    logger.info(
                        f"[GENERATE] Blocked — signal id={any_pending.id} "
                        f"({_pending_symbol}) is still PENDING. No new signal until it resolves."
                    )
                    return

            # ── Place order — retry every 5s for up to 2 minutes (24 attempts) ──
            # Stops immediately on first success. Gives the market time to open
            # after the candle boundary without wasting the full 15-min window.
            import time as _time
            ORDER_MAX_ATTEMPTS = 24
            ORDER_RETRY_DELAY  = 5    # seconds between retries
            order = {"success": False, "error": "not attempted"}

            # ── USDC balance check before placing order ─────────────────────
            # Prevents wasting retries on guaranteed-to-fail collateral errors.
            # If balance is below base stake, skip trade entirely.
            # If balance is below martingale stake, reduce to available balance.
            if mode == "live":
                try:
                    from limitless_executor import check_usdc_approval
                    # Known Limitless exchange contract on Base — same for all markets
                    _EXCHANGE = "0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5"
                    _bal_info = check_usdc_approval(_EXCHANGE)
                    _usdc_bal = float(_bal_info.get("usdc_balance", 0))
                    _base_size = float(settings.position_size or 1.0)
                    if _usdc_bal < _base_size:
                        logger.error(
                            f"[GENERATE] SKIPPING — USDC balance ${_usdc_bal:.2f} "
                            f"is below base stake ${_base_size:.2f}. "
                            f"Top up your wallet."
                        )
                        return
                    if _usdc_bal < position_size:
                        logger.warning(
                            f"[GENERATE] Balance ${_usdc_bal:.2f} < martingale stake "
                            f"${position_size:.2f} — reducing stake to available balance"
                        )
                        position_size = round(_usdc_bal, 2)
                except Exception as _be:
                    logger.warning(f"[GENERATE] Balance check failed: {_be} — proceeding anyway")

            # Direction toggle — two independent inversion sources:
            #   1. settings.invert_direction  — global toggle (flips all pairs)
            #   2. sig['invert']              — per-pair flag from PAIR_CONFIG
            # All pairs: XOR — flip if exactly one source says invert.
            # SOL-USDT is now normal (not inverted) — invert=False in PAIR_CONFIG.
            _global_invert = bool(getattr(settings, 'invert_direction', False))
            _pair_invert   = bool(sig.get('invert', False))

            _should_invert = _global_invert ^ _pair_invert

            if _should_invert:
                _trade_direction = "DOWN" if sig['direction'] == "UP" else "UP"
            else:
                _trade_direction = sig['direction']

            for _attempt in range(1, ORDER_MAX_ATTEMPTS + 1):
                order = execute_order(sig['symbol'], _trade_direction, mode,
                                      position_size, max_cp)
                if order.get("success"):
                    # Order placed — do NOT retry regardless of remaining attempts
                    break

                err_body = str(order.get("api_response", "") or order.get("error", ""))
                logger.warning(
                    f"[GENERATE] Order attempt {_attempt}/{ORDER_MAX_ATTEMPTS} FAILED | "
                    f"{sig['symbol']} | error={order.get('error','unknown')}"
                )

                # Insufficient collateral is a permanent failure — retrying won't help.
                # Reset martingale streak to 0 so the next trade uses base stake.
                if "insufficient collateral" in err_body.lower():
                    logger.error(
                        f"[GENERATE] Insufficient collateral — aborting retries. "
                        f"Martingale streak reset to 0. Top up your USDC balance."
                    )
                    try:
                        from models import Settings as _CBSettings
                        _cbs = _CBSettings.query.first()
                        if _cbs and _cbs.use_martingale:
                            _cbs.martingale_streak = 0
                            db.session.commit()
                    except Exception as _cbe:
                        logger.warning(f"[GENERATE] Streak reset error: {_cbe}")
                    break  # stop retrying immediately

                if _attempt < ORDER_MAX_ATTEMPTS:
                    logger.info(
                        f"[GENERATE] Retrying in {ORDER_RETRY_DELAY}s "
                        f"(attempt {_attempt + 1}/{ORDER_MAX_ATTEMPTS})…"
                    )
                    _time.sleep(ORDER_RETRY_DELAY)  # 5s gap catches retracements

            contracts      = order.get("contracts", 0)
            contract_price = order.get("price_per_contract", max_cp)
            order_id       = order.get("order_id") if order.get("success") else None

            if order.get("success"):
                logger.info(
                    f"[GENERATE] Order ✓ | {sig['symbol']} {_trade_direction} (signal={sig['direction']}) "
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
                pair              = sig['symbol'],         # legacy column kept in sync
                direction         = sig['direction'],      # legacy column kept in sync
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
                condition_id      = order.get("condition_id") if order.get("success") else None,
                position_size     = position_size,
                contracts_bought  = contracts,
                contract_price    = contract_price,
                outcome           = "PENDING",
                telegram_sent     = False,
            )
            db.session.add(signal_obj)
            db.session.commit()

            # ── Update family tracker immediately ────────────────────────────────
            _last_fired_family = _FAMILY_MAP.get(sig['symbol'])
            logger.info(f"[GENERATE] Family tracker updated → {_last_fired_family} ({sig['symbol']})")

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
            from models import Signal, DailyStats, ShadowBalance, Settings
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

                        # ── Winnings notification (manual redemption required) ──
                        # POST /portfolio/redeem only works with Limitless
                        # server-managed wallets. This bot uses a self-custody
                        # wallet (HMAC-signed orders), so auto-claim via API
                        # always returns 403. Winnings must be redeemed manually
                        # on the Limitless web UI or directly on-chain.
                        if outcome == "WIN" and sig.mode == "live":
                            logger.info(
                                f"[RESOLVE] WIN ✓ {sym} — redeem manually on Limitless | "
                                f"slug={sig.market_slug} | "
                                f"conditionId={getattr(sig, 'condition_id', 'N/A')}"
                            )

                        # ── Martingale streak update ──────────────────────
                        # Streak moves ONLY when Limitless confirms an on-chain fill.
                        # Three possible outcomes:
                        #
                        #   OKX=WIN  + filled  → streak RESETS to 0 (real profit)
                        #   OKX=LOSS + filled  → streak ADVANCES (real loss, stake ↑)
                        #   any OKX result + NOT filled → streak FROZEN (same stake next trade)
                        #
                        # "Frozen" means the streak integer is untouched so the
                        # same stake fires again next candle. Neither penalised
                        # nor rewarded — the sequence just waits for a real fill.
                        try:
                            from models import Settings as _MSettings
                            from limitless_executor import check_order_filled
                            _ms = _MSettings.query.first()
                            if _ms and _ms.use_martingale:
                                cap       = int(_ms.martingale_cap or 10)
                                # Parse custom sequence
                                _raw_seq = _ms.martingale_sequence or "1,1.5,2,3,4.5,6.7"
                                try:
                                    _MART_SEQ = [round(float(x.strip()), 1)
                                                 for x in _raw_seq.split(",") if x.strip()]
                                    if not _MART_SEQ:
                                        raise ValueError
                                except Exception:
                                    _MART_SEQ = [1.0, 1.5, 2.0, 3.0, 4.5, 6.7]
                                _order_id = sig.order_id
                                _mkt_slug = sig.market_slug

                                # Shadow or missing order — no on-chain fill possible
                                if not _order_id or str(_order_id).startswith("shadow_"):
                                    _was_filled  = False
                                    _fill_status = "NO_ORDER_ID"
                                else:
                                    _fill_check  = check_order_filled(_mkt_slug, _order_id)
                                    _was_filled  = _fill_check.get("filled", False)
                                    _fill_status = _fill_check.get("status", "UNKNOWN")

                                # Persist Limitless fill status on the signal record
                                try:
                                    from models import Signal as _SigModel
                                    _sig_rec = _SigModel.query.get(sig.id)
                                    if _sig_rec:
                                        if str(_order_id).startswith("shadow_") or _fill_status == "NO_ORDER_ID":
                                            _sig_rec.limitless_fill = "NEUTRAL"
                                        else:
                                            _sig_rec.limitless_fill = "FILLED" if _was_filled else "UNFILLED"
                                        db.session.commit()
                                except Exception as _fe:
                                    logger.warning(f"[RESOLVE] limitless_fill persist error: {_fe}")

                                if not _was_filled:
                                    # Not confirmed on-chain — freeze streak, carry stake forward
                                    cur_streak = int(_ms.martingale_streak or 0)
                                    cur_stake  = _MART_SEQ[min(cur_streak, len(_MART_SEQ) - 1)]
                                    logger.warning(
                                        f"[RESOLVE] Martingale FROZEN — order NOT filled on Limitless "
                                        f"(status={_fill_status}, order={_order_id}, OKX={outcome}) | "
                                        f"streak stays at {cur_streak} → same stake ${cur_stake:.2f} carried to next trade"
                                    )
                                    # streak intentionally not modified

                                elif outcome == "WIN":
                                    # Confirmed fill + OKX WIN → real profit → reset streak
                                    _ms.martingale_streak = 0
                                    logger.info(
                                        f"[RESOLVE] Martingale streak RESET — "
                                        f"WIN confirmed FILLED (order={_order_id}) | "
                                        f"next stake=${_MART_SEQ[0]:.2f}"
                                    )

                                else:  # LOSS + filled
                                    # Confirmed fill + OKX LOSS → real loss → advance streak
                                    new_streak = int(_ms.martingale_streak or 0) + 1
                                    if new_streak >= cap:
                                        _ms.martingale_streak = 0
                                        logger.warning(
                                            f"[RESOLVE] Martingale CAP reached ({cap} losses) — "
                                            f"streak RESET to 0 | next stake=${_MART_SEQ[0]:.2f}"
                                        )
                                    else:
                                        _ms.martingale_streak = new_streak
                                        next_stake = _MART_SEQ[min(new_streak, len(_MART_SEQ) - 1)]
                                        logger.info(
                                            f"[RESOLVE] Martingale streak={new_streak} — "
                                            f"LOSS confirmed FILLED | next stake=${next_stake:.2f}"
                                        )
                        except Exception as _me:
                            logger.warning(f"[RESOLVE] Martingale update error: {_me}")

                        # ── Cooldown counter update — DISABLED ───────────────
                        # (2-loss-streak cooldown commented out)
                        # try:
                        #     _resolve_settings = Settings.query.first()
                        #     if _resolve_settings:
                        #         _inv_mode = bool(getattr(_resolve_settings, 'invert_direction', False))
                        #         if outcome == "WIN":
                        #             _resolve_settings.cooldown_loss_count = 0
                        #             if _inv_mode:
                        #                 new_win_count = int(getattr(_resolve_settings, 'cooldown_win_count', 0) or 0) + 1
                        #                 if new_win_count >= 2:
                        #                     _resolve_settings.cooldown_remaining = 2
                        #                     _resolve_settings.cooldown_win_count = 1
                        #                 else:
                        #                     _resolve_settings.cooldown_win_count = new_win_count
                        #             else:
                        #                 _resolve_settings.cooldown_win_count = 0
                        #         else:  # LOSS
                        #             _resolve_settings.cooldown_win_count = 0
                        #             new_loss_count = int(_resolve_settings.cooldown_loss_count or 0) + 1
                        #             if new_loss_count >= 2:
                        #                 _resolve_settings.cooldown_remaining  = 2
                        #                 _resolve_settings.cooldown_loss_count = 1
                        #             else:
                        #                 _resolve_settings.cooldown_loss_count = new_loss_count
                        #         db.session.commit()
                        # except Exception as _cde:
                        #     logger.warning(f"[RESOLVE] Cooldown update error: {_cde}")

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
            logger.info("[RETRAIN] 4-hourly retrain starting — rolling 960-candle window...")
            retrain_all(limit=960)
            logger.info("[RETRAIN] Done — all 6 models updated")
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
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="0,15,30,45", second="1"),
        id="generate",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )

    # Daily summary at 23:59 UTC
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=23, minute=59),
        id="daily",
        replace_existing=True,
    )

    # 4-hourly model retrain — keeps models current with intraday regime shifts.
    # Runs at 02:00, 06:00, 10:00, 14:00, 18:00, 22:00 UTC.
    # Each retrain uses rolling 960-candle window (last 10 days).
    # Scheduled outside the :00/:15/:30/:45 signal boundaries to avoid overlap.
    scheduler.add_job(
        job_retrain,
        CronTrigger(hour="2,6,10,14,18,22", minute=5),
        id="retrain",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        "[SCHEDULER] Started | "
        "resolve@:00/:15/:30/:45+0s | "
        "generate@:00/:15/:30/:45+1s | "
        "daily@23:59 | retrain@02/06/10/14/18/22:05-UTC"
    )
