"""
Polymarket CLOB — Order Executor  (v2 — L2 Auth)
══════════════════════════════════════════════════════════════════════════════
AUTH MODEL
──────────
Polymarket CLOB supports two auth levels:

  L1 — Private key signs every request header directly.
       Simple but Polymarket recommends L2 for production trading.
       signatureType = 0 (EOA)

  L2 — Derived API key/secret/passphrase signs requests via HMAC-SHA256.
       The API key is generated ONCE by calling POST /auth/api-key, signed
       with the private key. After that only key+secret+passphrase are needed.
       signatureType = 2 (POLY_GNOSIS_SAFE compatible, but works for EOA too)

  This executor uses L2 when all three L2 env vars are set, otherwise
  falls back to L1 automatically.

CREDENTIALS (env vars on Render, secrets on Fly)
──────────────────────────────
  Required always:
    POLYMARKET_PRIVATE_KEY     — EOA wallet private key (0x... or raw hex)

  Required for L2 (recommended):
    POLYMARKET_API_KEY         — from POST /auth/api-key
    POLYMARKET_API_SECRET      — from POST /auth/api-key
    POLYMARKET_API_PASSPHRASE  — from POST /auth/api-key

  To generate L2 credentials, call:
    from polymarket_executor import derive_api_key
    derive_api_key()
  This prints the key/secret/passphrase to your logs once.

WALLET / CHAIN
──────────────
  Chain:    Polygon mainnet (chain ID 137)
  USDC:     0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
  CTF Exch: 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E

MARKET SLUGS (15-min crypto)
──────────────────────────────
  btc-updown-15m  eth-updown-15m  sol-updown-15m
  xrp-updown-15m  bnb-updown-15m  doge-updown-15m
"""

import os
import time
import json
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone

from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
CLOB_BASE    = "https://clob.polymarket.com"
CHAIN_ID     = 137
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
ZERO_ADDR    = "0x0000000000000000000000000000000000000000"

_KNOWN_SLUGS: dict[str, str] = {
    "BTC":  "btc-updown-15m",
    "ETH":  "eth-updown-15m",
    "SOL":  "sol-updown-15m",
    "XRP":  "xrp-updown-15m",
    "BNB":  "bnb-updown-15m",
    "DOGE": "doge-updown-15m",
}

# EIP-712 order type — same for both L1 and L2
_ORDER_TYPES = [
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
]

_market_cache: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_private_key() -> str | None:
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        return None
    return pk if pk.startswith("0x") else "0x" + pk


def get_wallet_address() -> str | None:
    pk = get_private_key()
    if not pk:
        return None
    try:
        return Web3.to_checksum_address(Account.from_key(pk).address)
    except Exception:
        return None


def _get_l2_creds() -> tuple[str, str, str] | None:
    """
    Returns (api_key, api_secret, api_passphrase) if all three are set,
    otherwise None (caller falls back to L1).
    """
    key        = os.environ.get("POLYMARKET_API_KEY",        "").strip()
    secret     = os.environ.get("POLYMARKET_API_SECRET",     "").strip()
    passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    if key and secret and passphrase:
        return key, secret, passphrase
    return None


def _using_l2() -> bool:
    return _get_l2_creds() is not None


