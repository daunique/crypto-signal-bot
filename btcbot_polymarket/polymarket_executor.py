"""
Polymarket Order Executor — FAK (Fill-And-Kill) Limit Orders via CLOB API
══════════════════════════════════════════════════════════════════════════════
ORDER TYPE: FAK limit order
  time_in_force = "FAK"
  FAK = Fill as much as possible at the limit price, cancel the rest instantly.
  This is the closest Polymarket equivalent to the GTC limit orders on Limitless.
  Use this when you want guaranteed price control with immediate partial execution.

HOW POLYMARKET CLOB WORKS:
  Polymarket uses a Central Limit Order Book (CLOB) on Polygon mainnet.
  Every market has two sides: YES tokens and NO tokens.
  Your signal direction maps to:
    UP   → BUY YES tokens  (you believe the event resolves YES)
    DOWN → BUY NO tokens   (you believe the event resolves NO)

  Token prices range 0.01–0.99 USDC each (= implied probability).
  You spend USDC, receive shares. At resolution: winning shares = $1.00 each.

AUTH (one-time setup):
  Polymarket uses an L2 key system (separate from your wallet):
    POLY_API_KEY        → from Polymarket profile → API Keys
    POLY_API_SECRET     → shown once at creation
    POLY_API_PASSPHRASE → chosen at creation
    POLY_PRIVATE_KEY    → your wallet private key (signs on-chain approvals)
    POLY_CHAIN_ID       → 137 (Polygon mainnet)

MARKET MAPPING:
  Signals come from crypto price pairs (BTC-USDT, ETH-USDT, etc.)
  These are mapped to Polymarket prediction market condition IDs.
  Use POLY_MARKET_MAP env var (JSON) or the DEFAULT_MARKET_MAP below.
  Markets reset every day/week — update condition IDs regularly.

USDC APPROVAL (one-time):
  Run: python polymarket_executor.py --approve
  Or call approve_usdc() once before live trading.
  Approves CTF Exchange + NegRisk contracts on Polygon.

IMPORTANT — GEO-RESTRICTION:
  Polymarket blocks US IPs and some other regions.
  Deploy on AWS eu-west-1 (Ireland) — this is already outside the block list.
  Never run from a US IP or you will get 403 errors.
"""

import os
import json
import time
import hmac
import base64
import hashlib
import logging
import requests
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Polymarket CLOB API ────────────────────────────────────────────────────────
CLOB_BASE      = "https://clob.polymarket.com"
GAMMA_BASE     = "https://gamma-api.polymarket.com"   # market search
CHAIN_ID       = int(os.environ.get("POLY_CHAIN_ID", "137"))  # Polygon mainnet

# USDC on Polygon
USDC_ADDR      = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# CTF Exchange (the venue that holds collateral)
CTF_EXCHANGE   = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
# NegRisk CTF Exchange (used for multi-outcome markets)
NEG_RISK_ADDR  = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# ── Default market map: signal symbol → Polymarket condition_id ───────────────
# ⚠️  Polymarket markets expire — update these condition IDs regularly.
# Find current IDs at: https://polymarket.com/markets or via search_market()
# Example: "Will BTC be above $X on [date]?" markets
# You MUST replace these with real, active condition IDs before going live.
DEFAULT_MARKET_MAP: dict[str, dict] = {
    "BTC-USDT": {
        "condition_id": os.environ.get("POLY_MARKET_BTC", ""),
        "description":  "BTC price prediction market",
        "token_yes":    "",   # token_id for YES — from market details
        "token_no":     "",   # token_id for NO  — from market details
    },
    "ETH-USDT": {
        "condition_id": os.environ.get("POLY_MARKET_ETH", ""),
        "description":  "ETH price prediction market",
        "token_yes":    "",
        "token_no":     "",
    },
    "SOL-USDT": {
        "condition_id": os.environ.get("POLY_MARKET_SOL", ""),
        "description":  "SOL price prediction market",
        "token_yes":    "",
        "token_no":     "",
    },
    "XRP-USDT": {
        "condition_id": os.environ.get("POLY_MARKET_XRP", ""),
        "description":  "XRP price prediction market",
        "token_yes":    "",
        "token_no":     "",
    },
    "BNB-USDT": {
        "condition_id": os.environ.get("POLY_MARKET_BNB", ""),
        "description":  "BNB price prediction market",
        "token_yes":    "",
        "token_no":     "",
    },
    "DOGE-USDT": {
        "condition_id": os.environ.get("POLY_MARKET_DOGE", ""),
        "description":  "DOGE price prediction market",
        "token_yes":    "",
        "token_no":     "",
    },
}

