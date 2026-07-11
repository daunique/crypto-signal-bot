"""
Chainlink Data Streams — shared crypto price fetch
══════════════════════════════════════════════════════════════════════════════
Both Limitless and Polymarket now resolve their short-duration crypto markets
against Chainlink Data Streams for the same underlying assets (Limitless
migrated from Pyth; Polymarket's 15-min crypto markets have used it from the
start). Rather than build two platform-specific price fetchers, this module
asks Chainlink directly, via Polymarket's public real-time data socket, which
requires no authentication and exposes exactly the feed both platforms settle
against: docs.polymarket.com/market-data/websocket/rtds

Supported symbols (Chainlink side of that feed): BTC, ETH, SOL, XRP.
BNB and DOGE are not on this feed — callers should keep an OKX fallback for
those two pairs specifically.

This opens a short-lived connection per call (connect → subscribe → read one
tick → disconnect) rather than keeping a persistent background connection —
simpler to reason about and consistent with the rest of this codebase's
on-demand, synchronous style. The trade-off is a small connection-setup cost
per call (typically well under a second) rather than an always-warm feed.
"""

import json
import os
import time
import logging

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-live-data.polymarket.com"

_CHAINLINK_SYMBOLS = {"BTC", "ETH", "SOL", "XRP"}

# Same proxy env var and format as polymarket_executor.py — this WebSocket
# endpoint is also Polymarket's own infrastructure, so it's subject to the
# same potential regional block. See polymarket_executor._get_proxies() for
# the full reasoning; duplicated here in miniature rather than imported, to
# keep this module's only dependency (websocket-client) independent of
# polymarket_executor's much larger surface (web3, eth_account, etc.).
_PROXY_ENV_VAR = "POLYMARKET_PROXY"


def _get_ws_proxy_kwargs() -> dict:
    """HOST:PORT:USER:PASS -> websocket-client's create_connection kwargs. Empty dict if unset."""
    raw = os.environ.get(_PROXY_ENV_VAR, "").strip()
    if not raw:
        return {}
    parts = raw.split(":", 3)
    if len(parts) != 4:
        logger.error("[CHAINLINK] %s set but not HOST:PORT:USER:PASS — connecting directly.", _PROXY_ENV_VAR)
        return {}
    host, port, user, password = parts
    try:
        port = int(port)
    except ValueError:
        logger.error("[CHAINLINK] %s has a non-numeric port — connecting directly.", _PROXY_ENV_VAR)
        return {}
    return {
        "http_proxy_host": host,
        "http_proxy_port": port,
        "http_proxy_auth": (user, password),
        "proxy_type": "http",
    }


def _ticker_to_chainlink_symbol(symbol: str) -> str | None:
    """'BTC-USDT' -> 'btc/usd'. Returns None for pairs not on this feed (BNB, DOGE)."""
    ticker = symbol.upper().replace("-USDT", "").replace("-USD", "")
    if ticker not in _CHAINLINK_SYMBOLS:
        return None
    return f"{ticker.lower()}/usd"


def get_chainlink_price(symbol: str, timeout: float = 5.0) -> dict:
    """
    Fetches the current Chainlink price for a symbol (e.g. "BTC-USDT").

    Returns dict: price(float|None), symbol(str), source("chainlink"|None),
    error(str|None). A None price with no error just means the symbol isn't
    on this feed (BNB/DOGE) — callers should fall back to OKX for those.
    """
    chainlink_symbol = _ticker_to_chainlink_symbol(symbol)
    if not chainlink_symbol:
        return {"price": None, "symbol": symbol, "source": None,
                "error": f"{symbol} is not on the Chainlink feed (BTC/ETH/SOL/XRP only)"}

    try:
        import websocket  # websocket-client package
    except ImportError:
        return {"price": None, "symbol": symbol, "source": None,
                "error": "websocket-client not installed"}

    ws = None
    try:
        ws = websocket.create_connection(WS_URL, timeout=timeout, **_get_ws_proxy_kwargs())
        sub_msg = json.dumps({
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "filters": chainlink_symbol,
            }],
        })
        ws.send(sub_msg)

        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            ws.settimeout(remaining)
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue

            payload = msg.get("payload") or {}
            msg_symbol = (payload.get("symbol") or "").lower()
            # Feed uses concatenated form for Binance ("btcusdt") but we
            # subscribed with the slash form Chainlink itself uses
            # ("btc/usd") — match on the asset prefix defensively either way.
            asset = chainlink_symbol.split("/")[0]
            if asset in msg_symbol.replace("/", ""):
                value = payload.get("value")
                if value is not None:
                    return {"price": float(value), "symbol": symbol,
                            "source": "chainlink", "error": None}
        return {"price": None, "symbol": symbol, "source": None,
                "error": f"no tick received within {timeout}s"}
    except Exception as e:
        logger.warning("[CHAINLINK] price fetch failed for %s: %s", symbol, e)
        return {"price": None, "symbol": symbol, "source": None, "error": str(e)}
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
