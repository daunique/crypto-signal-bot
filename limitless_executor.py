"""
Limitless Exchange — Order Executor (EOA signer)
══════════════════════════════════════════════════════════════════════════════
WALLET MODEL:
  EOA mode (recommended for MetaMask/private-key setups):
    • maker = signer = your EOA address (derived from LIMITLESS_PRIVATE_KEY)
    • Do NOT set LIMITLESS_SMART_WALLET — leave it unset or blank.
    • Fund your EOA address directly with USDC on Base.

  Smart-wallet mode (social login accounts only):
    • MAKER  = the smart wallet address (LIMITLESS_SMART_WALLET env var)
    • SIGNER = the embedded EOA that controls the smart wallet
    • The embedded EOA private key must match the smart wallet — NOT a
      MetaMask key. If you get "Signer does not match", you are in this
      mode accidentally. Unset LIMITLESS_SMART_WALLET to fix it.

AUTH (HMAC — new accounts):
  LIMITLESS_TOKEN_ID      → tokenId from POST /auth/api-tokens/derive
  LIMITLESS_TOKEN_SECRET  → base64 secret (shown once — store it now)

  Legacy fallback (old lmts_... keys):
  LIMITLESS_API_KEY       → X-API-Key header (deprecated, no longer issued)

USDC APPROVAL (one-time per venue.exchange contract):
  After deploying, hit GET /api/approval-status in your dashboard.
  If approved=false, visit limitless.exchange with your SMART wallet and
  place one manual trade — the UI triggers the approval automatically.
  The approval must come from your SMART WALLET (not the EOA signer).

ORDER TYPE: GTC limit buy
  makerAmount = price * size * 1e6   (USDC to spend, 6 decimals)
  takerAmount = size * 1e6           (shares to receive, 6 decimals)
  price capped at max_contract_price from settings (default 0.50)
"""

import os
import time
import hmac
import base64
import hashlib
import logging
import json
import requests
from datetime import datetime, timezone
from web3 import Web3
from eth_account import Account

logger = logging.getLogger(__name__)

API_BASE  = "https://api.limitless.exchange"
CHAIN_ID  = 8453   # Base mainnet
ZERO_ADDR = "0x0000000000000000000000000000000000000000"
USDC_ADDR = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # USDC on Base

_slug_cache:    dict = {}
_market_cache:  dict = {}
_page_id_cache: str | None = None

# Known page/group slugs per ticker — used as matching hints in active/slugs discovery,
# and as last-resort fallback if all dynamic strategies fail.
# Keys are tickers without -USDT suffix; values are the base page slugs on limitless.exchange.
# Group slug prefix format confirmed from live test trade:
# btc-up-or-down-15-min-<timestamp>
# These are used as match hints when ticker field is missing from a child entry.
_KNOWN_SLUGS: dict[str, str] = {
    "BTC":  "btc-up-or-down-15-min",
    "ETH":  "eth-up-or-down-15-min",
    "SOL":  "sol-up-or-down-15-min",
    "XRP":  "xrp-up-or-down-15-min",
    "BNB":  "bnb-up-or-down-15-min",
    "DOGE": "doge-up-or-down-15-min",
}


# ══════════════════════════════════════════════════════════════════════════════
# WALLET HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_signer_address() -> str | None:
    """EOA address derived from LIMITLESS_PRIVATE_KEY — signs EIP-712 messages."""
    pk = os.environ.get("LIMITLESS_PRIVATE_KEY", "").strip()
    if not pk:
        return None
    try:
        return Web3.to_checksum_address(Account.from_key(pk).address)
    except Exception:
        return None


def get_maker_address() -> str | None:
    """
    Smart wallet address — the 'maker' in orders, holds USDC on Limitless.
    Priority:
      1. LIMITLESS_SMART_WALLET env var (explicit — preferred)
      2. EOA address fallback (single-wallet setup)
    """
    smart = os.environ.get("LIMITLESS_SMART_WALLET", "").strip()
    if smart:
        try:
            return Web3.to_checksum_address(smart)
        except Exception:
            logger.warning("LIMITLESS_SMART_WALLET invalid address: %s", smart)
    return get_signer_address()


# ══════════════════════════════════════════════════════════════════════════════
# HMAC AUTH
# ══════════════════════════════════════════════════════════════════════════════

