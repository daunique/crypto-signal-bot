"""
Main Flask application
Serves dashboard, API routes, and WebSocket events
NOTE: eventlet.monkey_patch() is called in wsgi.py / main.py BEFORE this import
"""
import os
import logging
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, jsonify, request
from extensions import db, socketio
from models import Signal, DailyStats, Settings, ShadowBalance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    # Use /tmp for SQLite on Render (ephemeral but survives restarts within same instance)
    # For persistence across deploys, set DATABASE_URL to a PostgreSQL URL in Render env vars
    default_db = "sqlite:////tmp/signals.db"
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me-32chars")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", default_db)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="eventlet",
                      logger=False, engineio_logger=False)

    with app.app_context():
        db.create_all()
        if not Settings.query.first():
            s = Settings(
                mode=os.environ.get("DEFAULT_MODE", "shadow"),
                position_size=float(os.environ.get("DEFAULT_POSITION_SIZE", "10")),
            )
            db.session.add(s)
            db.session.commit()
            logger.info("[APP] Default settings seeded")
        if not ShadowBalance.query.first():
            db.session.add(ShadowBalance(balance=1000.0))
            db.session.commit()

    # ─── Dashboard ────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    # ─── API: Health check ────────────────────────────────────────────────────
    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

    # ─── API: Stats ───────────────────────────────────────────────────────────
    @app.route("/api/stats/today")
    def stats_today():
        today = date.today()
        today_start = datetime.combine(today, datetime.min.time())

        today_signals = Signal.query.filter(
            Signal.created_at >= today_start
        ).all()

        wins    = sum(1 for s in today_signals if s.outcome == "WIN")
        losses  = sum(1 for s in today_signals if s.outcome == "LOSS")
        pending = sum(1 for s in today_signals if s.outcome == "PENDING")
        total   = len(today_signals)
        win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0

        settings = Settings.query.first()
        shadow   = ShadowBalance.query.first()

        return jsonify({
            "date":          str(today),
            "wins":          wins,
            "losses":        losses,
            "total_signals": total,
            "pending":       pending,
            "win_rate":      win_rate,
            "mode":          settings.mode if settings else "shadow",
            "shadow_balance": shadow.to_dict() if shadow else {},
        })

    @app.route("/api/stats/history")
    def stats_history():
        days   = int(request.args.get("days", 30))
        cutoff = date.today() - timedelta(days=days)
        records = DailyStats.query.filter(
            DailyStats.date >= cutoff
        ).order_by(DailyStats.date.desc()).all()
        return jsonify([r.to_dict() for r in records])

    # ─── API: Signals ─────────────────────────────────────────────────────────
    @app.route("/api/signals")
    def get_signals():
        page    = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        symbol  = request.args.get("symbol")
        outcome = request.args.get("outcome")
        mode    = request.args.get("mode")

        q = Signal.query
        if symbol:  q = q.filter(Signal.symbol == symbol)
        if outcome: q = q.filter(Signal.outcome == outcome)
        if mode:    q = q.filter(Signal.mode == mode)
        q = q.order_by(Signal.created_at.desc())
        paginated = q.paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            "signals": [s.to_dict() for s in paginated.items],
            "total":   paginated.total,
            "pages":   paginated.pages,
            "page":    page,
        })

    @app.route("/api/signals/today")
    def signals_today():
        today       = date.today()
        today_start = datetime.combine(today, datetime.min.time())
        signals = Signal.query.filter(
            Signal.created_at >= today_start
        ).order_by(Signal.created_at.desc()).all()
        logger.info(f"[API] /signals/today → {len(signals)} signals for {today}")
        return jsonify([s.to_dict() for s in signals])

    # ─── API: Settings ────────────────────────────────────────────────────────
    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        s = Settings.query.first()
        return jsonify(s.to_dict() if s else {})

    @app.route("/api/settings", methods=["POST"])
    def update_settings():
        data = request.json
        s = Settings.query.first()
        if not s:
            s = Settings()
            db.session.add(s)

        if "mode" in data and data["mode"] in ("live", "shadow"):
            s.mode = data["mode"]
        if "position_size" in data:
            s.position_size = max(1.0, min(1000.0, float(data["position_size"])))
        if "use_martingale" in data:
            s.use_martingale = bool(data["use_martingale"])
        if "martingale_multiplier" in data:
            s.martingale_multiplier = float(data["martingale_multiplier"])
        if "max_contract_price" in data:
            s.max_contract_price = min(float(data["max_contract_price"]), 0.50)
        if "min_confidence" in data:
            s.min_confidence = float(data["min_confidence"])

        db.session.commit()
        try:
            socketio.emit("settings_updated", s.to_dict())
        except Exception:
            pass
        return jsonify({"success": True, "settings": s.to_dict()})

    # ─── API: Shadow Balance ──────────────────────────────────────────────────
    @app.route("/api/shadow/balance")
    def shadow_balance():
        sb = ShadowBalance.query.first()
        return jsonify(sb.to_dict() if sb else {"balance": 1000.0, "total_profit_loss": 0.0})

    @app.route("/api/shadow/reset", methods=["POST"])
    def shadow_reset():
        sb = ShadowBalance.query.first()
        if not sb:
            sb = ShadowBalance()
            db.session.add(sb)
        sb.balance          = 1000.0
        sb.total_profit_loss = 0.0
        db.session.commit()
        return jsonify({"success": True, "balance": 1000.0})

    # ─── API: Manual triggers (testing) ───────────────────────────────────────
    @app.route("/api/trigger", methods=["POST"])
    def manual_trigger():
        from scheduler import job_generate_signal
        job_generate_signal()
        return jsonify({"success": True, "message": "Signal evaluation triggered"})

    @app.route("/api/resolve", methods=["POST"])
    def manual_resolve():
        from scheduler import job_resolve_outcomes
        job_resolve_outcomes()
        return jsonify({"success": True, "message": "Outcome resolution triggered"})

    # ─── API: Debug ───────────────────────────────────────────────────────────
    @app.route("/api/debug")
    def debug():
        today       = date.today()
        today_start = datetime.combine(today, datetime.min.time())
        all_signals  = Signal.query.order_by(Signal.id.desc()).limit(20).all()
        today_sigs   = Signal.query.filter(Signal.created_at >= today_start).all()
        settings     = Settings.query.first()
        shadow       = ShadowBalance.query.first()
        daily        = DailyStats.query.order_by(DailyStats.date.desc()).limit(7).all()

        return jsonify({
            "server_time_utc":     datetime.utcnow().isoformat(),
            "today_date":          str(today),
            "today_start":         today_start.isoformat(),
            "db_url":              app.config["SQLALCHEMY_DATABASE_URI"].split("@")[-1],  # hide creds
            "total_signals_in_db": Signal.query.count(),
            "today_signals_count": len(today_sigs),
            "today_signals":       [s.to_dict() for s in today_sigs],
            "last_20_signals":     [s.to_dict() for s in all_signals],
            "settings":            settings.to_dict() if settings else None,
            "shadow_balance":      shadow.to_dict() if shadow else None,
            "daily_stats":         [d.to_dict() for d in daily],
        })

    # ─── API: Per-pair stats ──────────────────────────────────────────────────
    @app.route("/api/stats/pairs")
    def pair_stats():
        from signal_engine import get_pair_stats
        live = get_pair_stats()

        # Enrich with DB win/loss counts per pair (all time)
        from sqlalchemy import func
        db_stats = {}
        for sym in ["BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","BNB-USDT","DOGE-USDT"]:
            wins   = Signal.query.filter_by(symbol=sym, outcome="WIN").count()
            losses = Signal.query.filter_by(symbol=sym, outcome="LOSS").count()
            total  = wins + losses
            db_stats[sym] = {
                "wins":     wins,
                "losses":   losses,
                "total":    total,
                "win_rate": round(wins / total * 100, 1) if total > 0 else None,
                "threshold": live.get(sym, {}).get("threshold", 0.58),
            }
        return jsonify(db_stats)

    # ─── API: Live prices ─────────────────────────────────────────────────────
    @app.route("/api/prices")
    def live_prices():
        from signal_engine import fetch_okx_candles, SYMBOLS
        prices = {}
        for sym in SYMBOLS:
            try:
                df = fetch_okx_candles(sym, limit=2)
                if not df.empty and len(df) >= 2:
                    prices[sym] = {
                        "price": float(df.iloc[-1]["close"]),
                        "change_pct": round(
                            (df.iloc[-1]["close"] - df.iloc[-2]["close"])
                            / df.iloc[-2]["close"] * 100, 2
                        ),
                    }
            except Exception:
                pass
        return jsonify(prices)

    # ─── WebSocket events ──────────────────────────────────────────────────────
    @socketio.on("connect")
    def on_connect():
        logger.info("[WS] Client connected")

    @socketio.on("disconnect")
    def on_disconnect():
        logger.info("[WS] Client disconnected")

    return app


app = create_app()