_market_cache: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
# AUTH — L2 key HMAC signing
# ══════════════════════════════════════════════════════════════════════════════

def _get_credentials() -> tuple[str, str, str]:
    """Return (api_key, secret, passphrase) from env vars."""
    key        = os.environ.get("POLY_API_KEY", "").strip()
    secret     = os.environ.get("POLY_API_SECRET", "").strip()
    passphrase = os.environ.get("POLY_API_PASSPHRASE", "").strip()
    if not all([key, secret, passphrase]):
        raise ValueError(
            "Missing Polymarket credentials. Set POLY_API_KEY, "
            "POLY_API_SECRET, POLY_API_PASSPHRASE in environment."
        )
    return key, secret, passphrase


def _build_hmac_headers(method: str, path: str, body: str = "") -> dict:
    """
    Build L2 HMAC signed headers for Polymarket CLOB API.
    Signature = HMAC-SHA256(secret, timestamp + method + path + body)
    """
    key, secret, passphrase = _get_credentials()
    ts  = str(int(time.time() * 1000))   # milliseconds
    msg = ts + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "POLY-API-KEY":    key,
        "POLY-SIGNATURE":  sig,
        "POLY-TIMESTAMP":  ts,
        "POLY-PASSPHRASE": passphrase,
        "Content-Type":    "application/json",
    }


