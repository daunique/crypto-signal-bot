"""
POLYBOT — Dashboard
Flask app serving the mobile dashboard and JSON API.
"""
from flask import Flask, jsonify, render_template, request
import time
from config import Config


def create_app(tracker=None, discovery=None, journal=None, capital=None,
                listener=None, runtime_settings=None, breaker=None,
                sim_engine=None):
    app = Flask(__name__)

    @app.route("/")
    def index():
        # Simulation-only runs (no tracker/journal wired) get a
        # simplified template; live runs get the full dashboard.
        if sim_engine is not None and tracker is None:
            return render_template("simulation.html")
        return render_template("index.html")

    @app.route("/api/summary")
    def summary():
        if tracker is None:
            return jsonify({"error": "not available in this mode"}), 503
        data = tracker.get_summary()
        data["capital_mode"] = Config.CAPITAL_MODE
        data["unit_size"] = Config.UNIT_SIZE
        if runtime_settings is not None:
            data["duration_mode"] = runtime_settings.duration_mode
        if breaker is not None:
            data["halt_reason"] = breaker.halt_reason
        return jsonify(data)

    @app.route("/api/trades")
    def trades():
        if tracker is None:
            return jsonify([])
        return jsonify(tracker.get_recent_trades(20))

    @app.route("/api/simulation/summary")
    def simulation_summary():
        if sim_engine is None:
            return jsonify({"error": "simulation not running"}), 503
        return jsonify(sim_engine.get_summary())

    @app.route("/api/simulation/trades")
    def simulation_trades():
        if sim_engine is None:
            return jsonify({"error": "simulation not running"}), 503
        return jsonify(sim_engine.get_recent_trades(30))

    @app.route("/api/duration-comparison")
    def duration_comparison():
        """
        5min vs 15min side-by-side stats — combines real executed
        trades with observed-only opportunities so the comparison
        stays honest even while one duration is toggled off.
        """
        if journal is None:
            return jsonify({"error": "not available in this mode"}), 503
        return jsonify(journal.get_duration_comparison())

    @app.route("/api/settings/duration-mode", methods=["GET"])
    def get_duration_mode():
        if runtime_settings is None:
            return jsonify({"error": "runtime settings not available"}), 503
        return jsonify({"duration_mode": runtime_settings.duration_mode})

    @app.route("/api/settings/duration-mode", methods=["POST"])
    def set_duration_mode():
        if runtime_settings is None:
            return jsonify({"error": "runtime settings not available"}), 503
        body = request.get_json(silent=True) or {}
        mode = body.get("duration_mode", "")
        ok = runtime_settings.set_duration_mode(mode)
        if not ok:
            return jsonify({
                "error": f"invalid duration_mode '{mode}', "
                         f"must be one of BOTH/5MIN/15MIN"
            }), 400
        return jsonify({"duration_mode": runtime_settings.duration_mode})

    @app.route("/api/circuit-breaker/status", methods=["GET"])
    def circuit_breaker_status():
        if breaker is None:
            return jsonify({"error": "circuit breaker not available"}), 503
        return jsonify(breaker.status())

    @app.route("/api/circuit-breaker/resume", methods=["POST"])
    def circuit_breaker_resume():
        """
        Manually resume trading after a halt. This existed as
        breaker.resume() but had no reachable entry point — the only
        way to clear a halt was to fully restart the bot process,
        which also loses the halt reason and re-initializes every
        other component unnecessarily. This makes the deliberate,
        review-then-resume workflow actually usable from the
        dashboard without SSH access.
        """
        if breaker is None:
            return jsonify({"error": "circuit breaker not available"}), 503
        if not breaker.is_halted:
            return jsonify({"error": "not currently halted"}), 400
        breaker.resume()
        return jsonify({"halted": breaker.is_halted})

    @app.route("/api/markets")
    def markets():
        result = {}
        now = time.time()
        for pair_id in Config.ACTIVE_PAIRS:
            m = discovery.get_current_market_for_pair(pair_id)
            if not m:
                continue
            yes_ask, no_ask = None, None
            if listener is not None:
                yes_book = listener.book.get(m.get("yes_token"), {})
                no_book  = listener.book.get(m.get("no_token"), {})
                yes_ask = yes_book.get("ask")
                no_ask  = no_book.get("ask")
            is_live = True
            if runtime_settings is not None:
                is_live = runtime_settings.is_pair_live(pair_id)
            result[pair_id] = {
                "slug": m.get("slug", ""),
                "expiry": m.get("expiry", 0),
                "seconds_left": max(0, round(m.get("expiry", 0) - now)),
                "volume_24h": m.get("volume_24h", 0),
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "is_live": is_live,
            }
        return jsonify(result)

    @app.route("/api/health")
    def health():
        ws_connected = listener.ws_connected if listener is not None else None
        return jsonify({
            "status": "ok" if ws_connected else "degraded",
            "websocket_connected": ws_connected,
            "time": time.time(),
        })

    return app
