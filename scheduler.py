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
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")

# Throttle for the early fill-check inside job_track_best_dip (runs every 3s,
# but we only want to hit the Limitless fill-status API every ~10s).
_last_early_fill_check_ts = 0.0


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

            # ── Cooldown after 2 consecutive losses (manual toggle) ────────
            # Only active when settings.use_cooldown is True (user opt-in).
            # cooldown_remaining is decremented each candle regardless of
            # whether a signal fires, so it counts wall-clock candles not trades.
            if bool(getattr(settings, 'use_cooldown', False)):
                cd_remaining = int(settings.cooldown_remaining or 0)
                if cd_remaining > 0:
                    settings.cooldown_remaining = cd_remaining - 1
                    db.session.commit()
                    logger.info(
                        f"[GENERATE] COOLDOWN active — sitting out this candle "
                        f"({cd_remaining} candle(s) remaining after this one). "
                        f"Resuming in {cd_remaining - 1} more candle(s)."
                    )
                    return

            logger.info(f"[GENERATE] Starting | mode={mode} min_conf={min_conf}")

            # Clear slug/market cache at start of each candle so we always
            # discover the freshest market, not a stale one from 15min ago.
            try:
                from limitless_executor import _slug_cache, _market_cache
                _slug_cache.clear()
                _market_cache.clear()
            except Exception:
                pass

            # ── Stop loss balance check ──────────────────────────────────────────
            # If stop_loss_balance is set, halt trading when balance hits that level.
            # Shadow mode checks shadow balance; live mode checks USDC wallet balance.
            _sl_balance = getattr(settings, 'stop_loss_balance', None)
            if _sl_balance and float(_sl_balance) > 0:
                try:
                    if mode == 'shadow':
                        _sb = ShadowBalance.query.first()
                        _cur_bal = float(_sb.balance) if _sb else 1000.0
                    else:
                        from limitless_executor import check_usdc_approval
                        _EXCHANGE = '0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5'
                        _bal_info = check_usdc_approval(_EXCHANGE)
                        _cur_bal  = float(_bal_info.get('usdc_balance', 9999))
                    if _cur_bal <= float(_sl_balance):
                        logger.warning(
                            '[GENERATE] STOP LOSS HIT — balance $%.2f <= threshold $%.2f. '
                            'Trading halted. Adjust stop-loss in Settings to resume.',
                            _cur_bal, _sl_balance
                        )
                        return
                    else:
                        logger.info('[GENERATE] Stop-loss OK — balance $%.2f > threshold $%.2f', _cur_bal, _sl_balance)
                except Exception as _sle:
                    logger.warning('[GENERATE] Stop-loss check error: %s — proceeding', _sle)


            # ── Per-pair loss cooldown ───────────────────────────────────────────
            _pair_cooldown_excludes = []
            try:
                import json as _json
                _pcd_raw  = getattr(settings, 'pair_loss_cooldowns', '{}') or '{}'
                # Guard: SQLAlchemy may return a dict if psycopg2 auto-parses TEXT as JSON
                _pcd      = _pcd_raw if isinstance(_pcd_raw, dict) else _json.loads(_pcd_raw)
                _pcd_next = {}
                for _pair, _pcd_state in _pcd.items():
                    _rem = int(_pcd_state.get('candles_remaining', 0))
                    if _rem > 0:
                        _pair_cooldown_excludes.append(_pair)
                        _pcd_next[_pair] = {'candles_remaining': _rem - 1, 'tier': _pcd_state.get('tier', 'T2')}
                        logger.info('[GENERATE] Pair cooldown: %s suppressed (%d candle(s) left)', _pair, _rem)
                settings.pair_loss_cooldowns = _json.dumps(_pcd_next)
                db.session.commit()
            except Exception as _pcde:
                logger.warning('[GENERATE] Pair cooldown read error: %s', _pcde)
                _pair_cooldown_excludes = []

            # ── Pair family rotation ──────────────────────────────────────────
            # Family groupings (user-defined):
            #   Family A: BTC-USDT + ETH-USDT
            #   Family B: DOGE-USDT + SOL-USDT
            #   Family C: XRP-USDT + BNB-USDT
            #
            # Gated by settings.use_family_rotation (Settings model, default
            # False). OFF by default: every family is eligible every candle,
            # same as today's pick_best_signal() behavior with no rotation
            # applied. ON: after a signal fires from a family, the next
            # candle must come from a different family, preventing
            # consecutive BTC/ETH-style spam. Toggle lives in Settings UI.
            _preferred_families = None
            _excluded_families  = None
            _FAMILY_MAP = {
                "BTC-USDT":  "A", "ETH-USDT":  "A",
                "DOGE-USDT": "B", "SOL-USDT":  "B",
                "XRP-USDT":  "C", "BNB-USDT":  "C",
            }
            if getattr(settings, 'use_family_rotation', False):
                try:
                    # Look at last signal overall (including PENDING — covers
                    # the 15-min window while the current signal is waiting
                    # to resolve).
                    _last_resolved = Signal.query.filter(
                        Signal.outcome.in_(['WIN', 'LOSS', 'UNKNOWN'])
                    ).order_by(Signal.candle_open_time.desc()).first()

                    _last_any = Signal.query.order_by(
                        Signal.candle_open_time.desc()
                    ).first()

                    # Use the more recent of the two
                    _ref_sig = None
                    if _last_any and _last_resolved:
                        _ref_sig = _last_any if (
                            _last_any.candle_open_time >= _last_resolved.candle_open_time
                        ) else _last_resolved
                    else:
                        _ref_sig = _last_any or _last_resolved

                    if _ref_sig:
                        _excl = _FAMILY_MAP.get(_ref_sig.symbol)
                        _excluded_families  = [_excl] if _excl else None
                        _preferred_families = [f for f in ['A', 'B', 'C'] if f != _excl]
                        logger.info(
                            '[GENERATE] Family rotation ON | last=%s family=%s → '
                            'excluding family %s | eligible=%s',
                            _ref_sig.symbol, _excl,
                            _excluded_families, _preferred_families
                        )
                    else:
                        _excluded_families  = None
                        _preferred_families = None
                        logger.info('[GENERATE] Family rotation ON | no previous signal — all families eligible')
                except Exception as _fe:
                    logger.warning('[GENERATE] Family rotation check error: %s — allowing all families', _fe)
                    _excluded_families  = None
                    _preferred_families = None
            else:
                logger.debug('[GENERATE] Family rotation OFF (use_family_rotation=False) — all families eligible')

            # ── Rule 2: Directional Saturation Filter ────────────────────────
            # If the same direction has lost ≥3 times in the last 6 signals,
            # raise the confidence floor for that direction.
            # This prevents the engine chasing a losing directional trend with
            # low-conviction signals.
            #
            # FIX (signal_engine v5 / RSI(2) swap): the old ML engine's
            # confidence was a model probability that could exceed 0.67.
            # The new engine's confidence is a fixed backtested win rate per
            # pair/direction, which only ranges ~0.558-0.612 (see
            # signal_engine._BACKTEST_WIN_RATE). A 0.67 floor is therefore
            # unreachable by ANY signal under the new engine — once Rule 2
            # tripped, that direction would be blocked permanently (in
            # practice, until enough time passed for the losses to age out
            # of the 6-event window on their own, making the "block" do
            # nothing extra beyond what the window expiry already does).
            # 0.60 sits near the top of the engine's real range, so it still
            # meaningfully raises the bar without being unreachable.
            _blocked_directions = {}
            try:
                import json as _json_r2
                _sat_raw = getattr(settings, 'dir_saturation_history', '[]') or '[]'
                _sat_history = _sat_raw if isinstance(_sat_raw, list) else _json_r2.loads(_sat_raw)
                _window = _sat_history[-6:]  # last 6 signals
                for _chk_dir in ('UP', 'DOWN'):
                    _dir_losses = sum(
                        1 for e in _window
                        if e.get('dir') == _chk_dir and e.get('result') == 'LOSS'
                    )
                    if _dir_losses >= 3:
                        _blocked_directions[_chk_dir] = 0.60
                        logger.info(
                            '[GENERATE] Rule2 dir-saturation: %s has %d/6 losses → '
                            'floor raised to 0.60',
                            _chk_dir, _dir_losses
                        )
            except Exception as _r2e:
                logger.warning('[GENERATE] Rule2 saturation check error: %s', _r2e)
                _blocked_directions = {}

            # Get signal from engine first — it computes the correct candle boundary
            _exclude_list = _pair_cooldown_excludes or None
            sig = pick_best_signal(
                min_confidence=min_conf,
                exclude=_exclude_list,
                preferred_families=_preferred_families,
                excluded_families=_excluded_families,
                blocked_directions=_blocked_directions if _blocked_directions else None,
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
                logger.info(
                    f"[GENERATE] Blocked — signal id={any_pending.id} "
                    f"({any_pending.symbol} {any_pending.signal_direction}) "
                    f"is still PENDING. No new signal until it resolves."
                )
                return

            # ── Place order — retry every 5s for up to 2 minutes (24 attempts) ──
            # Stops immediately on first success. Gives the market time to open
            # after the candle boundary without wasting the full 15-min window.
            import time as _time
            ORDER_MAX_ATTEMPTS = 24
            ORDER_RETRY_DELAY  = 2    # seconds between order retries (market already found)
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

            # Direction: only per-pair invert flag from PAIR_CONFIG applies.
            # The global invert toggle has been removed. All pairs including SOL
            # now trade in their signal direction (SOL invert removed per v3).
            _pair_invert = bool(sig.get('invert', False))
            if _pair_invert:
                _trade_direction = "DOWN" if sig['direction'] == "UP" else "UP"
            else:
                _trade_direction = sig['direction']

            # ── Platform toggles — read from settings ────────────────────────
            _ltl_val        = getattr(settings, 'use_limitless',  None)
            _poly_val       = getattr(settings, 'use_polymarket', None)
            _use_limitless  = bool(_ltl_val  if _ltl_val  is not None else True)
            _use_polymarket = bool(_poly_val if _poly_val is not None else False)
            _poly_size      = float(getattr(settings, 'poly_position_size', 10.0) or 10.0)
            _poly_max_price = float(getattr(settings, 'poly_max_price', 0.50) or 0.50)

            # At least one platform must be active — fall back to Limitless
            if not _use_limitless and not _use_polymarket:
                logger.warning("[GENERATE] No platform enabled — defaulting to Limitless")
                _use_limitless = True

            # ── Parallel execution: both platforms run in threads ─────────────
            # Polymarket: market is ALWAYS available at t=0 of new candle → fires immediately.
            # Limitless:  new market appears within ~60s   → discovery loop runs in thread.
            # Both threads run concurrently so neither waits on the other.
            # Results are joined before saving the signal to DB.
            import threading as _threading

            order          = {"success": False, "error": "Limitless disabled"}
            order_id       = None
            contracts      = 0
            contract_price = max_cp
            _poly_order    = {"success": False, "error": "Polymarket disabled"}
            _poly_order_id = None

            # ── No-execute pairs: signal fires normally but NO live order ────
            # XRP-USDT (and any other pairs in this list) will generate signals,
            # appear on the dashboard, and track dip/resolve outcomes —
            # but the actual Limitless / Polymarket orders are skipped.
            _no_execute = []
            try:
                import json as _nep_j
                _nep_raw = getattr(settings, 'no_execute_pairs', '[]') or '[]'
                _no_execute = _nep_raw if isinstance(_nep_raw, list) else _nep_j.loads(_nep_raw)
            except Exception as _nepe:
                logger.warning('[GENERATE] no_execute_pairs read error: %s', _nepe)
                _no_execute = []

            _is_no_execute_pair = sig['symbol'] in _no_execute
            if _is_no_execute_pair:
                logger.info(
                    '[GENERATE] %s is in no_execute_pairs — signal fires but NO live order will be placed.',
                    sig['symbol']
                )

            def _run_limitless():
                nonlocal order, order_id, contracts, contract_price
                for _attempt in range(1, ORDER_MAX_ATTEMPTS + 1):
                    _result = execute_order(sig['symbol'], _trade_direction, mode,
                                            position_size, max_cp)
                    if _result.get("success"):
                        order          = _result
                        contracts      = _result.get("contracts", 0)
                        contract_price = _result.get("price_per_contract", max_cp)
                        order_id       = _result.get("order_id")
                        logger.info(
                            f"[GENERATE][LTL] Order ✓ | {sig['symbol']} {_trade_direction} "
                            f"${position_size} | {contracts} contracts @ ${contract_price} "
                            f"id={order_id}"
                        )
                        return
                    _err_body = str(_result.get("api_response", "") or _result.get("error", ""))
                    logger.warning(
                        f"[GENERATE][LTL] Attempt {_attempt}/{ORDER_MAX_ATTEMPTS} FAILED | "
                        f"{sig['symbol']} | error={_result.get('error','unknown')}"
                    )
                    if "insufficient collateral" in _err_body.lower():
                        logger.error("[GENERATE][LTL] Insufficient collateral — aborting")
                        try:
                            from models import Settings as _CBSettings
                            _cbs = _CBSettings.query.first()
                            if _cbs and _cbs.use_martingale:
                                _cbs.martingale_streak = 0
                                db.session.commit()
                        except Exception as _cbe:
                            logger.warning(f"[GENERATE][LTL] Streak reset: {_cbe}")
                        break
                    if _attempt < ORDER_MAX_ATTEMPTS:
                        _time.sleep(ORDER_RETRY_DELAY)
                order = _result
                logger.error(
                    f"[GENERATE][LTL] Order FAILED after {ORDER_MAX_ATTEMPTS} attempts | "
                    f"{sig['symbol']} | last_error={order.get('error','unknown')}"
                )

            def _run_polymarket():
                nonlocal _poly_order, _poly_order_id
                try:
                    from polymarket_executor import execute_order as _poly_exec
                    # Polymarket market is always available immediately —
                    # attempt once with fast retries (3× × 3s = 9s max)
                    _POLY_ATTEMPTS = 10
                    _POLY_DELAY    = 3   # seconds
                    for _pa in range(1, _POLY_ATTEMPTS + 1):
                        _r = _poly_exec(sig['symbol'], _trade_direction, mode,
                                        _poly_size, _poly_max_price)
                        if _r.get("success"):
                            _poly_order    = _r
                            _poly_order_id = _r.get("order_id")
                            logger.info(
                                f"[GENERATE][POLY] Order ✓ | {sig['symbol']} {_trade_direction} "
                                f"${_poly_size} @ ${_poly_max_price} id={_poly_order_id}"
                            )
                            return
                        logger.warning(
                            f"[GENERATE][POLY] Attempt {_pa}/{_POLY_ATTEMPTS} FAILED | "
                            f"{sig['symbol']} | error={_r.get('error','unknown')}"
                        )
                        if _pa < _POLY_ATTEMPTS:
                            _time.sleep(_POLY_DELAY)
                    _poly_order = _r
                    logger.error(
                        f"[GENERATE][POLY] Order FAILED | {sig['symbol']} | "
                        f"last_error={_poly_order.get('error','unknown')}"
                    )
                except Exception as _pe:
                    logger.error("[GENERATE][POLY] Exception: %s", _pe, exc_info=True)
                    _poly_order = {"success": False, "error": str(_pe)}

            # Start threads — skipped for no_execute_pairs
            _threads = []
            if _use_limitless and not _is_no_execute_pair:
                _t_ltl = _threading.Thread(target=_run_limitless, name="ltl-order", daemon=True)
                _threads.append(_t_ltl)
                _t_ltl.start()
            elif _is_no_execute_pair:
                # Synthetic SHADOW result so the signal still resolves correctly
                order = {"success": True, "order_id": None,
                         "contracts": 0, "price_per_contract": max_cp,
                         "status": "NO_EXECUTE",
                         "note": f"{sig['symbol']} is in no_execute_pairs — signal only"}
                logger.info('[GENERATE] %s no-execute: synthetic order created', sig['symbol'])

            if _use_polymarket and not _is_no_execute_pair:
                _t_poly = _threading.Thread(target=_run_polymarket, name="poly-order", daemon=True)
                _threads.append(_t_poly)
                _t_poly.start()

            # Join both threads — wait for whichever is slowest (Limitless ~10-60s)
            # Timeout = 120s absolute ceiling so we never hang the scheduler
            for _t in _threads:
                _t.join(timeout=120)

            logger.info(
                "[GENERATE] Both platforms done | LTL=%s POLY=%s",
                "✓" if order.get("success") else "✗",
                "✓" if _poly_order.get("success") else "✗",
            )

            # ── Save signal ──────────────────────────────────────────────────────
            # Pull slug/condition_id from order response if available (live mode).
            # In shadow mode the order response has no slug, so we start with None
            # and patch it in a background thread — this way the DB commit is
            # NEVER blocked by Limitless market discovery retries (up to 150s).
            _mkt_slug = order.get("slug")         if order.get("success") else None
            _cond_id  = order.get("condition_id") if order.get("success") else None

            # Also check cache immediately (may already be populated from
            # a prior candle's discovery run) so slug is set when possible.
            if not _mkt_slug:
                try:
                    from limitless_executor import _slug_cache
                    _mkt_slug = _slug_cache.get(sig['symbol']) or None
                except Exception:
                    pass

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
                market_slug       = _mkt_slug,
                condition_id      = _cond_id,
                position_size     = position_size,
                contracts_bought  = contracts,
                contract_price    = contract_price,
                outcome           = "PENDING",
                telegram_sent     = False,
                poly_order_id     = _poly_order_id,
                poly_fill         = ("PENDING" if _poly_order_id else "NEUTRAL"),
            )
            db.session.add(signal_obj)
            db.session.commit()

            # ── Background slug discovery (shadow / live without slug) ──────────
            # If we don't have a slug yet, discover it in a daemon thread so the
            # scheduler is never stalled by Limitless market retry delays.
            # Once found, the signal row is patched and a WS update is emitted.
            if not _mkt_slug:
                _sig_id = signal_obj.id
                _sym    = sig['symbol']

                def _bg_discover_slug(sig_id, symbol):
                    try:
                        from limitless_executor import discover_slug, fetch_market
                        slug = discover_slug(symbol)
                        if not slug:
                            logger.warning(
                                "[GENERATE] BG slug discovery: no 15-min market found for %s", symbol
                            )
                            return
                        cond_id = None
                        mkt_data = fetch_market(slug)
                        if mkt_data:
                            cond_id = (
                                mkt_data.get("conditionId")
                                or mkt_data.get("condition_id")
                                or mkt_data.get("ctfConditionId")
                                or mkt_data.get("condId")
                            )
                        logger.info(
                            "[GENERATE] BG slug discovery — %s slug=%s conditionId=%s",
                            symbol, slug, cond_id
                        )
                        # Patch the signal row inside a fresh app context
                        with _ctx():
                            from extensions import db as _db, socketio as _sio
                            from models import Signal as _Signal
                            _s = _Signal.query.get(sig_id)
                            if _s:
                                _s.market_slug  = slug
                                _s.condition_id = cond_id
                                _db.session.commit()
                                try:
                                    _sio.emit("signal_updated", _s.to_dict())
                                except Exception:
                                    pass
                    except Exception as _bge:
                        logger.warning("[GENERATE] BG slug discovery error: %s", _bge)

                _t_slug = _threading.Thread(
                    target=_bg_discover_slug,
                    args=(_sig_id, _sym),
                    name="bg-slug-discovery",
                    daemon=True,
                )
                _t_slug.start()

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

            # ── Rule 2: record this signal in saturation history (result=PENDING) ──
            try:
                import json as _json_sat
                _sat_raw2 = getattr(settings, 'dir_saturation_history', '[]') or '[]'
                _sat_hist2 = _sat_raw2 if isinstance(_sat_raw2, list) else _json_sat.loads(_sat_raw2)
                _sat_hist2.append({'id': signal_obj.id, 'dir': sig['direction'], 'result': 'PENDING'})
                _sat_hist2 = _sat_hist2[-10:]  # keep last 10 max
                settings.dir_saturation_history = _json_sat.dumps(_sat_hist2)
                db.session.commit()
            except Exception as _sate:
                logger.warning('[GENERATE] Rule2 history append error: %s', _sate)

            logger.info(
                f"[GENERATE] Saved signal id={signal_obj.id} | "
                f"{sig['symbol']} {sig['direction']} conf={sig['confidence']:.3f} | "
                f"candle {candle_open}→{candle_close}"
            )

        except Exception as e:
            logger.error(f"[GENERATE] Unhandled error: {e}", exc_info=True)


