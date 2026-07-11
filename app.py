"""
Main Flask application
"""
import os
import logging
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, jsonify, request
from extensions import db, socketio
from models import Signal, DailyStats, Settings, ShadowBalance

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-32chars")

    # ── Database URL ────────────────────────────────────────────────────────
    # Supabase provides postgres:// URIs — SQLAlchemy 2.x requires postgresql://.
    # Render free tier has NO IPv6. Supabase DNS resolves to IPv6 by default,
    # causing "Network is unreachable". We pre-resolve to IPv4 via a custom
    # creator function that forces AF_INET DNS lookup before connecting.
    import socket as _socket
    import psycopg2 as _psycopg2
    from urllib.parse import urlparse as _urlparse

    _db_url = os.environ.get("DATABASE_URL", "sqlite:////tmp/signals.db")
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    if "postgresql" in _db_url:
        # Render free tier has no IPv6. Supabase's direct DB host resolves to
        # IPv6. The fix is to use Supabase's Transaction Mode pooler which
        # uses aws-0-*.pooler.supabase.com — an IPv4-only hostname.
        # We rewrite the URL automatically:
        #   db.xxxx.supabase.co:5432  →  aws-0-<region>.pooler.supabase.com:6543
        # The user also changes: postgres → postgres.xxxx (project ref appended)
        _parsed  = _urlparse(_db_url)
        _host    = _parsed.hostname or ""
        _newurl  = _db_url

        if "supabase.co" in _host and "pooler" not in _host:
            # Extract project ref from db.<ref>.supabase.co
            _ref = _host.replace("db.", "").replace(".supabase.co", "")
            # Supabase pooler host — uses AWS us-east-1 by default;
            # works for all regions as an IPv4 endpoint
            _pool_host = f"aws-0-us-east-1.pooler.supabase.com"
            _pool_port = 6543
            _orig_user = _parsed.username or "postgres"
            # Pooler requires user in format: postgres.<project-ref>
            if f".{_ref}" not in _orig_user:
                _pool_user = f"{_orig_user}.{_ref}"
            else:
                _pool_user = _orig_user
            _password = _parsed.password or ""
            _dbname   = (_parsed.path or "/postgres").lstrip("/")
            _newurl   = (f"postgresql://{_pool_user}:{_password}"
                         f"@{_pool_host}:{_pool_port}/{_dbname}")
            logger.info(f"[APP] Supabase pooler URL active (IPv4): {_pool_host}:{_pool_port}")

        app.config["SQLALCHEMY_DATABASE_URI"] = _newurl
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_size":     2,
            "max_overflow":  3,
            "pool_timeout":  30,
            "pool_recycle":  300,
            "pool_pre_ping": True,
            "connect_args":  {
                "sslmode":         "require",
                "connect_timeout": 10,
            },
        }

    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="gevent",
                      logger=False, engineio_logger=False)

    with app.app_context():
        db.create_all()

        # ── Safe column migrations ──────────────────────────────────────────────
        # db.create_all() only creates missing TABLES — it never adds new columns
        # to existing tables. Each migration uses IF NOT EXISTS on PostgreSQL
        # and falls back to try/catch for SQLite compatibility.
        _is_pg = "postgresql" in str(app.config["SQLALCHEMY_DATABASE_URI"])

        def _add_column(table, col, coldef):
            from sqlalchemy import text
            try:
                with db.engine.connect() as _c:
                    if _is_pg:
                        _c.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coldef}"))
                    else:
                        _c.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}"))
                    _c.commit()
                    logger.info(f"[APP] Migration: added {table}.{col}")
            except Exception as _e:
                _em = str(_e).lower()
                if "duplicate column" not in _em and "already exists" not in _em:
                    logger.warning(f"[APP] Migration warning ({table}.{col}): {_e}")

        # signals table
        for _col, _def in [
            ("market_slug",    "VARCHAR(200)"),
            ("condition_id",   "VARCHAR(100)"),
            ("limitless_fill", "VARCHAR(10) DEFAULT 'NEUTRAL'"),
            # v2 additions
            ("best_entry_pct", "REAL"),
            ("poly_order_id",  "VARCHAR(120)"),
            ("poly_fill",      "VARCHAR(10) DEFAULT 'NEUTRAL'"),
            # v4 additions — Limitless-authoritative resolution + partial-fill tracking
            ("resolution_source",    "VARCHAR(20)"),
            ("okx_outcome",          "VARCHAR(10)"),
            ("limitless_open_price", "REAL"),
            ("fill_ratio",           "REAL"),
            ("filled_usd",           "REAL"),
            # v5 additions — Polymarket data parity (mirrors v4, Polymarket side)
            ("poly_market_slug",       "VARCHAR(200)"),
            ("poly_token_id",          "VARCHAR(100)"),
            ("poly_position_size",     "REAL"),
            ("poly_outcome",           "VARCHAR(10)"),
            ("poly_resolution_source", "VARCHAR(20)"),
            ("poly_fill_ratio",        "REAL"),
            ("poly_filled_usd",        "REAL"),
            # v7 additions — platform-native entry/close prices + Polymarket best-dip
            ("limitless_close_price", "REAL"),
            ("poly_open_price",       "REAL"),
            ("poly_close_price",      "REAL"),
            ("poly_best_entry_pct",   "REAL"),
            # v8 addition — dual-timeframe (5m/15m) support. db.create_all() only
            # creates missing TABLES (PairLadder is new, so it appeared fine on
            # its own) — it never adds a new COLUMN to an already-existing table
            # like signals, so this one needs the same explicit migration as
            # everything else above it.
            ("timeframe", "VARCHAR(4) DEFAULT '15m'"),
        ]:
            _add_column("signals", _col, _def)

        # ── Type-fix: limitless_fill / poly_fill may have been created as
        # DOUBLE PRECISION in older deployments. Coerce to VARCHAR(10).
        # Safe no-op if already the correct type.
        if _is_pg:
            from sqlalchemy import text as _text
            for _fix_col in ("limitless_fill", "poly_fill"):
                try:
                    with db.engine.connect() as _c:
                        _row = _c.execute(_text(
                            "SELECT data_type FROM information_schema.columns "
                            "WHERE table_name='signals' AND column_name=:col"
                        ), {"col": _fix_col}).fetchone()
                        if _row and _row[0] != "character varying":
                            _c.execute(_text(
                                f"ALTER TABLE signals ALTER COLUMN {_fix_col} "
                                f"TYPE VARCHAR(10) USING "
                                f"CASE WHEN {_fix_col} IS NULL THEN 'NEUTRAL' "
                                f"     ELSE 'NEUTRAL' END"
                            ))
                            _c.commit()
                            logger.info(
                                "[APP] Migration: fixed signals.%s type → VARCHAR(10)", _fix_col
                            )
                except Exception as _tfe:
                    logger.warning("[APP] Type-fix migration (%s): %s", _fix_col, _tfe)

        # settings table
        for _col, _def in [
            ("martingale_step",      "REAL DEFAULT 0.5"),
            ("martingale_cap",       "INTEGER DEFAULT 10"),
            ("martingale_streak",    "INTEGER DEFAULT 0"),
            ("cooldown_remaining",   "INTEGER DEFAULT 0"),
            ("cooldown_loss_count",  "INTEGER DEFAULT 0"),
            ("cooldown_win_count",   "INTEGER DEFAULT 0"),
            ("martingale_sequence",  "VARCHAR(200) DEFAULT '1,1.5,2,3,4.5,6.7'"),
            ("use_cooldown",         "BOOLEAN DEFAULT FALSE"),
            ("stop_loss_balance",    "REAL"),
            ("use_family_rotation",  "BOOLEAN DEFAULT FALSE"),
            # v2 additions
            ("pair_loss_cooldowns",  "TEXT DEFAULT '{}'"),
            ("dir_saturation_history", "TEXT DEFAULT '[]'"),
            ("use_limitless",        "BOOLEAN DEFAULT TRUE"),
            ("use_polymarket",       "BOOLEAN DEFAULT FALSE"),
            ("poly_position_size",   "REAL DEFAULT 10.0"),
            ("poly_max_price",       "REAL DEFAULT 0.5"),
            # v3 additions
            ("no_execute_pairs",     "TEXT DEFAULT '[]'"),
            ("cooldown_log",         "TEXT DEFAULT '[]'"),
            # v4 additions — Limitless-authoritative resolution + partial-fill tracking
            ("use_limitless_resolution",      "BOOLEAN DEFAULT TRUE"),
            ("martingale_fill_threshold_pct", "REAL DEFAULT 95.0"),
            # v5 additions — Polymarket data parity
            ("poly_fill_threshold_pct",    "REAL DEFAULT 95.0"),
            # v7 additions — independent Polymarket martingale ladder
            ("use_poly_martingale",      "BOOLEAN DEFAULT FALSE"),
            ("poly_martingale_sequence", "VARCHAR(200) DEFAULT '1,1.5,2,3,4.5,6.7'"),
            ("poly_martingale_cap",      "INTEGER DEFAULT 10"),
            ("poly_martingale_streak",   "INTEGER DEFAULT 0"),
            # v9 additions — market vs limit order execution choice, per venue
            ("limitless_order_type", "VARCHAR(4) DEFAULT 'GTC'"),
            ("poly_order_type",      "VARCHAR(4) DEFAULT 'GTC'"),
        ]:
            _add_column("settings", _col, _def)

        if not Settings.query.first():
            db.session.add(Settings(
                mode=os.environ.get("DEFAULT_MODE", "shadow"),
                position_size=float(os.environ.get("DEFAULT_POSITION_SIZE", "10")),
                min_confidence=0.0,
                no_execute_pairs='[]',
                cooldown_log='[]',
            ))
            db.session.commit()
            logger.info("[APP] Default settings seeded")
        else:
            # v3 migration: reset legacy min_confidence=0.58 to 0.0 (disabled)
            # Per-pair thresholds in PAIR_CONFIG are now the sole confidence gates.
            _existing = Settings.query.first()
            _changed  = False
            if _existing and _existing.min_confidence and _existing.min_confidence >= 0.55:
                _existing.min_confidence = 0.0
                _changed = True
                logger.info("[APP] Migration: min_confidence reset to 0.0 (per-pair gates active)")
            # v3.4: XRP-USDT re-enabled for live execution.
            # Remove XRP-USDT from no_execute_pairs on existing deployments.
            if _existing:
                import json as _nep_clr_j
                try:
                    _nep_raw  = _existing.no_execute_pairs or '[]'
                    _nep_list = _nep_raw if isinstance(_nep_raw, list) else _nep_clr_j.loads(_nep_raw)
                    if 'XRP-USDT' in _nep_list:
                        _nep_list.remove('XRP-USDT')
                        _existing.no_execute_pairs = _nep_clr_j.dumps(_nep_list)
                        _changed = True
                        logger.info("[APP] Migration: XRP-USDT removed from no_execute_pairs — live execution re-enabled")
                except Exception:
                    pass
            # v3.3 (superseded by v6 below): SOL-USDT was disabled from live
            # execution here. Removed — see v6, which re-enables it and is
            # now the source of truth for this pair's live-execution default.
            # v6: SOL-USDT re-enabled for live execution (invert flag was also
            # corrected to False around the same time — see PAIR_CONFIG).
            # One-time removal from no_execute_pairs on existing deployments;
            # from here on this list is user-controlled via the Settings page
            # toggle rather than by code changes.
            if _existing:
                import json as _nep_sol2_j
                try:
                    _nep_raw  = _existing.no_execute_pairs or '[]'
                    _nep_list = _nep_raw if isinstance(_nep_raw, list) else _nep_sol2_j.loads(_nep_raw)
                    if 'SOL-USDT' in _nep_list:
                        _nep_list.remove('SOL-USDT')
                        _existing.no_execute_pairs = _nep_sol2_j.dumps(_nep_list)
                        _changed = True
                        logger.info("[APP] Migration: SOL-USDT removed from no_execute_pairs — live execution re-enabled")
                except Exception:
                    pass
            if _changed:
                db.session.commit()
        if not ShadowBalance.query.first():
            db.session.add(ShadowBalance(balance=1000.0))
            db.session.commit()

        # ── Startup wallet diagnostics — Limitless ────────────────────────
        try:
            from limitless_executor import get_maker_address, get_signer_address
            maker  = get_maker_address()
            signer = get_signer_address()
            smart_w_env = os.environ.get("LIMITLESS_SMART_WALLET", "").strip()
            logger.info("[APP] Wallet check — maker=%s  signer=%s", maker, signer)
            if smart_w_env:
                logger.warning(
                    "[APP] LIMITLESS_SMART_WALLET is set (%s). "
                    "If using MetaMask/EOA, REMOVE this env var from Render so maker=signer=EOA.",
                    smart_w_env
                )
            if maker and signer and maker.lower() != signer.lower():
                logger.error(
                    "[APP] MISMATCH: maker=%s != signer=%s — orders WILL fail with "
                    "'Signer does not match'. Fix: ensure LIMITLESS_PRIVATE_KEY is the "
                    "private key for address %s, OR remove LIMITLESS_SMART_WALLET.",
                    maker, signer, maker
                )
            elif maker and signer:
                logger.info("[APP] Wallet OK — maker == signer == %s", maker)
        except Exception as _e:
            logger.warning("[APP] Wallet diagnostics failed: %s", _e)

        # ── Startup wallet diagnostics — Polymarket (CLOB V2) ──────────────
        try:
            from polymarket_executor import validate_credentials, start_heartbeat_thread
            _poly_creds  = validate_credentials()
            _poly_signer = _poly_creds.get("signer_address")
            _poly_funder = _poly_creds.get("funder_address")
            _poly_sigtype = _poly_creds.get("signature_type_name")
            _poly_auth   = _poly_creds.get("auth_level", "NONE")
            _poly_l2     = _poly_creds.get("l2_ready", False)
            _poly_pk     = _poly_creds.get("POLYMARKET_PRIVATE_KEY", False)

            if not _poly_pk:
                logger.warning(
                    "[APP][POLY] POLYMARKET_PRIVATE_KEY not set — "
                    "Polymarket execution disabled. Add key to Render env vars."
                )
            else:
                logger.info(
                    "[APP][POLY] signer=%s funder=%s sig_type=%s | Auth: %s | L2 ready: %s",
                    _poly_signer, _poly_funder, _poly_sigtype, _poly_auth, _poly_l2
                )
                if not _poly_creds.get("funder_configured"):
                    logger.warning(
                        "[APP][POLY] POLYMARKET_FUNDER_ADDRESS not set — required for "
                        "signature_type=%s. Orders WILL fail without it. Find your address "
                        "at polymarket.com/settings.", _poly_creds.get("signature_type")
                    )
                if not _poly_l2:
                    logger.info(
                        "[APP][POLY] L2 credentials not cached yet — will auto-derive on "
                        "first authenticated call (no manual step needed)."
                    )
                else:
                    logger.info("[APP][POLY] L2 auth ready — HMAC signing active for all requests.")
                # V2 requires a heartbeat every ~10s or resting orders are
                # auto-cancelled — start it now so it's running before the
                # first order is ever placed.
                start_heartbeat_thread()
        except Exception as _pe:
            logger.warning("[APP][POLY] Wallet diagnostics failed: %s", _pe)

    # ── Dashboard ─────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Health ────────────────────────────────────────────────────────────────
    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

    # ── Limitless orderbook proxy (avoids CORS from browser) ─────────────────
    @app.route("/api/orderbook")
    def limitless_orderbook():
        """
        Proxy for the Limitless orderbook.

        Correct endpoint (confirmed from limitless_executor internals):
            GET /markets/{slug}/orderbook
        Returns: { bids, asks, tokenId, midpoint, adjustedMidpoint, ... }

        midpoint / adjustedMidpoint is the YES price (0–1).
        The dashboard multiplies by 100 to get YES%.

        Falls back in order:
          1. ?slug= query param
          2. ?condition_id= query param  → fetches /markets first to get slug
          3. Most recent PENDING signal in DB
          4. discover_slug() live call (shadow mode safety net)
        """
        import requests as _req
        API = "https://api.limitless.exchange"

        slug         = request.args.get("slug", "").strip()
        condition_id = request.args.get("condition_id", "").strip()

        # Fallback 1: pull from DB
        if not slug:
            pending = Signal.query.filter(Signal.outcome=="PENDING").order_by(
                Signal.candle_open_time.desc()
            ).first()
            if pending:
                slug         = pending.market_slug  or ""
                condition_id = condition_id or pending.condition_id or ""

        # Fallback 2: condition_id given but no slug — try to resolve slug via market search
        if not slug and condition_id:
            try:
                # Limitless /markets?conditionId=... or search by condition
                r = _req.get(f"{API}/markets", params={"conditionId": condition_id}, timeout=6)
                if r.ok:
                    items = r.json()
                    if isinstance(items, list) and items:
                        slug = items[0].get("slug", "")
                    elif isinstance(items, dict):
                        slug = items.get("slug", "")
            except Exception:
                pass

        # Fallback 3: check in-memory slug cache for the pending signal's symbol
        if not slug:
            try:
                from limitless_executor import _slug_cache
                _pending2 = Signal.query.filter(Signal.outcome=="PENDING").order_by(
                    Signal.candle_open_time.desc()
                ).first()
                if _pending2:
                    _sym_base = _pending2.symbol.replace("-USDT", "")
                    slug = _slug_cache.get(_pending2.symbol) or _slug_cache.get(_sym_base) or ""
            except Exception:
                pass

        # Fallback 4: hit active/slugs endpoint with tight timeout to find the
        # market for this signal's ACTUAL timeframe (was hardcoded to only
        # ever match 15-min slugs — meaning this fallback could never
        # succeed for a 5-min signal, silently returning "no slug" and
        # forcing the gauge to its 50/50/50 placeholder every time this
        # path was needed for a 5-min pair).
        if not slug:
            try:
                _pending3 = Signal.query.filter(Signal.outcome=="PENDING").order_by(
                    Signal.candle_open_time.desc()
                ).first()
                if _pending3:
                    _sym_base = _pending3.symbol.replace("-USDT", "")
                    _tf3 = _pending3.timeframe or "15m"
                    _tf_needle = "15" if _tf3 == "15m" else "5"
                    _r = _req.get(
                        "https://api.limitless.exchange/markets/active/slugs",
                        timeout=4
                    )
                    if _r.ok:
                        _markets = _r.json() if isinstance(_r.json(), list) else []
                        _matches = [
                            m for m in _markets
                            if isinstance(m, dict)
                            and m.get("ticker", "").upper() == _sym_base.upper()
                            # "15" also matches inside "15-min", so check the
                            # 15-min case first and only accept a "5" match
                            # when it's NOT actually a 15-min slug (since "5"
                            # is itself a substring of "15").
                            and (
                                ("15" in m.get("slug", "").lower()) if _tf_needle == "15"
                                else ("5" in m.get("slug", "").lower() and "15" not in m.get("slug", "").lower())
                            )
                        ]
                        if _matches:
                            slug = _matches[0].get("slug", "")
                            logger.info("[orderbook] Resolved slug via active/slugs (tf=%s): %s", _tf3, slug)
                            # Backfill DB
                            _pending3.market_slug = slug
                            db.session.commit()
            except Exception as _fb4e:
                logger.debug("[orderbook] active/slugs fallback: %s", _fb4e)

        if not slug:
            return jsonify({"error": "no market slug available", "pending": True,
                            "hint": "15-min market not yet open on Limitless"}), 404

        try:
            url  = f"{API}/markets/{slug}/orderbook"
            logger.info("[orderbook] Fetching: %s", url)
            resp = _req.get(url, timeout=8)
            logger.info("[orderbook] Response: status=%d slug=%s", resp.status_code, slug)

            if not resp.ok:
                # Log the actual error body from Limitless
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text[:300]
                logger.warning("[orderbook] Upstream %d for slug=%s body=%s",
                               resp.status_code, slug, err_body)
                return jsonify({
                    "error":       f"upstream {resp.status_code}",
                    "slug":        slug,
                    "upstream_body": err_body,
                    "upstream_status": resp.status_code,
                }), resp.status_code

            ob = resp.json() or {}
            logger.info("[orderbook] Keys: %s | adjustedMidpoint=%s midpoint=%s lastTradePrice=%s",
                        list(ob.keys()),
                        ob.get("adjustedMidpoint"),
                        ob.get("midpoint"),
                        ob.get("lastTradePrice"))

            # ── Parse orderbook entries ────────────────────────────────────
            # Limitless returns one orderbook per token (YES or NO).
            # The response may contain:
            #   { bids, asks }                   → single-side (YES token)
            #   { yes: {bids,asks}, no: {bids,asks} } → dual-side
            # We need the BEST ASK (lowest ask) for each token because
            # that is the actual execution price when buying.
            # Best dip tracking must use the ask, not the midpoint.

            def _parse_price(entry):
                """Handle [price, size] arrays, plain floats, or dicts."""
                if isinstance(entry, (list, tuple)) and len(entry) > 0:
                    return float(entry[0])
                if isinstance(entry, dict):
                    return float(entry.get("price", entry.get("p", 0)) or 0)
                try:
                    return float(entry)
                except Exception:
                    return None

            def _best_ask(asks_list):
                """Lowest ask price from a list = cheapest entry for a buyer."""
                prices = []
                for e in (asks_list or []):
                    p = _parse_price(e)
                    if p is not None and p > 0:
                        prices.append(p)
                return min(prices) if prices else None

            def _best_bid(bids_list):
                """Highest bid price from a list."""
                prices = []
                for e in (bids_list or []):
                    p = _parse_price(e)
                    if p is not None and p > 0:
                        prices.append(p)
                return max(prices) if prices else None

            # Handle both response shapes
            yes_ob = ob.get("yes") or {}
            no_ob  = ob.get("no")  or {}

            if yes_ob and no_ob:
                # Dual-side shape: { yes: {bids,asks}, no: {bids,asks} }
                yes_best_ask = _best_ask(yes_ob.get("asks"))
                yes_best_bid = _best_bid(yes_ob.get("bids"))
                no_best_ask  = _best_ask(no_ob.get("asks"))
                no_best_bid  = _best_bid(no_ob.get("bids"))
            else:
                # Single-side shape (YES token only) — NO ask = 1 - YES bid
                yes_best_ask = _best_ask(ob.get("asks"))
                yes_best_bid = _best_bid(ob.get("bids"))
                # NO token price is complement: buying NO = paying (1 - YES_bid)
                no_best_ask  = round(1.0 - yes_best_bid, 6) if yes_best_bid is not None else None
                no_best_bid  = round(1.0 - yes_best_ask, 6) if yes_best_ask is not None else None

            # Midpoint for reference only (NOT used for dip tracking)
            midpoint = (
                ob.get("adjustedMidpoint")
                or ob.get("midpoint")
                or ob.get("lastTradePrice")
            )

            logger.info(
                "[orderbook] yes_ask=%s yes_bid=%s no_ask=%s no_bid=%s midpoint=%s slug=%s",
                yes_best_ask, yes_best_bid, no_best_ask, no_best_bid, midpoint, slug
            )

            # ── Expose clean fields for the frontend gauge ─────────────────
            # _yes_ask_pct : cheapest price to BUY YES token right now (cents on $1)
            # _no_ask_pct  : cheapest price to BUY NO  token right now (cents on $1)
            # _midpoint_pct: market midpoint for reference
            # _yes_pct     : kept for backward compat — same as _yes_ask_pct or midpoint fallback
            #
            # Gauge logic:
            #   UP   signal → bot bought UP (YES) token → track _yes_ask_pct
            #   DOWN signal → bot bought DOWN (NO)  token → track _no_ask_pct
            #   "Best dip" = the lowest that ask price reached = cheapest limit entry seen

            yes_ask_pct = round(yes_best_ask * 100, 2) if yes_best_ask is not None else None
            no_ask_pct  = round(no_best_ask  * 100, 2) if no_best_ask  is not None else None
            mid_pct     = round(float(midpoint) * 100, 2) if midpoint is not None else None

            # _yes_pct: backward compat — prefer real ask, fall back to midpoint
            legacy_yes_pct = yes_ask_pct if yes_ask_pct is not None else mid_pct

            ob["_slug"]          = slug
            ob["_yes_ask_pct"]   = yes_ask_pct
            ob["_no_ask_pct"]    = no_ask_pct
            ob["_yes_bid_pct"]   = round(yes_best_bid * 100, 2) if yes_best_bid is not None else None
            ob["_no_bid_pct"]    = round(no_best_bid  * 100, 2) if no_best_bid  is not None else None
            ob["_midpoint_pct"]  = mid_pct
            ob["_yes_pct"]       = legacy_yes_pct   # kept for any older dashboard code
            ob["_source"]        = "ask" if yes_best_ask is not None else (
                                   "midpoint" if midpoint is not None else "none")

            logger.info(
                "[orderbook] yes_ask_pct=%s no_ask_pct=%s mid_pct=%s source=%s",
                yes_ask_pct, no_ask_pct, mid_pct, ob["_source"]
            )
            return jsonify(ob), 200

        except Exception as _e:
            logger.error("[orderbook] Exception for slug=%s: %s", slug, _e, exc_info=True)
            return jsonify({"error": str(_e), "slug": slug}), 502

    # ── Orderbook live debug — bypasses all caching, full raw dump ──────────
    @app.route("/api/orderbook/live")
    def orderbook_live_debug():
        """
        Direct Limitless orderbook call — no proxy logic, full raw response.
        Shows exactly what Limitless returns so we can diagnose 502 issues.
        Visit: /api/orderbook/live?slug=<slug>
        Or without params to use the current pending signal's slug.
        """
        import requests as _req
        API = "https://api.limitless.exchange"

        slug = request.args.get("slug", "").strip()
        if not slug:
            pending = Signal.query.filter(Signal.outcome=="PENDING").order_by(
                Signal.candle_open_time.desc()
            ).first()
            slug = (pending.market_slug or "") if pending else ""

        if not slug:
            return jsonify({"error": "no slug — pass ?slug= or have a pending signal"}), 400

        result = {"slug": slug, "steps": []}

        # Step 1: fetch orderbook
        try:
            url = f"{API}/markets/{slug}/orderbook"
            result["steps"].append({"action": "GET", "url": url})
            r = _req.get(url, timeout=10)
            result["http_status"] = r.status_code
            result["response_headers"] = dict(r.headers)
            try:
                result["response_body"] = r.json()
            except Exception:
                result["response_body_raw"] = r.text[:500]
            result["steps"].append({"status": r.status_code, "ok": r.ok})
        except Exception as e:
            result["exception"] = str(e)
            result["steps"].append({"exception": str(e)})

        # Step 2: also fetch market details for comparison
        try:
            r2 = _req.get(f"{API}/markets/{slug}", timeout=6)
            result["market_status"] = r2.status_code
            if r2.ok:
                mkt = r2.json()
                result["market_keys"] = list(mkt.keys()) if isinstance(mkt, dict) else "(list)"
                result["market_slug_field"] = mkt.get("slug") if isinstance(mkt, dict) else None
                result["market_active"] = mkt.get("active") if isinstance(mkt, dict) else None
                result["market_deadline"] = mkt.get("deadline") if isinstance(mkt, dict) else None
        except Exception as e:
            result["market_exception"] = str(e)

        return jsonify(result), 200

    # ── Orderbook debug endpoint ─────────────────────────────────────────────
    @app.route("/api/orderbook/debug")
    def orderbook_debug():
        """Returns raw orderbook + parsed fields for the current PENDING signal."""
        import requests as _req
        API = "https://api.limitless.exchange"
        pending = Signal.query.filter(Signal.outcome=="PENDING").order_by(
            Signal.candle_open_time.desc()
        ).first()
        if not pending:
            return jsonify({"error": "no pending signal"}), 404
        slug = pending.market_slug or ""
        if not slug:
            return jsonify({"error": "no slug on pending signal", "signal_id": pending.id}), 404
        try:
            r = _req.get(f"{API}/markets/{slug}/orderbook", timeout=8)
            raw = r.json() if r.ok else {}
            return jsonify({
                "signal_id":   pending.id,
                "symbol":      pending.symbol,
                "direction":   pending.signal_direction,
                "slug":        slug,
                "http_status": r.status_code,
                "raw_keys":    list(raw.keys()),
                "adjustedMidpoint": raw.get("adjustedMidpoint"),
                "midpoint":    raw.get("midpoint"),
                "lastTradePrice": raw.get("lastTradePrice"),
                "tokenId":     raw.get("tokenId"),
                "bids_count":  len(raw.get("bids") or []),
                "asks_count":  len(raw.get("asks") or []),
                "full_raw":    raw,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    # ── Limitless market info proxy ───────────────────────────────────────────
    @app.route("/api/market_info")
    def limitless_market_info():
        """
        Return info for the current PENDING signal's market.
        Works in both shadow and live mode.
        If the DB signal has no condition_id (e.g. old row), we fetch the
        market JSON from Limitless and extract it from the multi-key response.
        If there's no pending signal at all, try a live discover_slug so the
        gauge can start tracking even before the signal row exists.
        """
        import requests as _req

        pending = Signal.query.filter(Signal.outcome=="PENDING").order_by(
            Signal.candle_open_time.desc()
        ).first()

        market_slug  = pending.market_slug  if pending else None
        condition_id = pending.condition_id if pending else None
        symbol       = pending.symbol       if pending else None
        direction    = pending.signal_direction if pending else None
        candle_close = pending.candle_close_time.isoformat() if (pending and pending.candle_close_time) else None

        # If the DB row exists but is missing slug, check the in-memory cache only.
        # Never call discover_slug() here — it has blocking retries (up to 150s)
        # which would stall the gevent request handler.
        if pending and not market_slug:
            try:
                from limitless_executor import _slug_cache
                market_slug = _slug_cache.get(symbol) or None
                if market_slug:
                    pending.market_slug = market_slug
                    db.session.commit()
                    logger.info("[market_info] Backfilled market_slug=%s (cache) for signal id=%s", market_slug, pending.id)
            except Exception as _de:
                logger.warning("[market_info] slug cache lookup failed: %s", _de)

        if not market_slug:
            return jsonify({"pending": bool(pending), "slug_missing": True})

        # Fetch full market JSON with a tight timeout — this is a foreground request handler.
        data = {}
        try:
            url  = f"https://api.limitless.exchange/markets/{market_slug}"
            resp = _req.get(url, timeout=3)
            if resp.ok:
                data = resp.json()
                # Extract condition_id from whichever field the API returns
                fetched_cid = (
                    data.get("conditionId")
                    or data.get("condition_id")
                    or data.get("ctfConditionId")
                    or data.get("condId")
                )
                # Prefer DB value if we already have it; otherwise use fetched
                if not condition_id and fetched_cid:
                    condition_id = fetched_cid
                    if pending:
                        pending.condition_id = condition_id
                        db.session.commit()
                        logger.info("[market_info] Backfilled condition_id=%s for signal id=%s", condition_id, pending.id)
        except Exception as _e:
            logger.warning("[market_info] Market fetch failed: %s", _e)

        return jsonify({
            "pending":         True,
            "signal_id":       pending.id if pending else None,
            "symbol":          symbol,
            "direction":       direction,
            "market_slug":     market_slug,
            "condition_id":    condition_id,
            "candle_close":    candle_close,
            "best_entry_pct":  pending.best_entry_pct if pending else None,
            "market":          data,
        })

    # ── Stats: today ──────────────────────────────────────────────────────────
    @app.route("/api/stats/today")
    def stats_today():
        today       = date.today()
        today_start = datetime.combine(today, datetime.min.time())
        sigs        = Signal.query.filter(
            db.or_(
                Signal.created_at >= today_start,
                Signal.candle_open_time >= today_start,
            )
        ).all()

        wins    = sum(1 for s in sigs if s.outcome == "WIN")
        losses  = sum(1 for s in sigs if s.outcome == "LOSS")
        pending = sum(1 for s in sigs if s.outcome == "PENDING")
        total   = len(sigs)
        wr      = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0

        settings = Settings.query.first()
        shadow   = ShadowBalance.query.first()
        return jsonify({
            "date":          str(today),
            "wins":          wins,
            "losses":        losses,
            "total_signals": total,
            "pending":       pending,
            "win_rate":      wr,
            "mode":          settings.mode if settings else "shadow",
            "shadow_balance": shadow.to_dict() if shadow else {},
        })

    # ── Stats: history ────────────────────────────────────────────────────────
    @app.route("/api/stats/history")
    def stats_history():
        days    = int(request.args.get("days", 30))
        cutoff  = date.today() - timedelta(days=days)
        records = DailyStats.query.filter(
            DailyStats.date >= cutoff
        ).order_by(DailyStats.date.desc()).all()
        return jsonify([r.to_dict() for r in records])

    # ── Stats: per-pair ───────────────────────────────────────────────────────
    @app.route("/api/stats/pairs")
    def pair_stats():
        from signal_engine import get_pair_config, SYMBOLS, TIMEFRAMES

        config = get_pair_config()  # keyed "SYMBOL:timeframe"

        result = {}
        for sym in SYMBOLS:
            result[sym] = {}
            for tf in TIMEFRAMES:
                wins   = Signal.query.filter_by(symbol=sym, timeframe=tf, outcome="WIN").count()
                losses = Signal.query.filter_by(symbol=sym, timeframe=tf, outcome="LOSS").count()
                total  = wins + losses
                cfg    = config.get(f"{sym}:{tf}", {})
                result[sym][tf] = {
                    "wins":      wins,
                    "losses":    losses,
                    "total":     total,
                    "win_rate":  round(wins / total * 100, 1) if total > 0 else None,
                    "threshold": cfg.get("threshold"),
                    "rearm_threshold": cfg.get("rearm_threshold"),
                }
        return jsonify(result)

    @app.route("/api/stats/ladders")
    def ladder_stats():
        """
        Live per-(symbol, timeframe, venue) martingale/breaker state — this
        is the REAL streak/cooldown data since the V2 parallel engine, kept
        independently per stream in PairLadder. Settings.martingale_streak /
        poly_martingale_streak (the old single global counters) are no
        longer updated by the resolve job and would show stale/frozen
        values if read directly — this endpoint is the source of truth now.
        """
        from models import PairLadder
        rows = PairLadder.query.all()
        ladders = [r.to_dict() for r in rows]

        def _max_streak(venue):
            active = [r for r in rows if r.venue == venue]
            return max((r.consecutive_losses or 0) for r in active) if active else 0

        return jsonify({
            "ladders": ladders,
            "max_streak_limitless": _max_streak("limitless"),
            "max_streak_polymarket": _max_streak("polymarket"),
        })

    # ── Signals ───────────────────────────────────────────────────────────────
    @app.route("/api/signals")
    def get_signals():
        page        = int(request.args.get("page", 1))
        per_page    = int(request.args.get("per_page", 50))
        symbol      = request.args.get("symbol")
        symbols_raw = request.args.get("symbols")   # comma-separated multi-pair filter
        timeframe   = request.args.get("timeframe")  # '5m' or '15m'; omitted = both
        outcome     = request.args.get("outcome")
        mode        = request.args.get("mode")
        date_filter = request.args.get("date_filter")  # today|yesterday|7d|30d (legacy presets)
        date_from   = request.args.get("date_from")     # YYYY-MM-DD, inclusive
        date_to     = request.args.get("date_to")       # YYYY-MM-DD, inclusive

        best_dip    = request.args.get("best_dip")   # 5|10|20|30|40 (≤ threshold)

        q = Signal.query
        if symbols_raw:
            _symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
            if _symbols:
                q = q.filter(Signal.symbol.in_(_symbols))
        elif symbol:
            q = q.filter(Signal.symbol == symbol)
        if timeframe: q = q.filter(Signal.timeframe == timeframe)
        if outcome:  q = q.filter(Signal.outcome == outcome)
        if mode:     q = q.filter(Signal.mode == mode)
        if best_dip:
            try:
                # Exclusive ranges — each bucket is independent, no overlap:
                # ≤5%  → 0–5       ≤10% → 5.1–10
                # ≤20% → 10.1–20   ≤30% → 20.1–30   ≤40% → 30.1–40
                _range_map = {
                    "5":  (0,    5.0),
                    "10": (5.0,  10.0),
                    "20": (10.0, 20.0),
                    "30": (20.0, 30.0),
                    "40": (30.0, 40.0),
                }
                _lo, _hi = _range_map.get(str(best_dip), (0, float(best_dip)))
                q = q.filter(
                    Signal.best_entry_pct.isnot(None),
                    Signal.best_entry_pct >  _lo,
                    Signal.best_entry_pct <= _hi
                )
            except (ValueError, TypeError):
                pass

        # Explicit calendar range takes priority over the legacy presets —
        # the Signal Log page's From/To date pickers send date_from/date_to
        # directly; date_filter is kept for any older callers.
        if date_from or date_to:
            try:
                if date_from:
                    _from_dt = datetime.strptime(date_from, "%Y-%m-%d")
                    q = q.filter(Signal.candle_open_time >= _from_dt)
                if date_to:
                    _to_dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
                    q = q.filter(Signal.candle_open_time < _to_dt)
            except ValueError:
                pass
        elif date_filter:
            from datetime import timezone as _tz
            _now   = datetime.now(_tz.utc).replace(tzinfo=None)
            _today = _now.replace(hour=0, minute=0, second=0, microsecond=0)
            if date_filter == "today":
                q = q.filter(Signal.candle_open_time >= _today)
            elif date_filter == "yesterday":
                _ystart = _today - timedelta(days=1)
                q = q.filter(Signal.candle_open_time >= _ystart,
                              Signal.candle_open_time <  _today)
            elif date_filter == "7d":
                q = q.filter(Signal.candle_open_time >= _today - timedelta(days=7))
            elif date_filter == "30d":
                q = q.filter(Signal.candle_open_time >= _today - timedelta(days=30))

        q  = q.order_by(Signal.created_at.desc())
        pg = q.paginate(page=page, per_page=per_page, error_out=False)

        # Aggregate summary over the FULL filtered set (every page, not just
        # the one returned) — this is what answers "how does this exact pair
        # combination + date range perform", independent of pagination.
        _wins    = q.filter(Signal.outcome == "WIN").count()
        _losses  = q.filter(Signal.outcome == "LOSS").count()
        _pending = q.filter(Signal.outcome == "PENDING").count()
        _resolved = _wins + _losses

        return jsonify({
            "signals": [s.to_dict() for s in pg.items],
            "total":   pg.total,
            "pages":   pg.pages,
            "page":    page,
            "summary": {
                "total":    pg.total,
                "wins":     _wins,
                "losses":   _losses,
                "pending":  _pending,
                "win_rate": round(_wins / _resolved * 100, 1) if _resolved > 0 else None,
            },
        })

    @app.route("/api/signals/dip_stats")
    def dip_stats():
        """
        Returns hit counts for each best_dip threshold across all resolved signals.
        Used to show frequency analysis in the History filter UI.
        """
        from sqlalchemy import func as _func
        thresholds = [5, 10, 20, 30, 40]
        resolved = Signal.query.filter(
            Signal.outcome.in_(["WIN", "LOSS"]),
            Signal.best_entry_pct.isnot(None)
        )
        total_resolved = resolved.count()
        total_with_dip = resolved.filter(Signal.best_entry_pct.isnot(None)).count()

        # Exclusive ranges — each bucket contains only signals in that band
        range_map = {
            5:  (0,    5.0),
            10: (5.0,  10.0),
            20: (10.0, 20.0),
            30: (20.0, 30.0),
            40: (30.0, 40.0),
        }
        result = {"total_resolved": total_resolved, "thresholds": {}}
        for t in thresholds:
            lo, hi = range_map[t]
            hits = resolved.filter(
                Signal.best_entry_pct > lo,
                Signal.best_entry_pct <= hi
            ).count()
            wins_at_t = Signal.query.filter(
                Signal.outcome == "WIN",
                Signal.best_entry_pct.isnot(None),
                Signal.best_entry_pct > lo,
                Signal.best_entry_pct <= hi
            ).count()
            losses_at_t = Signal.query.filter(
                Signal.outcome == "LOSS",
                Signal.best_entry_pct.isnot(None),
                Signal.best_entry_pct > lo,
                Signal.best_entry_pct <= hi
            ).count()
            result["thresholds"][str(t)] = {
                "hits":      hits,
                "wins":      wins_at_t,
                "losses":    losses_at_t,
                "range":     f"{lo}–{hi}%",
                "win_rate":  round(wins_at_t / hits * 100, 1) if hits > 0 else None,
                "hit_rate":  round(hits / total_resolved * 100, 1) if total_resolved > 0 else None,
            }
        return jsonify(result)

    @app.route("/api/signals/today")
    def signals_today():
        today       = date.today()
        today_start = datetime.combine(today, datetime.min.time())
        # Query by both created_at AND candle_open_time to catch all of today's signals
        # regardless of timezone edge cases
        sigs = Signal.query.filter(
            db.or_(
                Signal.created_at >= today_start,
                Signal.candle_open_time >= today_start,
            )
        ).order_by(Signal.created_at.desc()).all()
        logger.info(f"[API] /signals/today → {len(sigs)} signals (date={today})")
        return jsonify([s.to_dict() for s in sigs])

    # ── Settings ──────────────────────────────────────────────────────────────
    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        s = Settings.query.first()
        return jsonify(s.to_dict() if s else {})

    @app.route("/api/settings/reset-streak", methods=["POST"])
    def reset_streak():
        """
        Manually resets a martingale streak back to 0 (next stake = base
        rung) without waiting for a win or hitting the loss cap. Body:
        {"platform": "limitless"} or {"platform": "polymarket"}.
        """
        data = request.json or {}
        platform = (data.get("platform") or "limitless").lower()
        s = Settings.query.first()
        if not s:
            return jsonify({"success": False, "error": "no settings row"}), 404
        if platform == "polymarket":
            s.poly_martingale_streak = 0
        else:
            s.martingale_streak = 0
        db.session.commit()
        logger.info("[APP] Martingale streak manually reset to 0 (%s)", platform)
        return jsonify({"success": True, "platform": platform, "settings": s.to_dict()})

    @app.route("/api/settings", methods=["POST"])
    def update_settings():
        data = request.json or {}
        s = Settings.query.first()
        if not s:
            s = Settings(); db.session.add(s)

        if "mode" in data and data["mode"] in ("live", "shadow"):
            s.mode = data["mode"]
        if "position_size" in data:
            s.position_size = max(1.0, min(1000.0, float(data["position_size"])))
        if "use_martingale" in data:
            s.use_martingale = bool(data["use_martingale"])
        if "martingale_sequence" in data:
            # Validate: comma-separated decimals, rounded to 1dp, min 1 entry
            raw = str(data["martingale_sequence"])
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            parsed = []
            for p in parts:
                try:
                    parsed.append(round(float(p), 1))
                except ValueError:
                    pass
            if parsed:
                s.martingale_sequence = ",".join(str(v) for v in parsed)
        if "martingale_step" in data:
            s.martingale_step = max(0.10, float(data["martingale_step"]))
        if "martingale_cap" in data:
            s.martingale_cap = max(1, int(data["martingale_cap"]))
        if "martingale_streak" in data:
            s.martingale_streak = max(0, int(data["martingale_streak"]))
        if "max_contract_price" in data:
            s.max_contract_price = min(float(data["max_contract_price"]), 0.50)
        if "min_confidence" in data:
            s.min_confidence = float(data["min_confidence"])
        if "no_execute_pairs" in data:
            import json as _nep_json
            _nep = data["no_execute_pairs"]
            if isinstance(_nep, list):
                s.no_execute_pairs = _nep_json.dumps(_nep)
            elif isinstance(_nep, str):
                # Validate it's a valid JSON list
                try:
                    _nep_json.loads(_nep)
                    s.no_execute_pairs = _nep
                except Exception:
                    pass
        if "use_cooldown" in data:
            s.use_cooldown = bool(data["use_cooldown"])
        if "stop_loss_balance" in data:
            val = data["stop_loss_balance"]
            s.stop_loss_balance = float(val) if val not in (None, "", 0) else None
        if "use_family_rotation" in data:
            s.use_family_rotation = bool(data["use_family_rotation"])
        if "use_limitless" in data:
            s.use_limitless       = bool(data["use_limitless"])
        if "use_polymarket" in data:
            s.use_polymarket      = bool(data["use_polymarket"])
        if "limitless_order_type" in data and data["limitless_order_type"] in ("GTC", "FOK"):
            s.limitless_order_type = data["limitless_order_type"]
        if "poly_order_type" in data and data["poly_order_type"] in ("GTC", "FOK"):
            s.poly_order_type = data["poly_order_type"]
        if "poly_position_size" in data:
            s.poly_position_size  = float(data["poly_position_size"])
        if "poly_max_price" in data:
            s.poly_max_price      = float(data["poly_max_price"])
        if "use_limitless_resolution" in data:
            s.use_limitless_resolution = bool(data["use_limitless_resolution"])
        if "martingale_fill_threshold_pct" in data:
            s.martingale_fill_threshold_pct = max(0.0, min(100.0, float(data["martingale_fill_threshold_pct"])))
        if "use_poly_martingale" in data:
            s.use_poly_martingale = bool(data["use_poly_martingale"])
        if "poly_martingale_sequence" in data:
            raw = str(data["poly_martingale_sequence"])
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            parsed = []
            for p in parts:
                try:
                    parsed.append(round(float(p), 1))
                except ValueError:
                    pass
            if parsed:
                s.poly_martingale_sequence = ",".join(str(v) for v in parsed)
        if "poly_martingale_cap" in data:
            s.poly_martingale_cap = max(1, int(data["poly_martingale_cap"]))
        if "poly_fill_threshold_pct" in data:
            s.poly_fill_threshold_pct = max(0.0, min(100.0, float(data["poly_fill_threshold_pct"])))

        db.session.commit()
        try:
            socketio.emit("settings_updated", s.to_dict())
        except Exception:
            pass
        return jsonify({"success": True, "settings": s.to_dict()})

    # ── Shadow ────────────────────────────────────────────────────────────────
    @app.route("/api/shadow/balance")
    def shadow_balance():
        sb = ShadowBalance.query.first()
        return jsonify(sb.to_dict() if sb else {"balance": 1000.0, "total_profit_loss": 0.0})

    @app.route("/api/shadow/reset", methods=["POST"])
    def shadow_reset():
        sb = ShadowBalance.query.first()
        if not sb:
            sb = ShadowBalance(); db.session.add(sb)
        sb.balance           = 1000.0
        sb.total_profit_loss = 0.0
        db.session.commit()
        return jsonify({"success": True, "balance": 1000.0})

    # ── Manual triggers ───────────────────────────────────────────────────────
    @app.route("/api/trigger", methods=["POST"])
    def manual_trigger():
        """
        Dashboard 'Force Signal Now' — runs the full job_generate_signal pipeline:
        evaluates, saves to DB, emits WebSocket, and places shadow/live order.
        This is identical to what the scheduler does at :00/:15/:30/:45 UTC.
        After this call the signal appears on the dashboard immediately.
        """
        try:
            from scheduler import job_generate_signal
            import threading
            # Run in a thread so it doesn't block the HTTP response
            # (signal generation takes 5-10s due to OKX candle fetches)
            result = {}
            done  = threading.Event()

            def _run():
                try:
                    job_generate_signal()
                    result['ok'] = True
                except Exception as _e:
                    result['ok']    = False
                    result['error'] = str(_e)
                finally:
                    done.set()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            done.wait(timeout=30)  # wait up to 30s for the job to complete

            if result.get('ok'):
                # Fetch the most recent signal to return its details
                from models import Signal as _Signal
                latest = _Signal.query.order_by(_Signal.created_at.desc()).first()
                sig_data = latest.to_dict() if latest else None
                return jsonify({
                    "success": True,
                    "message": "Signal generation complete — dashboard updated",
                    "signal":  sig_data,
                })
            elif 'error' in result:
                return jsonify({"success": False, "message": result['error']}), 500
            else:
                return jsonify({"success": True, "message": "No qualifying signal this candle", "signal": None})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    # ── Test order — fires a real/shadow order and returns full result ─────────
    @app.route("/api/limitless/status", methods=["GET"])
    def limitless_status():
        """
        Returns credential status for Limitless — mirrors /api/polymarket/status.
        Called by the new Limitless settings page.
        """
        try:
            from limitless_executor import validate_credentials
            creds = validate_credentials()
            return jsonify({"success": True, "credentials": creds})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/polymarket/geo-check", methods=["GET"])
    def polymarket_geo_check():
        """
        Asks Polymarket directly whether THIS server's outbound IP is
        geo-restricted — the definitive way to tell a blocked region apart
        from a genuine credential problem, since both can surface as a plain
        401 from the trading API itself.
        """
        try:
            from polymarket_executor import check_geoblock
            return jsonify(check_geoblock())
        except Exception as e:
            return jsonify({"blocked": None, "error": str(e)}), 500

    @app.route("/api/polymarket/status", methods=["GET"])
    def polymarket_status():
        """
        Returns credential status and auth level (L1/L2/NONE).
        Called by the Settings page to show the credential indicator.
        """
        try:
            from polymarket_executor import validate_credentials
            creds = validate_credentials()
            return jsonify({"success": True, "credentials": creds})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/polymarket/balance", methods=["GET"])
    def polymarket_balance():
        """
        Returns live on-chain USDC + POL balance for the configured
        trading wallet (read directly from Polygon, no Polymarket API call).
        """
        try:
            from polymarket_executor import get_balance
            return jsonify(get_balance())
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/polymarket/derive-key", methods=["POST"])
    def polymarket_derive_key():
        """
        One-time call to generate L2 API credentials from the private key.
        Returns { api_key, api_secret, api_passphrase } — add these to
        Render env vars as POLYMARKET_API_KEY / _SECRET / _PASSPHRASE.

        Must have POLYMARKET_PRIVATE_KEY set first.
        Can only be called in shadow or live mode (not unauthenticated).
        """
        try:
            from polymarket_executor import derive_api_key
            result = derive_api_key()
            if result.get("success"):
                logger.info("[APP] Polymarket L2 credentials derived successfully")
            else:
                logger.warning("[APP] Polymarket L2 derivation failed: %s",
                               result.get("error"))
            return jsonify(result)
        except Exception as e:
            logger.error("[APP] polymarket_derive_key error: %s", e, exc_info=True)
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/polymarket/debug", methods=["GET"])
    def polymarket_debug():
        """
        Walks every layer of the Polymarket connection independently and
        reports the RAW result of each — completely safe, places no real
        order. Each step runs even if an earlier one fails, so a single
        broken layer doesn't hide information about the others.

        Steps: env/address setup -> geo-check -> FRESH auth derivation
        (bypasses any cached credentials, forcing a real round-trip) ->
        one real heartbeat attempt -> unauthenticated Gamma market lookup
        (isolates "can we reach Polymarket's network at all" from "is our
        auth valid", since Gamma needs no credentials).
        """
        import time as _t
        out = {"steps": {}, "diagnosis": None}

        try:
            from polymarket_executor import (
                get_signer_address, get_funder_address, get_signature_type,
                check_geoblock, derive_api_key, send_heartbeat,
                discover_market, _proxy_status_for_log,
            )

            # Step 1 — configuration
            pk_set = bool(os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip())
            signer = get_signer_address() if pk_set else None
            sig_type = get_signature_type()
            funder = get_funder_address()
            out["steps"]["1_config"] = {
                "private_key_set": pk_set,
                "signer_address": signer,
                "signature_type": sig_type,
                "signature_type_name": {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE", 3: "POLY_1271"}.get(sig_type, "unknown"),
                "funder_address": funder,
                "funder_configured": bool(funder),
                "proxy_configured": _proxy_status_for_log(),
            }
            if not pk_set:
                out["diagnosis"] = "POLYMARKET_PRIVATE_KEY is not set — nothing else can work until this is."
                return jsonify(out)
            if sig_type != 0 and not funder:
                out["diagnosis"] = (
                    f"signature_type={sig_type} requires POLYMARKET_FUNDER_ADDRESS, but it's not set. "
                    "This alone would cause every authenticated call to fail. Set it from "
                    "polymarket.com/settings and redeploy."
                )
                return jsonify(out)

            # Step 2 — geo-check (through proxy if configured)
            t0 = _t.time()
            geo = check_geoblock()
            geo["elapsed_s"] = round(_t.time() - t0, 2)
            out["steps"]["2_geoblock"] = geo
            if geo.get("blocked") is True:
                out["diagnosis"] = (
                    f"Polymarket reports this IP ({geo.get('ip')}, {geo.get('country')}) as BLOCKED. "
                    "This is a regional block, not a credential problem — POLYMARKET_PROXY needs to "
                    "be pointed at an unblocked IP, or the current proxy isn't actually being used "
                    "(check proxy_configured above)."
                )
                return jsonify(out)

            # Step 3 — FRESH auth derivation (bypasses cache entirely)
            t0 = _t.time()
            auth = derive_api_key()
            out["steps"]["3_auth_derivation"] = {**auth, "elapsed_s": round(_t.time() - t0, 2)}
            if not auth.get("success"):
                out["diagnosis"] = (
                    f"Deriving L2 API credentials itself failed: {auth.get('error')}. "
                    "This happens before any trading-specific call, so nothing downstream can work "
                    "until this succeeds. If geo-check (step 2) came back clean, this points at the "
                    "signer/signature_type/funder relationship not being set up correctly on "
                    "Polymarket's side for this wallet."
                )
                return jsonify(out)

            # Step 4 — one real heartbeat attempt with the credentials just derived
            t0 = _t.time()
            hb = send_heartbeat()
            out["steps"]["4_heartbeat"] = {**hb, "elapsed_s": round(_t.time() - t0, 2)}

            # Step 5 — unauthenticated Gamma lookup (isolates network/proxy
            # reachability from auth validity — needs no credentials at all)
            t0 = _t.time()
            mkt = discover_market("BTC-USDT", timeframe="15m")
            out["steps"]["5_gamma_market_lookup"] = {
                "found": bool(mkt),
                "slug": mkt.get("slug") if mkt else None,
                "elapsed_s": round(_t.time() - t0, 2),
            }

            # Diagnosis
            if hb.get("success"):
                out["diagnosis"] = "Everything succeeded — auth derivation and a real heartbeat both worked. If the scheduler's own heartbeat is still failing, it may be using stale cached credentials from before this fix; a fresh redeploy (not just a secrets update) should pick up a clean cache."
            elif not out["steps"]["5_gamma_market_lookup"]["found"]:
                out["diagnosis"] = "Even the unauthenticated Gamma lookup failed — this points at basic network/proxy reachability to Polymarket's infrastructure, not credentials specifically."
            else:
                out["diagnosis"] = (
                    f"Auth derivation (step 3) succeeded, but the heartbeat (step 4) still failed: "
                    f"{hb.get('error')}. Gamma (unauthenticated) worked, so basic connectivity is fine — "
                    "this specifically means the derived L2 key isn't accepted for authenticated actions. "
                    "Check step 3's raw response for what Polymarket actually said about the key it created."
                )
            return jsonify(out)

        except Exception as e:
            logger.error("[APP] polymarket_debug error: %s", e, exc_info=True)
            out["error"] = str(e)
            return jsonify(out)

    @app.route("/api/polymarket/test-trade", methods=["POST"])
    def polymarket_test_trade():
        """
        Places ONE real, minimal-size order to verify the full signing +
        submission pipeline end-to-end — the thing /api/polymarket/debug
        deliberately does NOT do, since that endpoint is safe-by-design.

        Requires an explicit {"confirm": true} in the POST body — this
        places a REAL order for REAL money if the account is in live mode.
        Uses a small fixed $1 test size regardless of configured position
        size, and defaults to a GTC limit far from the current price
        (0.02) so it's extremely unlikely to actually fill — the point is
        verifying the order is ACCEPTED (a real order_id comes back), not
        completing a trade. Pass {"confirm": true, "order_type": "FOK"} to
        test a market order instead, which WILL fill if accepted (FOK
        either fills immediately or is cancelled — there's no resting-far-
        from-price option for a market order).
        """
        data = request.get_json(silent=True) or {}
        if data.get("confirm") is not True:
            return jsonify({
                "success": False,
                "error": "Refusing to place a real order without explicit confirmation.",
                "how_to_confirm": 'POST {"confirm": true} to actually run this test. '
                                  'Optionally add {"symbol": "BTC-USDT", "order_type": "GTC"|"FOK"}.',
            }), 400

        symbol = data.get("symbol", "BTC-USDT")
        order_type = data.get("order_type", "GTC")
        if order_type not in ("GTC", "FOK"):
            order_type = "GTC"

        try:
            settings = Settings.query.first()
            mode = settings.mode if settings else "shadow"

            from polymarket_executor import execute_order
            test_price = 0.02 if order_type == "GTC" else 0.99  # GTC: far from market, won't fill. FOK: irrelevant, ignored for FOK anyway.
            logger.warning(
                "[APP] TEST TRADE requested — symbol=%s order_type=%s mode=%s $1.00",
                symbol, order_type, mode
            )
            result = execute_order(symbol, "UP", mode, 1.00, test_price, timeframe="15m", order_type=order_type)
            logger.warning("[APP] TEST TRADE result: %s", result)
            return jsonify({
                "test_config": {"symbol": symbol, "order_type": order_type, "mode": mode, "size_usd": 1.00},
                "result": result,
                "note": ("GTC order placed far from market price (won't fill) — cancel it manually in "
                         "Polymarket's own UI if you don't want it resting." if order_type == "GTC" and result.get("success")
                         else "FOK either filled or was cancelled immediately — check Polymarket's order history for the real outcome."),
            })
        except Exception as e:
            logger.error("[APP] polymarket_test_trade error: %s", e, exc_info=True)
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/signals/<int:signal_id>/best_entry", methods=["POST"])
    def update_best_entry(signal_id):
        data = request.get_json(silent=True) or {}
        pct  = data.get("best_entry_pct")
        if pct is None:
            return jsonify({"error": "best_entry_pct required"}), 400
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return jsonify({"error": "best_entry_pct must be a number"}), 400
        sig = Signal.query.get(signal_id)
        if not sig:
            return jsonify({"error": "signal not found"}), 404
        if sig.best_entry_pct is None or pct < sig.best_entry_pct:
            sig.best_entry_pct = round(pct, 1)
            db.session.commit()
        return jsonify({"success": True, "signal_id": signal_id, "best_entry_pct": sig.best_entry_pct})

    @app.route("/api/resolve", methods=["POST"])
    def manual_resolve():
        from scheduler import job_resolve_outcomes
        job_resolve_outcomes()
        return jsonify({"success": True, "message": "Outcome resolution triggered"})

    # ── USDC Approval status ──────────────────────────────────────────────────
    @app.route("/api/approval-status")
    def approval_status():
        """
        Check USDC approval status and balance for the maker (smart) wallet.
        REQUIRED before any live BUY order will succeed (one-time per contract).

        Two-wallet model:
          maker  = LIMITLESS_SMART_WALLET (holds USDC, must approve exchange)
          signer = EOA derived from LIMITLESS_PRIVATE_KEY (signs orders)
        """
        from limitless_executor import (
            check_usdc_approval,
            validate_credentials, get_maker_address, get_signer_address,
        )

        # The Limitless venue.exchange address is the same contract for ALL markets on Base.
        # We use it directly here — no slug discovery needed just to check USDC approval.
        # Calling discover_slug() here would block the gevent worker for up to 2.5 minutes
        # (5 attempts x 30s) and could race with the scheduler's order placement.
        KNOWN_EXCHANGE = "0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5"

        creds         = validate_credentials()
        exchange_addr = KNOWN_EXCHANGE

        # Check approval for maker (smart) wallet — NOT the EOA signer
        approval = check_usdc_approval(exchange_addr)

        all_pairs = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT"]
        results   = {symbol: approval for symbol in all_pairs}

        maker_addr  = get_maker_address()
        signer_addr = get_signer_address()

        return jsonify({
            "credentials":     creds,
            "exchange":        exchange_addr,
            "approved":        approval.get("approved"),
            "maker_wallet":    maker_addr,    # smart wallet (must approve + hold USDC)
            "signer_wallet":   signer_addr,   # EOA (signs orders only)
            "usdc_balance":    approval.get("usdc_balance"),   # USDC in maker wallet
            "allowance_usdc":  (approval.get("allowance") or 0) / 1e6,
            "approval_status": results,
            "traded_pairs":    all_pairs,
            "instructions": (
                "maker_wallet must approve USDC to the exchange AND hold USDC balance. "
                "approved=true + usdc_balance>0 means live trading is ready. "
                "If approved=false: visit limitless.exchange with your SMART wallet "
                "(not the EOA signer) and place one manual trade — this triggers approval. "
                "Set LIMITLESS_SMART_WALLET=0xefFA501072810d9CE08D5D299bf086Abd6D61b73 "
                "in your Render env vars."
            ),
        })

    # ── Test order ────────────────────────────────────────────────────────────
    @app.route("/api/test-order", methods=["POST"])
    def test_order():
        """
        Fire a test order and return step-by-step diagnostics.
        POST: {"symbol":"BTC-USDT","direction":"UP","size":1,"mode":"shadow"}
        Defaults to current settings mode if "mode" not specified.
        """
        from limitless_executor import (
            discover_slug, fetch_market,
            _extract_exchange, _extract_token_id,
            check_usdc_approval, execute_order,
            validate_credentials,
        )
        import traceback

        data      = request.json or {}
        symbol    = data.get("symbol", "BTC-USDT")
        direction = data.get("direction", "UP")
        size      = float(data.get("size", 1.0))
        settings  = Settings.query.first()
        mode      = data.get("mode", settings.mode if settings else "shadow")

        steps = {}

        # 1. Credentials + profile id
        steps["1_credentials"] = validate_credentials()
        try:
            from limitless_executor import get_owner_id, get_maker_address
            maker = get_maker_address()
            owner_id = get_owner_id(maker) if maker else None
            steps["1_credentials"]["owner_id"] = owner_id
            steps["1_credentials"]["profile_id_ok"] = owner_id is not None
        except Exception as e:
            steps["1_credentials"]["owner_id"] = None
            steps["1_credentials"]["profile_id_ok"] = False
            steps["1_credentials"]["profile_id_error"] = str(e)

        # 2. Slug discovery
        slug     = None
        market   = None
        exchange = None
        token_id = None
        try:
            slug = discover_slug(symbol)
            steps["2_slug"] = {"ok": bool(slug), "slug": slug}
        except Exception as e:
            steps["2_slug"] = {"ok": False, "slug": None, "error": str(e)}

        # 3. Market data (only if slug found)
        if slug:
            try:
                market   = fetch_market(slug)
                exchange = _extract_exchange(market)
                token_id = _extract_token_id(market, direction)
                steps["3_market"] = {
                    "ok":          bool(market),
                    "exchange":    exchange,
                    "token_id":    token_id,
                    "tokens":      market.get("tokens") if market else None,
                    "positionIds": market.get("positionIds") if market else None,
                    "market_keys": list(market.keys()) if market else [],
                }
            except Exception as e:
                steps["3_market"] = {"ok": False, "error": str(e)}
        else:
            steps["3_market"] = {
                "ok": False, "exchange": None, "token_id": None,
                "tokens": None, "positionIds": None, "market_keys": [],
                "note": "Skipped — no slug found in step 2",
            }

        # 4. USDC approval (always show — useful even in shadow mode to pre-check)
        if exchange:
            steps["4_usdc_approval"] = check_usdc_approval(exchange)
        else:
            steps["4_usdc_approval"] = {"skipped": "no exchange address found"}

        # 5. Execute order
        try:
            result = execute_order(symbol, direction, mode, size,
                                   settings.max_contract_price if settings else 0.50)
            steps["5_order"] = result
        except Exception as e:
            steps["5_order"] = {
                "ok": False, "error": str(e),
                "trace": traceback.format_exc(),
            }

        return jsonify({
            "mode": mode, "symbol": symbol,
            "direction": direction, "size": size,
            "steps": steps,
        })

    # ── Debug slug discovery ──────────────────────────────────────────────────
    @app.route("/api/debug-slug")
    def debug_slug():
        """
        Shows raw Limitless API responses for slug discovery.
        Use to diagnose 15-min slug issues.
        GET /api/debug-slug?symbol=BTC-USDT
        """
        import requests as _req
        symbol  = request.args.get("symbol", "BTC-USDT")
        ticker  = symbol.replace("-USDT", "").upper()
        API     = "https://api.limitless.exchange"
        out     = {"symbol": symbol, "ticker": ticker, "steps": {}}

        # Step 1: raw active/slugs — show ALL ticker matches with their fields
        try:
            r   = _req.get(f"{API}/markets/active/slugs", timeout=10)
            raw = r.json() if r.ok else r.text
            ticker_matches = [
                {
                    "ticker":      g.get("ticker"),
                    "slug":        g.get("slug"),
                    "name":        g.get("name"),
                    "deadline":    g.get("deadline"),
                    "frequency":   g.get("frequency"),
                    "subFrequency": g.get("subFrequency"),
                    "markets":     g.get("markets", [])[:2],
                }
                for g in (raw if isinstance(raw, list) else [])
                if ticker.lower() in str(g.get("ticker", "")).lower()
                   or ticker.lower() in str(g.get("slug", "")).lower()
            ]
            out["steps"]["active_slugs"] = {
                "status":         r.status_code,
                "total_groups":   len(raw) if isinstance(raw, list) else None,
                "ticker_matches": ticker_matches,
                "all_tickers":    sorted(set(
                    g.get("ticker", "") for g in (raw if isinstance(raw, list) else [])
                )),
            }
        except Exception as e:
            out["steps"]["active_slugs"] = {"error": str(e)}

        # Step 2: market-pages /crypto/15-min
        try:
            r    = _req.get(f"{API}/market-pages/by-path",
                            params={"path": "/crypto/15-min"}, timeout=10)
            page = r.json() if r.ok else r.text
            out["steps"]["market_page"] = {
                "status":  r.status_code,
                "page_id": page.get("id") if isinstance(page, dict) else None,
                "name":    page.get("name") if isinstance(page, dict) else None,
            }
            if isinstance(page, dict) and page.get("id"):
                r2    = _req.get(f"{API}/market-pages/{page['id']}/markets",
                                 params={"limit": 20, "filters[ticker]": ticker.lower()},
                                 timeout=10)
                mdata = r2.json() if r2.ok else r2.text
                items = (mdata.get("data", mdata) if isinstance(mdata, dict)
                         else mdata if isinstance(mdata, list) else [])
                out["steps"]["market_page"]["markets_status"] = r2.status_code
                out["steps"]["market_page"]["markets_sample"] = [
                    {"slug": m.get("slug"), "ticker": m.get("ticker"),
                     "frequency": m.get("frequency"), "subFrequency": m.get("subFrequency"),
                     "title": m.get("title")}
                    for m in (items[:10] if isinstance(items, list) else [])
                ]
        except Exception as e:
            out["steps"]["market_page"] = {"error": str(e)}

        # Step 3: run actual discover_slug (clears cache first)
        try:
            from limitless_executor import discover_slug, _slug_cache, _is_15min_market
            _slug_cache.pop(symbol, None)
            slug = discover_slug(symbol)
            out["steps"]["discover_slug_result"] = slug
            if slug:
                from limitless_executor import fetch_market, _market_cache
                _market_cache.pop(slug, None)
                market = fetch_market(slug)
                out["steps"]["market_detail"] = {
                    "slug":         slug,
                    "frequency":    market.get("frequency") if market else None,
                    "subFrequency": market.get("subFrequency") if market else None,
                    "title":        market.get("title") if market else None,
                    "tokens":       market.get("tokens") if market else None,
                    "is_15min":     _is_15min_market(
                        slug,
                        (market or {}).get("title", ""),
                        (market or {}).get("frequency", ""),
                        (market or {}).get("subFrequency", ""),
                    ) if market else None,
                }
        except Exception as e:
            out["steps"]["discover_slug_result"] = {"error": str(e)}

        return jsonify(out)

    @app.route("/api/debug-resolution")
    def debug_resolution():
        """
        Manually inspect the Limitless-authoritative resolution path for a
        market and/or order — use this to confirm winningOutcomeIndex /
        metadata.openPrice / order settlement are parsing as expected against
        live data.
          GET /api/debug-resolution?slug=<market_slug>
          GET /api/debug-resolution?order_id=<order_id>
          GET /api/debug-resolution?signal_id=<id>   (fills in slug/order_id/
                                                        intended_usd from the DB)
        """
        from limitless_executor import get_market_resolution, check_order_status_batch

        slug          = request.args.get("slug")
        order_id      = request.args.get("order_id")
        intended_usd  = request.args.get("intended_usd", type=float)
        signal_id     = request.args.get("signal_id", type=int)
        out = {}

        if signal_id:
            sig = Signal.query.get(signal_id)
            if not sig:
                return jsonify({"error": f"signal id={signal_id} not found"}), 404
            slug         = slug or sig.market_slug
            order_id     = order_id or sig.order_id
            intended_usd = intended_usd or sig.position_size
            out["signal"] = sig.to_dict()

        if slug:
            out["market_resolution"] = get_market_resolution(slug)
        if order_id:
            out["order_status"] = check_order_status_batch([order_id]).get(str(order_id))
            if intended_usd:
                from limitless_executor import check_order_filled
                out["check_order_filled"] = check_order_filled(slug, order_id, intended_usd)

        if not slug and not order_id:
            out["error"] = "pass ?slug=, ?order_id=, or ?signal_id="
        return jsonify(out)

    # ── Debug ─────────────────────────────────────────────────────────────────────
    @app.route("/api/debug")
    def debug():
        today       = date.today()
        today_start = datetime.combine(today, datetime.min.time())
        all_sigs    = Signal.query.order_by(Signal.id.desc()).limit(20).all()
        today_sigs  = Signal.query.filter(Signal.created_at >= today_start).all()
        settings    = Settings.query.first()
        shadow      = ShadowBalance.query.first()
        daily       = DailyStats.query.order_by(DailyStats.date.desc()).limit(7).all()
        return jsonify({
            "server_time_utc":     datetime.utcnow().isoformat(),
            "today_date":          str(today),
            "db_path":             app.config["SQLALCHEMY_DATABASE_URI"],
            "total_signals_in_db": Signal.query.count(),
            "today_signals_count": len(today_sigs),
            "today_signals":       [s.to_dict() for s in today_sigs],
            "last_20_signals":     [s.to_dict() for s in all_sigs],
            "settings":            settings.to_dict() if settings else None,
            "shadow_balance":      shadow.to_dict() if shadow else None,
            "daily_stats":         [d.to_dict() for d in daily],
        })

    # ── Live prices ───────────────────────────────────────────────────────────
    @app.route("/api/prices")
    def live_prices():
        import requests as _req
        from signal_engine import SYMBOLS
        OKX = "https://www.okx.com"
        prices = {}

        def _fetch_one(sym):
            try:
                r = _req.get(
                    f"{OKX}/api/v5/market/candles",
                    params={"instId": sym, "bar": "1m", "limit": "2"},
                    timeout=4,
                )
                if not r.ok:
                    return
                rows = r.json().get("data", [])
                if len(rows) < 2:
                    return
                # OKX row: [ts, open, high, low, close, vol, ...]
                close_now  = float(rows[0][4])
                close_prev = float(rows[1][4])
                prices[sym] = {
                    "price":      close_now,
                    "change_pct": round((close_now - close_prev) / close_prev * 100, 2),
                }
            except Exception:
                pass

        # Run all fetches concurrently via gevent greenlets
        try:
            import gevent
            jobs = [gevent.spawn(_fetch_one, sym) for sym in SYMBOLS]
            gevent.joinall(jobs, timeout=5)
        except Exception:
            # Fallback: sequential if gevent unavailable
            for sym in SYMBOLS:
                _fetch_one(sym)

        return jsonify(prices)

    # ── WebSocket ─────────────────────────────────────────────────────────────
    @socketio.on("connect")
    def on_connect():
        logger.info("[WS] Client connected")

    @socketio.on("disconnect")
    def on_disconnect():
        logger.info("[WS] Client disconnected")

    return app


app = create_app()
