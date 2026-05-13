"""
Limitless Exchange — Order Executor
═══════════════════════════════════════════════════════════════════════
SOURCE: Official Limitless Python SDK README (github.com/limitless-labs-group/limitless-sdk)

Auth:
  Header: X-API-Key: <your key>    ← all requests (public + authed)
  EIP-712 signature in order body  ← signs the order struct

Market data:
  GET /markets/:slug → returns:
    market.tokens.yes  → YES token ID (string)
    market.tokens.no   → NO  token ID (string)
    market.venue.exchange → verifyingContract for EIP-712

Order placement (POST /orders):
  GTC order uses: price + size (NOT makerAmount/takerAmount)
    price = per-share price in USDC (0.01 – 0.99)
    size  = number of shares to buy
  Payload: { order: {..., signature}, orderType: "GTC", marketSlug: "..." }
  No ownerId in the payload.

IMPORTANT — USDC approval required before first live order:
  Your wallet must approve USDC spend for venue.exchange on Base mainnet.
  Run /api/check-approval after deploying to verify.
  One-time setup — does NOT need repeating.
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

# Base mainnet USDC contract
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

SYMBOL_TO_SLUG = {
    "BTC-USDT":  "btc-usdt-15-min",
    "ETH-USDT":  "eth-usdt-15-min",
    "SOL-USDT":  "sol-usdt-15-min",
    "XRP-USDT":  "xrp-usdt-15-min",
    "BNB-USDT":  "bnb-usdt-15-min",
    "DOGE-USDT": "doge-usdt-15-min",
}

_market_cache: dict = {}


# ── Auth header ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    """X-API-Key — confirmed correct per official SDK and README."""
    key = os.environ.get("LIMITLESS_API_KEY", "")
    if not key:
        raise ValueError(
            "LIMITLESS_API_KEY not set.\n"
            "Get it from limitless.exchange → Profile → API Keys.\n"
            "Set LIMITLESS_API_KEY in Render environment variables."
        )
    return {
        "X-API-Key":    key,
        "Content-Type": "application/json",
    }


# ── EIP-712 order signing ─────────────────────────────────────────────────────

def _eip712_sign(order_data: dict, verifying_contract: str) -> str:
    """Sign the order struct. verifyingContract = market.venue.exchange."""
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


# ── Market data ───────────────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict | None:
    """GET /markets/:slug — returns tokens.yes, tokens.no, venue.exchange."""
    if slug in _market_cache:
        return _market_cache[slug]
    try:
        resp = requests.get(
            f"{API_BASE}/markets/{slug}",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 404:
            logger.warning(f"Market not found: {slug}")
            return None
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Market {slug} keys: {list(data.keys())}")
        _market_cache[slug] = data
        return data
    except Exception as e:
        logger.error(f"fetch_market({slug}): {e}")
        return None


def resolve_slug(symbol: str) -> str | None:
    """Search active markets, fall back to static map."""
    try:
        resp = requests.get(
            f"{API_BASE}/markets/active",
            headers=_headers(),
            params={"category": "crypto"},
            timeout=10,
        )
        resp.raise_for_status()
        body    = resp.json()
        markets = body if isinstance(body, list) else body.get("markets", body.get("data", []))
        token   = symbol.replace("-USDT", "").lower()
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


def _extract_token_id(market: dict, direction: str) -> str | None:
    """
    Extract YES or NO token ID from market data.
    SDK uses market.tokens.yes / market.tokens.no
    Also handles positionIds[] fallback.
    """
    tokens = market.get("tokens", {})

    if direction == "UP":
        # UP → YES token
        tid = tokens.get("yes") or tokens.get("YES")
        if not tid and market.get("positionIds"):
            tid = market["positionIds"][0]
    else:
        # DOWN → NO token
        tid = tokens.get("no") or tokens.get("NO")
        if not tid and market.get("positionIds"):
            pid = market["positionIds"]
            tid = pid[1] if len(pid) > 1 else pid[0]

    return str(tid) if tid else None


def _extract_exchange(market: dict) -> str | None:
    """Extract venue.exchange address (verifyingContract for EIP-712)."""
    venue = market.get("venue", {})
    return (
        venue.get("exchange")
        or venue.get("condExchange")
        or market.get("exchange")
    )


# ── USDC approval check ───────────────────────────────────────────────────────

def check_usdc_approval(exchange_addr: str) -> dict:
    """
    Check if wallet has approved USDC for exchange contract.
    Required ONE TIME before any live BUY order can execute.
    """
    pk = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    if not pk:
        return {"approved": False, "error": "No private key"}
    try:
        w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
        account = Account.from_key(pk)
        wallet  = Web3.to_checksum_address(account.address)
        exch    = Web3.to_checksum_address(exchange_addr)

        # ERC-20 allowance ABI (minimal)
        abi = [{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
                "name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}]

        usdc     = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=abi)
        allowance= usdc.functions.allowance(wallet, exch).call()
        approved = allowance > 0
        return {
            "approved":  approved,
            "allowance": str(allowance),
            "wallet":    wallet,
            "exchange":  exch,
            "message":   "OK" if approved else "USDC approval required — run setup_approval()"
        }
    except Exception as e:
        return {"approved": False, "error": str(e)}


# ── Live order placement ──────────────────────────────────────────────────────

def _reverse(direction: str) -> str:
    """
    Contrarian direction reversal.
    ML signal UP   → place trade DOWN (buy NO  token = positionIds[1])
    ML signal DOWN → place trade UP   (buy YES token = positionIds[0])
    The signal_direction saved in the DB remains the original ML prediction.
    """
    return "DOWN" if direction == "UP" else "UP"


def place_live_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    """
    Place a GTC limit BUY order on Limitless Exchange.
    Direction is REVERSED from the ML signal (contrarian strategy):
    signal UP   → buy NO  token (trade DOWN)
    signal DOWN → buy YES token (trade UP)

    GTC order struct:
      price = per-share price (capped at $0.50)
      size  = number of shares = position_size_usd / price
      makerAmount = USDC to spend = price * size * 1e6
      takerAmount = shares to receive = size * 1e6
    """
    logger.info(f"[{symbol}] LIVE order {signal_direction} ${position_size_usd}")

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
        exchange_addr = _extract_exchange(market)
        if not exchange_addr:
            return {"success": False, "error": f"venue.exchange missing. Market keys: {list(market.keys())}"}

        # Reverse direction: ML UP → buy NO (trade DOWN), ML DOWN → buy YES (trade UP)
        trade_direction = _reverse(signal_direction)
        token_id = _extract_token_id(market, trade_direction)
        if not token_id:
            return {"success": False, "error": f"Token ID missing. market.tokens: {market.get('tokens')} positionIds: {market.get('positionIds')}"}
        logger.info(f"[{symbol}] ML signal={signal_direction} → trade={trade_direction} (contrarian)")

        # Check USDC approval (non-blocking — just warn)
        approval = check_usdc_approval(exchange_addr)
        if not approval.get("approved"):
            logger.warning(f"[{symbol}] USDC not approved for {exchange_addr}. "
                           "Visit /api/approval-status. Orders may be rejected.")

        price = min(max_contract_price, 0.50)
        size  = round(position_size_usd / price, 4)

        # Scale to 1e6 (USDC 6 decimals)
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
            "expiration":    0,       # GTC = no expiry
            "nonce":         0,
            "feeRateBps":    0,
            "side":          0,       # BUY
            "signatureType": 0,       # EOA
        }

        signature = _eip712_sign(order_data, exchange_addr)

        payload = {
            "order":      {**order_data, "signature": signature},
            "orderType":  "GTC",
            "marketSlug": slug,
        }

        resp = requests.post(
            f"{API_BASE}/orders",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        logger.info(f"[{symbol}] POST /orders {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()

        result   = resp.json()
        order_id = (result.get("order", {}).get("id")
                    or result.get("id")
                    or result.get("orderId")
                    or str(salt))

        logger.info(f"[{symbol}] ORDER ✓ {signal_direction} {size} shares "
                    f"@ ${price} id={order_id}")
        return {
            "success":            True,
            "order_id":           str(order_id),
            "contracts":          size,
            "price_per_contract": price,
            "total_spent":        position_size_usd,
            "slug":               slug,
            "signal_direction":   signal_direction,
            "usdc_approved":      approval.get("approved"),
        }

    except requests.HTTPError as e:
        body = e.response.text if e.response else str(e)
        logger.error(f"[{symbol}] HTTPError: {body}")
        return {"success": False, "error": body}
    except Exception as e:
        logger.error(f"[{symbol}] Exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Shadow order ──────────────────────────────────────────────────────────────

def place_shadow_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    trade_direction = _reverse(signal_direction)
    price = min(max_contract_price, 0.50)
    size  = round(position_size_usd / price, 4)
    logger.info(f"[{symbol}] SHADOW signal={signal_direction} trade={trade_direction} {size} shares @ ${price}")
    return {
        "success":            True,
        "order_id":           f"shadow_{int(time.time())}",
        "contracts":          size,
        "price_per_contract": price,
        "total_spent":        position_size_usd,
        "slug":               SYMBOL_TO_SLUG.get(symbol, ""),
        "signal_direction":   signal_direction,
        "trade_direction":    trade_direction,
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
    slug = SYMBOL_TO_SLUG.get(symbol, "")
    return (f"https://limitless.exchange/markets/crypto/{slug}"
            if slug else "https://limitless.exchange/markets/crypto/15-min")
