"""
Limitless Exchange order executor
═══════════════════════════════════════════════════════════════════════════
SOURCE OF TRUTH: https://docs.limitless.exchange/developers/authentication
               + https://docs.limitless.exchange/developers/eip712-signing

CORRECT flow:
  1. GET /markets/:slug      → no auth needed
                               returns venue.exchange + positionIds
  2. HMAC-sign every request  → lmts-api-key, lmts-timestamp, lmts-signature
                               needed on POST /orders and GET /positions
  3. EIP-712-sign the order   → signature field inside the order body
                               uses venue.exchange as verifyingContract

WHAT IS NOT USED:
  - No ownerId in the order payload
  - No /profile endpoint
  - No X-API-Key header (deprecated — HMAC replaces it)

DIRECTION REVERSAL (intentional contrarian strategy):
  ML signal UP   → place TRADE DOWN  (buy NO token  = positionIds[1])
  ML signal DOWN → place TRADE UP    (buy YES token = positionIds[0])
  The signal_direction saved in DB is the ML prediction (unchanged).
  The trade_direction field records what was actually placed.
═══════════════════════════════════════════════════════════════════════════
"""
import os
import time
import hmac
import hashlib
import base64
import json
import logging
import requests
from datetime import datetime, timezone
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

API_BASE  = "https://api.limitless.exchange"
CHAIN_ID  = 8453          # Base mainnet
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

SYMBOL_TO_SLUG = {
    "BTC-USDT":  "btc-usdt-15-min",
    "ETH-USDT":  "eth-usdt-15-min",
    "SOL-USDT":  "sol-usdt-15-min",
    "XRP-USDT":  "xrp-usdt-15-min",
    "BNB-USDT":  "bnb-usdt-15-min",
    "DOGE-USDT": "doge-usdt-15-min",
}

_market_cache: dict = {}


# ─── HMAC request signing ─────────────────────────────────────────────────────
# Required on: POST /orders, GET /positions
# NOT required on: GET /markets/:slug