def _iso_timestamp() -> str:
    """
    UTC timestamp in JS-compatible ISO 8601 with 'Z' suffix.
    The Limitless API requires timestamps within 30s of server time.
    Uses 'Z' not '+00:00' to match JS new Date().toISOString() format.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _build_hmac_headers(method: str, path: str, body: str = "") -> dict:
    """
    Build signed request headers per the Limitless HMAC spec:
      canonical message = {timestamp}\\n{METHOD}\\n{path+query}\\n{body}
      signature         = base64(HMAC-SHA256(base64decode(secret), message))

    Tries HMAC first (new system), falls back to X-API-Key (legacy).
    Raises ValueError if neither is configured.
    """
    token_id = os.environ.get("LIMITLESS_TOKEN_ID", "").strip()
    secret   = os.environ.get("LIMITLESS_TOKEN_SECRET", "").strip()

    if token_id and secret:
        ts  = _iso_timestamp()
        msg = f"{ts}\n{method}\n{path}\n{body}"
        sig = base64.b64encode(
            hmac.new(base64.b64decode(secret), msg.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        return {
            "lmts-api-key":   token_id,
            "lmts-timestamp": ts,
            "lmts-signature": sig,
            "Content-Type":   "application/json",
        }

    legacy = os.environ.get("LIMITLESS_API_KEY", "").strip()
    if legacy:
        logger.warning(
            "Using deprecated X-API-Key auth. "
            "Derive a new token at limitless.exchange → DevTools → Console "
            "and set LIMITLESS_TOKEN_ID + LIMITLESS_TOKEN_SECRET."
        )
        return {"X-API-Key": legacy, "Content-Type": "application/json"}

    raise ValueError(
        "No Limitless auth credentials found.\n"
        "Set LIMITLESS_TOKEN_ID + LIMITLESS_TOKEN_SECRET in your environment."
    )


def validate_credentials() -> dict:
    """
    Check all required env vars. Returns diagnostic dict used by /api/test-order.
    """
    has_hmac    = bool(os.environ.get("LIMITLESS_TOKEN_ID") and
                       os.environ.get("LIMITLESS_TOKEN_SECRET"))
    has_legacy  = bool(os.environ.get("LIMITLESS_API_KEY"))
    has_pk      = bool(os.environ.get("LIMITLESS_PRIVATE_KEY"))
    has_smart_w = bool(os.environ.get("LIMITLESS_SMART_WALLET"))

    signer_addr = get_signer_address()
    maker_addr  = get_maker_address()

    eoa_mismatch = (
        has_smart_w and has_pk and
        signer_addr and maker_addr and
        signer_addr.lower() != maker_addr.lower()
    )
    if eoa_mismatch:
        logger.warning(
            "WALLET MISMATCH: LIMITLESS_SMART_WALLET (%s) != EOA (%s). "
            "If your account is EOA-based (MetaMask), remove LIMITLESS_SMART_WALLET "
            "from your environment so maker=signer=EOA. "
            "Orders will fail with 'Signer does not match'.",
            maker_addr, signer_addr
        )

    return {
        "LIMITLESS_TOKEN_ID":     bool(os.environ.get("LIMITLESS_TOKEN_ID")),
        "LIMITLESS_TOKEN_SECRET": bool(os.environ.get("LIMITLESS_TOKEN_SECRET")),
        "LIMITLESS_PRIVATE_KEY":  has_pk,
        "LIMITLESS_SMART_WALLET": has_smart_w,
        "hmac_auth_ready":        has_hmac,
        "legacy_auth_ready":      has_legacy,
        "signing_ready":          has_pk,
        "live_trading_ready":     (has_hmac or has_legacy) and has_pk and not eoa_mismatch,
        "signer_address":         signer_addr,
        "maker_address":          maker_addr,
        "two_wallet_mode":        has_smart_w,
        "wallet_mismatch":        eoa_mismatch,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DISCOVERY  (public endpoints — no auth)
# ══════════════════════════════════════════════════════════════════════════════

def _get_crypto_15min_page_id() -> str | None:
    """Resolve the Market Page ID for /crypto/15-min via the navigation API."""
    try:
        resp = requests.get(
            f"{API_BASE}/market-pages/by-path",
            params={"path": "/crypto/15-min"},
            timeout=10,
        )
        logger.info("GET /market-pages/by-path?path=/crypto/15-min → %d", resp.status_code)
        if resp.ok:
            page    = resp.json()
            page_id = page.get("id")
            if page_id:
                logger.info("crypto/15-min page id = %s", page_id)
                return str(page_id)
    except Exception as e:
        logger.warning("market-pages/by-path error: %s", e)
    return None


def _market_timeframe_rank(slug: str, name: str = "", frequency: str = "",
                           sub_frequency: str = "") -> int:
    """
    Return a preference rank for a market timeframe (lower = more preferred).
    Returns -1 if the market should be rejected entirely.

    Preference order:
      0  = 15-min  (only timeframe the bot should trade)
     -1  = reject  (everything else)

    Returns -1 for all non-15-min markets so the bot strictly trades
    15-minute markets. The rank tuple sort still works — rank 0 wins.
    """
    combined = (slug + " " + name + " " + frequency + " " + sub_frequency).lower()

    # IMPORTANT: check 15-min BEFORE 5-min — "15-min" contains "5-min" as a substring
    if "15-min" in combined or "15min" in combined or "-15-" in combined:
        return 0
    # Now safe to reject 1-min and 5-min
    if "1-min" in combined or "1min" in combined:
        return -1
    if "5-min" in combined or "5min" in combined:
        return -1
    # All other timeframes rejected — bot only trades 15-min markets
    return -1


def _is_15min_market(slug: str, name: str = "", frequency: str = "",
                     sub_frequency: str = "") -> bool:
    """Legacy helper — kept for call-sites not yet updated."""
    return _market_timeframe_rank(slug, name, frequency, sub_frequency) == 0


def _discover_slug_via_active_slugs(ticker: str) -> str | None:
    """
    Primary strategy: GET /markets/active/slugs

    Response structure:
      - Group rows: ticker=null/empty, slug=<group-slug>, markets=[{ticker, slug, deadline}, ...]
      - Leaf rows:  ticker=<TICKER>, slug=<instance-slug>, markets=[]

    Strategy: match on TICKER field first (authoritative), never rely on slug
    string patterns which differ between tokens. Fall back to slug hint only
    when ticker field is missing from a child.
    Works identically for all 6 tokens: BTC, ETH, SOL, XRP, BNB, DOGE.
    """
    # Normalise: "SOL-USDT" → "SOL", "SOL" → "SOL"
    base_ticker = ticker.upper().replace("-USDT", "")
    known_page_slug = _KNOWN_SLUGS.get(base_ticker, "")

    try:
        resp = requests.get(f"{API_BASE}/markets/active/slugs", timeout=10)
        logger.info("GET /markets/active/slugs → %d", resp.status_code)
        if not resp.ok:
            return None

        entries = resp.json()
        if not isinstance(entries, list):
            return None

        # Debug: log first entry's structure so we can see the real API shape
        if entries and isinstance(entries[0], dict):
            sample = entries[0]
            logger.debug("[%s] active/slugs sample entry keys=%s ticker=%r slug=%r markets_count=%d",
                         base_ticker, list(sample.keys()),
                         sample.get("ticker"), sample.get("slug"),
                         len(sample.get("markets") or []))

        matches = []  # list of (rank, deadline, slug, market_dict_or_None)

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            entry_ticker   = (entry.get("ticker") or "").upper().replace("-USDT", "")
            entry_slug     = (entry.get("slug")   or "").lower()
            entry_deadline = entry.get("deadline") or ""
            children       = entry.get("markets") or []

            # ── Group row: ticker is null/empty ──────────────────────────────────
            if not entry_ticker:
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    child_ticker   = (child.get("ticker") or "").upper().replace("-USDT", "")
                    child_slug     = (child.get("slug") or "").lower()
                    child_deadline = child.get("deadline") or entry_deadline

                    # Match on ticker field (primary) or slug hint (fallback)
                    explicit_ticker_match = (child_ticker == base_ticker)
                    slug_hint_match = (
                        not child_ticker and (
                            base_ticker.lower() in child_slug
                            or (known_page_slug and child_slug.startswith(known_page_slug))
                        )
                    )
                    if not (explicit_ticker_match or slug_hint_match):
                        continue

                    if not child_slug:
                        continue

                    # When the child ticker explicitly matches, trust it — child dicts
                    # from markets[] rarely carry frequency/subFrequency, so running
                    # _is_15min_market causes false negatives that drop every valid match.
                    # Only apply the frequency filter for slug-hint matches (no ticker field).
                    freq     = (child.get("frequency") or "").lower()
                    sub_freq = (child.get("subFrequency") or "").lower()
                    name     = (child.get("title") or child.get("name") or "")
                    rank = _market_timeframe_rank(child_slug, name, freq, sub_freq)
                    if rank == -1:
                        continue  # reject 1-min / 5-min
                    logger.debug("[%s] group child match: ticker=%s slug=%s rank=%d",
                                 base_ticker, child_ticker, child_slug, rank)
                    matches.append((rank, child_deadline, child_slug, child))

            # ── Ticker row: has ticker set (may still have children) ─────────────
            else:
                if entry_ticker != base_ticker:
                    continue

                # If this entry has children, the real instance slugs are inside
                # markets[] — the parent slug is just a group slug (e.g. sol-15min-price)
                # which does not exist as a standalone market endpoint.
                # Since the parent ticker already matched, we trust all children belong
                # to this ticker's 15-min group and pick them directly without re-filtering
                # on _is_15min_market (child dicts rarely carry frequency/subFrequency,
                # causing false negatives that drop every valid match).
                if children:
                    for child in children:
                        if not isinstance(child, dict):
                            continue
                        child_slug     = (child.get("slug") or "").lower()
                        child_deadline = child.get("deadline") or entry_deadline
                        if not child_slug:
                            continue
                        rank = _market_timeframe_rank(child_slug)
                        if rank == -1:
                            continue
                        logger.debug("[%s] ticker-group child match: slug=%s rank=%d",
                                     base_ticker, child_slug, rank)
                        matches.append((rank, child_deadline, child_slug, child))
                else:
                    # True leaf — the entry slug IS the instance slug
                    freq     = (entry.get("frequency") or "").lower()
                    sub_freq = (entry.get("subFrequency") or "").lower()
                    name     = (entry.get("title") or entry.get("name") or "")
                    rank = _market_timeframe_rank(entry_slug, name, freq, sub_freq)
                    if rank != -1:
                        logger.debug("[%s] leaf match: ticker=%s slug=%s rank=%d",
                                     base_ticker, entry_ticker, entry_slug, rank)
                        matches.append((rank, entry_deadline, entry_slug, entry))

        if matches:
            # Filter out markets whose deadline has already passed.
            # Add a 5-second grace so a market expiring at :00:00 is still
            # usable if we fire at :00:02 and it opened a new window.
            now_utc = datetime.utcnow()
            def _deadline_ok(d):
                if not d:
                    return True
                try:
                    dl = datetime.strptime(d[:19], "%Y-%m-%dT%H:%M:%S")
                    # 5s grace: at the :00/:15/:30/:45 boundary the expiring market
                    # has deadline==now; accept it briefly while the new one publishes.
                    from datetime import timedelta
                    return dl >= now_utc - timedelta(seconds=5)
                except Exception:
                    return True  # unparseable — keep and let API decide
            matches = [(r, d, s, m) for r, d, s, m in matches if _deadline_ok(d)]
            if not matches:
                logger.warning("[%s] active/slugs: all matching markets have passed deadline", ticker)
                return None
            # Sort by timeframe rank first (15-min preferred), then by soonest deadline
            matches.sort(key=lambda x: (x[0], x[1] or ""))
            rank, deadline, slug, market_data = matches[0]
            if rank > 0:
                tf_names = {0: "15-min", 1: "30-min", 2: "hourly", 3: "daily", 4: "weekly", 5: "unknown"}
                logger.warning("[%s] no 15-min market available — using %s market: %s",
                               ticker, tf_names.get(rank, str(rank)), slug)

            # NOTE: do NOT pre-cache shallow market_data from active/slugs here.
            # It only contains {slug, strikePrice, ticker, deadline} — missing
            # exchange, yes_token, no_token. Caching it causes place_live_order
            # to reuse the shallow entry and fail with 'venue.exchange missing'.
            # fetch_market() will populate _market_cache with the full response.

            logger.info("[%s] slug via active/slugs: %s (deadline=%s)", ticker, slug, deadline)
            return slug

        # Log all ticker/slug pairs to diagnose why matching failed
        found_tickers = {}
        btc_entries = []
        for e in entries:
            t = (e.get("ticker") or "").upper()
            s = e.get("slug", "")
            if t:
                found_tickers[t] = s
                if t == base_ticker:
                    btc_entries.append(e)
            for c in (e.get("markets") or []):
                ct = (c.get("ticker") or "").upper()
                if ct:
                    found_tickers[ct] = c.get("slug", "")
                    if ct == base_ticker:
                        btc_entries.append({"_parent_slug": s, **c})
        logger.warning("[%s] active/slugs: no match found. All tickers: %s",
                       base_ticker, list(found_tickers.keys()))
        if btc_entries:
            import json as _json
            logger.warning("[%s] raw matching entries: %s",
                           base_ticker, _json.dumps(btc_entries[:2], default=str))

    except Exception as e:
        logger.warning("[%s] active/slugs error: %s", ticker, e)
    return None


def _discover_slug_via_page_markets(ticker: str, page_id: str) -> str | None:
    """
    Secondary strategy: Market Pages API /crypto/15-min with ticker filter.
    Since we're already on the /crypto/15-min page, all results should be
    15-min — but we double-check frequency fields to be safe.
    """
    try:
        resp = requests.get(
            f"{API_BASE}/market-pages/{page_id}/markets",
            params={
                "limit": 50,
                "sort":  "deadline",
                "filters[ticker]": ticker.lower(),
            },
            timeout=10,
        )
        logger.info("GET /market-pages/%s/markets?ticker=%s → %d",
                    page_id, ticker.lower(), resp.status_code)
        if resp.ok:
            data    = resp.json()
            markets = data.get("data", [])
            for m in markets:
                if not isinstance(m, dict):
                    continue
                slug      = m.get("slug", "")
                t         = (m.get("ticker") or "").upper()
                frequency = (m.get("frequency") or "").lower()
                sub_freq  = (m.get("subFrequency") or "").lower()

                if t != ticker and ticker.lower() not in slug.lower():
                    continue

                # Must be 15-min (page should guarantee this, but verify)
                if not _is_15min_market(slug, "", frequency, sub_freq):
                    logger.debug("[%s] page market not 15-min: %s freq=%s",
                                 ticker, slug, frequency)
                    continue

                logger.info("[%s] slug via page markets: %s", ticker, slug)
                return slug
    except Exception as e:
        logger.warning("[%s] page markets error: %s", ticker, e)
    return None


def _discover_slug_via_search(ticker: str) -> str | None:
    """Fallback: /markets/search?q=TICKER for active 15-min markets."""
    try:
        resp = requests.get(
            f"{API_BASE}/markets/search",
            params={"q": ticker, "limit": 50},
            timeout=10,
        )
        logger.info("GET /markets/search?q=%s → %d", ticker, resp.status_code)
        if resp.ok:
            body    = resp.json()
            markets = body.get("data", body) if isinstance(body, dict) else body
            for m in (markets if isinstance(markets, list) else []):
                slug      = m.get("slug", "")
                frequency = (m.get("frequency") or "").lower()
                sub_freq  = (m.get("subFrequency") or "").lower()
                combined  = (slug + " " + (m.get("title") or "") +
                             " " + (m.get("ticker") or "")).lower()

                if ticker.lower() not in combined:
                    continue

                if not _is_15min_market(slug, m.get("title", ""), frequency, sub_freq):
                    continue

                logger.info("[%s] slug via /markets search: %s", ticker, slug)
                return slug
    except Exception as e:
        logger.error("[%s] /markets search error: %s", ticker, e)
    return None


_profile_cache: dict = {}
_owner_id_cache: int | None = None
_fee_rate_bps_cache: int | None = None   # read from profile, required by Limitless

def get_owner_id(wallet_address: str) -> int | None:
    """
    Fetch the numeric profile ID for the authenticated user.

    Strategy (in order):
      1. GET /profiles/me  (HMAC-authenticated — returns the token owner's profile)
         This is correct regardless of wallet type (EOA or smart wallet).
      2. Fallback: GET /profiles/public/{address} (may return wrong profile if
         the address is linked to a different account)

    ownerId in POST /orders must be this numeric int, not the wallet address.
    """
    global _owner_id_cache
    if _owner_id_cache is not None:
        return _owner_id_cache

    # GET /profiles/{address} with HMAC auth — only works for your own address.
    # The returned numeric id is the ownerId required in POST /orders.
    try:
        path = f"/profiles/{wallet_address}"
        headers = _build_hmac_headers("GET", path)
        resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
        logger.info("GET /profiles/%s → %d", wallet_address, resp.status_code)
        if resp.ok:
            data = resp.json()
            profile_id   = data.get("id")
            fee_rate_bps = (data.get("rank") or {}).get("feeRateBps")  # nested: rank.feeRateBps
            if profile_id is not None:
                _owner_id_cache = int(profile_id)
                if fee_rate_bps is not None:
                    global _fee_rate_bps_cache
                    _fee_rate_bps_cache = int(fee_rate_bps)
                    logger.info("Owner profile id = %d, feeRateBps = %d", _owner_id_cache, _fee_rate_bps_cache)
                else:
                    logger.info("Owner profile id = %d (feeRateBps not in profile response)", _owner_id_cache)
                return _owner_id_cache
            logger.error("GET /profiles/%s: no 'id' field in response: %s", wallet_address, data)
        else:
            logger.error("GET /profiles/%s failed: %d %s", wallet_address, resp.status_code, resp.text[:300])
    except Exception as e:
        logger.error("GET /profiles/%s error: %s", wallet_address, e)

    # Should not reach here if HMAC token matches wallet_address
    try:
        addr = wallet_address.lower()
        if addr in _profile_cache:
            return _profile_cache[addr]
        path = f"/profiles/public/{wallet_address}"
        try:
            headers = _build_hmac_headers("GET", path)
        except ValueError:
            headers = {}
        resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
        logger.info("GET /profiles/public/%s → %d", wallet_address, resp.status_code)
        if resp.ok:
            data = resp.json()
            profile_id = data.get("id")
            if profile_id is not None:
                owner_id = int(profile_id)
                _profile_cache[addr] = owner_id
                logger.info("Owner profile id (public fallback) = %d", owner_id)
                return owner_id
            logger.error("GET /profiles/public/%s: no 'id' field: %s", wallet_address, data)
        else:
            logger.error("GET /profiles/public/%s failed: %d %s",
                         wallet_address, resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("get_owner_id fallback error: %s", e)
    return None


def discover_slug(symbol: str) -> str | None:
    """
    Find the current active 15-min market slug for this symbol.

    Markets rotate every 15 minutes. At the :00/:15/:30/:45 boundary the old
    market expires and the new one may not be published for a few seconds.
    We retry up to 5 times with 2s sleep to ride out that gap.
    """
    import time as _time
    global _page_id_cache
    ticker = symbol.replace("-USDT", "").upper()

    max_attempts = 5
    retry_delay  = 30  # seconds between retries at the boundary

    for attempt in range(1, max_attempts + 1):
        slug = _discover_slug_via_active_slugs(ticker)
        if slug:
            _slug_cache[symbol] = slug
            return slug

        if attempt < max_attempts:
            logger.info("[%s] no 15-min market yet (attempt %d/%d) — retrying in %ds",
                        ticker, attempt, max_attempts, retry_delay)
            _time.sleep(retry_delay)

    logger.warning("[%s] no active 15-min market found after %d attempts", ticker, max_attempts)
    return None


# Backward-compat alias
resolve_slug = discover_slug


def fetch_market(slug: str) -> dict | None:
    """
    Fetch market metadata for a slug.

    The Limitless GET /markets/{slug} endpoint only returns:
      {slug, strikePrice, ticker, deadline}
    Exchange address and token IDs come from GET /markets/{slug}/orderbook.

    Strategy:
      1. Return from cache if available (pre-populated by active/slugs discovery)
         BUT only if cache entry already has orderbook data merged in.
      2. GET /markets/{slug}            — confirms market exists, gets strikePrice
      3. GET /markets/{slug}/orderbook  — gets exchange addr + YES/NO token IDs
      4. Merge both into one dict and cache it.
    """
    # Return cache only if it already has orderbook data (exchange/positionIds)
    if slug in _market_cache:
        cached = _market_cache[slug]
        if cached.get("_orderbook_merged") or cached.get("exchange") or cached.get("positionIds"):
            return cached
        # Cache entry from active/slugs has no orderbook data — fall through to fetch

    market = {}

    # Step 1: GET /markets/{slug}
    try:
        resp = requests.get(f"{API_BASE}/markets/{slug}", timeout=10)
        logger.info("GET /markets/%s → %d", slug, resp.status_code)
        if resp.status_code == 404:
            return None
        if resp.ok:
            market = resp.json() or {}
            logger.debug("Market %s base keys: %s", slug, list(market.keys()))
        else:
            logger.warning("GET /markets/%s returned %d", slug, resp.status_code)
    except Exception as e:
        logger.warning("fetch_market base fetch failed (%s): %s", slug, e)

    # Step 2: GET /markets/{slug}/orderbook — required for exchange addr + token IDs
    try:
        resp2 = requests.get(f"{API_BASE}/markets/{slug}/orderbook", timeout=10)
        logger.info("GET /markets/%s/orderbook → %d", slug, resp2.status_code)
        if resp2.ok:
            ob = resp2.json() or {}
            logger.info("Orderbook %s keys: %s", slug, list(ob.keys()))
            # Orderbook only returns: bids, asks, tokenId (YES), adjustedMidpoint,
            # midpoint, maxSpread, minSize, lastTradePrice
            # exchange, condId, positionIds, tokens come from the base market response.
            ob_yes_token = ob.get("tokenId") or ob.get("yesTokenId")
            ob_no_token  = ob.get("noTokenId")  # may be absent

            # Pull exchange from base market (top-level or venue.exchange)
            venue = market.get("venue") or {}
            exchange_addr = (
                (venue.get("exchange") if isinstance(venue, dict) else None)
                or market.get("exchange")
                or market.get("condExchange")
            )

            # Pull positionIds from base market; fall back to building from tokens
            pos_ids = market.get("positionIds") or market.get("position_ids") or []

            # Pull tokens — prefer base market's tokens dict, supplement with orderbook
            base_tokens = market.get("tokens") or {}
            if isinstance(base_tokens, list):
                # Some responses return tokens as a list of {outcome, tokenId} dicts
                yes_token = next((t.get("tokenId") for t in base_tokens
                                  if str(t.get("outcome", "")).lower() in ("yes", "up", "1")), None)
                no_token  = next((t.get("tokenId") for t in base_tokens
                                  if str(t.get("outcome", "")).lower() in ("no", "down", "0")), None)
            elif isinstance(base_tokens, dict):
                yes_token = base_tokens.get("yes") or base_tokens.get("Yes")
                no_token  = base_tokens.get("no")  or base_tokens.get("No")
            else:
                yes_token = no_token = None

            # Fill in from orderbook if still missing
            yes_token = yes_token or ob_yes_token
            no_token  = no_token  or ob_no_token

            # Build positionIds if still empty
            if not pos_ids and yes_token:
                pos_ids = [yes_token] + ([no_token] if no_token else [])

            # Extract conditionId from base market — required for POST /portfolio/redeem
            condition_id = (
                market.get("conditionId")
                or market.get("condition_id")
                or market.get("ctfConditionId")
                or market.get("condId")
            )

            market.update({
                "slug":              slug,
                "exchange":          exchange_addr,
                "venue":             {"exchange": exchange_addr},
                "tokens":            {"yes": yes_token, "no": no_token},
                "positionIds":       pos_ids,
                "conditionId":       condition_id,
                "_raw_orderbook":    ob,
                "_orderbook_merged": True,
            })
            logger.info("fetch_market %s — exchange=%s yes_token=%s no_token=%s pos_ids=%s",
                        slug, exchange_addr, yes_token, no_token, pos_ids)
        else:
            logger.warning("Orderbook fetch failed for %s — exchange/tokenId will be missing", slug)
    except Exception as e:
        logger.error("fetch_market orderbook fetch failed (%s): %s", slug, e)

    if not market:
        return None

    market.setdefault("slug", slug)
    _market_cache[slug] = market
    return market


def _extract_exchange(market: dict | None) -> str | None:
    """Extract venue.exchange address from market data."""
    if not market:
        return None
    venue = market.get("venue") or {}
    if isinstance(venue, dict):
        addr = venue.get("exchange") or venue.get("condExchange")
        if addr:
            return addr
    return market.get("exchange") or market.get("condExchange")


def _extract_token_id(market: dict | None, direction: str) -> str | None:
    """Extract YES (UP) or NO (DOWN) position token ID from market data."""
    if not market:
        return None
    tokens = market.get("tokens") or {}
    if isinstance(tokens, dict):
        tid = tokens.get("yes") if direction == "UP" else tokens.get("no")
        if tid:
            return str(tid)
    pos_ids = market.get("positionIds") or market.get("position_ids") or []
    if pos_ids:
        if direction == "UP":
            return str(pos_ids[0])
        if direction == "DOWN" and len(pos_ids) > 1:
            return str(pos_ids[1])
    logger.error("Cannot find token ID for %s. tokens=%s positionIds=%s",
                 direction, tokens, pos_ids)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# USDC APPROVAL + BALANCE CHECK  (read-only on-chain)
# ══════════════════════════════════════════════════════════════════════════════

_ERC20_ABI = [
    {
        "name": "allowance", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "owner",   "type": "address"},
                    {"name": "spender", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def check_usdc_approval(exchange_address: str) -> dict:
    """
    Check USDC approval + balance for the MAKER (smart) wallet on Base.

    The maker wallet is LIMITLESS_SMART_WALLET (or EOA fallback).
    This is the wallet that must approve USDC to the exchange contract —
    NOT the EOA signer.

    Returns:
      { "approved": bool, "allowance": int, "usdc_balance": float,
        "wallet": str (maker), "signer": str (EOA),
        "exchange": str, "error": str|None }
    """
    maker  = get_maker_address()
    signer = get_signer_address()

    if not maker:
        return {
            "approved": False, "allowance": 0, "usdc_balance": 0.0,
            "wallet": None, "signer": None,
            "exchange": exchange_address,
            "error": "No wallet address (check LIMITLESS_PRIVATE_KEY)",
        }

    try:
        rpc  = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
        w3   = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        usdc = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDR), abi=_ERC20_ABI
        )
        exchange_cs = Web3.to_checksum_address(exchange_address)
        maker_cs    = Web3.to_checksum_address(maker)

        allowance = usdc.functions.allowance(maker_cs, exchange_cs).call()
        balance   = usdc.functions.balanceOf(maker_cs).call()
        approved  = allowance > 0

        if not approved:
            logger.warning(
                "USDC NOT approved. Maker wallet %s has not approved exchange %s. "
                "Visit limitless.exchange with your smart wallet and place one manual trade.",
                maker, exchange_address,
            )

        return {
            "approved":     approved,
            "allowance":    allowance,
            "usdc_balance": balance / 1e6,
            "wallet":       maker_cs,    # smart wallet (maker / holds USDC)
            "signer":       signer,      # EOA (signs orders)
            "exchange":     exchange_address,
            "error":        None,
        }
    except Exception as e:
        logger.warning("USDC approval check failed (non-fatal): %s", e)
        return {
            "approved": None, "allowance": None, "usdc_balance": None,
            "wallet": maker, "signer": signer,
            "exchange": exchange_address, "error": str(e),
        }


# ══════════════════════════════════════════════════════════════════════════════
# EIP-712 ORDER SIGNING
# ══════════════════════════════════════════════════════════════════════════════

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


def _sign_order(order: dict, exchange_addr: str) -> str:
    """
    EIP-712 sign an order struct with the EOA private key.
    signatureType=0 = EOA wallet signature.
    """
    pk = os.environ.get("LIMITLESS_PRIVATE_KEY", "").strip()
    if not pk:
        raise ValueError("LIMITLESS_PRIVATE_KEY not set")

    acct   = Account.from_key(pk)
    domain = {
        "name":              "Limitless CTF Exchange",
        "version":           "1",
        "chainId":           CHAIN_ID,
        "verifyingContract": Web3.to_checksum_address(exchange_addr),
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
        "signatureType": int(order["signatureType"]),
    }

    # eth-account >= 0.9
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
    return acct.sign_message(encode_typed_data(typed)).signature.hex()


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
    Place a GTC limit BUY order on Limitless Exchange.

    Two-wallet model:
      • maker  = smart wallet (LIMITLESS_SMART_WALLET or EOA fallback)
      • signer = EOA derived from LIMITLESS_PRIVATE_KEY

    Call flow:
      1. Validate env vars
      2. Discover active 15-min market slug (fresh every call)
      3. Fetch market data for exchange address + token ID
      4. Check USDC approval (advisory warning if not approved)
      5. Build + EIP-712 sign the order with EOA key
      6. POST /orders with HMAC-signed request headers
    """
    logger.info("[%s] LIVE order: dir=%s $%.2f max_price=$%.2f",
                symbol, signal_direction, position_size_usd, max_contract_price)

    # 1. Env check
    if not os.environ.get("LIMITLESS_PRIVATE_KEY", "").strip():
        return {"success": False,
                "error": "LIMITLESS_PRIVATE_KEY not set — set it in Render env vars"}
    try:
        _build_hmac_headers("GET", "/ping")   # raises if no auth creds
    except ValueError as e:
        return {"success": False, "error": str(e)}

    maker_addr  = get_maker_address()
    signer_addr = get_signer_address()
    if not maker_addr or not signer_addr:
        return {"success": False, "error": "Cannot derive wallet addresses from private key"}

    # Note: signer in the order is always ZERO_ADDR (CTF exchange convention meaning
    # "maker signs for itself"). The actual signing key is LIMITLESS_PRIVATE_KEY,
    # which must correspond to the maker address.

    # 2. Slug — use cached if available, only re-discover if missing.
    # The scheduler retries execute_order up to 6 times at 5s intervals.
    # Clearing the cache on every attempt forces a full 30s slug-discovery
    # cycle each time. Instead: keep the cached slug across retries within
    # the same candle. The cache is cleared by job_generate_signal between
    # candles via the duplicate guard.
    if symbol not in _slug_cache:
        slug = discover_slug(symbol)  # may wait up to 2.5min for market rotation
    else:
        slug = _slug_cache[symbol]
        logger.info("[%s] reusing cached slug=%s", symbol, slug)
    if not slug:
        return {"success": False,
                "error": f"No active 15-min market found for {symbol}"}

    # 3. Market data — reuse cache only if it has full exchange data.
    # Shallow entries from discover_slug lack exchange/token fields and
    # will cause 'venue.exchange missing' on every order attempt.
    _cached_mkt = _market_cache.get(slug)
    _has_full_data = bool(_cached_mkt and _cached_mkt.get("exchange"))
    if _has_full_data:
        market = _cached_mkt
        logger.info("[%s] reusing cached market slug=%s", symbol, slug)
    else:
        # Pop any shallow entry so fetch_market does a clean fetch
        _market_cache.pop(slug, None)
        market = fetch_market(slug)
    if not market:
        return {"success": False, "error": f"Could not fetch market data for slug={slug}"}

    exchange_addr = _extract_exchange(market)
    if not exchange_addr:
        return {"success": False,
                "error": f"venue.exchange missing from market. keys={list(market.keys())}"}

    token_id = _extract_token_id(market, signal_direction)
    if not token_id:
        return {"success": False,
                "error": (f"Token ID missing for dir={signal_direction}. "
                          f"tokens={market.get('tokens')} "
                          f"positionIds={market.get('positionIds')}")}

    # 4. Advisory USDC approval check (non-blocking)
    approval = check_usdc_approval(exchange_addr)
    if approval.get("approved") is False:
        logger.warning(
            "[%s] USDC not approved for maker=%s — order will likely fail on-chain. "
            "See GET /api/approval-status.", symbol, maker_addr
        )

    # 5. Build + sign order
    price        = round(min(max_contract_price, 0.99), 2)
    size         = round(position_size_usd / price, 4)
    maker_amount = int(round(price * size * 1_000_000))
    taker_amount = int(round(size * 1_000_000))
    salt         = int(time.time() * 1000)

    # signer = maker for EOA accounts (the wallet signs for itself).
    order = {
        "salt":          str(salt),
        "maker":         Web3.to_checksum_address(maker_addr),
        "signer":        Web3.to_checksum_address(maker_addr),
        "taker":         ZERO_ADDR,
        "tokenId":       str(token_id),
        "makerAmount":   maker_amount,
        "takerAmount":   taker_amount,
        "expiration":    "0",
        "nonce":         0,
        "feeRateBps":    get_fee_rate_bps(maker_addr),
        "side":          0,    # BUY
        "signatureType": 0,    # EOA
    }

    try:
        signature = _sign_order(order, exchange_addr)
    except Exception as e:
        return {"success": False, "error": f"EIP-712 signing failed: {e}"}

    if not signature.startswith("0x"):
        signature = "0x" + signature

    # ownerId is required. Fetch it from the authenticated token via multiple endpoint attempts.
    owner_id = get_owner_id(maker_addr)
    if owner_id is None:
        return {"success": False,
                "error": f"Could not resolve ownerId for maker={maker_addr}"}

    payload = {
        "order":      {**order, "signature": signature, "signatureType": 0,
                       "price": round(price, 2)},        # price must be float 0.01-0.99, max 2 decimals
        "orderType":  "GTC",
        "marketSlug": slug,
        "ownerId":    owner_id,
    }

    # 6. POST /orders
    path     = "/orders"
    body_str = json.dumps(payload)
    try:
        headers = _build_hmac_headers("POST", path, body_str)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    try:
        resp = requests.post(
            f"{API_BASE}{path}",
            headers=headers,
            data=body_str,      # data= not json= — body must match HMAC signature
            timeout=15,
        )
        logger.info("[%s] POST /orders → %d: %s",
                    symbol, resp.status_code, resp.text[:800])

        if not resp.ok:
            # Return the raw API error body — critical for debugging
            try:
                err_detail = resp.json()
            except Exception:
                err_detail = resp.text
            logger.error("[%s] Order rejected %d: %s", symbol, resp.status_code, err_detail)
            return {
                "success":        False,
                "http_status":    resp.status_code,
                "error":          f"HTTP {resp.status_code}",
                "api_response":   err_detail,
                "payload_sent":   payload,   # show what we sent for debugging
            }

        resp.raise_for_status()

        result   = resp.json()
        order_id = (result.get("order", {}).get("id")
                    or result.get("id")
                    or result.get("orderId")
                    or str(salt))

        logger.info("[%s] ORDER ✓ dir=%s %.4f shares @ $%.4f id=%s slug=%s",
                    symbol, signal_direction, size, price, order_id, slug)
        return {
            "success":            True,
            "order_id":           str(order_id),
            "contracts":          size,
            "price_per_contract": price,
            "total_spent":        position_size_usd,
            "slug":               slug,
            "condition_id":       market.get("conditionId") or market.get("condition_id") or market.get("condId"),
            "signal_direction":   signal_direction,
            "trade_direction":    signal_direction,
            "maker":              maker_addr,
            "signer":             signer_addr,
        }

    except Exception as e:
        logger.error("[%s] Exception placing order: %s", symbol, e, exc_info=True)
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
    price = min(max_contract_price, 0.99)
    size  = round(position_size_usd / price, 4)
    logger.info("[%s] SHADOW dir=%s %.4f shares @ $%.4f",
                symbol, signal_direction, size, price)
    return {
        "success":            True,
        "order_id":           f"shadow_{int(time.time())}",
        "contracts":          size,
        "price_per_contract": price,
        "total_spent":        position_size_usd,
        "signal_direction":   signal_direction,
        "trade_direction":    signal_direction,
        "shadow":             True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED ENTRY POINT  (called by scheduler)
# ══════════════════════════════════════════════════════════════════════════════

def get_fee_rate_bps(maker_addr: str) -> int:
    """
    Return the feeRateBps for this account from the Limitless profile.
    The value is cached after the first successful fetch (via get_owner_id).
    Returns 0 with an error log if it cannot be determined — the API will
    reject the order, making the misconfiguration visible rather than silent.
    """
    global _fee_rate_bps_cache
    if _fee_rate_bps_cache is not None:
        return _fee_rate_bps_cache

    # Profile fetch also populates _fee_rate_bps_cache as a side-effect
    get_owner_id(maker_addr)
    if _fee_rate_bps_cache is not None:
        return _fee_rate_bps_cache

    # Direct retry if owner_id fetch did not surface feeRateBps
    try:
        path = f"/profiles/{maker_addr}"
        headers = _build_hmac_headers("GET", path)
        resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
        if resp.ok:
            data = resp.json()
            rank = data.get("rank") or {}
            fee_bps = rank.get("feeRateBps")
            if fee_bps is not None:
                _fee_rate_bps_cache = int(fee_bps)
                logger.info("feeRateBps resolved = %d (rank.feeRateBps)", _fee_rate_bps_cache)
                return _fee_rate_bps_cache
            logger.warning("feeRateBps not in profile rank — rank keys: %s, top-level keys: %s",
                           list(rank.keys()), list(data.keys()))
    except Exception as e:
        logger.warning("get_fee_rate_bps fetch error: %s", e)

    logger.error("feeRateBps unknown — order will be rejected by Limitless")
    return 0


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


def get_limitless_market_url(symbol: str) -> str:
    slug = _slug_cache.get(symbol, "")
    return (f"https://limitless.exchange/markets/crypto/{slug}"
            if slug else "https://limitless.exchange/crypto/15-min")


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-CLAIM WINNINGS
# ══════════════════════════════════════════════════════════════════════════════

def _get_condition_id_from_positions(market_slug: str, symbol: str) -> str | None:
    """
    Fallback: fetch GET /portfolio/positions and find the conditionId
    for a resolved market matching market_slug.
    Used when conditionId was not stored at order time.
    """
    try:
        path    = "/portfolio/positions"
        headers = _build_hmac_headers("GET", path)
        resp    = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
        if not resp.ok:
            logger.warning("[%s] GET /portfolio/positions → %d", symbol, resp.status_code)
            return None
        data  = resp.json() if resp.text else {}
        clobs = data.get("clob") or []
        for pos in clobs:
            mkt = pos.get("market") or {}
            slug = mkt.get("slug") or ""
            if market_slug and market_slug in slug:
                cid = (mkt.get("condition_id")
                       or mkt.get("conditionId")
                       or mkt.get("condId"))
                if cid:
                    logger.info("[%s] conditionId from portfolio/positions: %s", symbol, cid)
                    return cid
        logger.warning("[%s] slug %s not found in portfolio/positions", symbol, market_slug)
        return None
    except Exception as e:
        logger.error("[%s] _get_condition_id_from_positions error: %s", symbol, e)
        return None


def claim_winnings(market_slug: str, signal_direction: str, symbol: str, cond_id: str | None = None) -> dict:
    """
    Redeem winning conditional-token positions via POST /portfolio/redeem.

    Per Limitless docs: POST /portfolio/redeem with conditionId (bytes32 hex).

    conditionId resolution order:
      1. Pre-stored cond_id passed in (fastest — saved at order time)
      2. Fetch GET /markets/{slug} and read condition_id field
      3. Fetch GET /portfolio/positions and match by slug (fallback)
    """
    try:
        # ── 1. Resolve conditionId ────────────────────────────────────────────
        condition_id = cond_id

        if not condition_id and market_slug:
            # Try market endpoint first
            _market_cache.pop(market_slug, None)
            market = fetch_market(market_slug)
            if market:
                condition_id = (
                    market.get("conditionId")
                    or market.get("condition_id")
                    or market.get("ctfConditionId")
                    or market.get("condId")
                )

        if not condition_id and market_slug:
            # Fallback: portfolio positions endpoint
            logger.info("[%s] conditionId not in market — trying portfolio/positions", symbol)
            condition_id = _get_condition_id_from_positions(market_slug, symbol)

        if not condition_id:
            logger.error(
                "[%s] conditionId could not be resolved for slug=%s. "
                "Cannot redeem — check market response fields.",
                symbol, market_slug
            )
            return {
                "success": False,
                "error":   f"conditionId not found for {market_slug}",
            }

        # ── 2. POST /portfolio/redeem ─────────────────────────────────────────
        path     = "/portfolio/redeem"
        payload  = {"conditionId": str(condition_id)}
        body_str = json.dumps(payload, separators=(",", ":"))
        headers  = _build_hmac_headers("POST", path, body_str)

        logger.info(
            "[%s] POST /portfolio/redeem slug=%s conditionId=%s",
            symbol, market_slug, condition_id
        )

        resp = requests.post(
            f"{API_BASE}{path}",
            headers=headers,
            data=body_str,
            timeout=15,
        )

        logger.info(
            "[%s] POST /portfolio/redeem → %d: %s",
            symbol, resp.status_code, resp.text[:400]
        )

        if resp.ok:
            result   = resp.json() if resp.text else {}
            redeemed = result.get("amount") or result.get("redeemed") or result.get("value")
            tx_hash  = result.get("txHash") or result.get("tx_hash") or result.get("transactionHash")
            logger.info(
                "[%s] CLAIM ✓ slug=%s conditionId=%s redeemed=%s tx=%s",
                symbol, market_slug, condition_id, redeemed, tx_hash,
            )
            return {
                "success":         True,
                "redeemed_amount": redeemed,
                "tx_hash":         tx_hash,
                "condition_id":    condition_id,
                "raw":             result,
            }
        else:
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            logger.error(
                "[%s] CLAIM FAILED %d: %s | conditionId=%s",
                symbol, resp.status_code, err, condition_id
            )
            return {
                "success":      False,
                "http_status":  resp.status_code,
                "error":        f"HTTP {resp.status_code}",
                "api_response": err,
            }

    except Exception as e:
        logger.error("[%s] claim_winnings exception: %s", symbol, e)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# ORDER FILL VERIFICATION  (via /portfolio/trades — on-chain source of truth)
# ══════════════════════════════════════════════════════════════════════════════

def check_order_filled(market_slug: str, order_id: str) -> dict:
    """
    Verify whether an order was actually executed on Limitless by checking
    GET /portfolio/trades — the on-chain source of truth.

    This is stronger than checking /markets/{slug}/user-orders because trades
    only appear once the blockchain confirms the fill. A limit order can show
    as LIVE in user-orders indefinitely if never matched, but it will NEVER
    appear in /portfolio/trades unless it was executed on-chain.

    This is the correct source for martingale decisions: an OKX WIN does not
    mean the Limitless position was filled. Only a confirmed trade does.

    Falls back to GET /markets/{slug}/user-orders if trades endpoint fails.

    Returns:
        dict with keys:
          filled  (bool)   — True if a confirmed on-chain trade was found
          status  (str)    — "FILLED", "NOT_FOUND", "SHADOW", or "ERROR"
          trade   (dict)   — the matching trade record if found, else None
          error   (str)    — error message on failure
    """
    if not market_slug or not order_id:
        return {"filled": False, "status": "ERROR", "error": "missing slug or order_id",
                "trade": None}

    if str(order_id).startswith("shadow_"):
        return {"filled": False, "status": "SHADOW",
                "error": "shadow order — not on-chain", "trade": None}

    # ── Primary: GET /portfolio/trades ───────────────────────────────────────
    # Returns all on-chain executed trades for this wallet. We match on
    # market slug (each trade has market.slug) and optionally transactionHash
    # if we stored it. Since we store order_id from POST /orders response,
    # we match on market slug as the reliable key — slug is unique per 15-min
    # window, so one slug = one trade per candle.
    try:
        path    = "/portfolio/trades"
        headers = _build_hmac_headers("GET", path)
        resp    = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)

        logger.info("[FILL-CHECK] GET /portfolio/trades → %d", resp.status_code)

        if resp.ok:
            trades = resp.json() if resp.text else []
            if not isinstance(trades, list):
                trades = trades.get("trades") or trades.get("data") or []

            for trade in trades:
                mkt      = trade.get("market") or {}
                t_slug   = mkt.get("slug") or ""
                t_hash   = trade.get("transactionHash") or ""
                strategy = (trade.get("strategy") or "").lower()

                # Match: trade's market slug contains our slug (slugs are unique
                # per 15-min window so this is safe), and it's a Buy (not a sell/redeem)
                if market_slug and market_slug in t_slug and strategy == "buy":
                    logger.info(
                        "[FILL-CHECK] ✓ On-chain trade confirmed | slug=%s "
                        "txHash=%s collateral=%s outcomeTokenPrice=%s",
                        t_slug, t_hash,
                        trade.get("collateralAmount"), trade.get("outcomeTokenPrice")
                    )
                    return {
                        "filled": True,
                        "status": "FILLED",
                        "trade":  trade,
                        "error":  None,
                    }

            logger.info(
                "[FILL-CHECK] No on-chain trade found for slug=%s in %d trades",
                market_slug, len(trades)
            )
            # Confirmed: no trade on-chain — order was not executed
            return {"filled": False, "status": "NOT_FOUND", "trade": None, "error": None}

        else:
            logger.warning(
                "[FILL-CHECK] /portfolio/trades failed %d — falling back to user-orders",
                resp.status_code
            )

    except Exception as e:
        logger.warning("[FILL-CHECK] /portfolio/trades error: %s — falling back", e)

    # ── Fallback: GET /markets/{slug}/user-orders ─────────────────────────────
    # Less reliable (shows LIVE unfilled orders too) but better than nothing.
    try:
        path    = f"/markets/{market_slug}/user-orders"
        params  = "?statuses=MATCHED&statuses=LIVE&limit=100"
        headers = _build_hmac_headers("GET", path)
        resp    = requests.get(f"{API_BASE}{path}{params}", headers=headers, timeout=10)

        logger.info("[FILL-CHECK] fallback GET %s%s → %d", path, params, resp.status_code)

        if not resp.ok:
            return {
                "filled": False, "status": "ERROR", "trade": None,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }

        data   = resp.json() if resp.text else {}
        orders = data.get("orders") or []

        for order in orders:
            oid    = str(order.get("id", ""))
            status = str(order.get("status", "")).upper()
            if oid == str(order_id):
                filled = status == "MATCHED"
                logger.info(
                    "[FILL-CHECK] fallback: order_id=%s status=%s filled=%s",
                    order_id, status, filled
                )
                return {"filled": filled, "status": status, "trade": None, "error": None}

        logger.info(
            "[FILL-CHECK] fallback: order_id=%s NOT FOUND in %d user-orders",
            order_id, len(orders)
        )
        return {"filled": False, "status": "NOT_FOUND", "trade": None, "error": None}

    except Exception as e:
        logger.error("[FILL-CHECK] fallback exception for order_id=%s: %s", order_id, e)
        return {"filled": False, "status": "ERROR", "trade": None, "error": str(e)}
