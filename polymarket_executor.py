"""
Polymarket CLOB — Order Executor  (V2 — post April 28, 2026 migration)
══════════════════════════════════════════════════════════════════════════════
Polymarket shipped a breaking "CLOB V2" upgrade on April 28, 2026: new Exchange
contracts, a new collateral token (pUSD), a new signed Order struct, and a new
EIP-712 domain. V1-signed orders are rejected outright — there's no backward
compatibility. This module targets V2 exclusively.
Docs: docs.polymarket.com/v2-migration · docs.polymarket.com/api-reference/authentication

WHICH WALLET/CREDENTIALS DO I NEED?
────────────────────────────────────
Every normal Polymarket.com account — whether you logged in with an email/
Google (Magic Link) or a browser wallet — trades through a WALLET Polymarket
created for you, not directly through the key you hold. Two addresses matter:

  SIGNER  — the key that actually signs orders. For a Magic/email login this
            is a key Magic manages, which you export once from
            polymarket.com/settings → Export Private Key.
  FUNDER  — the wallet that actually HOLDS your pUSD/positions. This is your
            Polymarket profile address, also visible at polymarket.com/settings.
            For a plain MetaMask-direct account these two are the same
            address; for every other account type (Magic/Google login, or a
            connected browser wallet) they are DIFFERENT addresses, and using
            the signer's address as the maker/funder is why an order signed
            correctly can still be rejected or silently reference an empty
            wallet.

SIGNATURE_TYPE tells the exchange contract how to relate the two:
    0 = EOA          — signer IS the funder (bare private-key trading, no
                        Polymarket.com account involved). Needs POL for gas.
    1 = POLY_PROXY    — signer controls a separate proxy funder wallet. This
                        is what a normal Magic Link email/Google login account
                        uses. *** Most people reading this docstring want 1. ***
    2 = GNOSIS_SAFE   — signer controls a separate Gnosis Safe funder wallet.
                        Used by accounts that connected a browser wallet
                        (MetaMask etc.) through polymarket.com normally.
    3 = POLY_1271     — new "deposit wallet" onboarding path for brand-new
                        API-only users (ERC-1271 validated). Not what an
                        existing polymarket.com account should use.

CREDENTIALS (env vars)
───────────────────────
  Required always:
    POLYMARKET_PRIVATE_KEY      — the SIGNER's private key (0x... or raw hex)

  Required for signature types 1/2/3 (i.e. almost everyone — see above):
    POLYMARKET_FUNDER_ADDRESS   — your Polymarket profile address, the wallet
                                   that actually holds funds. Find it at
                                   polymarket.com/settings. For type 0 this
                                   defaults to the signer's own address if
                                   left unset.
    POLYMARKET_SIGNATURE_TYPE   — 0, 1, 2, or 3. Defaults to 1 (POLY_PROXY —
                                   the Magic Link email/Google case) since
                                   that covers the large majority of accounts.
                                   Set explicitly if you're on a different
                                   wallet type.

  Optional (auto-derived and cached in memory on first use if absent):
    POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE
        L2 trading credentials. You do not need to set these yourself —
        derive_api_key() runs automatically the first time an authenticated
        call is made and caches the result for the life of the process. Set
        them explicitly only if you want a fixed set across restarts.

WALLET / CHAIN  (V2 — see docs.polymarket.com/resources/contracts)
────────────────────────────────────────────────────────────────
  Chain:                Polygon mainnet (chain ID 137)
  pUSD (collateral):    0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB  (6 decimals)
  CTF Exchange V2:      0xE111180000d2663C0091e4f400237545B87B996B
  Neg Risk CTF Exch V2: 0xe2222d279d744050d28e00520010520000310F59

MARKET DISCOVERY
────────────────
Unlike a single evergreen slug, Polymarket's 15-minute crypto markets use a
NEW slug every window: "{asset}-updown-15m-{unix_window_start}", e.g.
"btc-updown-15m-1768502700". discover_market() computes the current window's
timestamp and queries the public Gamma API (gamma-api.polymarket.com) — no
auth needed for discovery.

HEARTBEAT  (new, mandatory in V2 for resting orders to survive)
─────────────────────────────────────────────────────────────
V2 introduced a liveness requirement: if the trading account doesn't send a
heartbeat at least every ~10 seconds, ALL open orders are auto-cancelled —
a safety net against a bot going silent while orders are still resting. Any
GTC order this module places would get cancelled within seconds of placement
unless something keeps beating. start_heartbeat_thread() runs that beat
continuously in the background for the life of the process; call it once at
app startup.
"""

import os
import time
import json
import hmac
import base64
import random
import hashlib
import logging
import threading
import requests
from datetime import datetime, timezone

from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
CLOB_BASE    = "https://clob.polymarket.com"
GAMMA_BASE   = "https://gamma-api.polymarket.com"
WWW_BASE     = "https://polymarket.com"
CHAIN_ID     = 137

CTF_EXCHANGE_V2          = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
PUSD_ADDRESS              = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # collateral, 6 decimals
ZERO_ADDR    = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32

_ERC20_BALANCE_ABI = [{
    "constant": True,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "type": "function",
}]