def _hmac_headers(method: str, path: str, body: str = "") -> dict:
    """
    Build HMAC auth headers per the official docs.
    Canonical message: {timestamp}\\n{METHOD}\\n{path+query}\\n{body}
    """
    token_id   = os.environ.get("LIMITLESS_TOKEN_ID", "")
    secret_b64 = os.environ.get("LIMITLESS_TOKEN_SECRET", "")

    if not token_id or not secret_b64:
        raise ValueError(
            "LIMITLESS_TOKEN_ID and LIMITLESS_TOKEN_SECRET are required.\n"
            "Derive them via POST /auth/api-tokens/derive on limitless.exchange.\n"
            "The secret is shown ONCE — store it immediately in Render env vars."
        )

    timestamp = datetime.now(timezone.utc).isoformat()
    message   = f"{timestamp}\n{method}\n{path}\n{body}"

    sig = base64.b64encode(
        hmac.new(
            base64.b64decode(secret_b64),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    return {
        "lmts-api-key":   token_id,
        "lmts-timestamp": timestamp,
        "lmts-signature": sig,
        "Content-Type":   "application/json",
    }


def _hmac_post(path: str, payload: dict) -> requests.Response:
    body = json.dumps(payload, separators=(",", ":"))
    return requests.post(
        f"{API_BASE}{path}",
        headers=_hmac_headers("POST", path, body),
        data=body,
        timeout=15,
    )


def _hmac_get(path: str, params: dict | None = None) -> requests.Response:
    query     = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    full_path = path + query
    return requests.get(
        f"{API_BASE}{full_path}",
        headers=_hmac_headers("GET", full_path, ""),
        timeout=10,
    )


# ─── EIP-712 order signing ────────────────────────────────────────────────────
# Signs the ORDER STRUCT — separate from HMAC which signs the HTTP request.
# verifyingContract = venue.exchange from GET /markets/:slug

def _eip712_sign(order_data: dict, verifying_contract: str) -> str:
    pk = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    if not pk:
        raise ValueError("LIMITLESS_PRIVATE_KEY not set")

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt",          "type": "uint256"},
                {"name": "maker",         "type": "address"},
                {"name": "signer",        "type": "address"},
                {"name": "taker",         "type": "address"},
                {"name": "tokenId",       "type": "uint256"},
                {"name": "makerAmount",   "type": "uint256"},
                {"name": "takerAmount",   "type": "uint256"},
                {"name": "expiration",    "type": "uint256"},
                {"name": "nonce",         "type": "uint256"},
                {"name": "feeRateBps",    "type": "uint256"},
                {"name": "side",          "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name":              "Limitless CTF Exchange",
            "version":           "1",
            "chainId":           CHAIN_ID,
            "verifyingContract": Web3.to_checksum_address(verifying_contract),
        },
        "message": {
            "salt":          order_data["salt"],
            "maker":         Web3.to_checksum_address(order_data["maker"]),
            "signer":        Web3.to_checksum_address(order_data["signer"]),
            "taker":         Web3.to_checksum_address(order_data["taker"]),
            "tokenId":       int(order_data["tokenId"]),
            "makerAmount":   order_data["makerAmount"],
            "takerAmount":   order_data["takerAmount"],
            "expiration":    order_data["expiration"],
            "nonce":         order_data["nonce"],
            "feeRateBps":    order_data["feeRateBps"],
            "side":          order_data["side"],
            "signatureType": order_data["signatureType"],
        },
    }
    encoded = encode_typed_data(typed_data)
    return Account.from_key(pk).sign_message(encoded).signature.hex()


# ─── Market data ──────────────────────────────────────────────────────────────
# GET /markets/:slug requires NO auth — public endpoint

def fetch_market(slug: str) -> dict | None:
    if slug in _market_cache:
        return _market_cache[slug]
    try:
        # No auth on market fetch
        resp = requests.get(f"{API_BASE}/markets/{slug}", timeout=10)
        if resp.status_code == 404:
            logger.warning(f"Market not found: {slug}")
            return None
        resp.raise_for_status()
        data = resp.json()
        _market_cache[slug] = data
        return data
    except Exception as e:
        logger.error(f"fetch_market({slug}): {e}")
        return None


def resolve_slug(symbol: str) -> str | None:
    """Search active markets (no auth) then fall back to static map."""
    try:
        resp = requests.get(
            f"{API_BASE}/markets/active",
            params={"category": "crypto"},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json() if isinstance(resp.json(), list) else resp.json().get("markets", [])
        token = symbol.replace("-USDT", "").lower()
        for m in markets:
            slug  = m.get("slug", "")
            title = m.get("title", "").lower()
            if "15" in slug and token in slug:
                return slug
            if "15" in title and token in title and "min" in title:
                return m.get("slug")
    except Exception as e:
        logger.warning(f"resolve_slug search failed: {e}")
    return SYMBOL_TO_SLUG.get(symbol)


# ─── Contrarian direction reversal ───────────────────────────────────────────

def _trade_direction(signal_direction: str) -> str:
    """ML UP → place DOWN (buy NO).  ML DOWN → place UP (buy YES)."""
    return "DOWN" if signal_direction == "UP" else "UP"


# ─── Live order placement ─────────────────────────────────────────────────────

def place_live_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    """
    Place a GTC BUY order on Limitless Exchange.

    Auth:
      HTTP request → HMAC  (lmts-api-key / lmts-timestamp / lmts-signature)
      Order payload → EIP-712 signature  (wallet private key)

    Contrarian:
      signal UP   → buy NO  token (positionIds[1])
      signal DOWN → buy YES token (positionIds[0])
    """
    trade_dir = _trade_direction(signal_direction)
    logger.info(f"[{symbol}] ML={signal_direction} → trade={trade_dir} (contrarian)")

    slug = resolve_slug(symbol)
    if not slug:
        return {"success": False, "error": f"No market slug for {symbol}"}

    market = fetch_market(slug)
    if not market:
        return {"success": False, "error": f"Market unavailable: {slug}"}

    pk = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    if not pk:
        return {"success": False, "error": "LIMITLESS_PRIVATE_KEY not set"}

    try:
        venue        = market.get("venue", {})
        position_ids = market.get("positionIds", [])
        exchange_addr = venue.get("exchange", "")

        if not position_ids:
            return {"success": False, "error": "positionIds missing from market data"}
        if not exchange_addr:
            return {"success": False, "error": "venue.exchange missing from market data"}

        # Contrarian token selection:
        # signal UP → NO token (index 1) | signal DOWN → YES token (index 0)
        token_id = position_ids[1] if signal_direction == "UP" else position_ids[0]

        # Amount calculation per docs:
        # BUY: makerAmount = price * size * 1e6  (USDC to spend)
        #      takerAmount = size * 1e6           (shares to receive)
        price_per_contract = min(max_contract_price, 0.50)
        num_shares         = round(position_size_usd / price_per_contract, 4)
        maker_amount       = int(price_per_contract * num_shares * 1_000_000)
        taker_amount       = int(num_shares * 1_000_000)

        account    = Account.from_key(pk)
        maker_addr = Web3.to_checksum_address(account.address)
        salt       = int(time.time() * 1000)

        order_data = {
            "salt":          salt,
            "maker":         maker_addr,
            "signer":        maker_addr,         # same as maker for EOA
            "taker":         ZERO_ADDR,          # open order — any taker
            "tokenId":       token_id,
            "makerAmount":   maker_amount,
            "takerAmount":   taker_amount,
            "expiration":    0,                  # 0 = no expiry (GTC)
            "nonce":         0,
            "feeRateBps":    0,
            "side":          0,                  # 0 = BUY
            "signatureType": 0,                  # 0 = EOA
        }

        # EIP-712 sign the order struct
        signature = _eip712_sign(order_data, exchange_addr)

        # POST /orders payload — no ownerId per actual API spec
        payload = {
            "order":      {**order_data, "signature": signature},
            "orderType":  "GTC",
            "marketSlug": slug,
        }

        # HMAC-signed POST
        resp = _hmac_post("/orders", payload)
        logger.info(f"[{symbol}] POST /orders → {resp.status_code}: {resp.text[:400]}")
        resp.raise_for_status()

        result   = resp.json()
        order_id = result.get("id") or result.get("orderId") or str(salt)

        logger.info(
            f"[{symbol}] ORDER PLACED ✓ signal={signal_direction} trade={trade_dir} "
            f"${position_size_usd} | {num_shares} shares @ ${price_per_contract} | id={order_id}"
        )
        return {
            "success":            True,
            "order_id":           str(order_id),
            "contracts":          num_shares,
            "price_per_contract": price_per_contract,
            "total_spent":        position_size_usd,
            "slug":               slug,
            "signal_direction":   signal_direction,
            "trade_direction":    trade_dir,
        }

    except requests.HTTPError as e:
        body = e.response.text if e.response else str(e)
        logger.error(f"[{symbol}] HTTPError: {body}")
        return {"success": False, "error": body}
    except Exception as e:
        logger.error(f"[{symbol}] Exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ─── Shadow order ─────────────────────────────────────────────────────────────

def place_shadow_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    trade_dir          = _trade_direction(signal_direction)
    price_per_contract = min(max_contract_price, 0.50)
    num_shares         = round(position_size_usd / price_per_contract, 4)
    slug               = SYMBOL_TO_SLUG.get(symbol, "")

    logger.info(
        f"[{symbol}] SHADOW signal={signal_direction} trade={trade_dir} "
        f"${position_size_usd} | {num_shares} shares @ ${price_per_contract}"
    )
    return {
        "success":            True,
        "order_id":           f"shadow_{int(time.time())}",
        "contracts":          num_shares,
        "price_per_contract": price_per_contract,
        "total_spent":        position_size_usd,
        "slug":               slug,
        "signal_direction":   signal_direction,
        "trade_direction":    trade_dir,
        "shadow":             True,
    }


# ─── Unified entry ────────────────────────────────────────────────────────────

def execute_order(
    symbol: str,
    signal_direction: str,
    mode: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    if mode == "live":
        return place_live_order(symbol, signal_direction, position_size_usd, max_contract_price)
    return place_shadow_order(symbol, signal_direction, position_size_usd, max_contract_price)


def get_limitless_market_url(symbol: str) -> str:
    slug = SYMBOL_TO_SLUG.get(symbol, "")
    return f"https://limitless.exchange/markets/crypto/{slug}" if slug else "https://limitless.exchange/markets/crypto/15-min"
