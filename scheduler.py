"""
Scheduler — Signal timing (deterministic V2, parallel, dual-timeframe)
──────────────────────────────────────────────────────────────────────────
GENERATE fires every minute, checking BOTH 5m and 15m every time.
signal_engine.get_signal_for_symbol_tf only returns a candidate in the
~1-minute window right after that timeframe's own peek bar closes (1-min
peek for 5m, 3-min peek for 15m), so a single every-minute schedule serves
both timeframes without needing two separately-offset cron triggers.

PARALLEL: every pair that qualifies, on both timeframes, fires its own
independent order attempt this tick — there is no "pick the single best
signal" step. Each (symbol, timeframe, venue) stream is gated by its own
PairLadder row (see models.py) — independent 3-loss breaker + cooldown +
magnitude-based rearm per stream, not one shared/global counter.

RESOLVE fires every 5 minutes at second=0 — 15-min candle closes
(:00/:15/:30/:45) are a subset of every-5-min boundaries, so one schedule
catches both timeframes' closes.

  Duplicate guard:
    generate: Signal(symbol, timeframe, candle_open_time) combo must not
              already exist.
    resolve:  Signal.outcome == "PENDING" AND
              candle_close_time <= (now + tolerance).

  Resolution precedence (see job_resolve_outcomes):
    1. Limitless/Polymarket's OWN native resolution (their real settlement,
       backed by Chainlink Data Streams) — used whenever the platform has
       actually resolved within the poll window. This requires
       market_slug/poly_market_slug to be populated at generate time,
       which is why both place_shadow_order functions now discover their
       slug synchronously rather than relying on a background thread.
    2. OKX fallback (USD-quoted instrument preferred, USDT as a secondary
       fallback for accounts without USD spot access) — only when the
       platform hasn't resolved in time, or for BNB/DOGE which aren't on
       the Chainlink feed captured by chainlink_feed.py at all.

  WIN/LOSS (OKX fallback path):
    signal UP   + close_price > open_price → WIN
    signal DOWN + close_price < open_price → WIN
"""
import logging
import time
from datetime import datetime, timezone, timedelta, date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def _ctx():
    from app import app
    return app.app_context()


def _models_ready():
    """
    The deterministic V2 engine has no trained models to wait for — it's
    ready as soon as signal_engine imports successfully. Kept as a function
    (rather than removing the call sites) so nothing else has to change.
    """
    try:
        import signal_engine  # noqa: F401 — import success is the only check needed
        return True
    except Exception:
        return False


def job_generate_signal():
    """
    Fires every minute. Checks BOTH timeframes (5m, 15m) on every run — the
    peek-bar boundary-alignment check inside signal_engine.get_signal_for_symbol_tf
    naturally makes this a no-op except in the ~1-minute window right after
    each candle's peek bar closes, so running one job every minute (rather
    than juggling two differently-offset cron schedules for 5m vs 15m) is
    simpler and more drift-tolerant.

    PARALLEL, NOT SINGLE-PICK: every pair that qualifies, on both timeframes,
    gets its own independent order attempt this tick. Each
    (symbol, timeframe, venue) stream is gated by its own PairLadder row —
    there's no "pick the one best signal" step and no shared cooldown.
    """
    if not _models_ready():
        return
    with _ctx():
        try:
            from extensions import db
            from models import Settings
            import signal_engine as _se

            settings = Settings.query.first()
            if not settings:
                settings = Settings()
                db.session.add(settings)
                db.session.commit()

            mode = settings.mode
            max_cp = settings.max_contract_price

            # ── Stop loss balance check (unchanged from before) ──────────
            _sl_balance = getattr(settings, 'stop_loss_balance', None)
            if _sl_balance and float(_sl_balance) > 0:
                try:
                    if mode == 'shadow':
                        from models import ShadowBalance
                        _sb = ShadowBalance.query.first()
                        _cur_bal = float(_sb.balance) if _sb else 1000.0
                    else:
                        from limitless_executor import check_usdc_approval
                        _EXCHANGE = '0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5'
                        _bal_info = check_usdc_approval(_EXCHANGE)
                        _cur_bal = float(_bal_info.get('usdc_balance', 9999))
                    if _cur_bal <= float(_sl_balance):
                        logger.warning('[GENERATE] STOP LOSS HIT (balance=$%.2f <= $%.2f) — trading halted.',
                                       _cur_bal, float(_sl_balance))
                        return
                except Exception as _sle:
                    logger.warning('[GENERATE] Stop-loss check error: %s', _sle)

            try:
                no_exec_raw = settings.no_execute_pairs or '[]'
                no_exec = set(json.loads(no_exec_raw)) if isinstance(no_exec_raw, str) else set(no_exec_raw)
            except Exception:
                no_exec = set()

            for timeframe in _se.TIMEFRAMES:
                try:
                    _generate_for_timeframe(timeframe, settings, mode, max_cp, no_exec)
                except Exception as _tfe:
                    logger.error("[GENERATE][%s] timeframe error: %s", timeframe, _tfe, exc_info=True)

        except Exception as e:
            logger.error(f"[GENERATE] Unhandled error: {e}", exc_info=True)