# EIP-712 order struct — V2 (drops taker/expiration/nonce/feeRateBps, adds
# timestamp/metadata/builder). docs.polymarket.com/v2-migration
_ORDER_TYPES = [
    {"name": "salt",          "type": "uint256"},
    {"name": "maker",         "type": "address"},
    {"name": "signer",        "type": "address"},
    {"name": "tokenId",       "type": "uint256"},
    {"name": "makerAmount",   "type": "uint256"},
    {"name": "takerAmount",   "type": "uint256"},
    {"name": "side",          "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
    {"name": "timestamp",     "type": "uint256"},
    {"name": "metadata",      "type": "bytes32"},
    {"name": "builder",       "type": "bytes32"},
]

# ClobAuthDomain (L1 API-key auth) stays at version "1" in V2 — only the
# Exchange (order-signing) domain bumped to "2".
_CLOB_AUTH_TYPES = [
    {"name": "address",   "type": "address"},
    {"name": "timestamp", "type": "string"},
    {"name": "nonce",     "type": "uint256"},
    {"name": "message",   "type": "string"},
]
_CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

_market_cache: dict = {}
_l2_creds_cache: dict = {}       # in-memory cache: {"key":..., "secret":..., "passphrase":...}
_heartbeat_thread = None
_heartbeat_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_private_key() -> str | None:
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        return None
    return pk if pk.startswith("0x") else "0x" + pk


def get_signer_address() -> str | None:
    """The address that SIGNS orders — derived from the private key."""
    pk = get_private_key()
    if not pk:
        return None
    try:
        return Web3.to_checksum_address(Account.from_key(pk).address)
    except Exception:
        return None


def get_signature_type() -> int:
    """
    0=EOA, 1=POLY_PROXY (Magic/email/Google login — default), 2=GNOSIS_SAFE,
    3=POLY_1271 (deposit wallet). Defaults to 1 because that's what a normal
    polymarket.com email/Google account uses, which is the common case.
    """
    raw = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "").strip()
    if raw == "":
        return 1
    try:
        val = int(raw)
    except ValueError:
        logger.warning("[POLY:AUTH] POLYMARKET_SIGNATURE_TYPE=%r is not an integer — defaulting to 1 (POLY_PROXY)", raw)
        return 1
    if val not in (0, 1, 2, 3):
        logger.warning("[POLY:AUTH] POLYMARKET_SIGNATURE_TYPE=%d is not 0-3 — defaulting to 1 (POLY_PROXY)", val)
        return 1
    return val


def get_funder_address() -> str | None:
    """
    The wallet that actually HOLDS funds — the 'maker' in every order.
    For signature type 0 (EOA) this defaults to the signer address if not
    explicitly set. For types 1/2/3 it MUST be set explicitly — this is your
    Polymarket profile address from polymarket.com/settings, and it is NOT
    generally the same address as the signer.
    """
    explicit = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip()
    if explicit:
        try:
            return Web3.to_checksum_address(explicit)
        except Exception:
            logger.error("[POLY:AUTH] POLYMARKET_FUNDER_ADDRESS=%r is not a valid address", explicit)
            return None
    if get_signature_type() == 0:
        return get_signer_address()
    return None  # required but missing for types 1/2/3


def _get_l2_creds() -> tuple:
    """
    Returns (api_key, api_secret, api_passphrase) — from env vars if set,
    else from the in-memory cache (populated by a prior auto-derive this
    process), else None.
    """
    key        = os.environ.get("POLYMARKET_API_KEY",        "").strip()
    secret     = os.environ.get("POLYMARKET_API_SECRET",     "").strip()
    passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    if key and secret and passphrase:
        return key, secret, passphrase
    if _l2_creds_cache:
        return (_l2_creds_cache.get("key"), _l2_creds_cache.get("secret"),
                _l2_creds_cache.get("passphrase"))
    return None


def _get_l2_source() -> str:
    """
    'env' | 'cache' | 'none' — which path _get_l2_creds() actually returned
    from. Matters because a PINNED env-var credential that was derived while
    hosted somewhere geo-blocked stays exactly as stale after moving to a
    non-blocked host — moving hosts only helps a credential that gets
    re-derived fresh (the 'cache'/auto-derive path). If POLYMARKET_API_KEY
    etc. are set as env vars and heartbeat/orders are still 401ing after a
    host move, the env vars are the first thing to clear.
    """
    key    = os.environ.get("POLYMARKET_API_KEY",        "").strip()
    secret = os.environ.get("POLYMARKET_API_SECRET",     "").strip()
    passph = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    if key and secret and passph:
        return "env"
    if _l2_creds_cache:
        return "cache"
    return "none"


def _using_l2() -> bool:
    return _get_l2_creds() is not None


def ensure_l2_creds() -> dict:
    """
    Makes sure L2 credentials exist — from env vars, from the in-memory
    cache, or by deriving them fresh via L1 auth right now. This is what
    lets someone trade with nothing more than POLYMARKET_PRIVATE_KEY (+
    funder/signature-type) set: no separate manual key-derivation step.
    """
    existing = _get_l2_creds()
    if existing:
        return {"success": True, "api_key": existing[0], "api_secret": existing[1],
                "api_passphrase": existing[2], "source": "cached"}
    result = derive_api_key()
    if result.get("success"):
        _l2_creds_cache.update({
            "key": result["api_key"], "secret": result["api_secret"],
            "passphrase": result["api_passphrase"],
        })
        result["source"] = "derived"
    return result


def check_geoblock() -> dict:
    """
    Asks Polymarket's own public endpoint whether THIS SERVER's outbound IP
    is geo-restricted — the only way to actually know, rather than going by
    a country name and a list. Polymarket fully blocks several countries
    (including Germany) on both the frontend and the API; a server hosted in
    a blocked region gets every authenticated call rejected regardless of
    how correct the credentials and signing are, and that rejection can look
    identical to a bad API key (e.g. a generic 401) depending on which layer
    catches it first — so this needs to be checked directly, not inferred.
    Docs: docs.polymarket.com/api-reference/geoblock

    Returns dict: blocked(bool|None — None if the check itself failed),
    ip(str|None), country(str|None), region(str|None), error(str|None).
    """
    try:
        resp = requests.get(f"{WWW_BASE}/api/geoblock", timeout=8)
        if not resp.ok:
            return {"blocked": None, "ip": None, "country": None, "region": None,
                    "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        return {
            "blocked": data.get("blocked"),
            "ip":      data.get("ip"),
            "country": data.get("country"),
            "region":  data.get("region"),
            "error":   None,
        }
    except Exception as e:
        return {"blocked": None, "ip": None, "country": None, "region": None, "error": str(e)}


