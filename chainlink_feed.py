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
import time
import logging

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-live-data.polymarket.com"

_CHAINLINK_SYMBOLS = {"BTC", "ETH", "SOL", "XRP"}


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
        ws = websocket.create_connection(WS_URL, timeout=timeout)
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
