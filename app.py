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
            ("symbol",           "VARCHAR(20)"),
            ("candle_open_time", "TIMESTAMP"),
            ("candle_close_time","TIMESTAMP"),
            ("signal_direction", "VARCHAR(4)"),
            ("ml_confidence",    "REAL"),
            ("rsi_14",           "REAL"),
            ("macd_hist",        "REAL"),
            ("adx",              "REAL"),
            ("vol_ratio",        "REAL"),
            ("tier",             "VARCHAR(10)"),
            ("outcome",          "VARCHAR(10) DEFAULT 'PENDING'"),
            ("open_price",       "REAL"),
            ("close_price",      "REAL"),
            ("mode",             "VARCHAR(10) DEFAULT 'shadow'"),
            ("order_id",         "VARCHAR(100)"),
            ("position_size",    "REAL"),
            ("contracts_bought", "REAL"),
            ("contract_price",   "REAL"),
            ("telegram_sent",    "BOOLEAN DEFAULT FALSE"),
            ("market_slug",      "VARCHAR(200)"),
            ("condition_id",     "VARCHAR(100)"),
            ("limitless_fill",   "VARCHAR(10) DEFAULT 'NEUTRAL'"),
        ]:
            _add_column("signals", _col, _def)

        # daily_stats table — add ALL columns including id
        if _is_pg:
            from sqlalchemy import text
            try:
                with db.engine.connect() as _dc:
                    # Add id column if missing (bare INTEGER, sequence wired below)
                    _dc.execute(text(
                        "ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS id INTEGER"
                    ))
                    _dc.commit()
            except Exception as _de:
                logger.warning(f"[APP] daily_stats.id migration: {_de}")
        for _col, _def in [
            ("date",          "DATE UNIQUE"),
            ("total_signals", "INTEGER DEFAULT 0"),
            ("wins",          "INTEGER DEFAULT 0"),
            ("losses",        "INTEGER DEFAULT 0"),
            ("win_rate",      "REAL DEFAULT 0.0"),
            ("mode",          "VARCHAR(10) DEFAULT 'shadow'"),
        ]:
            _add_column("daily_stats", _col, _def)

        # settings table
        for _col, _def in [
            ("invert_direction",    "BOOLEAN DEFAULT FALSE"),
            ("martingale_step",     "REAL DEFAULT 0.5"),
            ("martingale_cap",      "INTEGER DEFAULT 10"),
            ("martingale_streak",   "INTEGER DEFAULT 0"),
            ("cooldown_remaining",  "INTEGER DEFAULT 0"),
            ("cooldown_loss_count", "INTEGER DEFAULT 0"),
            ("cooldown_win_count",  "INTEGER DEFAULT 0"),
            ("martingale_sequence", "VARCHAR(200) DEFAULT '1,1.5,2,3,4.5,6.7'"),
        ]:
            _add_column("settings", _col, _def)

        # ── Fix broken SERIAL sequences on PostgreSQL ──────────────────────────
        # Runs AFTER all column migrations so id cols are guaranteed to exist.
        if _is_pg:
            from sqlalchemy import text
            _seq_fixes = [
                ("signals",       "signals_id_seq"),
                ("daily_stats",   "daily_stats_id_seq"),
                ("settings",      "settings_id_seq"),
                ("shadow_balance","shadow_balance_id_seq"),
            ]
            try:
                with db.engine.connect() as _sc:
                    for _tbl, _seq in _seq_fixes:
                        _sc.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {_seq} START 1"))
                        _sc.execute(text(
                            f"ALTER TABLE {_tbl} ALTER COLUMN id SET DEFAULT nextval('{_seq}')"))
                        _sc.execute(text(
                            f"SELECT setval('{_seq}', "
                            f"GREATEST(COALESCE((SELECT MAX(id) FROM {_tbl}), 0) + 1, 1), false)"))
                    _sc.commit()
                    logger.info("[APP] SERIAL sequence repair complete")
            except Exception as _se:
                logger.warning(f"[APP] Sequence repair warning: {_se}")

        if not Settings.query.first():
            db.session.add(Settings(
                mode=os.environ.get("DEFAULT_MODE", "shadow"),
                position_size=float(os.environ.get("DEFAULT_POSITION_SIZE", "10")),
            ))
            db.session.commit()
            logger.info("[APP] Default settings seeded")
        if not ShadowBalance.query.first():
            db.session.add(ShadowBalance(balance=1000.0))
            db.session.commit()

        # ── Startup wallet diagnostics ─────────────────────────────────────
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

    # ── Dashboard ─────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Health ────────────────────────────────────────────────────────────────
    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

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
        from signal_engine import get_pair_stats, get_pair_config
        live   = get_pair_stats()
        config = get_pair_config()

        result = {}
        for sym in ["BTC-USDT", "ETH-USDT", "SOL-USDT",
                    "XRP-USDT", "BNB-USDT", "DOGE-USDT"]:
            wins   = Signal.query.filter_by(symbol=sym, outcome="WIN").count()
            losses = Signal.query.filter_by(symbol=sym, outcome="LOSS").count()
            total  = wins + losses
            cfg    = config.get(sym, {})
            result[sym] = {
                "wins":      wins,
                "losses":    losses,
                "total":     total,
                "win_rate":  round(wins / total * 100, 1) if total > 0 else None,
                "threshold": cfg.get("threshold", 0.58),
                "tier":      cfg.get("tier", "B"),
            }
        return jsonify(result)

    # ── Signals ───────────────────────────────────────────────────────────────
    @app.route("/api/signals")
    def get_signals():
        page        = int(request.args.get("page", 1))
        per_page    = int(request.args.get("per_page", 50))
        symbol      = request.args.get("symbol")
        outcome     = request.args.get("outcome")
        mode        = request.args.get("mode")
        date_filter = request.args.get("date_filter")  # today|yesterday|7d|30d

        q = Signal.query
        if symbol:  q = q.filter(Signal.symbol == symbol)
        if outcome: q = q.filter(Signal.outcome == outcome)
        if mode:    q = q.filter(Signal.mode == mode)

        if date_filter:
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

        return jsonify({
            "signals": [s.to_dict() for s in pg.items],
            "total":   pg.total,
            "pages":   pg.pages,
            "page":    page,
        })

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
        if "invert_direction" in data:
            s.invert_direction = bool(data["invert_direction"])

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
        Dashboard trigger — evaluates the current signal and returns it for display.
        Does NOT place a live order. Orders are placed exclusively by the cron scheduler
        (job_generate_signal) at :00/:15/:30/:45 UTC to avoid duplicate positions.
        """
        from signal_engine import pick_best_signal
        from models import Settings
        settings = Settings.query.first()
        min_conf = settings.min_confidence if settings else 0.58
        sig = pick_best_signal(min_confidence=min_conf)
        if sig:
            return jsonify({
                "success": True,
                "message": "Signal evaluated (order placed by scheduler only)",
                "signal": {
                    "symbol":     sig.get("symbol"),
                    "direction":  sig.get("direction"),
                    "confidence": sig.get("confidence"),
                    "tier":       sig.get("tier"),
                },
            })
        return jsonify({"success": True, "message": "No qualifying signal this candle", "signal": None})

    # ── Test order — fires a real/shadow order and returns full result ─────────
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
        from signal_engine import fetch_okx_candles, SYMBOLS
        prices = {}
        for sym in SYMBOLS:
            try:
                df = fetch_okx_candles(sym, limit=2)
                if not df.empty and len(df) >= 2:
                    prices[sym] = {
                        "price":      float(df.iloc[-1]["close"]),
                        "change_pct": round(
                            (df.iloc[-1]["close"] - df.iloc[-2]["close"])
                            / df.iloc[-2]["close"] * 100, 2
                        ),
                    }
            except Exception:
                pass
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