def validate_credentials() -> dict:
    pk      = get_private_key()
    address = get_wallet_address()
    l2      = _get_l2_creds()
    auth_level = "L2" if l2 else ("L1" if pk else "NONE")
    return {
        "POLYMARKET_PRIVATE_KEY":    bool(pk),
        "POLYMARKET_API_KEY":        bool(l2),
        "POLYMARKET_API_SECRET":     bool(l2),
        "POLYMARKET_API_PASSPHRASE": bool(l2),
        "wallet_address":            address,
        "auth_level":                auth_level,
        "signing_ready":             bool(pk),
        "live_trading_ready":        bool(pk and address),
        "l2_ready":                  bool(l2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# L2 API KEY DERIVATION  (run once — saves credentials to logs/output)
# ══════════════════════════════════════════════════════════════════════════════

def derive_api_key() -> dict:
    """
    Generate L2 API credentials by calling POST /auth/api-key.

    This is signed with the private key (L1 style) and returns:
      { apiKey, secret, passphrase }

    Call this ONCE, then add the three values as env vars (Render) or secrets (Fly):
      POLYMARKET_API_KEY        = apiKey
      POLYMARKET_API_SECRET     = secret
      POLYMARKET_API_PASSPHRASE = passphrase

    After that, the executor will automatically use L2 auth for all requests.
    """
    pk = get_private_key()
    if not pk:
        return {"success": False, "error": "POLYMARKET_PRIVATE_KEY not set"}

    try:
        # L1-sign the derivation request — Polymarket uses POST /auth/api-key
        # The body must contain the wallet nonce (use 0 for first derivation)
        body_str = '{"nonce":0}'
        headers  = _build_l1_headers("POST", "/auth/api-key", body_str)
        resp     = requests.post(
            f"{CLOB_BASE}/auth/api-key",
            headers=headers,
            data=body_str,
            timeout=15,
        )
        logger.info("[POLY:AUTH] POST /auth/api-key → %d", resp.status_code)

        if not resp.ok:
            return {
                "success": False,
                "error":   f"HTTP {resp.status_code}: {resp.text[:400]}",
            }

        data = resp.json()
        api_key        = data.get("apiKey")        or data.get("api_key")
        api_secret     = data.get("secret")        or data.get("api_secret")
        api_passphrase = data.get("passphrase")    or data.get("api_passphrase")

        if not all([api_key, api_secret, api_passphrase]):
            return {
                "success": False,
                "error":   f"Unexpected response shape: {data}",
            }

        logger.info(
            "[POLY:AUTH] ✓ L2 API credentials derived — add these as env vars (Render) or secrets (Fly):\n"
            "  POLYMARKET_API_KEY        = %s\n"
            "  POLYMARKET_API_SECRET     = %s\n"
            "  POLYMARKET_API_PASSPHRASE = %s",
            api_key, api_secret, api_passphrase,
        )
        return {
            "success":         True,
            "api_key":         api_key,
            "api_secret":      api_secret,
            "api_passphrase":  api_passphrase,
        }

    except Exception as e:
        logger.error("[POLY:AUTH] derive_api_key exception: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# L1 AUTH  (private key signs every request — fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _build_l1_headers(method: str, path: str, body: str = "") -> dict:
    """
    L1 auth per Polymarket CLOB API spec:
      - Message to sign: timestamp (as string, EIP-191 personal_sign)
      - Headers: POLY-ADDRESS, POLY-SIGNATURE, POLY-TIMESTAMP, POLY-NONCE=0

    Note: Polymarket L1 signs ONLY the timestamp string, not the full
    method+path+body concatenation. This is what the CLOB API verifies.
    """
    pk = get_private_key()
    if not pk:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set")

    address = get_wallet_address()
    ts      = str(int(time.time()))

    # Sign only the timestamp — Polymarket L1 spec
    msg = encode_defunct(text=ts)

    try:
        signed    = Account.from_key(pk).sign_message(msg)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
    except Exception as e:
        raise ValueError(f"L1 signing failed: {e}")

    return {
        "POLY-ADDRESS":   address,
        "POLY-SIGNATURE": signature,
        "POLY-TIMESTAMP": ts,
        "POLY-NONCE":     "0",
        "Content-Type":   "application/json",
    }


# ══════════════════════════════════════════════════════════════════════════════
# L2 AUTH  (HMAC-SHA256 with derived API key — recommended)
# ══════════════════════════════════════════════════════════════════════════════

def _build_l2_headers(method: str, path: str, body: str = "") -> dict:
    """
    L2 HMAC auth per Polymarket CLOB API spec:
      Message:   timestamp + method.upper() + path + body
      Signature: base64( HMAC-SHA256(base64decode(secret), message) )
      Headers:
        POLY-ADDRESS      : wallet address
        POLY-API-KEY      : api_key
        POLY-SIGNATURE    : base64-encoded HMAC digest
        POLY-TIMESTAMP    : unix timestamp (seconds, string)
        POLY-PASSPHRASE   : api_passphrase

    Polymarket uses base64-encoded secret and base64-encoded signature output,
    not raw hex. This matches the py-clob-client reference implementation.
    """
    creds = _get_l2_creds()
    if not creds:
        raise ValueError("L2 credentials not set — falling back to L1")

    api_key, api_secret, api_passphrase = creds
    address = get_wallet_address()
    if not address:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set — cannot derive address")

    import base64
    ts      = str(int(time.time()))
    message = ts + method.upper() + path + body

    # Secret is base64-encoded — decode it first before using as HMAC key
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception:
        # If not valid base64, use raw bytes (some implementations store it raw)
        secret_bytes = api_secret.encode("utf-8")

    raw_sig   = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    signature = base64.b64encode(raw_sig).decode("utf-8")

    return {
        "POLY-ADDRESS":    address,
        "POLY-API-KEY":    api_key,
        "POLY-SIGNATURE":  signature,
        "POLY-TIMESTAMP":  ts,
        "POLY-PASSPHRASE": api_passphrase,
        "Content-Type":    "application/json",
    }


def _build_headers(method: str, path: str, body: str = "") -> dict:
    """
    Auto-select L2 if credentials are available, otherwise L1.
    Logs which auth level is being used on first call.
    """
    if _using_l2():
        try:
            return _build_l2_headers(method, path, body)
        except Exception as e:
            logger.warning("[POLY:AUTH] L2 header build failed (%s) — falling back to L1", e)
    return _build_l1_headers(method, path, body)


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

def _ticker_from_symbol(symbol: str) -> str:
    return symbol.upper().replace("-USDT", "")


def discover_market(symbol: str) -> dict | None:
    """
    Find the active 15-min market for a symbol via slug pattern.
    Caches for 13 minutes — refreshes before window expires.
    Market discovery uses no auth (public endpoint).
    """
    ticker    = _ticker_from_symbol(symbol)
    base_slug = _KNOWN_SLUGS.get(ticker)
    if not base_slug:
        logger.error("[POLY:%s] No slug pattern for ticker=%s", symbol, ticker)
        return None

    cached = _market_cache.get(ticker)
    if cached and time.time() - cached.get("_cached_at", 0) < 780:
        logger.info("[POLY:%s] Using cached market condition_id=%s",
                    symbol, cached.get("condition_id"))
        return cached

    try:
        resp = requests.get(
            f"{CLOB_BASE}/markets",
            params={"slug": base_slug, "active": "true", "closed": "false"},
            timeout=10,
        )
        logger.info("[POLY:%s] GET /markets?slug=%s → %d", symbol, base_slug, resp.status_code)
        if not resp.ok:
            logger.error("[POLY:%s] Market discovery HTTP %d", symbol, resp.status_code)
            return None

        data    = resp.json()
        markets = data if isinstance(data, list) else data.get("data", []) or []
        if not markets:
            logger.error("[POLY:%s] No active market for slug=%s", symbol, base_slug)
            return None

        # Pick soonest non-expired market
        now_ts = time.time()
        valid  = []
        for m in markets:
            end_str = m.get("end_date_iso") or m.get("endDate") or ""
            try:
                end_ts = datetime.fromisoformat(
                    end_str.replace("Z", "+00:00")).timestamp()
                if end_ts > now_ts - 5:
                    valid.append((end_ts, m))
            except Exception:
                valid.append((now_ts + 900, m))

        if not valid:
            logger.error("[POLY:%s] All markets expired for slug=%s", symbol, base_slug)
            return None

        valid.sort(key=lambda x: x[0])
        market = valid[0][1]

        # Extract token IDs — outcome = "Up"/"Down" or "Yes"/"No"
        tokens        = market.get("tokens") or []
        up_token_id   = None
        down_token_id = None

        for t in tokens:
            outcome = str(t.get("outcome", "")).lower()
            tid     = t.get("token_id") or t.get("tokenId")
            if outcome in ("up", "yes", "1"):
                up_token_id = tid
            elif outcome in ("down", "no", "0"):
                down_token_id = tid

        if not up_token_id or not down_token_id:
            if len(tokens) >= 2:
                up_token_id   = tokens[0].get("token_id") or tokens[0].get("tokenId")
                down_token_id = tokens[1].get("token_id") or tokens[1].get("tokenId")
            else:
                logger.error("[POLY:%s] Cannot extract token IDs: tokens=%s", symbol, tokens)
                return None

        result = {
            "condition_id":  market.get("condition_id") or market.get("conditionId"),
            "question_id":   market.get("question_id")  or market.get("questionId"),
            "slug":          market.get("market_slug")  or base_slug,
            "up_token_id":   str(up_token_id),
            "down_token_id": str(down_token_id),
            "end_date":      market.get("end_date_iso") or market.get("endDate"),
            "active":        market.get("active", True),
            "neg_risk":      market.get("neg_risk", False),
            "_cached_at":    time.time(),
        }

        _market_cache[ticker] = result
        logger.info(
            "[POLY:%s] Market ✓ condition_id=%s up=%s… down=%s… end=%s auth=%s",
            symbol, result["condition_id"],
            result["up_token_id"][:10], result["down_token_id"][:10],
            result["end_date"], "L2" if _using_l2() else "L1",
        )
        return result

    except Exception as e:
        logger.error("[POLY:%s] discover_market exception: %s", symbol, e, exc_info=True)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# EIP-712 ORDER SIGNING
# ══════════════════════════════════════════════════════════════════════════════

def _sign_order(order: dict, signature_type: int) -> str:
    """
    Sign order with EIP-712 against CTF Exchange on Polygon.
    signature_type: 0 = EOA (L1), 2 = POLY_GNOSIS_SAFE (L2)
    The signing key is always the EOA private key in both cases.
    """
    pk   = get_private_key()
    acct = Account.from_key(pk)

    domain = {
        "name":              "CTFExchange",
        "version":           "1",
        "chainId":           CHAIN_ID,
        "verifyingContract": CTF_EXCHANGE,
    }

    message = {
        "salt":          int(order["salt"]),
        "maker":         Web3.to_checksum_address(order["maker"]),
        "signer":        Web3.to_checksum_address(order["signer"]),
        "taker":         Web3.to_checksum_address(order["taker"]),
        "tokenId":       int(order["tokenId"]),
        "makerAmount":   int(order["makerAmount"]),
        "takerAmount":   int(order["takerAmount"]),
        "expiration":    int(order["expiration"]),
        "nonce":         int(order["nonce"]),
        "feeRateBps":    int(order["feeRateBps"]),
        "side":          int(order["side"]),
        "signatureType": signature_type,
    }

    try:
        signed = acct.sign_typed_data(
            domain_data=domain,
            message_types={"Order": _ORDER_TYPES},
            message_data=message,
        )
        return signed.signature.hex()
    except AttributeError:
        pass

    # eth-account < 0.9 fallback
    from eth_account.messages import encode_typed_data
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": _ORDER_TYPES,
        },
        "primaryType": "Order",
        "domain":      domain,
        "message":     message,
    }
    return acct.sign_message(
        encode_typed_data(typed)
    ).signature.hex()


# ══════════════════════════════════════════════════════════════════════════════
# LIVE ORDER
# ══════════════════════════════════════════════════════════════════════════════

def place_live_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    """
    Place a GTC limit BUY on Polymarket CLOB.
    Uses L2 auth (HMAC) if credentials set, otherwise L1 (private key).

    Flow:
      1. Validate credentials
      2. Discover active market + token IDs (public endpoint, no auth)
      3. Build EIP-712 signed order
         signatureType=2 for L2, signatureType=0 for L1
      4. POST /order with auto-selected auth headers
    """
    auth_level = "L2" if _using_l2() else "L1"
    logger.info("[POLY:%s] LIVE order: dir=%s $%.2f max=$%.3f auth=%s",
                symbol, signal_direction, position_size_usd,
                max_contract_price, auth_level)

    # 1. Validate
    pk = get_private_key()
    if not pk:
        return {"success": False,
                "error": "POLYMARKET_PRIVATE_KEY not set"}

    maker_addr = get_wallet_address()
    if not maker_addr:
        return {"success": False,
                "error": "Cannot derive wallet address from POLYMARKET_PRIVATE_KEY"}

    # 2. Market discovery (no auth needed)
    market = discover_market(symbol)
    if not market:
        return {"success": False,
                "error": f"No active 15-min Polymarket market for {symbol}"}

    direction_upper = signal_direction.upper()
    if direction_upper == "UP":
        token_id = market["up_token_id"]
    elif direction_upper == "DOWN":
        token_id = market["down_token_id"]
    else:
        return {"success": False,
                "error": f"Invalid direction: {signal_direction}"}

    if not token_id:
        return {"success": False,
                "error": f"token_id missing for direction={signal_direction}"}

    # 3. Build + sign order
    # signatureType: 0 = EOA (L1), 2 = POLY_GNOSIS_SAFE (L2 with EOA key)
    sig_type     = 2 if _using_l2() else 0
    price        = round(min(max_contract_price, 0.999), 3)
    size         = round(position_size_usd / price, 4)
    maker_amount = int(round(price * size * 1_000_000))  # USDC 6 decimals
    taker_amount = int(round(size * 1_000_000))           # outcome token 6 decimals
    salt         = int(time.time() * 1000)

    order = {
        "salt":          str(salt),
        "maker":         maker_addr,
        "signer":        maker_addr,
        "taker":         ZERO_ADDR,
        "tokenId":       str(token_id),
        "makerAmount":   maker_amount,
        "takerAmount":   taker_amount,
        "expiration":    "0",        # GTC = no expiry
        "nonce":         0,
        "feeRateBps":    0,
        "side":          0,          # BUY
        "signatureType": sig_type,
    }

    try:
        signature = _sign_order(order, sig_type)
    except Exception as e:
        return {"success": False, "error": f"EIP-712 signing failed: {e}"}

    if not signature.startswith("0x"):
        signature = "0x" + signature

    # 4. POST /order
    payload  = {
        "order": {**order, "signature": signature},
        "owner":     maker_addr,
        "orderType": "GTC",
    }
    body_str = json.dumps(payload, separators=(",", ":"))

    try:
        headers = _build_headers("POST", "/order", body_str)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        resp = requests.post(
            f"{CLOB_BASE}/order",
            headers=headers,
            data=body_str,
            timeout=15,
        )
        logger.info("[POLY:%s] POST /order → %d: %s",
                    symbol, resp.status_code, resp.text[:500])

        if not resp.ok:
            try:
                err_detail = resp.json()
            except Exception:
                err_detail = resp.text
            logger.error("[POLY:%s] Order rejected %d: %s",
                         symbol, resp.status_code, err_detail)

            # If L2 returns 401/403, log a helpful message
            if resp.status_code in (401, 403) and _using_l2():
                logger.error(
                    "[POLY:%s] L2 auth rejected — check POLYMARKET_API_KEY / "
                    "POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE are correct. "
                    "Re-run derive_api_key() to generate fresh credentials.", symbol
                )
            return {
                "success":      False,
                "http_status":  resp.status_code,
                "error":        f"HTTP {resp.status_code}",
                "api_response": err_detail,
                "auth_level":   auth_level,
            }

        result   = resp.json()
        order_id = (result.get("orderID")
                    or result.get("order_id")
                    or result.get("id")
                    or str(salt))

        logger.info("[POLY:%s] ORDER ✓ auth=%s dir=%s %.4f shares @ $%.4f id=%s",
                    symbol, auth_level, signal_direction, size, price, order_id)
        return {
            "success":            True,
            "order_id":           str(order_id),
            "contracts":          size,
            "price_per_contract": price,
            "total_spent":        position_size_usd,
            "condition_id":       market["condition_id"],
            "token_id":           token_id,
            "slug":               market["slug"],
            "signal_direction":   signal_direction,
            "trade_direction":    signal_direction,
            "maker":              maker_addr,
            "auth_level":         auth_level,
            "platform":           "polymarket",
        }

    except Exception as e:
        logger.error("[POLY:%s] Exception placing order: %s", symbol, e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SHADOW ORDER
# ══════════════════════════════════════════════════════════════════════════════

def place_shadow_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    price = min(max_contract_price, 0.999)
    size  = round(position_size_usd / price, 4)
    logger.info("[POLY:%s] SHADOW dir=%s %.4f @ $%.4f",
                symbol, signal_direction, size, price)
    return {
        "success":            True,
        "order_id":           f"poly_shadow_{int(time.time())}",
        "contracts":          size,
        "price_per_contract": price,
        "total_spent":        position_size_usd,
        "signal_direction":   signal_direction,
        "trade_direction":    signal_direction,
        "shadow":             True,
        "platform":           "polymarket",
    }


# ══════════════════════════════════════════════════════════════════════════════
# ORDER FILL CHECK
# ══════════════════════════════════════════════════════════════════════════════

def check_order_filled(order_id: str, token_id: str | None = None) -> dict:
    """
    Check fill status via GET /order/{order_id}.
    Uses L2 if available, otherwise L1.
    """
    if not order_id or str(order_id).startswith("poly_shadow_"):
        return {"filled": False, "status": "SHADOW", "error": "shadow order"}

    path = f"/order/{order_id}"
    try:
        headers = _build_headers("GET", path)
        resp    = requests.get(f"{CLOB_BASE}{path}", headers=headers, timeout=10)
        logger.info("[POLY:FILL] GET %s → %d", path, resp.status_code)

        if resp.ok:
            data       = resp.json()
            status_str = str(data.get("status", "")).upper()
            filled     = status_str in ("MATCHED", "FILLED")
            return {"filled": filled, "status": status_str, "trade": data, "error": None}
        return {"filled": False, "status": "ERROR",
                "error": f"HTTP {resp.status_code}", "trade": None}

    except Exception as e:
        logger.warning("[POLY:FILL] check_order_filled error: %s", e)
        return {"filled": False, "status": "ERROR", "error": str(e), "trade": None}


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def execute_order(
    symbol: str,
    signal_direction: str,
    mode: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
) -> dict:
    if mode == "live":
        return place_live_order(
            symbol, signal_direction, position_size_usd, max_contract_price
        )
    return place_shadow_order(
        symbol, signal_direction, position_size_usd, max_contract_price
    )