def job_track_best_dip():
    """
    Runs every 3s while a signal is PENDING.
    Fetches the Limitless orderbook and records the lowest signal-side %
    seen during the candle as best_entry_pct. Works entirely server-side
    so best_dip is accurate even when the dashboard is closed.

    Price extraction:
      UP   signal → tracks YES% directly from adjustedMidpoint
      DOWN signal → tracks NO%  = 1 - YES_mid  (binary market: YES+NO=1.0)
                    Also reads NO best-ask from bids (NO bids = YES asks inverted)
                    to get the most accurate tradeable NO price.
    """
    import requests as _req
    API = "https://api.limitless.exchange"

    with _ctx():
        try:
            from models import Signal
            from extensions import db
            pending = Signal.query.filter(
                Signal.outcome == "PENDING"
            ).order_by(Signal.candle_open_time.desc()).first()

            if not pending:
                return  # no active signal — nothing to do

            slug = pending.market_slug
            if not slug:
                try:
                    from limitless_executor import _slug_cache
                    sym = pending.symbol
                    slug = (_slug_cache.get(sym)
                            or _slug_cache.get(sym.replace("-USDT", "")))
                    if slug:
                        pending.market_slug = slug
                        db.session.commit()
                except Exception:
                    pass

            if not slug:
                return  # slug not yet discovered — bg thread will fill it

            # Fetch orderbook (3s timeout — runs every 3s so must be fast)
            try:
                r = _req.get(f"{API}/markets/{slug}/orderbook", timeout=3)
                if not r.ok:
                    logger.debug("[DIP_TRACK] orderbook %d for %s", r.status_code, slug)
                    return
                ob = r.json() or {}
            except Exception as _fe:
                logger.debug("[DIP_TRACK] fetch error: %s", _fe)
                return

            # ── Extract YES mid-price (0–1 float) ────────────────────────────
            # adjustedMidpoint is the primary YES price on Limitless.
            # Falls back to midpoint → lastTradePrice → best ask.
            yes_raw = (
                ob.get("adjustedMidpoint")
                or ob.get("midpoint")
                or ob.get("lastTradePrice")
            )
            if yes_raw is None:
                asks = ob.get("asks") or []
                if asks:
                    try:
                        entry = asks[0]
                        yes_raw = float(
                            entry[0] if isinstance(entry, (list, tuple)) else entry
                        )
                    except Exception:
                        pass

            if yes_raw is None:
                logger.debug("[DIP_TRACK] no price in orderbook for %s", slug)
                return

            yes_float = float(yes_raw)
            if yes_float > 1:               # already expressed as percentage
                yes_float = yes_float / 100
            yes_pct = round(yes_float * 100, 2)   # e.g. 65.40

            # ── Compute signal-side % ─────────────────────────────────────────
            # UP   signal: we want the YES token to dip as low as possible
            #              before resolving UP → track YES%
            # DOWN signal: we want the NO  token to dip as low as possible
            # Limitless shows two independent token prices:
            #   UP token   ("Up ↑ X%")   = adjustedMidpoint from orderbook
            #   DOWN token ("Down ↓ X%") = 1 - UP_mid (binary market complement)
            #
            # UP   signal → track UP%   (lowest UP% seen = best limit entry)
            # DOWN signal → track DOWN% (lowest DOWN% seen = best limit entry)
            if pending.signal_direction == "UP":
                signal_pct = yes_pct          # UP token % = adjustedMidpoint
                side_label = "UP"
            else:
                # DOWN token price = 1 - UP_mid on binary market
                down_mid = round(100 - yes_pct, 2)

                # Refinement: derive DOWN ask from best UP bid
                # Best UP bid = highest price someone pays for UP token
                # DOWN ask ≈ 1 - best_UP_bid (cheapest DOWN available to buy)
                bids = ob.get("bids") or []
                if bids:
                    try:
                        best_bid_entry = bids[0]
                        best_up_bid = float(
                            best_bid_entry[0]
                            if isinstance(best_bid_entry, (list, tuple))
                            else best_bid_entry
                        )
                        if best_up_bid > 1:
                            best_up_bid /= 100
                        down_ask = round((1 - best_up_bid) * 100, 2)
                        # Use lower of mid and ask — most conservative dip value
                        signal_pct = min(down_mid, down_ask)
                    except Exception:
                        signal_pct = down_mid
                else:
                    signal_pct = down_mid
                side_label = "DOWN"

            signal_pct = round(signal_pct, 1)

            # ── Update best_entry_pct only if new minimum ─────────────────────
            current_best = pending.best_entry_pct
            if current_best is None or signal_pct < current_best:
                pending.best_entry_pct = signal_pct
                db.session.commit()
                logger.info(
                    "[DIP_TRACK] %s %s — new best_dip=%.1f%% (prev=%s) "
                    "%s%%=%.2f UP%%=%.2f",
                    pending.symbol, pending.signal_direction,
                    signal_pct, current_best,
                    side_label, signal_pct, yes_pct
                )

        except Exception as _e:
            logger.warning("[DIP_TRACK] Unhandled error: %s", _e)

        # ── Early fill check (separate try block — must not break dip tracking) ──
        # Checks Limitless fill status while the signal is still PENDING, so the
        # dashboard's "Checking…" badge resolves to FILLED/UNFILLED within seconds
        # of order placement instead of waiting the full 15-minute candle.
        # Throttled to once every ~10s (this job runs every 3s) to avoid
        # hammering the Limitless API with redundant fill-status calls.
        global _last_early_fill_check_ts
        try:
            import time as _time_efc
            _now_efc = _time_efc.time()
            if _now_efc - _last_early_fill_check_ts >= 10:
                _last_early_fill_check_ts = _now_efc

                from models import Signal as _DipSigModel
                _p = _DipSigModel.query.filter(
                    _DipSigModel.outcome == "PENDING"
                ).order_by(_DipSigModel.candle_open_time.desc()).first()

                if _p and (_p.limitless_fill or "NEUTRAL") == "NEUTRAL":
                    _oid = _p.order_id
                    if _oid and not str(_oid).startswith("shadow_") and _p.market_slug:
                        from limitless_executor import check_order_filled
                        _fc = check_order_filled(_p.market_slug, _oid)
                        if _fc.get("filled"):
                            _p.limitless_fill = "FILLED"
                            db.session.commit()
                            logger.info("[DIP_TRACK][FILL] %s order=%s CONFIRMED FILLED (early check)",
                                        _p.symbol, _oid)
                        # If not yet filled, leave as NEUTRAL — resolve job will
                        # do the authoritative UNFILLED determination at candle close
                        # (an order can still fill in the remaining window).
        except Exception as _efe:
            logger.debug("[DIP_TRACK][FILL] early fill check error: %s", _efe)


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

                        # ── Rule 2: update saturation history with resolved result ──
                        try:
                            import json as _json_sat2
                            _resolve_settings2 = Settings.query.first()
                            if _resolve_settings2:
                                _sat_raw3 = getattr(_resolve_settings2, 'dir_saturation_history', '[]') or '[]'
                                _sat_hist3 = _sat_raw3 if isinstance(_sat_raw3, list) else _json_sat2.loads(_sat_raw3)
                                # Find and update the matching PENDING entry by signal id
                                _updated = False
                                for _e in _sat_hist3:
                                    if _e.get('id') == sig.id and _e.get('result') == 'PENDING':
                                        _e['result'] = outcome
                                        _updated = True
                                        break
                                if not _updated:
                                    # Entry missing — append directly with result
                                    _sat_hist3.append({'id': sig.id, 'dir': sig.signal_direction, 'result': outcome})
                                    _sat_hist3 = _sat_hist3[-10:]
                                _resolve_settings2.dir_saturation_history = _json_sat2.dumps(_sat_hist3)
                                # commit happens below with the rest of the resolve commit
                        except Exception as _sat3e:
                            logger.debug('[RESOLVE] Rule2 history update error: %s', _sat3e)

                        # ── Best-dip: fetch orderbook once at resolve time ────
                        # The frontend gauge tracks best_dip live and POSTs it
                        # when the signal resolves. But if the dashboard wasn't
                        # open, best_entry_pct stays NULL. As a server-side
                        # safety net, we fetch the orderbook at resolve time and
                        # record it if nothing better has already been saved.
                        # best_entry_pct is now tracked continuously by
                        # job_track_best_dip (every 30s). Only do a final
                        # snapshot here if it's still null (signal fired but
                        # tracker hadn't run yet).
                        if sig.best_entry_pct is None and sig.market_slug:
                            try:
                                import requests as _req
                                _ob_url = (
                                    f"https://api.limitless.exchange/markets/"
                                    f"{sig.market_slug}/orderbook"
                                )
                                _ob_r = _req.get(_ob_url, timeout=6)
                                if _ob_r.ok:
                                    _ob = _ob_r.json()
                                    _yes_raw = (
                                        _ob.get("adjustedMidpoint")
                                        or _ob.get("midpoint")
                                        or _ob.get("lastTradePrice")
                                    )
                                    if _yes_raw is not None:
                                        _yes_pct = float(_yes_raw)
                                        if _yes_pct > 1:
                                            _yes_pct = _yes_pct / 100
                                        _yes_pct_display = _yes_pct * 100
                                        _signal_pct = (
                                            _yes_pct_display
                                            if sig.signal_direction == "UP"
                                            else 100 - _yes_pct_display
                                        )
                                        sig.best_entry_pct = round(_signal_pct, 1)
                                        logger.info(
                                            "[RESOLVE] best_entry_pct (final snapshot) "
                                            "= %.1f%% for signal id=%s",
                                            sig.best_entry_pct, sig.id
                                        )
                            except Exception as _be:
                                logger.debug("[RESOLVE] best_entry_pct snapshot failed: %s", _be)

                        db.session.flush()

                        # Per-pair tracker
                        try:
                            record_outcome(sym, outcome, direction=sig.direction)
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
                        # ── Limitless / Polymarket fill check — ALWAYS runs ──────────
                        # Previously this entire block was nested inside
                        # `if _ms.use_martingale:` which meant limitless_fill
                        # was NEVER checked (stuck at default 'NEUTRAL') for
                        # any account with martingale turned off. Fill status
                        # must be checked regardless of martingale setting —
                        # martingale is a STAKE strategy that consumes the
                        # fill result, it should not gate whether we check it.
                        _was_filled  = False
                        _fill_status = "UNKNOWN"
                        try:
                            from models import Settings as _MSettings
                            from limitless_executor import check_order_filled
                            _ms = _MSettings.query.first()

                            _order_id = sig.order_id
                            _mkt_slug = sig.market_slug

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

                                    # Polymarket fill check (runs regardless of Limitless fill)
                                    if _sig_rec.poly_order_id and _sig_rec.poly_fill in (None, "PENDING", "NEUTRAL"):
                                        try:
                                            from polymarket_executor import check_order_filled as _poly_fill_fn
                                            _pf = _poly_fill_fn(_sig_rec.poly_order_id)
                                            if _pf.get("filled"):
                                                _sig_rec.poly_fill = "FILLED"
                                            elif _pf.get("status") not in ("SHADOW", "ERROR", "NO_ORDER_ID"):
                                                _sig_rec.poly_fill = "UNFILLED"
                                            logger.info("[RESOLVE][POLY] Fill: order=%s status=%s poly_fill=%s",
                                                _sig_rec.poly_order_id, _pf.get("status"), _sig_rec.poly_fill)
                                        except Exception as _pfe:
                                            logger.warning("[RESOLVE][POLY] Fill check error: %s", _pfe)

                                    db.session.commit()
                                    logger.info(
                                        "[RESOLVE] Fill check complete | order=%s status=%s filled=%s limitless_fill=%s",
                                        _order_id, _fill_status, _was_filled, _sig_rec.limitless_fill
                                    )
                            except Exception as _fe:
                                logger.warning(f"[RESOLVE] limitless_fill persist error: {_fe}")
                        except Exception as _fce:
                            logger.warning(f"[RESOLVE] Fill check block error: {_fce}")

                        # ── Martingale stake update — only runs when martingale is ON ───
                        # Uses the _was_filled / _fill_status computed above (unconditionally).
                        try:
                            if _ms and _ms.use_martingale:
                                cap       = int(_ms.martingale_cap or 10)
                                _raw_seq = _ms.martingale_sequence or "1,1.5,2,3,4.5,6.7"
                                try:
                                    _MART_SEQ = [round(float(x.strip()), 1)
                                                 for x in _raw_seq.split(",") if x.strip()]
                                    if not _MART_SEQ:
                                        raise ValueError
                                except Exception:
                                    _MART_SEQ = [1.0, 1.5, 2.0, 3.0, 4.5, 6.7]

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

                        # ── Cooldown counter update ───────────────────────
                        # Runs in both shadow and live mode, regardless of
                        # whether martingale is on. The cooldown is a signal
                        # quality filter — it applies to all trade modes.
                        # LOSS: 2 consecutive → sit out 2 candles.
                        # WIN (invert mode only): 2 consecutive → sit out 2 candles.
                        # Only track/trigger cooldown when use_cooldown is enabled by user.
                        # Always track the counts so they're ready when user turns it on.
                        try:
                            _resolve_settings = Settings.query.first()
                            if _resolve_settings:
                                _use_cd = bool(getattr(_resolve_settings, 'use_cooldown', False))
                                if outcome == "WIN":
                                    _resolve_settings.cooldown_loss_count = 0
                                    _resolve_settings.cooldown_win_count  = 0
                                    logger.info('[RESOLVE] Cooldown loss count RESET after WIN')
                                else:  # LOSS
                                    new_loss_count = int(_resolve_settings.cooldown_loss_count or 0) + 1
                                    if new_loss_count >= 2 and _use_cd:
                                        _resolve_settings.cooldown_remaining  = 2
                                        _resolve_settings.cooldown_loss_count = 1
                                        logger.warning(
                                            '[RESOLVE] COOLDOWN TRIGGERED (use_cooldown=ON) — '
                                            '2 consecutive losses. Sitting out next 2 candles.'
                                        )
                                    else:
                                        _resolve_settings.cooldown_loss_count = min(new_loss_count, 2)
                                        if _use_cd:
                                            logger.info('[RESOLVE] Cooldown loss count=%d/2', new_loss_count)
                                db.session.commit()
                        except Exception as _cde:
                            logger.warning('[RESOLVE] Cooldown update error: %s', _cde)

                        # Per-pair cooldown WRITE
                        try:
                            import json as _json2
                            _pcd2_raw = getattr(_resolve_settings, 'pair_loss_cooldowns', '{}') or '{}'
                            # Guard: SQLAlchemy may return a dict if psycopg2 auto-parses TEXT as JSON
                            _pcd2 = _pcd2_raw if isinstance(_pcd2_raw, dict) else _json2.loads(_pcd2_raw)
                            if outcome == 'WIN':
                                _pcd2.pop(sym, None)
                                logger.info('[RESOLVE] Cooldown cleared for %s after WIN', sym)
                            else:
                                _cd = 1 if (sig.tier == 'T1') else 2
                                _pcd2[sym] = {'candles_remaining': _cd, 'tier': sig.tier or 'T2'}
                                logger.warning('[RESOLVE] Cooldown SET: %s suppressed %d candle(s)', sym, _cd)
                            _resolve_settings.pair_loss_cooldowns = _json2.dumps(_pcd2)

                            # Write to cooldown_log for dashboard display
                            try:
                                import json as _cdl_j
                                from datetime import datetime as _dt
                                _cdl_raw = getattr(_resolve_settings, 'cooldown_log', '[]') or '[]'
                                _cdl = _cdl_raw if isinstance(_cdl_raw, list) else _cdl_j.loads(_cdl_raw)
                                _cdl_entry = {
                                    'ts':     _dt.utcnow().strftime('%Y-%m-%d %H:%M UTC'),
                                    'pair':   sym,
                                    'event':  'COOLDOWN_SET' if outcome == 'LOSS' else 'COOLDOWN_CLEARED',
                                    'reason': f'Loss after {sig.tier or "T2"} signal' if outcome == 'LOSS' else 'Win',
                                    'candles': _cd if outcome == 'LOSS' else 0,
                                    'tier':   sig.tier or 'T2',
                                    'outcome': outcome,
                                }
                                _cdl.append(_cdl_entry)
                                # Keep last 50 log entries only
                                if len(_cdl) > 50:
                                    _cdl = _cdl[-50:]
                                _resolve_settings.cooldown_log = _cdl_j.dumps(_cdl)
                            except Exception as _cdlw:
                                logger.warning('[RESOLVE] cooldown_log write error: %s', _cdlw)

                            db.session.commit()
                        except Exception as _pcd2e:
                            logger.warning('[RESOLVE] Cooldown write error: %s', _pcd2e)

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
    # Best-dip tracker — polls Limitless orderbook every 30s during active signal.
    # Runs server-side so best_entry_pct is accurate even when dashboard is closed.
    scheduler.add_job(
        job_track_best_dip,
        IntervalTrigger(seconds=3),
        id="track_dip",
        replace_existing=True,
        misfire_grace_time=20,
        max_instances=1,
        coalesce=True,
    )

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