def _clob_get(path: str, params: dict = None) -> dict:
    """Authenticated GET to CLOB API."""
    url = CLOB_BASE + path
    headers = _build_hmac_headers("GET", path)
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _clob_post(path: str, body: dict) -> dict:
    """Authenticated POST to CLOB API."""
    url      = CLOB_BASE + path
    body_str = json.dumps(body)
    headers  = _build_hmac_headers("POST", path, body_str)
    resp = requests.post(url, headers=headers, data=body_str, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
# MARKET LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def get_market_map() -> dict:
    """
    Return the active market map.
    Priority: POLY_MARKET_MAP env var (JSON) → DEFAULT_MARKET_MAP.
    """
    raw = os.environ.get("POLY_MARKET_MAP", "")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("[POLYMARKET] Invalid POLY_MARKET_MAP JSON — using defaults")
    return DEFAULT_MARKET_MAP


def resolve_token_ids(condition_id: str) -> tuple[str, str]:
    """
    Given a condition_id, return (yes_token_id, no_token_id).
    Caches results in _market_cache.
    """
    if condition_id in _market_cache:
        return _market_cache[condition_id]

    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"condition_id": condition_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            raise ValueError(f"No market found for condition_id={condition_id}")

        market = markets[0]
        tokens = market.get("tokens", [])
        yes_id = next((t["token_id"] for t in tokens if t.get("outcome") == "Yes"), "")
        no_id  = next((t["token_id"] for t in tokens if t.get("outcome") == "No"), "")

        if not yes_id or not no_id:
            raise ValueError(f"Could not find YES/NO token IDs for {condition_id}")

        _market_cache[condition_id] = (yes_id, no_id)
        logger.info(f"[POLYMARKET] Resolved tokens for {condition_id}: "
                    f"YES={yes_id[:12]}... NO={no_id[:12]}...")
        return yes_id, no_id

    except Exception as e:
        logger.error(f"[POLYMARKET] resolve_token_ids failed: {e}")
        raise


def get_orderbook_mid(token_id: str) -> Optional[float]:
    """
    Fetch the current mid-price for a token from the CLOB orderbook.
    Used to set a competitive FAK limit price.
    """
    try:
        resp = _clob_get(f"/book", params={"token_id": token_id})
        bids = resp.get("bids", [])
        asks = resp.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return round((best_bid + best_ask) / 2, 4)
        if asks:
            return float(asks[0]["price"])
    except Exception as e:
        logger.warning(f"[POLYMARKET] Orderbook fetch failed for {token_id[:12]}: {e}")
    return None


def search_market(query: str, limit: int = 5) -> list[dict]:
    """
    Search Polymarket markets by keyword.
    Useful for finding condition IDs for new markets.
    """
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"q": query, "limit": limit, "active": True},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])
        results = []
        for m in markets[:limit]:
            results.append({
                "question":     m.get("question", ""),
                "condition_id": m.get("conditionId", ""),
                "end_date":     m.get("endDateIso", ""),
                "active":       m.get("active", False),
                "volume":       m.get("volume", 0),
            })
        return results
    except Exception as e:
        logger.error(f"[POLYMARKET] search_market error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# USDC APPROVAL (one-time on-chain setup)
# ══════════════════════════════════════════════════════════════════════════════

def approve_usdc() -> bool:
    """
    Approve USDC spending for CTF Exchange and NegRisk on Polygon.
    Must be called once before live trading. Safe to call again — it's idempotent.
    """
    try:
        from web3 import Web3
        from eth_account import Account

        private_key = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        rpc         = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")

        if not private_key:
            logger.error("[POLYMARKET] POLY_PRIVATE_KEY not set")
            return False

        w3      = Web3(Web3.HTTPProvider(rpc))
        account = Account.from_key(private_key)
        wallet  = account.address

        ERC20_ABI = [{
            "name": "approve", "type": "function", "stateMutability": "nonpayable",
            "inputs": [{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
            "outputs": [{"name":"","type":"bool"}]
        }, {
            "name": "allowance", "type": "function", "stateMutability": "view",
            "inputs": [{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
            "outputs": [{"name":"","type":"uint256"}]
        }]

        usdc    = w3.eth.contract(Web3.to_checksum_address(USDC_ADDR), abi=ERC20_ABI)
        MAX_INT = 2**256 - 1

        for label, spender in [("CTF Exchange", CTF_EXCHANGE), ("NegRisk", NEG_RISK_ADDR)]:
            allowance = usdc.functions.allowance(wallet, Web3.to_checksum_address(spender)).call()
            if allowance > 10**12:   # already approved (> $1M in 6 decimals)
                logger.info(f"[POLYMARKET] {label} already approved ✓")
                continue

            nonce = w3.eth.get_transaction_count(wallet)
            tx    = usdc.functions.approve(
                Web3.to_checksum_address(spender), MAX_INT
            ).build_transaction({
                "from":     wallet,
                "nonce":    nonce,
                "gas":      100_000,
                "gasPrice": w3.eth.gas_price,
                "chainId":  CHAIN_ID,
            })
            signed = account.sign_transaction(tx)
            txh    = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)

            if receipt["status"] == 1:
                logger.info(f"[POLYMARKET] {label} approved ✓ tx={txh.hex()}")
            else:
                logger.error(f"[POLYMARKET] {label} approval FAILED tx={txh.hex()}")
                return False

        return True

    except Exception as e:
        logger.error(f"[POLYMARKET] approve_usdc error: {e}")
        return False


def check_approval_status() -> dict:
    """Check USDC approval status for both venues."""
    try:
        from web3 import Web3
        from eth_account import Account

        private_key = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        rpc         = os.environ.get("POLY_RPC_URL", "https://polygon-rpc.com")
        w3          = Web3(Web3.HTTPProvider(rpc))
        account     = Account.from_key(private_key)
        wallet      = account.address

        ERC20_ABI = [{"name":"allowance","type":"function","stateMutability":"view",
                      "inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
                      "outputs":[{"name":"","type":"uint256"}]}]

        usdc = w3.eth.contract(Web3.to_checksum_address(USDC_ADDR), abi=ERC20_ABI)
        MIN  = 10**12

        ctf_ok  = usdc.functions.allowance(wallet, Web3.to_checksum_address(CTF_EXCHANGE)).call() > MIN
        neg_ok  = usdc.functions.allowance(wallet, Web3.to_checksum_address(NEG_RISK_ADDR)).call() > MIN

        return {
            "wallet":       wallet,
            "ctf_approved": ctf_ok,
            "neg_approved": neg_ok,
            "ready":        ctf_ok and neg_ok,
            "action": "None — ready to trade!" if (ctf_ok and neg_ok)
                      else "Call /api/approve-usdc or run: python polymarket_executor.py --approve"
        }
    except Exception as e:
        logger.error(f"[POLYMARKET] check_approval_status error: {e}")
        return {"ready": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# BALANCE
# ══════════════════════════════════════════════════════════════════════════════

def get_usdc_balance() -> float:
    """Return available USDC balance on Polymarket CLOB."""
    try:
        data = _clob_get("/balance")
        return float(data.get("balance", 0))
    except Exception as e:
        logger.error(f"[POLYMARKET] get_balance error: {e}")
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION — FAK LIMIT ORDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_fak_order(
    token_id:    str,
    side:        str,          # "BUY" or "SELL"
    price:       float,        # limit price (0.01–0.99)
    size_usdc:   float,        # USDC to spend
    max_price:   float,        # cap from settings (max_contract_price)
) -> dict:
    """
    Build a FAK (Fill-And-Kill) limit order payload for the CLOB API.

    FAK semantics:
      - Place order at `price`
      - Fill as much as possible immediately against existing resting orders
      - Cancel the unfilled remainder instantly (no resting on book)
      - Guarantees price discipline — never pays more than your limit

    size in USDC → contracts = size_usdc / price
    """
    # Enforce price cap from settings
    capped_price = min(price, max_price)
    capped_price = max(0.01, min(0.99, capped_price))   # Polymarket bounds

    # Number of contracts (shares) to buy
    contracts = round(size_usdc / capped_price, 2)

    order = {
        "order": {
            "tokenID":       token_id,
            "side":          side,           # "BUY"
            "type":          "LIMIT",
            "timeInForce":   "FAK",          # Fill-And-Kill
            "price":         str(capped_price),
            "size":          str(contracts),  # in contracts (shares)
        },
        "owner":    os.environ.get("POLY_API_KEY", ""),
        "orderType": "LIMIT",
    }
    return order, capped_price, contracts


def place_fak_order(
    token_id:   str,
    side:       str,
    price:      float,
    size_usdc:  float,
    max_price:  float,
) -> dict:
    """
    Sign and submit a FAK limit order to the CLOB.
    Returns standardised result dict.
    """
    try:
        order_payload, final_price, contracts = _build_fak_order(
            token_id, side, price, size_usdc, max_price
        )

        # Sign the order with the L2 key
        # The CLOB API handles signing via the HMAC auth headers
        resp = _clob_post("/order", order_payload)

        order_id    = resp.get("orderID", resp.get("orderId", ""))
        status      = resp.get("status", "")
        size_matched = float(resp.get("sizeMatched", 0) or 0)
        size_posted  = float(resp.get("sizePosted", 0) or 0)

        success = bool(order_id) and status not in ("error", "failed")

        logger.info(
            f"[POLYMARKET] FAK order {'✓' if success else '✗'} | "
            f"token={token_id[:12]}... side={side} price={final_price} "
            f"size={contracts} contracts | matched={size_matched} posted={size_posted} "
            f"status={status} id={order_id}"
        )

        return {
            "success":            success,
            "order_id":           order_id,
            "status":             status,
            "price_per_contract": final_price,
            "contracts":          size_matched if size_matched > 0 else contracts,
            "size_matched":       size_matched,
            "size_posted":        size_posted,
            "size_cancelled":     max(0, contracts - size_matched - size_posted),
            "usdc_spent":         round(size_matched * final_price, 4),
            "token_id":           token_id,
            "side":               side,
            "time_in_force":      "FAK",
            "error":              resp.get("error", None),
        }

    except requests.HTTPError as e:
        body = e.response.text if e.response else str(e)
        logger.error(f"[POLYMARKET] HTTP error placing FAK order: {e} | {body}")
        return {"success": False, "error": f"HTTP {e.response.status_code}: {body}"}
    except Exception as e:
        logger.error(f"[POLYMARKET] place_fak_order error: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — called from scheduler.py (drop-in for execute_order)
# ══════════════════════════════════════════════════════════════════════════════

def execute_order(
    symbol:        str,
    direction:     str,   # "UP" or "DOWN"
    mode:          str,   # "shadow" or "live"
    position_size: float, # USDC to spend
    max_price:     float, # from Settings.max_contract_price
) -> dict:
    """
    Execute a FAK limit order on Polymarket.
    Drop-in replacement for limitless_executor.execute_order().

    direction UP   → BUY YES token (bet event resolves YES)
    direction DOWN → BUY NO token  (bet event resolves NO)

    Returns dict with keys: success, order_id, contracts, price_per_contract, error
    """
    if mode == "shadow":
        logger.info(f"[POLYMARKET] Shadow mode — skipping real order for {symbol} {direction}")
        return {
            "success":            True,
            "order_id":           f"SHADOW-{int(time.time())}",
            "contracts":          round(position_size / max_price, 2),
            "price_per_contract": max_price,
            "size_matched":       round(position_size / max_price, 2),
            "usdc_spent":         position_size,
            "time_in_force":      "FAK",
            "shadow":             True,
        }

    # ── Live order ─────────────────────────────────────────────────────────────
    market_map = get_market_map()

    if symbol not in market_map:
        err = f"No Polymarket mapping for symbol {symbol}"
        logger.error(f"[POLYMARKET] {err}")
        return {"success": False, "error": err}

    market_cfg   = market_map[symbol]
    condition_id = market_cfg.get("condition_id", "")

    if not condition_id:
        err = f"condition_id not configured for {symbol}. Set POLY_MARKET_{symbol.split('-')[0]}"
        logger.error(f"[POLYMARKET] {err}")
        return {"success": False, "error": err}

    # Resolve YES/NO token IDs
    try:
        yes_id, no_id = resolve_token_ids(condition_id)
    except Exception as e:
        return {"success": False, "error": f"Token resolution failed: {e}"}

    # Pick the right token based on direction
    token_id = yes_id if direction == "UP" else no_id
    side     = "BUY"

    # Get current market mid-price for FAK limit setting
    mid = get_orderbook_mid(token_id)
    if mid is None:
        # Fall back to max_price as limit
        mid = max_price
        logger.warning(f"[POLYMARKET] Could not fetch mid-price for {symbol} — using max_price={max_price}")

    # For FAK, set limit slightly above mid to maximise fill chance
    # but never above max_price cap
    limit_price = min(round(mid + 0.01, 4), max_price)

    logger.info(
        f"[POLYMARKET] Placing FAK order | {symbol} {direction} | "
        f"token={token_id[:12]}... | mid={mid} limit={limit_price} "
        f"size=${position_size} max_price={max_price}"
    )

    result = place_fak_order(
        token_id   = token_id,
        side       = side,
        price      = limit_price,
        size_usdc  = position_size,
        max_price  = max_price,
    )

    result["symbol"]    = symbol
    result["direction"] = direction
    return result


# ══════════════════════════════════════════════════════════════════════════════
# ORDER STATUS
# ══════════════════════════════════════════════════════════════════════════════

def get_order_status(order_id: str) -> dict:
    """Check status of a previously placed order."""
    try:
        data = _clob_get(f"/order/{order_id}")
        return {
            "order_id":     order_id,
            "status":       data.get("status", "unknown"),
            "size_matched": data.get("sizeMatched", 0),
            "size_posted":  data.get("sizePosted", 0),
            "price":        data.get("price", 0),
        }
    except Exception as e:
        logger.error(f"[POLYMARKET] get_order_status error: {e}")
        return {"order_id": order_id, "status": "error", "error": str(e)}


def get_open_positions() -> list[dict]:
    """Return all open positions (resting orders + filled shares)."""
    try:
        data = _clob_get("/positions")
        return data if isinstance(data, list) else data.get("positions", [])
    except Exception as e:
        logger.error(f"[POLYMARKET] get_open_positions error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# CLI — python polymarket_executor.py --approve / --balance / --search "BTC"
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if "--approve" in sys.argv:
        print("Approving USDC on Polygon...")
        ok = approve_usdc()
        print("✓ Approval complete" if ok else "✗ Approval failed — check logs")

    elif "--status" in sys.argv:
        status = check_approval_status()
        print(json.dumps(status, indent=2))

    elif "--balance" in sys.argv:
        bal = get_usdc_balance()
        print(f"USDC balance: ${bal:.2f}")

    elif "--search" in sys.argv:
        idx   = sys.argv.index("--search")
        query = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "BTC"
        results = search_market(query)
        for r in results:
            print(f"\n  Question:     {r['question']}")
            print(f"  condition_id: {r['condition_id']}")
            print(f"  End date:     {r['end_date']}")
            print(f"  Volume:       {r['volume']}")

    else:
        print("Usage:")
        print("  python polymarket_executor.py --approve      # One-time USDC approval")
        print("  python polymarket_executor.py --status       # Check approval status")
        print("  python polymarket_executor.py --balance      # Check USDC balance")
        print("  python polymarket_executor.py --search 'BTC' # Find market condition IDs")