def validate_credentials() -> dict:
    pk        = get_private_key()
    signer    = get_signer_address()
    sig_type  = get_signature_type()
    funder    = get_funder_address()
    l2        = _get_l2_creds()
    l2_source = _get_l2_source()
    auth_level = "L2" if l2 else ("L1" if pk else "NONE")
    sig_type_name = {0: "EOA", 1: "POLY_PROXY (Magic/email/Google)",
                     2: "GNOSIS_SAFE", 3: "POLY_1271 (deposit wallet)"}.get(sig_type, "?")
    return {
        "POLYMARKET_PRIVATE_KEY":    bool(pk),
        "POLYMARKET_FUNDER_ADDRESS": bool(funder),
        "POLYMARKET_API_KEY":        bool(l2),
        "POLYMARKET_API_SECRET":     bool(l2),
        "POLYMARKET_API_PASSPHRASE": bool(l2),
        "signer_address":            signer,
        "funder_address":            funder,
        "signature_type":            sig_type,
        "signature_type_name":       sig_type_name,
        "auth_level":                auth_level,
        "signing_ready":             bool(pk),
        "funder_configured":         bool(funder),
        "live_trading_ready":        bool(pk and funder),
        "l2_ready":                  bool(l2),
        "l2_source":                 l2_source,  # 'env' (pinned) | 'cache' (auto-derived this process) | 'none'
        "heartbeat_active":          bool(_heartbeat_thread and _heartbeat_thread.is_alive()),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT BALANCE  (on-chain — pUSD + POL, checked on the FUNDER wallet)
# ══════════════════════════════════════════════════════════════════════════════

_FALLBACK_POLYGON_RPCS = [
    "https://polygon-rpc.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.gateway.tenderly.co",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]


def get_balance() -> dict:
    """
    Reads on-chain pUSD balance for the FUNDER wallet (where trading funds
    actually live — NOT the signer's raw EOA for proxy/Safe/deposit-wallet
    accounts) directly from Polygon. Also reports the SIGNER's native POL
    balance, since type-0 (EOA) trading pays its own gas from the signer.

    Tries POLYGON_RPC_URL first if set, then a short list of public RPCs in
    sequence — a single public endpoint (especially the default
    polygon-rpc.com) is frequently rate-limited or flaky for automated
    traffic, which reads identically to "can't connect" if there's no
    fallback. For production reliability, set POLYGON_RPC_URL to a
    dedicated endpoint (Alchemy/Infura/QuickNode all have free tiers).
    """
    funder = get_funder_address()
    signer = get_signer_address()
    if not funder:
        sig_type = get_signature_type()
        return {"success": False,
                "error": ("POLYMARKET_FUNDER_ADDRESS not set — required for signature "
                           f"type {sig_type}. Find your address at polymarket.com/settings.")
                          if sig_type != 0 else
                          "POLYMARKET_PRIVATE_KEY not set — no wallet to check"}

    custom_rpc = os.environ.get("POLYGON_RPC_URL", "").strip()
    rpc_candidates = ([custom_rpc] if custom_rpc else []) + _FALLBACK_POLYGON_RPCS

    w3 = None
    last_error = None
    used_rpc = None
    for rpc_url in rpc_candidates:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
            if candidate.is_connected():
                w3 = candidate
                used_rpc = rpc_url
                break
            last_error = f"not connected ({rpc_url})"
        except Exception as e:
            last_error = f"{rpc_url}: {e}"
            continue

    if not w3:
        return {"success": False,
                "error": (f"Could not reach any Polygon RPC (tried {len(rpc_candidates)} "
                          f"endpoint(s), last error: {last_error}). Consider setting "
                          "POLYGON_RPC_URL to a dedicated provider (Alchemy/Infura/QuickNode "
                          "free tier) — public RPCs are often rate-limited for bot traffic.")}

    try:
        pusd = w3.eth.contract(
            address=Web3.to_checksum_address(PUSD_ADDRESS),
            abi=_ERC20_BALANCE_ABI,
        )
        pusd_raw = pusd.functions.balanceOf(funder).call()
        pusd_balance = pusd_raw / 1_000_000  # pUSD has 6 decimals

        pol_balance = None
        if signer:
            pol_raw = w3.eth.get_balance(signer)
            pol_balance = float(w3.from_wei(pol_raw, "ether"))

        return {
            "success":      True,
            "funder":       funder,
            "signer":       signer,
            "pusd_balance": round(pusd_balance, 4),
            "usdc_balance": round(pusd_balance, 4),  # alias — pUSD is the trading balance now
            "pol_balance":  round(pol_balance, 5) if pol_balance is not None else None,
            "chain":        "Polygon",
            "rpc_used":     used_rpc,
            "checked_at":   datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error("[POLYMARKET] get_balance failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# L1 AUTH  (EIP-712 ClobAuth signature — used to derive/create L2 API creds)
# ══════════════════════════════════════════════════════════════════════════════

def _sign_clob_auth(timestamp: str, nonce: int = 0) -> str:
    """
    Builds the POLY_SIGNATURE for L1 auth: an EIP-712 typed-data signature
    over the ClobAuth struct, NOT a plain personal_sign of the timestamp.
    domain: {name: "ClobAuthDomain", version: "1", chainId}
    message: {address: signer, timestamp, nonce, message: fixed attestation string}
    This scheme is unchanged by the V2 exchange upgrade — only order signing
    (a completely separate domain) changed. docs.polymarket.com/api-reference/authentication
    """
    pk     = get_private_key()
    acct   = Account.from_key(pk)
    signer = get_signer_address()
    domain = {"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID}
    message = {
        "address":   signer,
        "timestamp": timestamp,
        "nonce":     nonce,
        "message":   _CLOB_AUTH_MESSAGE,
    }

    try:
        signed = acct.sign_typed_data(
            domain_data=domain,
            message_types={"ClobAuth": _CLOB_AUTH_TYPES},
            message_data=message,
        )
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig
    except AttributeError:
        pass  # eth-account < 0.9 — fall through to the manual path below

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name",    "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": _CLOB_AUTH_TYPES,
        },
        "primaryType": "ClobAuth",
        "domain":      domain,
        "message":     message,
    }
    signed = acct.sign_message(encode_typed_data(typed))
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def _build_l1_headers(nonce: int = 0) -> dict:
    """
    L1 auth headers — used only for deriving/creating L2 API credentials.
    Header names use UNDERSCORES (POLY_ADDRESS etc.), matching the current
    official OpenAPI spec — NOT the hyphenated POLY-ADDRESS style.
    """
    pk = get_private_key()
    if not pk:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set")
    signer = get_signer_address()
    ts     = str(int(time.time()))
    signature = _sign_clob_auth(ts, nonce)
    return {
        "POLY_ADDRESS":   signer,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": ts,
        "POLY_NONCE":     str(nonce),
        "Content-Type":   "application/json",
    }


# ══════════════════════════════════════════════════════════════════════════════
# L2 API KEY DERIVATION  (auto-runs on first authenticated call)
# ══════════════════════════════════════════════════════════════════════════════

def derive_api_key(nonce: int = 0) -> dict:
    """
    Obtains L2 API credentials: tries GET /auth/derive-api-key first
    (retrieves existing credentials for this nonce — safe/idempotent), and
    falls back to POST /auth/api-key (creates new ones) if none exist yet.

    Returns {success, api_key, api_secret, api_passphrase} or {success: False, error}.
    """
    pk = get_private_key()
    if not pk:
        return {"success": False, "error": "POLYMARKET_PRIVATE_KEY not set"}

    def _extract(data: dict):
        api_key        = data.get("apiKey")        or data.get("api_key")        or data.get("key")
        api_secret     = data.get("secret")        or data.get("api_secret")
        api_passphrase = data.get("passphrase")    or data.get("api_passphrase")
        if all([api_key, api_secret, api_passphrase]):
            return api_key, api_secret, api_passphrase
        return None

    # 1. Try deriving existing credentials first (read-only, no side effects)
    try:
        headers = _build_l1_headers(nonce)
        resp = requests.get(f"{CLOB_BASE}/auth/derive-api-key", headers=headers, timeout=15)
        logger.info("[POLY:AUTH] GET /auth/derive-api-key → %d", resp.status_code)
        if resp.ok:
            found = _extract(resp.json())
            if found:
                logger.info("[POLY:AUTH] ✓ Derived existing L2 credentials for %s", get_signer_address())
                return {"success": True, "api_key": found[0], "api_secret": found[1], "api_passphrase": found[2]}
    except Exception as e:
        logger.info("[POLY:AUTH] derive-api-key attempt failed (%s), trying create", e)

    # 2. Fall back to creating new credentials
    try:
        body_str = json.dumps({"nonce": nonce}, separators=(",", ":"))
        headers  = _build_l1_headers(nonce)
        resp = requests.post(f"{CLOB_BASE}/auth/api-key", headers=headers, data=body_str, timeout=15)
        logger.info("[POLY:AUTH] POST /auth/api-key → %d", resp.status_code)
        if not resp.ok:
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:400]}"}
        found = _extract(resp.json())
        if not found:
            return {"success": False, "error": f"Unexpected response shape: {resp.text[:300]}"}
        logger.info(
            "[POLY:AUTH] ✓ New L2 API credentials created — to pin them across restarts, "
            "add to env vars: POLYMARKET_API_KEY=%s POLYMARKET_API_SECRET=%s POLYMARKET_API_PASSPHRASE=%s",
            found[0], found[1], found[2],
        )
        return {"success": True, "api_key": found[0], "api_secret": found[1], "api_passphrase": found[2]}
    except Exception as e:
        logger.error("[POLY:AUTH] derive_api_key exception: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# L2 AUTH  (HMAC-SHA256 with derived API key — used for every trading call)
# ══════════════════════════════════════════════════════════════════════════════

def _build_l2_headers(method: str, path: str, body: str = "") -> dict:
    """
    L2 HMAC auth: message = timestamp + method.upper() + path + body,
    signature = base64(HMAC-SHA256(base64decode(secret), message)).
    Header names use underscores per the current OpenAPI spec.
    """
    creds = _get_l2_creds()
    if not creds:
        raise ValueError("L2 credentials not available")
    api_key, api_secret, api_passphrase = creds
    signer = get_signer_address()
    if not signer:
        raise ValueError("POLYMARKET_PRIVATE_KEY not set — cannot derive signer address")

    ts      = str(int(time.time()))
    message = ts + method.upper() + path + body
    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception:
        secret_bytes = api_secret.encode("utf-8")
    raw_sig   = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    signature = base64.b64encode(raw_sig).decode("utf-8")

    return {
        "POLY_ADDRESS":    signer,
        "POLY_API_KEY":    api_key,
        "POLY_SIGNATURE":  signature,
        "POLY_TIMESTAMP":  ts,
        "POLY_PASSPHRASE": api_passphrase,
        "Content-Type":    "application/json",
    }


def _build_headers(method: str, path: str, body: str = "") -> dict:
    """Auto-derives L2 credentials if needed, then builds L2 headers."""
    creds = ensure_l2_creds()
    if not creds.get("success"):
        raise ValueError(f"L2 auth unavailable: {creds.get('error')}")
    return _build_l2_headers(method, path, body)


# ══════════════════════════════════════════════════════════════════════════════
# HEARTBEAT  (V2 requirement — resting orders are cancelled without one)
# ══════════════════════════════════════════════════════════════════════════════

def send_heartbeat() -> dict:
    try:
        headers = _build_headers("POST", "/heartbeats", "")
        resp = requests.post(f"{CLOB_BASE}/heartbeats", headers=headers, data="", timeout=8)
        if resp.ok:
            return {"success": True, "status": resp.json().get("status")}
        return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def start_heartbeat_thread(interval_s: float = 5.0) -> bool:
    """
    Starts a background daemon thread that beats every `interval_s` seconds
    (well inside the ~10s+5s buffer window) for the life of the process.
    Safe to call more than once — only ever starts one thread. No-ops
    (logs and returns False) if there's no private key configured yet;
    call again later once credentials are in place.
    """
    global _heartbeat_thread
    with _heartbeat_lock:
        if _heartbeat_thread and _heartbeat_thread.is_alive():
            return True
        if not get_private_key():
            logger.info("[POLY:HEARTBEAT] No POLYMARKET_PRIVATE_KEY yet — heartbeat not started")
            return False

        def _loop():
            fails = 0
            while True:
                result = send_heartbeat()
                if result.get("success"):
                    fails = 0
                else:
                    fails += 1
                    if fails <= 3 or fails % 12 == 0:  # don't spam logs on sustained outages
                        logger.warning("[POLY:HEARTBEAT] beat failed (%dx): %s", fails, result.get("error"))
                time.sleep(interval_s)

        _heartbeat_thread = threading.Thread(target=_loop, daemon=True, name="poly-heartbeat")
        _heartbeat_thread.start()
        logger.info("[POLY:HEARTBEAT] started — beating every %.1fs so GTC orders survive", interval_s)
        return True


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY  (Gamma API — dynamic per-window slugs, no auth needed)
# ══════════════════════════════════════════════════════════════════════════════

def _ticker_from_symbol(symbol: str) -> str:
    return symbol.upper().replace("-USDT", "")


def _window_start_ts(interval_s: int = 900, offset_windows: int = 0) -> int:
    now = int(time.time())
    window = (now // interval_s) * interval_s
    return window + offset_windows * interval_s


def discover_market(symbol: str, timeframe: str = "15m") -> dict | None:
    """
    Find the active market for a symbol at the given timeframe.

    Polymarket's crypto up/down markets use a slug that changes every window:
    "{asset}-updown-{tf}-{unix_window_start}" (e.g. "btc-updown-15m-1768502700"
    or "btc-updown-5m-1780297500" for the 5-minute cadence) — NOT a single
    static slug, so the slug has to be computed from the clock, not looked up
    from a fixed table. Queries the public Gamma API (no auth).

    Tries the current window first, then the next window (covers the few
    seconds right at a boundary where the new market may not be indexed yet
    yet the old one has already closed), caching whichever is found.
    """
    ticker = _ticker_from_symbol(symbol)
    asset  = ticker.lower()
    cache_key = f"{ticker}:{timeframe}"
    interval_s = 300 if timeframe == "5m" else 900

    cached = _market_cache.get(cache_key)
    if cached and time.time() - cached.get("_cached_at", 0) < 60 and cached.get("_window_end", 0) > time.time():
        return cached

    for offset in (0, 1):
        window_start = _window_start_ts(interval_s, offset)
        slug = f"{asset}-updown-{timeframe}-{window_start}"
        market = _fetch_gamma_market_by_slug(slug)
        if market:
            result = _parse_gamma_market(market, slug, window_start)
            if result:
                _market_cache[cache_key] = result
                logger.info(
                    "[POLY:%s/%s] Market ✓ slug=%s condition_id=%s up=%s… down=%s… end=%s",
                    symbol, timeframe, slug, result["condition_id"],
                    (result["up_token_id"] or "")[:10], (result["down_token_id"] or "")[:10],
                    result["end_date"],
                )
                return result

    logger.error("[POLY:%s/%s] No active market found (tried current + next window)", symbol, timeframe)
    return None


def _fetch_gamma_market_by_slug(slug: str) -> dict | None:
    """Tries /events?slug= first (typical wrapping for these markets), then /markets?slug= directly."""
    try:
        resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=10)
        if resp.ok:
            data   = resp.json()
            events = data if isinstance(data, list) else data.get("data", []) or []
            for ev in events:
                markets = ev.get("markets") or []
                if markets:
                    return markets[0]
    except Exception as e:
        logger.info("[POLY] gamma /events?slug=%s error: %s", slug, e)

    try:
        resp = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, timeout=10)
        if resp.ok:
            data    = resp.json()
            markets = data if isinstance(data, list) else data.get("data", []) or []
            if markets:
                return markets[0]
    except Exception as e:
        logger.info("[POLY] gamma /markets?slug=%s error: %s", slug, e)

    return None


