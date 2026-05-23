"""
Limitless Oracle — Backend Server
====================================
Serves the React dashboard and proxies all Limitless Exchange API calls
so that private keys and secrets never leave the server.

Render deployment:
  Build command : cd client && npm install && npm run build
  Start command : cd server && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
"""

import os
import json
import logging
import time
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# Import the executor you already have
import limitless_executor as ex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Flask setup ───────────────────────────────────────────────────
# CLIENT_DIR points to the Vite/React production build
CLIENT_DIR = os.path.join(os.path.dirname(__file__), "..", "client", "dist")

app = Flask(__name__, static_folder=CLIENT_DIR, static_url_path="/")
CORS(app)  # allow the React dev server during local development


# ══════════════════════════════════════════════════════════════════
# HEALTH / KEEP-ALIVE
# ══════════════════════════════════════════════════════════════════

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "ts": int(time.time())})


# ══════════════════════════════════════════════════════════════════
# CREDENTIAL INJECTION HELPER
# ══════════════════════════════════════════════════════════════════

def _inject_credentials(creds: dict):
    """
    Temporarily push credentials from the request body into the OS environment
    so the executor module can read them.  Env vars set here override the
    Render environment variables, so the server-side env vars act as defaults
    and the dashboard settings override them when the user supplies their own.
    """
    mapping = {
        "privateKey":   "LIMITLESS_PRIVATE_KEY",
        "tokenId":      "LIMITLESS_TOKEN_ID",
        "tokenSecret":  "LIMITLESS_TOKEN_SECRET",
    }
    for key, env_var in mapping.items():
        val = (creds or {}).get(key, "").strip()
        if val:
            os.environ[env_var] = val
        # If blank in request, fall back to whatever is already in env (Render env vars)


# ══════════════════════════════════════════════════════════════════
# ORDER EXECUTION ENDPOINT
# ══════════════════════════════════════════════════════════════════

@app.route("/api/limitless/execute", methods=["POST"])
def execute_order():
    """
    Execute a single order on Limitless Exchange (live or shadow).

    Expected JSON body:
    {
        "symbol":           "BTC-USDT",
        "direction":        "UP" | "DOWN",
        "mode":             "live" | "shadow",
        "positionSize":     10,
        "maxContractPrice": 0.50,
        "credentials": {
            "privateKey":   "0x...",
            "tokenId":      "...",
            "tokenSecret":  "..."
        }
    }
    """
    try:
        data = request.get_json(force=True) or {}

        symbol            = data.get("symbol", "BTC-USDT")
        direction         = data.get("direction", "UP")
        mode              = data.get("mode", "shadow")
        position_size     = float(data.get("positionSize", 10))
        max_price         = float(data.get("maxContractPrice", 0.50))
        credentials       = data.get("credentials") or {}

        _inject_credentials(credentials)

        result = ex.execute_order(
            symbol=symbol,
            signal_direction=direction,
            mode=mode,
            position_size_usd=position_size,
            max_contract_price=max_price,
        )

        logger.info("execute_order [%s] %s %s → success=%s", mode, symbol, direction, result.get("success"))
        return jsonify(result)

    except Exception as e:
        logger.exception("execute_order error")
        return jsonify({"success": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# CLAIM WINNINGS
# ══════════════════════════════════════════════════════════════════

@app.route("/api/limitless/claim", methods=["POST"])
def claim_winnings():
    """
    Redeem winning positions.

    Body: { "marketSlug": "...", "direction": "UP"|"DOWN",
            "symbol": "BTC-USDT", "conditionId": "0x...",
            "credentials": {...} }
    """
    try:
        data         = request.get_json(force=True) or {}
        market_slug  = data.get("marketSlug", "")
        direction    = data.get("direction", "UP")
        symbol       = data.get("symbol", "BTC-USDT")
        cond_id      = data.get("conditionId")
        credentials  = data.get("credentials") or {}

        _inject_credentials(credentials)

        result = ex.claim_winnings(
            market_slug=market_slug,
            signal_direction=direction,
            symbol=symbol,
            cond_id=cond_id,
        )
        return jsonify(result)

    except Exception as e:
        logger.exception("claim_winnings error")
        return jsonify({"success": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# ORDER STATUS CHECK
# ══════════════════════════════════════════════════════════════════

@app.route("/api/limitless/order-status", methods=["POST"])
def order_status():
    """
    Check if a specific order was filled.

    Body: { "marketSlug": "...", "orderId": "...", "credentials": {...} }
    """
    try:
        data        = request.get_json(force=True) or {}
        market_slug = data.get("marketSlug", "")
        order_id    = data.get("orderId", "")
        credentials = data.get("credentials") or {}

        _inject_credentials(credentials)

        result = ex.check_order_filled(market_slug=market_slug, order_id=order_id)
        return jsonify(result)

    except Exception as e:
        logger.exception("order_status error")
        return jsonify({"success": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# CREDENTIAL VALIDATION
# ══════════════════════════════════════════════════════════════════

@app.route("/api/limitless/validate", methods=["POST"])
def validate_credentials():
    """
    Validate wallet and API credentials without placing an order.
    Safe to call from the Settings page.
    """
    try:
        data        = request.get_json(force=True) or {}
        credentials = data.get("credentials") or {}
        _inject_credentials(credentials)
        result = ex.validate_credentials()
        return jsonify(result)
    except Exception as e:
        logger.exception("validate_credentials error")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# MARKET DISCOVERY (optional — frontend calls OKX directly, but
# this endpoint lets you query the Limitless slug for a symbol)
# ══════════════════════════════════════════════════════════════════

@app.route("/api/limitless/slug/<symbol>")
def get_slug(symbol):
    try:
        slug = ex.discover_slug(symbol)
        return jsonify({"symbol": symbol, "slug": slug})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# SERVE REACT FRONTEND (catch-all — must be last)
# ══════════════════════════════════════════════════════════════════

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    """Serve the compiled React app for all non-API routes."""
    full_path = os.path.join(app.static_folder, path)
    if path and os.path.exists(full_path):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


# ══════════════════════════════════════════════════════════════════
# LOCAL DEV
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
