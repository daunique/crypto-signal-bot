"""
Limitless Exchange — Order Executor
════════════════════════════════════════════════════════════════════════
CONFIRMED from official docs (docs.limitless.exchange/developers):

Market fetch:
  GET /markets/{slug}   → NO auth needed (public endpoint)
  Returns: market.tokens.yes, market.tokens.no, market.venue.exchange
  The slug for 15-min markets must be discovered via GET /markets
  (active markets list) — NOT assumed as "btc-usdt-15-min"

Order placement:
  POST /orders          → X-API-Key header required
  Body: { order: {..., signature}, orderType: "GTC", marketSlug: "..." }
  The order struct is EIP-712 signed with venue.exchange as verifyingContract

Env vars required:
  LIMITLESS_API_KEY     → from limitless.exchange Profile → API Keys
  LIMITLESS_PRIVATE_KEY → wallet private key for EIP-712 signing

Direction:
  Uses ML signal directly — no reversal.
  ML signal UP   → buy YES token (tokens.yes) = betting price goes UP
  ML signal DOWN → buy NO  token (tokens.no)  = betting price goes DOWN
"""
import os
import time
import json
import logging
import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

API_BASE  = "https://api.limitless.exchange"
CHAIN_ID  = 8453
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# Cache: symbol → resolved slug, market data
_slug_cache:   dict = {}
_market_cache: dict = {}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _auth_headers() -> dict:
    """X-API-Key header for authenticated endpoints (POST /orders)."""
    key = os.environ.get("LIMITLESS_API_KEY", "")
    if not key:
        raise ValueError("LIMITLESS_API_KEY not set in environment variables")
    return {"X-API-Key": key, "Content-Type": "application/json"}


# ── Market discovery (NO auth required) ──────────────────────────────────────

def discover_slug(symbol: str) -> str | None:
    """
    Search all active markets for the current 15-min market for this symbol.
    Markets rotate every 15 minutes — we must find the CURRENT active one.
    Returns the slug of the currently active market.
    """
    if symbol in _slug_cache:
        cached = _slug_cache[symbol]
        logger.debug(f"[{symbol}] Using cached slug: {cached}")

    token = symbol.replace("-USDT", "").lower()  # e.g. "btc", "eth"
    page = 1

    while page <= 5:  # search up to 5 pages
        try:
            resp = requests.get(
                f"{API_BASE}/markets",
                params={"page": page, "limit": 50, "sortBy": "createdAt"},
                timeout=10,
            )
            resp.raise_for_status()
            body    = resp.json()
            markets = body.get("data", body) if isinstance(body, dict) else body

            if not markets:
                break

            for m in markets:
                slug  = m.get("slug", "")
                title = m.get("title", "").lower()
                # Match: contains token name AND "15" (for 15-min) AND is active
                if (token in slug.lower() or token in title) and "15" in (slug + title):
                    logger.info(f"[{symbol}] Found market: slug={slug} title={m.get('title')}")
                    _slug_cache[symbol] = slug
                    return slug

            page += 1
        except Exception as e:
            logger.error(f"[{symbol}] Market discovery page {page}: {e}")
            break

    logger.warning(f"[{symbol}] No active 15-min market found")
    return None


def fetch_market(slug: str) -> dict | None:
    """
    GET /markets/{slug} — public, no auth.
    Returns full market object with tokens.yes, tokens.no, venue.exchange.
    """
    if slug in _market_cache:
        return _market_cache[slug]
    try:
        resp = requests.get(f"{API_BASE}/markets/{slug}", timeout=10)
        logger.info(f"GET /markets/{slug} → {resp.status_code}")
        if resp.status_code == 404:
            logger.warning(f"Market not found: {slug}")
            return None
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Market {slug} fields: {list(data.keys())}")
        _market_cache[slug] = data
        return data
    except Exception as e:
        logger.error(f"fetch_market({slug}): {e}")
        return None


def get_token_id(market: dict, direction: str) -> str | None:
    """
    Extract token ID from market data.
    direction UP   → tokens.yes (YES token)
    direction DOWN → tokens.no  (NO token)
    Falls back to positionIds[] if tokens dict missing.
    """
    tokens = market.get("tokens") or {}
    if isinstance(tokens, dict):
        if direction == "UP":
            tid = tokens.get("yes") or tokens.get("YES")
        else:
            tid = tokens.get("no") or tokens.get("NO")
        if tid:
            return str(tid)

    # Fallback: positionIds[0]=YES, positionIds[1]=NO
    pos_ids = market.get("positionIds") or market.get("position_ids") or []
    if pos_ids:
        if direction == "UP":
            return str(pos_ids[0])
        elif len(pos_ids) > 1:
            return str(pos_ids[1])

    logger.error(f"Cannot extract token ID for {direction}. "
                 f"market.tokens={tokens} positionIds={pos_ids}")
    return None


def get_exchange_addr(market: dict) -> str | None:
    """Extract venue.exchange — used as EIP-712 verifyingContract."""
    venue = market.get("venue") or {}
    if isinstance(venue, dict):
        addr = venue.get("exchange") or venue.get("condExchange")
        if addr:
            return addr
    # Some responses have it at top level
    return market.get("exchange") or market.get("condExchange")


# ── Direction (no reversal — uses ML signal directly) ────────────────────────

