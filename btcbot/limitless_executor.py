"""
Limitless Exchange order executor
─────────────────────────────────────────────────────────────────────────────
TWO independent auth layers (both required):

  1. HMAC-SHA256 request authentication
     Headers: lmts-api-key, lmts-timestamp, lmts-signature
     Signs the HTTP request itself so the server trusts the caller.
     Uses: LIMITLESS_TOKEN_ID + LIMITLESS_TOKEN_SECRET (base64)

  2. EIP-712 order signing
     Field: order.signature inside the JSON body
     Cryptographically proves the wallet owner authorised this specific order.
     Uses: LIMITLESS_PRIVATE_KEY (wallet private key)

Required env vars:
  LIMITLESS_TOKEN_ID      → token ID returned by POST /auth/api-tokens/derive
  LIMITLESS_TOKEN_SECRET  → base64 secret returned once at token creation
  LIMITLESS_PRIVATE_KEY   → wallet private key (0x...) for EIP-712
  LIMITLESS_OWNER_ID      → numeric profile ID from GET /profiles/{address}
─────────────────────────────────────────────────────────────────────────────
"""
import os
import time
import hmac
import hashlib
import base64
import logging
import json
import requests
from datetime import datetime, timezone
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

API_BASE  = "https://api.limitless.exchange"
CHAIN_ID  = 8453  # Base mainnet
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


# ─── Layer 1: HMAC Request Signing ───────────────────────────────────────────

def _hmac_headers(method: str, path: str, body: str = "") -> dict:
    """
    Build the three HMAC auth headers required on every API request.

    Canonical message format:
        {ISO-8601 timestamp}\\n{HTTP METHOD}\\n{path+query}\\n{body}

    Returns headers:
        lmts-api-key       token ID
        lmts-timestamp     ISO-8601 UTC timestamp (must be within 30s of server)
        lmts-signature     base64(HMAC-SHA256(base64decode(secret), message))
    """
    token_id   = os.environ.get("LIMITLESS_TOKEN_ID", "")
    secret_b64 = os.environ.get("LIMITLESS_TOKEN_SECRET", "")

    if not token_id or not secret_b64:
        raise ValueError(
            "LIMITLESS_TOKEN_ID and LIMITLESS_TOKEN_SECRET must be set. "
            "Derive them via POST /auth/api-tokens/derive on limitless.exchange."
        )

    timestamp = datetime.now(timezone.utc).isoformat()
    message   = f"{timestamp}\n{method}\n{path}\n{body}"

    secret_bytes = base64.b64decode(secret_b64)
    signature = base64.b64encode(
        hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    return {
        "lmts-api-key":   token_id,
        "lmts-timestamp": timestamp,
        "lmts-signature": signature,
        "Content-Type":   "application/json",
    }


def _get(path: str, params: dict | None = None) -> requests.Response:
    """Authenticated GET — HMAC-signed."""
    query     = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    full_path = path + query
    return requests.get(
        f"{API_BASE}{full_path}",
        headers=_hmac_headers("GET", full_path, ""),
        timeout=10,
    )


def _post(path: str, payload: dict) -> requests.Response:
    """Authenticated POST — HMAC-signed."""
    body = json.dumps(payload, separators=(",", ":"))
    return requests.post(
        f"{API_BASE}{path}",
        headers=_hmac_headers("POST", path, body),
        data=body,
        timeout=15,
    )



# ─── Layer 2: EIP-712 Order Signing ──────────────────────────────────────────

def _eip712_sign(order_data: dict, verifying_contract: str) -> str:
    """
    Sign the order struct with EIP-712 using the wallet private key.
    This authenticates the ORDER PAYLOAD — separate from HMAC which
    authenticates the HTTP REQUEST.
    """
    private_key = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    if not private_key:
        raise ValueError("LIMITLESS_PRIVATE_KEY not set")

    domain = {
        "name": "Limitless CTF Exchange",
        "version": "1",
        "chainId": CHAIN_ID,
        "verifyingContract": Web3.to_checksum_address(verifying_contract),
    }
    types = {
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
    }
    message = {
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
    }
    encoded = encode_typed_data({
        "types": types, "primaryType": "Order",
        "domain": domain, "message": message,
    })
    signed = Account.from_key(private_key).sign_message(encoded)
    return signed.signature.hex()


# ─── Market helpers ───────────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict | None:
    """Fetch and cache market data from Limitless API."""
    if slug in _market_cache:
        return _market_cache[slug]
    try:
        resp = _get(f"/markets/{slug}")
        if resp.status_code == 404:
            logger.warning(f"Market not found: {slug}")
            return None
        resp.raise_for_status()
        data = resp.json()
        _market_cache[slug] = data
        return data
    except Exception as e:
        logger.error(f"fetch_market({slug}) failed: {e}")
        return None


def fetch_active_markets() -> list:
    try:
        resp = _get("/markets/active", {"category": "crypto"})
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, list) else body.get("markets", [])
    except Exception as e:
        logger.error(f"fetch_active_markets failed: {e}")
        return []


def resolve_market_slug(symbol: str) -> str | None:
    """
    Resolve the correct market slug by searching active markets first,
    falling back to the static map.
    """
    static = SYMBOL_TO_SLUG.get(symbol)
    try:
        markets = fetch_active_markets()
        token = symbol.replace("-USDT", "").lower()
        for m in markets:
            slug  = m.get("slug", "")
            title = m.get("title", "").lower()
            if "15" in slug and token in slug:
                return slug
            if "15" in title and token in title and "min" in title:
                return m.get("slug")
    except Exception:
        pass
    return static