def _generate_for_timeframe(timeframe, settings, mode, max_cp, no_exec):
    """
    Evaluate every pair for one timeframe and fire an independent order
    attempt for each qualifying candidate. This is the parallel core: unlike
    the old single-pick design, there is no "choose the best one" step and
    no shared exclude-list across pairs — each candidate lives or dies on
    its own (symbol, timeframe, venue) PairLadder state only.
    """
    import threading as _threading
    import time as _time
    from extensions import db, socketio
    from models import Signal, ShadowBalance
    import signal_engine as _se
    from telegram_bot import send_signal_alert

    use_limitless = bool(settings.use_limitless if settings.use_limitless is not None else True)
    use_polymarket = bool(settings.use_polymarket)
    poly_max_price = float(getattr(settings, 'poly_max_price', 0.50) or 0.50)

    candidates = _se.get_all_candidates(timeframe)
    if not candidates:
        return

    for sig in candidates:
        symbol = sig['symbol']
        candle_open = sig['candle_open_time']
        candle_close = sig['candle_close_time']

        # Dedup 1: never create a second signal row for the exact same
        # (symbol, timeframe, candle_open_time) — covers job_generate_signal
        # somehow running twice within the same ~1-minute window.
        existing = Signal.query.filter_by(
            symbol=symbol, timeframe=timeframe, candle_open_time=candle_open
        ).first()
        if existing:
            continue

        # Dedup 2 — the actually important one: never fire a NEW signal for
        # this (symbol, timeframe) stream while a PREVIOUS one is still
        # PENDING, regardless of candle_open_time. Under normal timing this
        # can't happen — resolve runs every 5 min and a candle's own close
        # always precedes the next candle's peek bar by design — but if
        # resolution is ever delayed (OKX hiccup, a platform slow to
        # publish), the next candle's peek bar can become actionable before
        # the previous one resolves. Without this check that would fire a
        # SECOND real order for the same pair+timeframe while the first is
        # still open — exactly the kind of overlap the validated backtest
        # never modeled (each stream trades one position at a time,
        # sequentially) and that PairLadder's per-stream accounting assumes
        # won't happen.
        still_pending = Signal.query.filter_by(
            symbol=symbol, timeframe=timeframe, outcome="PENDING"
        ).first()
        if still_pending:
            logger.warning(
                "[GENERATE] Skipping %s/%s — signal id=%s still PENDING "
                "(candle_close=%s) — resolution appears delayed",
                symbol, timeframe, still_pending.id, still_pending.candle_close_time
            )
            continue

        is_no_execute = symbol in no_exec

        ltl_ready = (use_limitless and not is_no_execute
                     and _ladder_ready(symbol, timeframe, 'limitless', sig['magnitude']))
        poly_ready = (use_polymarket and not is_no_execute
                      and _ladder_ready(symbol, timeframe, 'polymarket', sig['magnitude']))

        if not is_no_execute and not (ltl_ready or poly_ready):
            # Neither venue clear to trade this candidate right now (both in
            # cooldown, or both disabled) — nothing to place, nothing to log.
            continue

        ltl_size = _position_size_for(
            symbol, timeframe, 'limitless', settings.position_size,
            settings.use_martingale, settings.martingale_sequence
        ) if ltl_ready else 0.0
        poly_size = _position_size_for(
            symbol, timeframe, 'polymarket',
            float(getattr(settings, 'poly_position_size', 10.0) or 10.0),
            bool(getattr(settings, 'use_poly_martingale', False)),
            getattr(settings, 'poly_martingale_sequence', None)
        ) if poly_ready else 0.0

        trade_direction = sig['direction']

        order = {"success": False}
        poly_order = {"success": False}
        contracts = 0
        contract_price = max_cp
        order_id = None
        poly_order_id = None
        poly_token_id = None
        ltl_open_price = None
        poly_open_price = None

        def _run_limitless():
            nonlocal order, contracts, contract_price, order_id, ltl_open_price
            from limitless_executor import execute_order, get_market_resolution
            ORDER_MAX_ATTEMPTS = 3
            ORDER_RETRY_DELAY = 2
            _result = {"success": False}
            for attempt in range(1, ORDER_MAX_ATTEMPTS + 1):
                _result = execute_order(symbol, trade_direction, mode, ltl_size, max_cp, timeframe=timeframe,
                                         order_type=getattr(settings, 'limitless_order_type', 'GTC') or 'GTC')
                if _result.get("success"):
                    order = _result
                    contracts = _result.get("contracts", 0)
                    contract_price = _result.get("price_per_contract", max_cp)
                    order_id = _result.get("order_id")
                    try:
                        _slug_for_price = _result.get("slug") or _result.get("market_slug")
                        if _slug_for_price:
                            _res = get_market_resolution(_slug_for_price)
                            if _res.get("open_price") is not None:
                                ltl_open_price = _res["open_price"]
                    except Exception:
                        pass
                    return
                if _result.get("ambiguous"):
                    # Network timeout/connection error — we don't know if the
                    # order actually went through. Retrying here risks placing
                    # a second real order for the same position. Stop and
                    # surface this loudly rather than guess.
                    logger.critical(
                        "[GENERATE][%s/%s] AMBIGUOUS Limitless order failure — "
                        "NOT retrying (could double-execute). Manual check needed: %s",
                        symbol, timeframe, _result.get("error")
                    )
                    order = _result
                    return
                if attempt < ORDER_MAX_ATTEMPTS:
                    _time.sleep(ORDER_RETRY_DELAY)
            order = _result

        def _run_polymarket():
            nonlocal poly_order, poly_order_id, poly_token_id, poly_open_price
            try:
                from polymarket_executor import discover_market as _poly_discover_dup
                _dup_market = _poly_discover_dup(symbol, timeframe=timeframe)
                if _dup_market and _dup_market.get("slug"):
                    _dup = Signal.query.filter(
                        Signal.poly_market_slug == _dup_market["slug"],
                        Signal.poly_order_id.isnot(None),
                    ).first()
                    if _dup:
                        poly_order = {"success": False, "error": "duplicate position guard"}
                        return
                from polymarket_executor import execute_order as _poly_exec
                _POLY_ATTEMPTS = 10
                _POLY_DELAY = 3
                _r = {"success": False}
                for _pa in range(1, _POLY_ATTEMPTS + 1):
                    _r = _poly_exec(symbol, trade_direction, mode, poly_size, poly_max_price, timeframe=timeframe,
                                     order_type=getattr(settings, 'poly_order_type', 'GTC') or 'GTC')
                    if _r.get("success"):
                        poly_order = _r
                        poly_order_id = _r.get("order_id")
                        poly_token_id = _r.get("token_id")
                        try:
                            from chainlink_feed import get_chainlink_price
                            _cl = get_chainlink_price(symbol)
                            if _cl.get("price") is not None:
                                poly_open_price = _cl["price"]
                        except Exception:
                            pass
                        return
                    if _r.get("ambiguous"):
                        # Same reasoning as the Limitless side — a network
                        # timeout/connection error means we don't know if the
                        # order actually went through. Stop rather than risk
                        # placing a second real order for the same position.
                        logger.critical(
                            "[GENERATE][%s/%s] AMBIGUOUS Polymarket order failure — "
                            "NOT retrying (could double-execute). Manual check needed: %s",
                            symbol, timeframe, _r.get("error")
                        )
                        poly_order = _r
                        return
                    if _pa < _POLY_ATTEMPTS:
                        _time.sleep(_POLY_DELAY)
                poly_order = _r
            except Exception as _pe:
                poly_order = {"success": False, "error": str(_pe)}

        threads = []
        if ltl_ready:
            t = _threading.Thread(target=_run_limitless, name=f"ltl-{symbol}-{timeframe}", daemon=True)
            threads.append(t); t.start()
        elif is_no_execute:
            order = {"success": True, "order_id": None, "contracts": 0,
                     "price_per_contract": max_cp, "status": "NO_EXECUTE"}

        if poly_ready:
            tp = _threading.Thread(target=_run_polymarket, name=f"poly-{symbol}-{timeframe}", daemon=True)
            threads.append(tp); tp.start()

        for t in threads:
            t.join(timeout=120)

        mkt_slug = order.get("slug") if order.get("success") else None
        cond_id = order.get("condition_id") if order.get("success") else None
        if not mkt_slug:
            try:
                from limitless_executor import _slug_cache
                mkt_slug = _slug_cache.get(f"{symbol}:{timeframe}") or _slug_cache.get(symbol)
            except Exception:
                pass

        signal_obj = Signal(
            symbol=symbol,
            timeframe=timeframe,
            candle_open_time=candle_open,
            candle_close_time=candle_close,
            signal_direction=trade_direction,
            ml_confidence=sig['magnitude'],  # repurposed: deterministic strength (early-move magnitude)
            tier=None,
            open_price=sig['open_price'],
            mode=mode,
            order_id=order_id,
            market_slug=mkt_slug,
            condition_id=cond_id,
            position_size=(ltl_size if ltl_ready else None),
            contracts_bought=contracts,
            contract_price=contract_price,
            outcome="PENDING",
            telegram_sent=False,
            poly_order_id=poly_order_id,
            poly_fill=("PENDING" if poly_order_id else "NEUTRAL"),
            poly_position_size=(poly_size if poly_ready else None),
            poly_market_slug=poly_order.get("slug"),
            poly_token_id=poly_token_id,
            poly_open_price=poly_open_price,
            limitless_open_price=ltl_open_price,
        )
        db.session.add(signal_obj)
        db.session.commit()

        # ── Background slug discovery (unchanged pattern) ────────────────
        if not mkt_slug and ltl_ready:
            _sig_id = signal_obj.id
            _sym = symbol
            _tf = timeframe

            def _bg_discover_slug(sig_id, sym, tf):
                try:
                    from limitless_executor import discover_slug, fetch_market
                    slug = discover_slug(sym, timeframe=tf)
                    if not slug:
                        logger.warning("[GENERATE] BG slug discovery: no %s market found for %s", tf, sym)
                        return
                    cond_id2 = None
                    mkt_data = fetch_market(slug)
                    if mkt_data:
                        cond_id2 = (mkt_data.get("conditionId") or mkt_data.get("condition_id")
                                    or mkt_data.get("ctfConditionId") or mkt_data.get("condId"))
                    logger.info("[GENERATE] BG slug discovery — %s/%s slug=%s conditionId=%s",
                               sym, tf, slug, cond_id2)
                    with _ctx():
                        from extensions import db as _db, socketio as _sio
                        from models import Signal as _Signal
                        _s = _Signal.query.get(sig_id)
                        if _s:
                            _s.market_slug = slug
                            _s.condition_id = cond_id2
                            _db.session.commit()
                            try:
                                _sio.emit("signal_updated", _s.to_dict())
                            except Exception:
                                pass
                except Exception as _bge:
                    logger.warning("[GENERATE] BG slug discovery error: %s", _bge)

            _t_slug = _threading.Thread(
                target=_bg_discover_slug, args=(_sig_id, _sym, _tf),
                name="bg-slug-discovery", daemon=True,
            )
            _t_slug.start()

        # ── Shadow balance deduction ───────────────────────────────────────
        if mode == "shadow":
            shadow = ShadowBalance.query.first()
            if shadow:
                _spend = (ltl_size if ltl_ready else 0.0)
                shadow.balance = max(0, shadow.balance - _spend)
                db.session.commit()

        # ── Telegram ────────────────────────────────────────────────────────
        try:
            sent = send_signal_alert(
                signal=sig, mode=mode,
                position_size=(ltl_size if ltl_ready else poly_size),
                contracts=contracts, contract_price=contract_price,
            )
            signal_obj.telegram_sent = sent
            db.session.commit()
        except Exception as e:
            logger.warning(f"[GENERATE] Telegram failed: {e}")

        # ── WebSocket push ──────────────────────────────────────────────────
        try:
            socketio.emit("new_signal", signal_obj.to_dict())
        except Exception as e:
            logger.warning(f"[GENERATE] WS emit: {e}")

        logger.info(
            "[GENERATE] Saved signal id=%s | %s/%s %s mag=%.4f (thresh=%.4f) | "
            "ltl=%s poly=%s | candle %s→%s",
            signal_obj.id, symbol, timeframe, trade_direction, sig['magnitude'], sig['threshold'],
            ltl_ready, poly_ready, candle_open, candle_close,
        )