def _trade_dir(signal_direction: str) -> str:
    """Returns ML direction unchanged — no reversal."""
    return signal_direction


# ── EIP-712 order signing ─────────────────────────────────────────────────────

def _sign_order(order_data: dict, verifying_contract: str) -> str:
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


# ── Live order ────────────────────────────────────────────────────────────────

def place_live_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    """
    Place a GTC limit BUY order on Limitless Exchange.
    Uses ML signal direction directly:
      ML UP   → buy YES token (tokens.yes) = betting UP
      ML DOWN → buy NO  token (tokens.no)  = betting DOWN
    """
    trade_dir = _trade_dir(signal_direction)
    logger.info(f"[{symbol}] LIVE order: {signal_direction} ${position_size_usd}")

    # Step 1: discover current active market slug
    slug = discover_slug(symbol)
    if not slug:
        return {"success": False, "error": f"No active 15-min market found for {symbol}"}

    # Step 2: fetch market data (no auth)
    # Clear cache to get fresh market for current candle
    _market_cache.pop(slug, None)
    market = fetch_market(slug)
    if not market:
        return {"success": False, "error": f"Could not fetch market data for slug={slug}"}

    # Step 3: extract required fields
    exchange_addr = get_exchange_addr(market)
    if not exchange_addr:
        return {
            "success": False,
            "error": f"venue.exchange missing. Market keys: {list(market.keys())}",
        }

    token_id = get_token_id(market, trade_dir)
    if not token_id:
        return {
            "success": False,
            "error": f"Token ID missing for direction={trade_dir}. tokens={market.get('tokens')}",
        }

    # Step 4: check API key
    pk = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    if not pk:
        return {"success": False, "error": "LIMITLESS_PRIVATE_KEY not set"}
    try:
        _auth_headers()  # validates LIMITLESS_API_KEY is set
    except ValueError as e:
        return {"success": False, "error": str(e)}

    # Step 5: build order
    price = min(max_contract_price, 0.50)
    size  = round(position_size_usd / price, 4)

    # makerAmount = USDC to spend (6 decimals)
    # takerAmount = shares to receive (6 decimals)
    maker_amount = int(price * size * 1_000_000)
    taker_amount = int(size * 1_000_000)

    account    = Account.from_key(pk)
    maker_addr = Web3.to_checksum_address(account.address)
    salt       = int(time.time() * 1000)

    order_data = {
        "salt":          salt,
        "maker":         maker_addr,
        "signer":        maker_addr,
        "taker":         ZERO_ADDR,
        "tokenId":       int(token_id),
        "makerAmount":   maker_amount,
        "takerAmount":   taker_amount,
        "expiration":    0,      # 0 = GTC (no expiry)
        "nonce":         0,
        "feeRateBps":    0,
        "side":          0,      # BUY
        "signatureType": 0,      # EOA
    }

    # Step 6: EIP-712 sign
    try:
        signature = _sign_order(order_data, exchange_addr)
    except Exception as e:
        return {"success": False, "error": f"EIP-712 signing failed: {e}"}

    payload = {
        "order":      {**order_data, "signature": signature},
        "orderType":  "GTC",
        "marketSlug": slug,
    }

    # Step 7: POST /orders with X-API-Key
    try:
        resp = requests.post(
            f"{API_BASE}/orders",
            headers=_auth_headers(),
            json=payload,
            timeout=15,
        )
        logger.info(f"[{symbol}] POST /orders → {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()

        result   = resp.json()
        order_id = (
            result.get("order", {}).get("id")
            or result.get("id")
            or result.get("orderId")
            or str(salt)
        )

        logger.info(
            f"[{symbol}] ORDER ✓ signal={signal_direction} trade={trade_dir} "
            f"{size} shares @ ${price} id={order_id} slug={slug}"
        )
        return {
            "success":            True,
            "order_id":           str(order_id),
            "contracts":          size,
            "price_per_contract": price,
            "total_spent":        position_size_usd,
            "slug":               slug,
            "signal_direction":   signal_direction,
            "trade_direction":    trade_dir,
        }

    except requests.HTTPError as e:
        body = e.response.text if e.response else str(e)
        logger.error(f"[{symbol}] HTTPError placing order: {body}")
        return {"success": False, "error": body}
    except Exception as e:
        logger.error(f"[{symbol}] Exception placing order: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Shadow order ──────────────────────────────────────────────────────────────

def place_shadow_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    trade_dir = _trade_dir(signal_direction)
    price     = min(max_contract_price, 0.50)
    size      = round(position_size_usd / price, 4)
    logger.info(f"[{symbol}] SHADOW signal={signal_direction} trade={trade_dir} "
                f"{size} shares @ ${price}")
    return {
        "success":            True,
        "order_id":           f"shadow_{int(time.time())}",
        "contracts":          size,
        "price_per_contract": price,
        "total_spent":        position_size_usd,
        "signal_direction":   signal_direction,
        "trade_direction":    trade_dir,
        "shadow":             True,
    }


# ── Unified entry ─────────────────────────────────────────────────────────────

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
    slug = _slug_cache.get(symbol, "")
    return (f"https://limitless.exchange/markets/crypto/{slug}"
            if slug else "https://limitless.exchange/markets/crypto/15-min")