def place_live_order(symbol: str, direction: str,
                     position_size_usd: float,
                     max_contract_price: float = 0.50) -> dict:
    """
    Place a GTC limit BUY order on Limitless Exchange.

    direction: 'UP'   → BUY YES token (positionIds[0])
               'DOWN' → BUY NO  token (positionIds[1])

    Auth flow:
      Step A — _eip712_sign()  signs the order struct  (LIMITLESS_PRIVATE_KEY)
      Step B — _post()         adds HMAC headers        (LIMITLESS_TOKEN_ID/SECRET)
    Both are required. Missing either causes a 401/403 from the API.
    """
    slug = resolve_market_slug(symbol)
    if not slug:
        return {"success": False, "error": f"No market slug for {symbol}"}

    market = fetch_market(slug)
    if not market:
        return {"success": False, "error": f"Market data unavailable for {slug}"}

    private_key = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    owner_id    = int(os.environ.get("LIMITLESS_OWNER_ID", "0"))

    if not private_key:
        return {"success": False, "error": "LIMITLESS_PRIVATE_KEY not configured"}

    try:
        venue        = market.get("venue", {})
        position_ids = market.get("positionIds", [])
        if not position_ids:
            return {"success": False, "error": "No positionIds in market data"}

        exchange_addr = venue.get("exchange", "")
        if not exchange_addr:
            return {"success": False, "error": "No venue.exchange address in market data"}

        # YES token = index 0 (UP), NO token = index 1 (DOWN)
        token_id = position_ids[0] if direction == "UP" else position_ids[1]

        # Price hard-capped at $0.50 per contract
        price_per_contract = min(max_contract_price, 0.50)
        num_contracts      = round(position_size_usd / price_per_contract, 4)

        # Amounts scaled by 1e6 (USDC has 6 decimals; shares also ×1e6)
        maker_amount = int(price_per_contract * num_contracts * 1_000_000)
        taker_amount = int(num_contracts * 1_000_000)

        account    = Account.from_key(private_key)
        maker_addr = Web3.to_checksum_address(account.address)
        salt       = int(time.time() * 1000)

        order_data = {
            "salt":          salt,
            "maker":         maker_addr,
            "signer":        maker_addr,
            "taker":         ZERO_ADDR,
            "tokenId":       token_id,
            "makerAmount":   maker_amount,
            "takerAmount":   taker_amount,
            "expiration":    0,     # 0 = no expiry (GTC)
            "nonce":         0,
            "feeRateBps":    0,
            "side":          0,     # BUY
            "signatureType": 0,     # EOA wallet signature
        }

        # ── Step A: EIP-712 sign the order payload (wallet private key) ────
        signature = _eip712_sign(order_data, exchange_addr)

        payload = {
            "order":      {**order_data, "signature": signature},
            "ownerId":    owner_id,
            "orderType":  "GTC",
            "marketSlug": slug,
        }

        # ── Step B: HMAC-signed POST (token ID + secret) ──────────────────
        resp = _post("/orders", payload)
        resp.raise_for_status()
        result = resp.json()

        order_id = result.get("id") or result.get("orderId") or str(salt)
        logger.info(
            f"LIVE ORDER ✓ {symbol} {direction} | "
            f"${position_size_usd} | {num_contracts} contracts "
            f"@ ${price_per_contract} | id={order_id}"
        )
        return {
            "success":            True,
            "order_id":           str(order_id),
            "contracts":          num_contracts,
            "price_per_contract": price_per_contract,
            "total_spent":        position_size_usd,
            "slug":               slug,
            "direction":          direction,
        }

    except requests.HTTPError as e:
        body = e.response.text if e.response else str(e)
        logger.error(f"HTTP error placing order for {symbol}: {body}")
        return {"success": False, "error": body}
    except Exception as e:
        logger.error(f"Order error for {symbol}: {e}")
        return {"success": False, "error": str(e)}


def place_shadow_order(symbol: str, direction: str, position_size_usd: float,
                       max_contract_price: float = 0.50) -> dict:
    """
    Shadow (demo) mode — mimics Limitless order structure without real execution.
    Returns same structure as place_live_order for consistent tracking.
    """
    price_per_contract = min(max_contract_price, 0.50)
    num_contracts = round(position_size_usd / price_per_contract, 4)
    slug = SYMBOL_TO_SLUG.get(symbol, f"{symbol.lower().replace('-', '-')}-15-min")

    logger.info(f"SHADOW ORDER: {symbol} {direction} | ${position_size_usd} | "
                f"{num_contracts} contracts @ ${price_per_contract}")

    return {
        "success": True,
        "order_id": f"shadow_{int(time.time())}",
        "contracts": num_contracts,
        "price_per_contract": price_per_contract,
        "total_spent": position_size_usd,
        "slug": slug,
        "direction": direction,
        "shadow": True,
    }


def execute_order(symbol: str, direction: str, mode: str,
                  position_size_usd: float, max_contract_price: float = 0.50) -> dict:
    """Unified order execution — routes to live or shadow based on mode."""
    if mode == "live":
        return place_live_order(symbol, direction, position_size_usd, max_contract_price)
    else:
        return place_shadow_order(symbol, direction, position_size_usd, max_contract_price)


def get_limitless_market_url(symbol: str) -> str:
    slug = SYMBOL_TO_SLUG.get(symbol, "")
    if slug:
        return f"https://limitless.exchange/markets/crypto/{slug}"
    return "https://limitless.exchange/markets/crypto/15-min"