def _track_limitless_dip(pending):
    """
    Fetches the Limitless orderbook and records the lowest signal-side %
    seen during the candle as best_entry_pct.

    Price extraction:
      UP   signal → tracks YES% directly from adjustedMidpoint
      DOWN signal → tracks NO%  = 1 - YES_mid  (binary market: YES+NO=1.0)
                    Also reads NO best-ask from bids (NO bids = YES asks inverted)
                    to get the most accurate tradeable NO price.
    """
    import requests as _req
    from extensions import db
    API = "https://api.limitless.exchange"
    try:
        slug = pending.market_slug
        if not slug:
            try:
                from limitless_executor import _slug_cache
                sym = pending.symbol
                tf = pending.timeframe or "15m"
                # Timeframe-aware key first — with both 5m and 15m signals
                # potentially pending on the same symbol at once, the plain
                # legacy key (no timeframe) could otherwise return whichever
                # timeframe happened to populate it last.
                slug = (_slug_cache.get(f"{sym}:{tf}")
                        or _slug_cache.get(sym)
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


def _track_polymarket_dip(pending):
    """
    Polymarket equivalent of _track_limitless_dip() — fetches the CLOB
    orderbook for the SPECIFIC token bought (poly_token_id, captured at order
    time) and records the lowest best-ask % seen as poly_best_entry_pct.
    Unlike Limitless, Polymarket exposes each outcome token's book directly,
    so no UP/DOWN complement math is needed — the ask on the token actually
    held IS the signal-side price already.

    Guards against /book's known "ghost market" quirk (some CLOB
    deployments occasionally return a stale 0.01/0.99 snapshot) by skipping
    a tick that looks like that pattern rather than recording a misleading dip.
    """
    if not pending.poly_order_id or not pending.poly_token_id:
        return
    if str(pending.poly_order_id).startswith("poly_shadow_"):
        return  # no real order book position to track a fill price against
    import requests as _req
    from extensions import db
    try:
        r = _req.get("https://clob.polymarket.com/book",
                     params={"token_id": pending.poly_token_id}, timeout=3)
        if not r.ok:
            logger.debug("[DIP_TRACK][POLY] book %d for token=%s", r.status_code, pending.poly_token_id)
            return
        book = r.json() or {}
        asks = book.get("asks") or []
        bids = book.get("bids") or []
        if not asks:
            return
        best_ask = float(asks[0].get("price"))
        best_bid = float(bids[0].get("price")) if bids else None
        # Ghost-market guard: reject a degenerate 0.99/0.01-style snapshot
        if best_ask >= 0.98 and (best_bid is None or best_bid <= 0.02):
            logger.debug("[DIP_TRACK][POLY] skipping degenerate book snapshot for token=%s", pending.poly_token_id)
            return

        signal_pct = round(best_ask * 100, 1)
        current_best = pending.poly_best_entry_pct
        if current_best is None or signal_pct < current_best:
            pending.poly_best_entry_pct = signal_pct
            db.session.commit()
            logger.info(
                "[DIP_TRACK][POLY] %s %s — new best_dip=%.1f%% (prev=%s)",
                pending.symbol, pending.signal_direction, signal_pct, current_best
            )
    except Exception as _e:
        logger.warning("[DIP_TRACK][POLY] Unhandled error: %s", _e)


_last_fill_monitor_ts: dict = {}   # signal id -> last-checked timestamp
                                    # (per-signal, not global — with several
                                    # pairs/timeframes potentially pending at
                                    # once now, a single shared timestamp
                                    # would let only the first one in each
                                    # loop iteration actually get checked
                                    # every 15s, starving the rest)


def _monitor_pending_fills(pending):
    """
    Continuously checks fill status for whichever order(s) are still open on
    the current pending signal, on both platforms — not just once at resolve
    time. Throttled to roughly every 15s PER SIGNAL (this job runs every 3s;
    checking fills that often would be a lot of avoidable API traffic for
    something that doesn't change that fast) via a per-signal-id timestamp
    gate. Stops mattering once the signal resolves, since `pending` naturally
    won't include it anymore.
    """
    now = time.time()
    last = _last_fill_monitor_ts.get(pending.id, 0.0)
    if now - last < 15:
        return
    _last_fill_monitor_ts[pending.id] = now
    # Bound memory growth — drop entries for signals no longer worth tracking
    # (cheap to recompute if one somehow reappears, so just clear periodically).
    if len(_last_fill_monitor_ts) > 500:
        _last_fill_monitor_ts.clear()

    from extensions import db
    from models import Settings
    _settings = Settings.query.first()
    _ltl_threshold  = float(getattr(_settings, 'martingale_fill_threshold_pct', 95.0) or 95.0) / 100.0
    _poly_threshold = float(getattr(_settings, 'poly_fill_threshold_pct', 95.0) or 95.0) / 100.0

    if pending.order_id and not str(pending.order_id).startswith("shadow_") and pending.limitless_fill != "FILLED":
        try:
            from limitless_executor import check_order_filled as _ltl_check
            _fc = _ltl_check(pending.market_slug, pending.order_id, pending.position_size)
            _ratio = _fc.get("fill_ratio")
            if _ratio is not None:
                pending.fill_ratio = _ratio
                pending.filled_usd = _fc.get("filled_usd")
                pending.limitless_fill = "FILLED" if _ratio >= _ltl_threshold else ("PARTIAL" if _ratio > 0 else "UNFILLED")
                db.session.commit()
                logger.info("[FILL_MONITOR][LTL] id=%s status=%s ratio=%s", pending.id, _fc.get("status"), _ratio)
        except Exception as _e:
            logger.debug("[FILL_MONITOR][LTL] error: %s", _e)

    if (pending.poly_order_id and not str(pending.poly_order_id).startswith("poly_shadow_")
            and pending.poly_fill != "FILLED"):
        try:
            from polymarket_executor import check_order_filled as _poly_check
            _pfc = _poly_check(pending.poly_order_id, pending.poly_position_size)
            _pratio = _pfc.get("fill_ratio")
            if _pratio is not None:
                pending.poly_fill_ratio = _pratio
                pending.poly_filled_usd = _pfc.get("filled_usd")
                pending.poly_fill = "FILLED" if _pratio >= _poly_threshold else ("PARTIAL" if _pratio > 0 else "UNFILLED")
                db.session.commit()
                logger.info("[FILL_MONITOR][POLY] id=%s status=%s ratio=%s", pending.id, _pfc.get("status"), _pratio)
        except Exception as _e:
            logger.debug("[FILL_MONITOR][POLY] error: %s", _e)


def job_track_best_dip():
    """
    Runs every 3s while ANY signal is PENDING. Tracks best GTC dip for both
    platforms (_track_limitless_dip / _track_polymarket_dip) and, on a
    slower throttle, keeps checking whether each platform's order has
    actually filled yet rather than only finding out once at resolve time
    (_monitor_pending_fills) — this is what backs each platform's "pending
    trade" log on its own dashboard page. Works entirely server-side so
    tracking is accurate even when the dashboard is closed.

    Loops over EVERY pending signal (not just the most recent) — under the
    parallel V2 design, multiple pairs across both timeframes can be pending
    at once, and each needs its own dip/fill tracking independently.
    """
    with _ctx():
        try:
            from models import Signal
            pending_all = Signal.query.filter(
                Signal.outcome == "PENDING"
            ).order_by(Signal.candle_open_time.desc()).all()

            if not pending_all:
                return  # no active signals — nothing to do

            for pending in pending_all:
                try:
                    _track_limitless_dip(pending)
                    _track_polymarket_dip(pending)
                    _monitor_pending_fills(pending)
                except Exception as _pe:
                    logger.warning("[DIP_TRACK] Error for signal id=%s: %s", pending.id, _pe)

        except Exception as _e:
            logger.warning("[DIP_TRACK] Unhandled error: %s", _e)


def _update_pair_ladder(symbol: str, timeframe: str, venue: str,
                         was_filled: bool, outcome: str,
                         martingale_sequence: str, martingale_cap: int) -> None:
    """
    Update the independent (symbol, timeframe, venue) ladder after a trade
    resolves. Mirrors the old global-streak logic exactly, just scoped to
    one stream instead of one shared counter across everything:

      not filled / partial → FROZEN, streak untouched (same stake next time)
      WIN  + filled         → streak resets to 0, cooldown cleared
      LOSS + filled         → streak += 1; at streak >= 3, set cooldown_until
                               (COOLDOWN_BARS native bars of this timeframe —
                               see signal_engine.cooldown_seconds); at
                               streak >= martingale_cap, hard reset to 0 as
                               an outer safety valve.

    Position sizing for the NEXT trade on this exact stream is a lookup of
    martingale_sequence[min(consecutive_losses, len(sequence)-1)] — done at
    generate time, not here; this function only advances the counter/clock.
    """
    from extensions import db
    from models import PairLadder
    import signal_engine as _se

    row = PairLadder.query.filter_by(symbol=symbol, timeframe=timeframe, venue=venue).first()
    if row is None:
        row = PairLadder(symbol=symbol, timeframe=timeframe, venue=venue, consecutive_losses=0)
        db.session.add(row)
        db.session.flush()

    if not was_filled:
        logger.warning(
            "[LADDER][%s/%s/%s] FROZEN (not filled) — streak stays at %d",
            symbol, timeframe, venue, row.consecutive_losses or 0
        )
        return

    if outcome == "WIN":
        row.consecutive_losses = 0
        row.cooldown_until = None
        logger.info("[LADDER][%s/%s/%s] WIN — streak reset to 0", symbol, timeframe, venue)
        return

    # LOSS + filled
    cap = int(martingale_cap or 10)
    new_streak = int(row.consecutive_losses or 0) + 1
    if new_streak >= cap:
        row.consecutive_losses = 0
        row.cooldown_until = None
        logger.warning(
            "[LADDER][%s/%s/%s] CAP reached (%d losses) — hard reset to 0",
            symbol, timeframe, venue, cap
        )
        return

    row.consecutive_losses = new_streak
    if new_streak >= 3:
        cd_seconds = _se.cooldown_seconds(timeframe)
        row.cooldown_until = datetime.utcnow() + timedelta(seconds=cd_seconds)
        logger.warning(
            "[LADDER][%s/%s/%s] streak=%d >= 3 — cooldown until %s "
            "(resume needs magnitude >= %.4f)",
            symbol, timeframe, venue, new_streak, row.cooldown_until,
            _se.rearm_threshold(symbol, timeframe)
        )
    else:
        logger.info("[LADDER][%s/%s/%s] streak=%d", symbol, timeframe, venue, new_streak)


def _position_size_for(symbol: str, timeframe: str, venue: str,
                        base_size: float, use_martingale: bool,
                        martingale_sequence: str) -> float:
    """Look up the next stake for this exact (symbol,timeframe,venue) stream."""
    if not use_martingale:
        return base_size
    from models import PairLadder
    row = PairLadder.query.filter_by(symbol=symbol, timeframe=timeframe, venue=venue).first()
    streak = int(row.consecutive_losses) if row and row.consecutive_losses else 0
    try:
        seq = [round(float(x.strip()), 2) for x in (martingale_sequence or "").split(",") if x.strip()]
        if not seq:
            raise ValueError
    except Exception:
        seq = [1.0, 1.5, 2.0, 3.0, 4.5, 6.7]
    multiplier = seq[min(streak, len(seq) - 1)]
    return round(base_size * multiplier, 2)


def _ladder_ready(symbol: str, timeframe: str, venue: str, magnitude: float) -> bool:
    """
    Is this (symbol,timeframe,venue) stream clear to trade right now?
    True if not in cooldown, OR cooldown has expired AND this candidate's
    magnitude clears the stricter rearm threshold.
    """
    from models import PairLadder
    import signal_engine as _se
    row = PairLadder.query.filter_by(symbol=symbol, timeframe=timeframe, venue=venue).first()
    if row is None or row.cooldown_until is None:
        return True
    now = datetime.utcnow()
    if now < row.cooldown_until:
        return False
    return magnitude >= _se.rearm_threshold(symbol, timeframe)


def job_resolve_outcomes():
    """
    Fires every 5 minutes at second=0 (independent of job_generate_signal,
    which runs every minute on its own schedule). Resolves any PENDING
    signal (5m or 15m, any pair) whose candle_close_time is within 5 seconds
    of now.

    Retries fetching the OKX candle up to 5 times with 1-second gaps to give
    OKX time to publish the just-closed candle.

    Resolution precedence per signal:
      1. Limitless/Polymarket's own native resolution (their real Chainlink-
         backed settlement) if it resolved within the poll window.
      2. OKX fallback (USD-quoted instrument preferred, USDT as secondary
         fallback — see signal_engine.fetch_okx_candles_for_resolution):
           open_price  = candle row 'open'  at candle_open_time
           close_price = candle row 'close' at candle_open_time
         WIN: signal UP   → close > open
         WIN: signal DOWN → close < open
    """
    with _ctx():
        try:
            from extensions import db, socketio
            from models import Signal, DailyStats, ShadowBalance, Settings
            from signal_engine import fetch_okx_candles_for_resolution as fetch_okx_candles, record_outcome
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

            # ── Kick off Limitless- AND Polymarket-side lookups CONCURRENTLY ────
            # with the OKX fetch below. Each platform's own resolution (the
            # actual outcome it paid out on) and its order's fill/settlement
            # status are independent of OKX and of each other, so they all run
            # in background threads while the OKX candle fetch (which already
            # has its own retry loop) proceeds — this keeps the total added
            # latency close to whichever check is slowest instead of summing
            # every check in series. resolve and generate no longer share a
            # tick (resolve runs every 5 min, generate every 1 min,
            # independently), so this is purely about resolve's own
            # responsiveness now, not about avoiding a collision with generate.
            import threading as _resolve_threading
            from limitless_executor import poll_market_resolution as _poll_ltl_resolution
            from limitless_executor import check_order_filled as _poll_ltl_fill
            from polymarket_executor import poll_market_resolution as _poll_poly_resolution
            from polymarket_executor import check_order_filled as _poll_poly_fill

            _ltl_resolution  = {}   # sig.id -> limitless get_market_resolution()-shaped dict
            _ltl_fill        = {}   # sig.id -> limitless check_order_filled()-shaped dict
            _poly_resolution = {}   # sig.id -> polymarket get_market_resolution()-shaped dict
            _poly_fill_data  = {}   # sig.id -> polymarket check_order_filled()-shaped dict
            _ltl_close_px    = {}   # sig.id -> float, Chainlink close price for Limitless's own log
            _poly_close_px   = {}   # sig.id -> float, Chainlink close price for Polymarket's own log
            _ltl_threads     = {}
            _poly_threads    = {}

            def _resolve_ltl_side(_psig):
                if _psig.market_slug:
                    try:
                        _ltl_resolution[_psig.id] = _poll_ltl_resolution(_psig.market_slug)
                    except Exception as _rle:
                        logger.warning("[RESOLVE] Limitless resolution poll error id=%s: %s", _psig.id, _rle)
                if _psig.order_id:
                    try:
                        _ltl_fill[_psig.id] = _poll_ltl_fill(_psig.market_slug, _psig.order_id, _psig.position_size)
                    except Exception as _rfe:
                        logger.warning("[RESOLVE] Limitless fill-check error id=%s: %s", _psig.id, _rfe)
                # Close-price reference for Limitless's own pending/log
                # display — Chainlink for the 4 pairs it covers, OKX close
                # (already being fetched anyway) covers BNB/DOGE as a fallback.
                try:
                    from chainlink_feed import get_chainlink_price as _cl_price
                    _cl = _cl_price(_psig.symbol)
                    if _cl.get("price") is not None:
                        _ltl_close_px[_psig.id] = _cl["price"]
                except Exception as _cle:
                    logger.info("[RESOLVE] Chainlink close-price fetch skipped for id=%s: %s", _psig.id, _cle)

            def _resolve_poly_side(_psig):
                if _psig.poly_market_slug:
                    try:
                        _poly_resolution[_psig.id] = _poll_poly_resolution(slug=_psig.poly_market_slug)
                    except Exception as _rpe:
                        logger.warning("[RESOLVE] Polymarket resolution poll error id=%s: %s", _psig.id, _rpe)
                if _psig.poly_order_id:
                    try:
                        _poly_fill_data[_psig.id] = _poll_poly_fill(_psig.poly_order_id, _psig.poly_position_size)
                    except Exception as _rqe:
                        logger.warning("[RESOLVE] Polymarket fill-check error id=%s: %s", _psig.id, _rqe)
                try:
                    from chainlink_feed import get_chainlink_price as _cl_price2
                    _cl2 = _cl_price2(_psig.symbol)
                    if _cl2.get("price") is not None:
                        _poly_close_px[_psig.id] = _cl2["price"]
                except Exception as _cle2:
                    logger.info("[RESOLVE] Chainlink close-price fetch skipped (poly) for id=%s: %s", _psig.id, _cle2)

            for _psig in pending:
                _th = _resolve_threading.Thread(
                    target=_resolve_ltl_side, args=(_psig,), daemon=True,
                    name=f"ltl-resolve-{_psig.id}",
                )
                _ltl_threads[_psig.id] = _th
                _th.start()
                if _psig.poly_order_id:
                    _pth = _resolve_threading.Thread(
                        target=_resolve_poly_side, args=(_psig,), daemon=True,
                        name=f"poly-resolve-{_psig.id}",
                    )
                    _poly_threads[_psig.id] = _pth
                    _pth.start()

            # Group by (symbol, timeframe) — each needs its OWN OKX bar size
            # (5m signals resolve against 5m candles, 15m against 15m candles;
            # a single fetch can't serve both even for the same symbol).
            by_sym_tf = {}
            for s in pending:
                tf = s.timeframe or "15m"
                by_sym_tf.setdefault((s.symbol, tf), []).append(s)

            for (sym, tf), sigs in by_sym_tf.items():
                # Retry fetching OKX data up to 3 times (1s apart) to let
                # OKX publish the just-closed candle before we read it.
                df = pd.DataFrame()
                # Retry until OKX has published the just-closed candle (up to 5s)
                target_opens = {pd.Timestamp(s.candle_open_time, tz="UTC") for s in sigs}
                df = pd.DataFrame()
                for attempt in range(5):
                    df = fetch_okx_candles(sym, bar=tf, limit=10)
                    if not df.empty and target_opens.issubset(set(df["timestamp"])):
                        break
                    logger.debug(f"[RESOLVE] OKX waiting for candle data, attempt {attempt+1}/5 for {sym} ({tf})")
                    _time.sleep(1)

                if df.empty:
                    logger.warning(f"[RESOLVE] No OKX data for {sym} ({tf}) after 5 attempts")
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

                        # The background Limitless/Polymarket resolution/fill
                        # threads were started before the OKX fetch above; join
                        # (with a safety-net timeout) so their results are
                        # actually ready to read. In the common case they finish
                        # well inside the OKX fetch's own retry window and this
                        # returns immediately. The timeout is set comfortably
                        # above the threads' own built-in sleep budget (roughly
                        # 6s resolution polling + 4.5s fill polling) — it was
                        # previously tighter than that budget, which could cut
                        # a check off right before it would have succeeded.
                        # Anything that still doesn't resolve in time falls
                        # back to OKX and gets a second chance from
                        # job_reconcile_resolutions shortly after.
                        _ltl_th = _ltl_threads.get(sig.id)
                        if _ltl_th:
                            _ltl_th.join(timeout=14)
                        _poly_th = _poly_threads.get(sig.id)
                        if _poly_th:
                            _poly_th.join(timeout=14)

                        # The new deterministic engine has no invert concept —
                        # the traded side is always exactly the signaled direction.
                        _traded_side = sig.signal_direction

                        # OKX-derived outcome — kept for display/comparison even
                        # when Limitless's own resolution is used as the source of
                        # truth below.
                        if _traded_side == "UP":
                            okx_outcome = "WIN" if close_price > open_price else "LOSS"
                        else:
                            okx_outcome = "WIN" if close_price < open_price else "LOSS"

                        # Limitless-derived outcome — winningOutcomeIndex 0=UP(YES)
                        # won, 1=DOWN(NO) won. This is what the market actually paid
                        # out on Base, resolved against Pyth Network, not OKX — see
                        # get_market_resolution() in limitless_executor.py. It wins
                        # over the OKX guess whenever it resolved in time, since it
                        # is the ground truth for what the bot's position is worth.
                        _ltl_res = _ltl_resolution.get(sig.id) or {}
                        _limitless_outcome = None
                        if _ltl_res.get('resolved'):
                            _limitless_outcome = "WIN" if _ltl_res.get('winning_side') == _traded_side else "LOSS"

                        _resolve_toggle_settings = Settings.query.first()
                        _use_ltl_res = bool(
                            _resolve_toggle_settings.use_limitless_resolution
                            if (_resolve_toggle_settings
                                and _resolve_toggle_settings.use_limitless_resolution is not None)
                            else True
                        )

                        if _limitless_outcome is not None and _use_ltl_res:
                            outcome            = _limitless_outcome
                            resolution_source  = "LIMITLESS"
                        else:
                            outcome            = okx_outcome
                            resolution_source  = "OKX_FALLBACK"

                        if _limitless_outcome is not None and _limitless_outcome != okx_outcome:
                            logger.warning(
                                "[RESOLVE] OKX/Limitless outcome MISMATCH id=%s %s signal=%s "
                                "traded=%s — OKX=%s Limitless=%s (winning_side=%s) — using %s (%s)",
                                sig.id, sym, sig.signal_direction, _traded_side,
                                okx_outcome, _limitless_outcome, _ltl_res.get('winning_side'),
                                outcome, resolution_source
                            )
                        elif _limitless_outcome is None:
                            logger.info(
                                "[RESOLVE] Limitless resolution unavailable for id=%s slug=%s "
                                "within poll budget — using OKX fallback (%s)",
                                sig.id, sig.market_slug, okx_outcome
                            )

                        sig.open_price  = open_price   # OKX candle open (display/history)
                        sig.close_price = close_price
                        sig.outcome     = outcome
                        sig.okx_outcome          = okx_outcome
                        sig.resolution_source    = resolution_source
                        sig.limitless_open_price = _ltl_res.get('open_price')
                        sig.limitless_close_price = _ltl_close_px.get(sig.id)

                        # ── Polymarket outcome parity ───────────────────────
                        # Same treatment as the Limitless block above, on its
                        # own platform-native resolution — independent outcome
                        # source, same OKX fallback pattern, same invert-aware
                        # traded side (both platforms buy the same side of the
                        # same signal). Polymarket's OWN martingale streak
                        # (poly_martingale_streak) is gated on this further
                        # down, fully independent of Limitless's streak.
                        _poly_res = _poly_resolution.get(sig.id) or {}
                        if _poly_res.get('resolved'):
                            sig.poly_outcome           = "WIN" if _poly_res.get('winning_side') == _traded_side else "LOSS"
                            sig.poly_resolution_source = "POLYMARKET"
                        elif sig.poly_order_id:
                            sig.poly_outcome           = okx_outcome
                            sig.poly_resolution_source = "OKX_FALLBACK"
                        sig.poly_close_price = _poly_close_px.get(sig.id)

                        # (Directional saturation / Rule 2 history removed —
                        # not part of the parallel V2 design; each stream is
                        # gated by its own PairLadder breaker instead.)

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
                            record_outcome(sym, tf, outcome)
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

                        # ── Fill-status persistence — ALWAYS runs, independent of ──
                        # whether martingale is enabled (previously this whole block,
                        # including simply recording FILLED/UNFILLED for the dashboard
                        # badge, only ran when use_martingale was True — so the badge
                        # stayed stuck on NEUTRAL forever with martingale off).
                        #
                        # A shadow / no_execute-pair signal has no real order to
                        # check — there is no orderbook liquidity constraint to
                        # freeze against, so it counts as a complete fill for
                        # gating purposes and displays as NEUTRAL (not UNFILLED).
                        _ms = Settings.query.first()
                        _fill_threshold = float(
                            getattr(_ms, 'martingale_fill_threshold_pct', 95.0) or 95.0
                        ) / 100.0

                        _order_id     = sig.order_id
                        _mkt_slug     = sig.market_slug
                        _is_shadowish = (not _order_id) or str(_order_id).startswith("shadow_")

                        if _is_shadowish:
                            _was_filled  = True
                            _fill_status = "SHADOW" if _order_id else "NO_ORDER_ID"
                            sig.fill_ratio     = None
                            sig.filled_usd     = None
                            sig.limitless_fill = "NEUTRAL"
                        else:
                            _fill_check = _ltl_fill.get(sig.id) or _poll_ltl_fill(
                                _mkt_slug, _order_id, sig.position_size)
                            _fill_ratio  = _fill_check.get("fill_ratio")
                            _fill_status = _fill_check.get("status", "UNKNOWN")
                            sig.fill_ratio = _fill_ratio
                            sig.filled_usd = _fill_check.get("filled_usd")

                            if _fill_ratio is not None:
                                if _fill_ratio >= _fill_threshold:
                                    sig.limitless_fill = "FILLED"
                                    _was_filled = True
                                elif _fill_ratio > 0:
                                    sig.limitless_fill = "PARTIAL"
                                    _was_filled = False   # below threshold — freeze, same as unfilled
                                else:
                                    sig.limitless_fill = "UNFILLED"
                                    _was_filled = False
                            else:
                                # Fill amount unknown (e.g. legacy fallback with no
                                # amount data) — fall back to the boolean filled flag.
                                _was_filled = bool(_fill_check.get("filled"))
                                sig.limitless_fill = "FILLED" if _was_filled else "UNFILLED"

                        # Polymarket fill check — runs regardless of Limitless
                        # fill or the martingale toggle; classified the same
                        # way (FILLED/PARTIAL/UNFILLED via fill_ratio) as
                        # Limitless above, using its own threshold setting.
                        # This only feeds the dashboard + poly_outcome parity
                        # above — Polymarket fills do not gate the shared
                        # martingale streak (see models.py for why).
                        if sig.poly_order_id and str(sig.poly_order_id).startswith("poly_shadow_"):
                            sig.poly_fill = "NEUTRAL"
                            sig.poly_fill_ratio = None
                            sig.poly_filled_usd = None
                        elif sig.poly_order_id:
                            try:
                                _pf = _poly_fill_data.get(sig.id) or _poll_poly_fill(
                                    sig.poly_order_id, sig.poly_position_size)
                                _poly_threshold = float(
                                    getattr(_ms, 'poly_fill_threshold_pct', 95.0) or 95.0
                                ) / 100.0
                                _pratio = _pf.get("fill_ratio")
                                sig.poly_fill_ratio = _pratio
                                sig.poly_filled_usd = _pf.get("filled_usd")

                                if _pratio is not None:
                                    if _pratio >= _poly_threshold:
                                        sig.poly_fill = "FILLED"
                                    elif _pratio > 0:
                                        sig.poly_fill = "PARTIAL"
                                    else:
                                        sig.poly_fill = "UNFILLED"
                                else:
                                    sig.poly_fill = "FILLED" if _pf.get("filled") else "UNFILLED"

                                logger.info(
                                    "[RESOLVE][POLY] Fill: order=%s status=%s ratio=%s poly_fill=%s",
                                    sig.poly_order_id, _pf.get("status"), _pratio, sig.poly_fill
                                )
                            except Exception as _pfe:
                                logger.warning("[RESOLVE][POLY] Fill check error: %s", _pfe)

                        # _poly_was_filled — used by Polymarket's own
                        # independent martingale block further below. No
                        # poly_order_id at all means Polymarket simply wasn't
                        # part of this signal (toggle off / no-execute pair) —
                        # that streak shouldn't move either way in that case.
                        _poly_was_filled = None
                        if sig.poly_order_id and str(sig.poly_order_id).startswith("poly_shadow_"):
                            _poly_was_filled = True
                        elif sig.poly_order_id:
                            _poly_was_filled = (sig.poly_fill == "FILLED")

                        try:
                            db.session.commit()
                        except Exception as _fe:
                            logger.warning(f"[RESOLVE] fill-status persist error: {_fe}")

                        # ── Independent per-(symbol,timeframe,venue) ladder update ──
                        # Replaces the old single global martingale_streak —
                        # see _update_pair_ladder for the exact freeze/reset/
                        # advance/cooldown rules (unchanged in spirit, just
                        # scoped to this one stream instead of everything).
                        try:
                            if _ms and _ms.use_martingale:
                                _update_pair_ladder(
                                    sym, tf, "limitless",
                                    was_filled=_was_filled, outcome=outcome,
                                    martingale_sequence=_ms.martingale_sequence,
                                    martingale_cap=_ms.martingale_cap,
                                )
                                db.session.commit()
                        except Exception as _me:
                            logger.warning(f"[RESOLVE] Ladder update error (limitless): {_me}")

                        # ── Polymarket's independent (symbol,timeframe,'polymarket') ladder ──
                        # Same helper, own venue key — fully separate from
                        # the Limitless ladder above by construction (different
                        # venue string in the PairLadder row).
                        try:
                            if _ms and getattr(_ms, 'use_poly_martingale', False) and _poly_was_filled is not None:
                                _update_pair_ladder(
                                    sym, tf, "polymarket",
                                    was_filled=_poly_was_filled, outcome=sig.poly_outcome,
                                    martingale_sequence=getattr(_ms, 'poly_martingale_sequence', None),
                                    martingale_cap=getattr(_ms, 'poly_martingale_cap', 10),
                                )
                                db.session.commit()
                        except Exception as _pme:
                            logger.warning(f"[RESOLVE][POLY] Ladder update error: {_pme}")

                        # (Old global use_cooldown / pair_loss_cooldowns / cooldown_log
                        # mechanic removed — fully superseded by the per-
                        # (symbol,timeframe,venue) PairLadder breaker above,
                        # which is what job_generate_signal actually checks now.)

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
                for sigs in by_sym_tf.values():
                    for s in sigs:
                        if s.outcome != "PENDING":
                            socketio.emit("signal_resolved", s.to_dict())
            except Exception as e:
                logger.warning(f"[RESOLVE] WS emit: {e}")

        except Exception as e:
            logger.error(f"[RESOLVE] Unhandled error: {e}", exc_info=True)


def job_reconcile_resolutions():
    """
    Fires every 20s, independent of both job_generate_signal (every 1 min)
    and job_resolve_outcomes (every 5 min). job_resolve_outcomes only waits
    up to ~14s for each platform's own resolution before falling back to
    OKX — long enough for the common case, but Limitless (and sometimes
    Polymarket) can occasionally take meaningfully longer to actually
    publish winningOutcomeIndex, which used to show up as OKX_FALLBACK on
    signals that would have resolved correctly a few seconds later. This
    job is the fix for that: it re-checks recently-fallen-back signals on a
    more relaxed budget and corrects resolution_source (and outcome, if it
    turns out to have genuinely differed) once the real answer is in.

    Deliberately does NOT touch PairLadder.consecutive_losses — a stake
    decision already made off the fallback value can't be safely unwound
    without risking a worse, compounding error. Only the stored record is
    corrected, and a correction that flips WIN/LOSS is logged loudly since
    that's the one case worth a human glancing at.
    """
    with _ctx():
        try:
            from extensions import db
            from models import Signal
            from limitless_executor import get_market_resolution as _ltl_resolution_check
            from polymarket_executor import get_market_resolution as _poly_resolution_check

            # Bounded lookback — if a platform hasn't resolved within an hour
            # something else is wrong and repeatedly polling it isn't
            # productive; older rows simply age out of this query on their own.
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)

            def _traded_side(sig):
                # No invert concept in the new deterministic engine — the
                # traded side is always exactly the signaled direction.
                return sig.signal_direction

            # ── Limitless ──────────────────────────────────────────────────
            ltl_candidates = Signal.query.filter(
                Signal.resolution_source == "OKX_FALLBACK",
                Signal.candle_close_time >= cutoff,
                Signal.market_slug.isnot(None),
            ).all()
            for sig in ltl_candidates:
                try:
                    res = _ltl_resolution_check(sig.market_slug)
                except Exception as e:
                    logger.warning("[RECONCILE][LTL] check failed id=%s: %s", sig.id, e)
                    continue
                if not res.get('resolved'):
                    continue
                corrected = "WIN" if res.get('winning_side') == _traded_side(sig) else "LOSS"
                if corrected != sig.outcome:
                    logger.warning(
                        "[RECONCILE][LTL] id=%s %s — OKX-fallback outcome was %s, Limitless "
                        "now confirms %s (winning_side=%s). Correcting the record. Daily/shadow "
                        "aggregates already counted the old value and are NOT retroactively "
                        "adjusted — martingale streak is also left untouched.",
                        sig.id, sig.symbol, sig.outcome, corrected, res.get('winning_side')
                    )
                    sig.outcome = corrected
                else:
                    logger.info("[RECONCILE][LTL] id=%s %s confirmed — Limitless agrees with OKX (%s)",
                                sig.id, sig.symbol, corrected)
                sig.resolution_source    = "LIMITLESS"
                sig.limitless_open_price = res.get('open_price')
                db.session.commit()

            # ── Polymarket ─────────────────────────────────────────────────
            poly_candidates = Signal.query.filter(
                Signal.poly_resolution_source == "OKX_FALLBACK",
                Signal.candle_close_time >= cutoff,
                Signal.poly_market_slug.isnot(None),
            ).all()
            for sig in poly_candidates:
                try:
                    res = _poly_resolution_check(slug=sig.poly_market_slug)
                except Exception as e:
                    logger.warning("[RECONCILE][POLY] check failed id=%s: %s", sig.id, e)
                    continue
                if not res.get('resolved'):
                    continue
                corrected = "WIN" if res.get('winning_side') == _traded_side(sig) else "LOSS"
                if corrected != sig.poly_outcome:
                    logger.warning(
                        "[RECONCILE][POLY] id=%s %s — OKX-fallback outcome was %s, Polymarket "
                        "now confirms %s. Correcting the record.",
                        sig.id, sig.symbol, sig.poly_outcome, corrected
                    )
                    sig.poly_outcome = corrected
                else:
                    logger.info("[RECONCILE][POLY] id=%s %s confirmed — Polymarket agrees with OKX (%s)",
                                sig.id, sig.symbol, corrected)
                sig.poly_resolution_source = "POLYMARKET"
                db.session.commit()

        except Exception as e:
            logger.error(f"[RECONCILE] Unhandled error: {e}", exc_info=True)


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


