"""
Limitless Exchange order executor
Handles both LIVE and SHADOW mode trading
"""
import os
import time
import logging
import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

API_BASE = "https://api.limitless.exchange"
CHAIN_ID = 8453  # Base
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Market slug pattern for 15-min crypto markets on Limitless
# Format: "btc-usdt-15-min" etc.
SYMBOL_TO_SLUG = {
    "BTC-USDT": "btc-usdt-15-min",
    "ETH-USDT": "eth-usdt-15-min",
    "SOL-USDT": "sol-usdt-15-min",
    "XRP-USDT": "xrp-usdt-15-min",
    "BNB-USDT": "bnb-usdt-15-min",
    "DOGE-USDT": "doge-usdt-15-min",
}

# Cache market data (venue addresses) per slug
_market_cache = {}


def _get_headers():
    return {
        "X-API-Key": os.environ.get("LIMITLESS_API_KEY", ""),
        "Content-Type": "application/json",
    }


def fetch_market(slug: str) -> dict | None:
    """Fetch and cache market data from Limitless API."""
    if slug in _market_cache:
        return _market_cache[slug]
    try:
        resp = requests.get(f"{API_BASE}/markets/{slug}", headers=_get_headers(), timeout=10)
        if resp.status_code == 404:
            logger.warning(f"Market not found: {slug}")
            return None
        resp.raise_for_status()
        data = resp.json()
        _market_cache[slug] = data
        return data
    except Exception as e:
        logger.error(f"Failed to fetch market {slug}: {e}")
        return None


def fetch_active_markets() -> list:
    """Fetch all active 15-min crypto markets."""
    try:
        resp = requests.get(
            f"{API_BASE}/markets/active",
            headers=_get_headers(),
            params={"category": "crypto"},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else resp.json().get("markets", [])
    except Exception as e:
        logger.error(f"Failed to fetch active markets: {e}")
        return []


def resolve_market_slug(symbol: str) -> str | None:
    """
    Resolve the correct market slug for a symbol by searching active markets.
    Falls back to the static map if search fails.
    """
    static = SYMBOL_TO_SLUG.get(symbol)

    # Try fetching active markets to find exact slug
    try:
        markets = fetch_active_markets()
        token = symbol.replace("-USDT", "").lower()
        for m in markets:
            slug = m.get("slug", "")
            title = m.get("title", "").lower()
            if "15" in slug and token in slug:
                return slug
            if "15" in title and token in title and "min" in title:
                return m.get("slug")
    except Exception:
        pass

    return static


def _sign_order(order_data: dict, verifying_contract: str) -> str:
    """Sign order with EIP-712 using wallet private key."""
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
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Order": [
            {"name": "salt", "type": "uint256"},
            {"name": "maker", "type": "address"},
            {"name": "signer", "type": "address"},
            {"name": "taker", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
            {"name": "makerAmount", "type": "uint256"},
            {"name": "takerAmount", "type": "uint256"},
            {"name": "expiration", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "feeRateBps", "type": "uint256"},
            {"name": "side", "type": "uint8"},
            {"name": "signatureType", "type": "uint8"},
        ],
    }
    message = {
        "salt": order_data["salt"],
        "maker": Web3.to_checksum_address(order_data["maker"]),
        "signer": Web3.to_checksum_address(order_data["signer"]),
        "taker": Web3.to_checksum_address(order_data["taker"]),
        "tokenId": int(order_data["tokenId"]),
        "makerAmount": order_data["makerAmount"],
        "takerAmount": order_data["takerAmount"],
        "expiration": order_data["expiration"],
        "nonce": order_data["nonce"],
        "feeRateBps": order_data["feeRateBps"],
        "side": order_data["side"],
        "signatureType": order_data["signatureType"],
    }
    typed_data = {
        "types": types,
        "primaryType": "Order",
        "domain": domain,
        "message": message,
    }
    encoded = encode_typed_data(typed_data)
    account = Account.from_key(private_key)
    signed = account.sign_message(encoded)
    return signed.signature.hex()


def place_live_order(symbol: str, direction: str, position_size_usd: float,
                     max_contract_price: float = 0.50) -> dict:
    """
    Place a GTC limit order on Limitless Exchange.
    direction: 'UP' -> BUY YES token | 'DOWN' -> BUY NO token
    Contract price capped at max_contract_price (default $0.50).
    Returns dict with order_id, contracts, price, status.
    """
    slug = resolve_market_slug(symbol)
    if not slug:
        return {"success": False, "error": f"No market slug for {symbol}"}

    market = fetch_market(slug)
    if not market:
        return {"success": False, "error": f"Market data unavailable for {slug}"}

    private_key = os.environ.get("LIMITLESS_PRIVATE_KEY", "")
    owner_id = int(os.environ.get("LIMITLESS_OWNER_ID", "0"))

    if not private_key:
        return {"success": False, "error": "LIMITLESS_PRIVATE_KEY not configured"}

    try:
        venue = market.get("venue", {})
        position_ids = market.get("positionIds", [])
        if not position_ids:
            return {"success": False, "error": "No positionIds in market data"}

        # UP = YES token (index 0), DOWN = NO token (index 1)
        token_id = position_ids[0] if direction == "UP" else position_ids[1]

        # Price per contract capped at $0.50
        price_per_contract = min(max_contract_price, 0.50)

        # Number of contracts we can buy
        num_contracts = position_size_usd / price_per_contract
        num_contracts = round(num_contracts, 4)

        # Scale amounts by 1e6 (USDC 6 decimals)
        maker_amount = int(price_per_contract * num_contracts * 1e6)
        taker_amount = int(num_contracts * 1e6)

        account = Account.from_key(private_key)
        maker_addr = account.address
        salt = int(time.time() * 1000)

        order_data = {
            "salt": salt,
            "maker": Web3.to_checksum_address(maker_addr),
            "signer": Web3.to_checksum_address(maker_addr),
            "taker": ZERO_ADDRESS,
            "tokenId": token_id,
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": 0,
            "nonce": 0,
            "feeRateBps": 0,
            "side": 0,  # BUY
            "signatureType": 0,  # EOA
        }

        exchange_addr = venue.get("exchange", "")
        if not exchange_addr:
            return {"success": False, "error": "No venue.exchange address"}

        signature = _sign_order(order_data, exchange_addr)

        payload = {
            "order": {**order_data, "signature": signature},
            "ownerId": owner_id,
            "orderType": "GTC",
            "marketSlug": slug,
        }

        resp = requests.post(
            f"{API_BASE}/orders",
            headers=_get_headers(),
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        result = resp.json()

        order_id = result.get("id") or result.get("orderId") or str(salt)
        logger.info(f"LIVE ORDER placed: {symbol} {direction} | ${position_size_usd} | "
                    f"{num_contracts} contracts @ ${price_per_contract} | order_id={order_id}")

        return {
            "success": True,
            "order_id": str(order_id),
            "contracts": num_contracts,
            "price_per_contract": price_per_contract,
            "total_spent": position_size_usd,
            "slug": slug,
            "direction": direction,
        }

    except requests.HTTPError as e:
        logger.error(f"HTTP error placing order: {e.response.text}")
        return {"success": False, "error": str(e.response.text)}
    except Exception as e:
        logger.error(f"Order error: {e}")
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
