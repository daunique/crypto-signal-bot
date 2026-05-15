"""
Main Flask application — Polymarket edition
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
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:////tmp/signals.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="gevent",
                      logger=False, engineio_logger=False)

    with app.app_context():
        db.create_all()
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

    # ── Dashboard ─────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    # ── Health ────────────────────────────────────────────────────────────────
    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "time": datetime.utcnow().isoformat(),
                        "exchange": "polymarket"})

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
            "position_size": settings.position_size if settings else 10,
            "shadow_balance": shadow.to_dict() if shadow else None,
            "exchange":      "polymarket",
        })

    # ── Stats: history ────────────────────────────────────────────────────────
    @app.route("/api/stats/history")
    def stats_history():
        days = int(request.args.get("days", 7))
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = DailyStats.query.filter(
            DailyStats.date >= cutoff.date()
        ).order_by(DailyStats.date.desc()).all()
        return jsonify([r.to_dict() for r in rows])

    # ── Signals ───────────────────────────────────────────────────────────────
    @app.route("/api/signals")
    def get_signals():
        limit  = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        sigs   = Signal.query.order_by(Signal.created_at.desc()).offset(offset).limit(limit).all()
        return jsonify([s.to_dict() for s in sigs])

    # ── Settings ──────────────────────────────────────────────────────────────
    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        s = Settings.query.first()
        return jsonify(s.to_dict() if s else {})

    @app.route("/api/settings", methods=["POST"])
    def update_settings():
        data = request.get_json() or {}
        s    = Settings.query.first()
        if not s:
            s = Settings()
            db.session.add(s)

        if "mode" in data:
            if data["mode"] not in ("shadow", "live"):
                return jsonify({"error": "mode must be 'shadow' or 'live'"}), 400
            s.mode = data["mode"]
        if "position_size" in data:
            s.position_size = float(data["position_size"])
        if "max_contract_price" in data:
            s.max_contract_price = float(data["max_contract_price"])
        if "min_confidence" in data:
            s.min_confidence = float(data["min_confidence"])
        if "use_martingale" in data:
            s.use_martingale = bool(data["use_martingale"])
        if "martingale_multiplier" in data:
            s.martingale_multiplier = float(data["martingale_multiplier"])

        s.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify(s.to_dict())

    # ── Polymarket: USDC approval status ──────────────────────────────────────
    @app.route("/api/approval-status")
    def approval_status():
        try:
            from polymarket_executor import check_approval_status
            return jsonify(check_approval_status())
        except Exception as e:
            return jsonify({"error": str(e), "ready": False}), 500

    # ── Polymarket: trigger USDC approval ─────────────────────────────────────
    @app.route("/api/approve-usdc", methods=["POST"])
    def approve_usdc_route():
        try:
            from polymarket_executor import approve_usdc
            ok = approve_usdc()
            return jsonify({"success": ok})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ── Polymarket: USDC balance ───────────────────────────────────────────────
    @app.route("/api/balance")
    def get_balance():
        try:
            from polymarket_executor import get_usdc_balance
            bal = get_usdc_balance()
            return jsonify({"usdc_balance": bal, "exchange": "polymarket"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Polymarket: search markets ─────────────────────────────────────────────
    @app.route("/api/markets/search")
    def search_markets():
        query = request.args.get("q", "BTC")
        try:
            from polymarket_executor import search_market
            results = search_market(query)
            return jsonify(results)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Polymarket: open positions ─────────────────────────────────────────────
    @app.route("/api/positions")
    def get_positions():
        try:
            from polymarket_executor import get_open_positions
            return jsonify(get_open_positions())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Pair stats ────────────────────────────────────────────────────────────
    @app.route("/api/pair-stats")
    def pair_stats():
        try:
            from signal_engine import get_pair_stats
            return jsonify(get_pair_stats())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Manual retrain ────────────────────────────────────────────────────────
    @app.route("/api/retrain", methods=["POST"])
    def manual_retrain():
        import threading
        def _retrain():
            with app.app_context():
                from signal_engine import retrain_all
                retrain_all(limit=300)
        threading.Thread(target=_retrain, daemon=True).start()
        return jsonify({"status": "retraining started"})

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