def start_scheduler():
    # Best-dip tracker — polls Limitless/Polymarket orderbooks every 3s while
    # ANY signal is pending (now potentially several at once, across pairs
    # and both timeframes — job_track_best_dip loops over all of them).
    scheduler.add_job(
        job_track_best_dip,
        IntervalTrigger(seconds=3),
        id="track_dip",
        replace_existing=True,
        misfire_grace_time=20,
        max_instances=1,
        coalesce=True,
    )

    # RESOLVE every 5 minutes at second=0. 15-min candle closes (:00/:15/:30/:45)
    # are a subset of every-5-min boundaries, so one schedule catches both
    # timeframes' closes — no separate trigger needed for 15m.
    scheduler.add_job(
        job_resolve_outcomes,
        CronTrigger(minute="*/5", second="0"),
        id="resolve",
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )

    # Catches up any signal that fell back to OKX because Limitless/Polymarket
    # hadn't resolved yet within resolve's own tight (few-second) window —
    # runs independently of the candle boundary so it never competes with
    # generate for the scheduler's attention. See job_reconcile_resolutions.
    scheduler.add_job(
        job_reconcile_resolutions,
        IntervalTrigger(seconds=20),
        id="reconcile",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
        coalesce=True,
    )

    # GENERATE every minute. Both timeframes are checked on every run —
    # signal_engine's peek-bar boundary-alignment check is what actually
    # makes each timeframe "fire" only once per its own candle, so a single
    # every-minute schedule serves 5m (peek=1min) and 15m (peek=3min) at
    # once without needing two separately-offset cron schedules.
    scheduler.add_job(
        job_generate_signal,
        CronTrigger(minute="*", second="5"),
        id="generate",
        replace_existing=True,
        misfire_grace_time=30,
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

    # (No retrain job — the deterministic V2 engine has no model to retrain.
    # Thresholds are fixed, walk-forward-validated constants in
    # signal_engine.MAG_THRESHOLD; changing them is a code change, not a
    # scheduled background task.)

    scheduler.start()
    logger.info(
        "[SCHEDULER] Started | "
        "resolve@every5min+0s (catches both 5m & 15m closes) | reconcile@every20s | "
        "generate@every1min+5s (checks both timeframes; peek-bar alignment "
        "gates which one actually fires) | daily@23:59"
    )