def _parse_gamma_market(market: dict, slug: str, window_start: int) -> dict | None:
    condition_id = market.get("conditionId") or market.get("condition_id")
    if not condition_id:
        return None

    # clobTokenIds and outcomes are parallel arrays, often JSON-encoded strings
    def _as_list(v):
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return []

    token_ids = _as_list(market.get("clobTokenIds"))
    outcomes  = _as_list(market.get("outcomes"))

    up_token_id, down_token_id = None, None
    for i, outcome in enumerate(outcomes):
        if i >= len(token_ids):
            break
        o = str(outcome).lower()
        if o in ("up", "yes"):
            up_token_id = token_ids[i]
        elif o in ("down", "no"):
            down_token_id = token_ids[i]

    if (not up_token_id or not down_token_id) and len(token_ids) >= 2:
        up_token_id, down_token_id = token_ids[0], token_ids[1]

    if not up_token_id or not down_token_id:
        logger.error("[POLY] Cannot extract token IDs for slug=%s: tokens=%s outcomes=%s", slug, token_ids, outcomes)
        return None

    return {
        "condition_id":   condition_id,
        "slug":           market.get("slug") or slug,
        "up_token_id":    str(up_token_id),
        "down_token_id":  str(down_token_id),
        "end_date":       market.get("endDate") or market.get("end_date_iso"),
        "active":         market.get("active", True),
        "closed":         market.get("closed", False),
        "neg_risk":       bool(market.get("negRisk", market.get("neg_risk", False))),
        "tick_size":      str(market.get("orderPriceMinTickSize") or market.get("minimum_tick_size") or "0.01"),
        "outcomes":       outcomes,
        "outcome_prices":  _as_list(market.get("outcomePrices")),
        "_cached_at":     time.time(),
        "_window_start":  window_start,
        "_window_end":    window_start + 900,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EIP-712 ORDER SIGNING  (V2 domain + struct)
# ══════════════════════════════════════════════════════════════════════════════

def _sign_order(order: dict, signature_type: int, neg_risk: bool = False) -> str:
    """
    Sign order with EIP-712 against CTF Exchange V2 (or Neg Risk CTF Exchange
    V2). The signing key is always the SIGNER's private key, regardless of
    signature_type — signature_type just tells the contract how to relate
    signer to maker (see module docstring).
    """
    pk   = get_private_key()
    acct = Account.from_key(pk)

    verifying_contract = NEG_RISK_CTF_EXCHANGE_V2 if neg_risk else CTF_EXCHANGE_V2
    domain = {
        "name":              "Polymarket CTF Exchange",
        "version":           "2",
        "chainId":           CHAIN_ID,
        "verifyingContract": verifying_contract,
    }
    message = {
        "salt":          int(order["salt"]),
        "maker":         Web3.to_checksum_address(order["maker"]),
        "signer":        Web3.to_checksum_address(order["signer"]),
        "tokenId":       int(order["tokenId"]),
        "makerAmount":   int(order["makerAmount"]),
        "takerAmount":   int(order["takerAmount"]),
        "side":          int(order["side"]),
        "signatureType": signature_type,
        "timestamp":     int(order["timestamp"]),
        "metadata":      order.get("metadata", ZERO_BYTES32),
        "builder":       order.get("builder", ZERO_BYTES32),
    }

    try:
        signed = acct.sign_typed_data(
            domain_data=domain,
            message_types={"Order": _ORDER_TYPES},
            message_data=message,
        )
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig
    except AttributeError:
        pass  # eth-account < 0.9 — fall through to the manual path below

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
    signed = acct.sign_message(encode_typed_data(typed))
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


# ══════════════════════════════════════════════════════════════════════════════
# LIVE ORDER
# ══════════════════════════════════════════════════════════════════════════════

def place_live_order(
    symbol: str,
    signal_direction: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
    timeframe: str = "15m",
    order_type: str = "GTC",
) -> dict:
    """
    Place an order on Polymarket CLOB V2 — GTC (resting limit) or FOK
    (fill-or-kill "market" order) depending on `order_type`.

    Same on-chain order payload either way (makerAmount/takerAmount/
    signature) — only the orderType field sent to /order differs.
    max_contract_price is the resting limit price for GTC, and the worst
    acceptable execution price (slippage bound) for FOK — a FOK order
    either fills completely, immediately, at up to this price, or is
    cancelled outright; it does not mean "accept any price".

    Flow:
      1. Validate credentials (signer, funder, signature type)
      2. Discover active market for this timeframe (Gamma API, public)
      3. Build + EIP-712 sign a V2 order (maker=funder, signer=EOA)
      4. POST /order with L2 auth headers (auto-derived if not cached)
      5. Make sure the heartbeat thread is running, or this GTC order gets
         auto-cancelled within ~10-15s regardless of how correctly it was placed
         (FOK orders resolve synchronously and don't need the heartbeat the
         same way, but starting it is harmless either way)
    """
    if order_type not in ("GTC", "FOK"):
        order_type = "GTC"
    sig_type = get_signature_type()
    logger.info("[POLY:%s/%s] LIVE %s order: dir=%s $%.2f max=$%.3f sig_type=%d",
                symbol, timeframe, order_type, signal_direction, position_size_usd, max_contract_price, sig_type)

    pk = get_private_key()
    if not pk:
        return {"success": False, "error": "POLYMARKET_PRIVATE_KEY not set"}

    signer_addr = get_signer_address()
    funder_addr = get_funder_address()
    if not signer_addr:
        return {"success": False, "error": "Cannot derive signer address from POLYMARKET_PRIVATE_KEY"}
    if not funder_addr:
        return {"success": False,
                "error": (f"POLYMARKET_FUNDER_ADDRESS not set — required for signature_type={sig_type}. "
                          "Find your funding wallet address at polymarket.com/settings.")}

    start_heartbeat_thread()  # no-op if already running

    market = discover_market(symbol, timeframe=timeframe)
    if not market:
        return {"success": False, "error": f"No active {timeframe} Polymarket market for {symbol}"}

    direction_upper = signal_direction.upper()
    if direction_upper == "UP":
        token_id = market["up_token_id"]
    elif direction_upper == "DOWN":
        token_id = market["down_token_id"]
    else:
        return {"success": False, "error": f"Invalid direction: {signal_direction}"}
    if not token_id:
        return {"success": False, "error": f"token_id missing for direction={signal_direction}"}

    try:
        tick_size = float(market.get("tick_size") or "0.01")
    except (TypeError, ValueError):
        tick_size = 0.01
    decimals = max(0, len(market.get("tick_size", "0.01").split(".")[-1])) if "." in str(market.get("tick_size", "0.01")) else 2

    if order_type == "FOK":
        # True market order: spend exactly position_size_usd, no price floor
        # on shares received — a max_contract_price cap doesn't apply here
        # the way it does for a resting GTC limit, since FOK means "fill
        # immediately at whatever the market offers, or cancel entirely."
        # taker_amount=1 (the smallest representable unit) means "accept at
        # least this many shares back" is trivially satisfied at any real
        # execution price — the tradeoff a true market order accepts is
        # exactly this: execution certainty over price certainty.
        maker_amount = int(round(position_size_usd * 1_000_000))
        taker_amount = 1
        price = round(1 - tick_size, decimals)   # informational only, for logging/response
        size  = round(position_size_usd / max(price, tick_size), 4)
        logger.warning(
            "[POLY:%s] FOK market order — spend $%.2f (makerAmount=%d), no price "
            "floor (takerAmount=%d, accepts any execution price).",
            symbol, position_size_usd, maker_amount, taker_amount
        )
    else:
        price = min(max_contract_price, round(1 - tick_size, decimals))
        price = round(round(price / tick_size) * tick_size, decimals)
        size  = round(position_size_usd / price, 4)
        maker_amount = int(round(price * size * 1_000_000))  # pUSD, 6 decimals
        taker_amount = int(round(size * 1_000_000))           # outcome token, 6 decimals

    ts_ms = int(time.time() * 1000)
    salt  = ts_ms * 1000 + random.randint(0, 999)

    order = {
        "salt":          salt,
        "maker":         funder_addr,
        "signer":        signer_addr,
        "tokenId":       str(token_id),
        "makerAmount":   maker_amount,
        "takerAmount":   taker_amount,
        "side":          0,          # BUY (signed struct uses uint8)
        "signatureType": sig_type,
        "timestamp":     ts_ms,
        "metadata":      ZERO_BYTES32,
        "builder":       ZERO_BYTES32,
    }

    try:
        signature = _sign_order(order, sig_type, neg_risk=market.get("neg_risk", False))
    except Exception as e:
        return {"success": False, "error": f"EIP-712 signing failed: {e}"}

    try:
        l2 = ensure_l2_creds()
        if not l2.get("success"):
            return {"success": False, "error": f"L2 auth unavailable: {l2.get('error')}"}
        api_key = l2["api_key"]
    except Exception as e:
        return {"success": False, "error": f"L2 credential error: {e}"}

    # Wire payload: side as STRING, expiration present here only (not signed),
    # salt/timestamp as their documented wire types. owner = L2 API key (UUID).
    wire_order = {
        "salt":          salt,
        "maker":         funder_addr,
        "signer":        signer_addr,
        "tokenId":       str(token_id),
        "makerAmount":   str(maker_amount),
        "takerAmount":   str(taker_amount),
        "side":          "BUY",
        "expiration":    "0",     # no expiry either way — FOK resolves synchronously
                                    # (fills immediately or is rejected), GTC rests
                                    # until filled/cancelled; "0" is a no-op for FOK.
        "timestamp":     str(ts_ms),
        "metadata":      "",
        "builder":       ZERO_BYTES32,
        "signature":     signature,
        "signatureType": sig_type,
    }
    payload  = {"order": wire_order, "owner": api_key, "orderType": order_type}
    body_str = json.dumps(payload, separators=(",", ":"))

    try:
        headers = _build_l2_headers("POST", "/order", body_str)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        resp = requests.post(f"{CLOB_BASE}/order", headers=headers, data=body_str, timeout=15)
        logger.info("[POLY:%s] POST /order → %d: %s", symbol, resp.status_code, resp.text[:500])

        if not resp.ok:
            try:
                err_detail = resp.json()
            except Exception:
                err_detail = resp.text
            logger.error("[POLY:%s] Order rejected %d: %s", symbol, resp.status_code, err_detail)
            if resp.status_code in (401, 403):
                logger.error(
                    "[POLY:%s] Auth rejected — check POLYMARKET_PRIVATE_KEY, "
                    "POLYMARKET_FUNDER_ADDRESS, and POLYMARKET_SIGNATURE_TYPE match your "
                    "actual Polymarket.com account (polymarket.com/settings).", symbol
                )
            return {"success": False, "http_status": resp.status_code, "error": f"HTTP {resp.status_code}",
                    "api_response": err_detail}

        result   = resp.json()
        order_id = result.get("orderID") or result.get("order_id") or result.get("id") or str(salt)
        status   = result.get("status", "unknown")

        logger.info("[POLY:%s] ORDER ✓ dir=%s %.4f shares @ $%.4f id=%s status=%s",
                    symbol, signal_direction, size, price, order_id, status)
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
            "maker":              funder_addr,
            "signer":             signer_addr,
            "order_status":       status,
            "auth_level":         "L2",
            "platform":           "polymarket",
        }
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # AMBIGUOUS FAILURE — same reasoning as limitless_executor.place_live_order:
        # we don't know if Polymarket's server received and processed this
        # signed order before the connection dropped or timed out. Blindly
        # retrying risks a second real order for the same intended position.
        # `ambiguous: True` tells scheduler._run_polymarket to stop and alert
        # instead of retrying automatically.
        logger.error("[POLY:%s] AMBIGUOUS order failure (network) — NOT safe to auto-retry: %s",
                     symbol, e, exc_info=True)
        return {"success": False, "error": str(e), "ambiguous": True}

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
    timeframe: str = "15m",
    order_type: str = "GTC",
) -> dict:
    """
    Simulated order — no real funds move, no signing/auth needed. Still
    discovers the real market (public Gamma API, no auth) so the slug is
    captured and shadow-mode trades can be checked against real Polymarket
    resolution later, the same way live trades are.

    order_type accepted for consistency with place_live_order — doesn't
    change simulated behavior itself.
    """
    if order_type not in ("GTC", "FOK"):
        order_type = "GTC"
    market = discover_market(symbol, timeframe=timeframe)
    price  = min(max_contract_price, 0.999)
    size   = round(position_size_usd / price, 4)
    logger.info("[POLY:%s/%s] SHADOW %s dir=%s %.4f @ $%.4f slug=%s",
                symbol, timeframe, order_type, signal_direction, size, price, market.get("slug") if market else None)
    return {
        "success":            True,
        "order_id":           f"poly_shadow_{int(time.time())}",
        "contracts":          size,
        "price_per_contract": price,
        "total_spent":        position_size_usd,
        "condition_id":       market.get("condition_id") if market else None,
        "slug":               market.get("slug") if market else None,
        "signal_direction":   signal_direction,
        "trade_direction":    signal_direction,
        "shadow":             True,
        "platform":           "polymarket",
        "order_type":         order_type,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ORDER STATUS / FILL CHECK  (GET /data/order/{orderID} — V2)
# ══════════════════════════════════════════════════════════════════════════════

_POLY_FILLED_STATUSES = {"ORDER_STATUS_MATCHED"}
_POLY_DEAD_STATUSES   = {"ORDER_STATUS_CANCELED", "ORDER_STATUS_CANCELED_MARKET_RESOLVED", "ORDER_STATUS_INVALID"}
_POLY_LIVE_STATUSES   = {"ORDER_STATUS_LIVE"}


def get_order_status(order_id: str) -> dict:
    """
    GET /data/order/{orderID} — the exact, ID-based order lookup (not a
    slug/trade-history guess). Returns original_size / size_matched (both
    6-decimal fixed-point strings) so a partial fill can be told apart from
    a complete one.
    """
    if not order_id or str(order_id).startswith("poly_shadow_"):
        return {"found": False, "status": "SHADOW", "error": "shadow order"}
    try:
        headers = _build_headers("GET", f"/data/order/{order_id}")
        resp    = requests.get(f"{CLOB_BASE}/data/order/{order_id}", headers=headers, timeout=10)
        logger.info("[POLY:ORDER-STATUS] GET /data/order/%s → %d", order_id, resp.status_code)
        if resp.status_code == 404:
            return {"found": False, "status": "NOT_FOUND", "error": "order not found"}
        if not resp.ok:
            return {"found": False, "status": "ERROR", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        data = resp.json()

        def _scaled(key):
            try:
                return float(data[key]) / 1_000_000 if key in data and data[key] is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "found":          True,
            "status":         data.get("status"),
            "outcome":        data.get("outcome"),
            "original_usd":   _scaled("original_size"),
            "matched_usd":    _scaled("size_matched"),
            "price":          data.get("price"),
            "trades":         data.get("associate_trades") or [],
            "raw":            data,
            "error":          None,
        }
    except Exception as e:
        logger.warning("[POLY:ORDER-STATUS] error for order=%s: %s", order_id, e)
        return {"found": False, "status": "ERROR", "error": str(e)}


def check_order_filled(order_id: str, intended_usd: float | None = None,
                        token_id: str | None = None) -> dict:
    """
    Verify whether an order actually executed, and how much of it filled —
    mirrors limitless_executor.check_order_filled()'s shape so both
    platforms can be classified (FILLED / PARTIAL / UNFILLED) the same way
    for martingale and dashboard purposes.

    Returns dict: filled(bool), status(str), fill_ratio(float|None),
    filled_usd(float|None), trade(dict|None), error(str|None).
    """
    if not order_id or str(order_id).startswith("poly_shadow_"):
        return {"filled": False, "status": "SHADOW", "fill_ratio": None,
                "filled_usd": None, "trade": None, "error": "shadow order"}

    last_status = None
    for attempt in range(1, 4):
        result = get_order_status(order_id)
        if result.get("found"):
            status = result.get("status")
            last_status = status
            matched_usd = result.get("matched_usd")

            if status in _POLY_FILLED_STATUSES:
                fill_ratio = (round(matched_usd / intended_usd, 4)
                              if (matched_usd is not None and intended_usd) else None)
                return {"filled": True, "status": status, "fill_ratio": fill_ratio,
                        "filled_usd": matched_usd, "trade": result.get("raw"), "error": None}

            if status in _POLY_DEAD_STATUSES:
                # A cancel can still carry a partial fill from before cancellation
                filled_any = bool(matched_usd and matched_usd > 0)
                fill_ratio = (round(matched_usd / intended_usd, 4)
                              if (matched_usd is not None and intended_usd) else (0.0 if intended_usd else None))
                return {"filled": filled_any, "status": status, "fill_ratio": fill_ratio,
                        "filled_usd": matched_usd or 0.0, "trade": result.get("raw"), "error": None}

            if status in _POLY_LIVE_STATUSES and attempt < 3:
                time.sleep(1.5)
                continue
            # Still LIVE after retries — report whatever has matched so far,
            # not filled/complete (the market's 15-min window may still be
            # open, or this is being checked well after the fact).
            fill_ratio = (round((matched_usd or 0.0) / intended_usd, 4) if intended_usd else None)
            return {"filled": False, "status": status or "LIVE", "fill_ratio": fill_ratio,
                    "filled_usd": matched_usd or 0.0, "trade": result.get("raw"), "error": None}
        else:
            break

    return {"filled": False, "status": last_status or "NOT_FOUND", "fill_ratio": None,
            "filled_usd": None, "trade": None, "error": "order not found via /data/order"}


# ══════════════════════════════════════════════════════════════════════════════
# MARKET RESOLUTION  (best-effort — see caveat below)
# ══════════════════════════════════════════════════════════════════════════════

def get_market_resolution(condition_id: str = None, slug: str = None) -> dict:
    """
    Best-effort resolution check via the Gamma API's outcomePrices — once a
    market resolves, the winning outcome prices to "1" and the losing one to
    "0". Polymarket's general-purpose resolution path (UMA Optimistic Oracle)
    can take ~2 hours even when undisputed, which is far slower than this
    bot's 15-minute cycle; the fast-cycling crypto up/down markets are
    understood to resolve automatically and quickly, but that specific
    mechanism isn't independently confirmed here the way Limitless's Pyth
    resolution was. If this comes back unresolved shortly after the window
    closes, the caller should fall back to the OKX-derived outcome (same
    pattern as the Limitless integration's OKX_FALLBACK) rather than block on it.

    Returns dict: resolved(bool), winning_side("UP"|"DOWN"|None), raw(dict), error(str|None).
    """
    empty = {"resolved": False, "winning_side": None, "raw": {}}
    try:
        if slug:
            market = _fetch_gamma_market_by_slug(slug)
        elif condition_id:
            resp = requests.get(f"{GAMMA_BASE}/markets", params={"condition_ids": condition_id}, timeout=8)
            data = resp.json() if resp.ok else []
            markets = data if isinstance(data, list) else data.get("data", []) or []
            market = markets[0] if markets else None
        else:
            return {**empty, "error": "need slug or condition_id"}
    except Exception as e:
        return {**empty, "error": str(e)}

    if not market:
        return {**empty, "error": "market not found"}

    closed = market.get("closed", False)

    def _as_list(v):
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []
        return []

    outcomes = _as_list(market.get("outcomes"))
    prices   = _as_list(market.get("outcomePrices"))

    winning_side = None
    if closed and outcomes and prices and len(outcomes) == len(prices):
        for outcome, price in zip(outcomes, prices):
            try:
                if float(price) >= 0.99:
                    o = str(outcome).lower()
                    winning_side = "UP" if o in ("up", "yes") else ("DOWN" if o in ("down", "no") else None)
                    break
            except (TypeError, ValueError):
                continue

    return {
        "resolved":      winning_side is not None,
        "winning_side":  winning_side,
        "raw":           market,
        "error":         None,
    }


def poll_market_resolution(condition_id: str = None, slug: str = None,
                            attempts: int = 12, delay_s: float = 1.5) -> dict:
    """
    Bounded retry wrapper — see get_market_resolution()'s caveat on timing
    (~18s total by default). This used to be a much tighter budget (4
    attempts, 1.5s apart — ~6s total) purely because job_resolve_outcomes
    needed to stay fast to avoid delaying job_generate_signal, which used to
    fire in the same tick. Generate and resolve are fully decoupled now
    (independent 1-min / 5-min schedules, each pending signal resolved on
    its own thread), so there's no longer a good reason to give up on
    Polymarket's own (Chainlink-backed) resolution quickly — the tight
    budget was the main reason resolution fell to OKX far more often than
    it needed to. job_reconcile_resolutions remains as a safety net for
    genuinely slow outliers.
    """
    result = {}
    for attempt in range(1, attempts + 1):
        result = get_market_resolution(condition_id, slug)
        if result.get("resolved"):
            return result
        if attempt < attempts:
            time.sleep(delay_s)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def execute_order(
    symbol: str,
    signal_direction: str,
    mode: str,
    position_size_usd: float,
    max_contract_price: float = 0.50,
    timeframe: str = "15m",
    order_type: str = "GTC",
) -> dict:
    if mode == "live":
        return place_live_order(symbol, signal_direction, position_size_usd, max_contract_price,
                                 timeframe=timeframe, order_type=order_type)
    return place_shadow_order(symbol, signal_direction, position_size_usd, max_contract_price,
                               timeframe=timeframe, order_type=order_type)
