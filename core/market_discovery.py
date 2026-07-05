"""
POLYBOT — Market Discovery
Polls Gamma API every 10 seconds to find new BTC/ETH markets.
Extracts YES/NO token IDs and subscribes WebSocket instantly.

IMPORTANT — keying design:
A new market for the same asset+duration (e.g. "BTC_5MIN") opens
every single cycle with a NEW slug and NEW token IDs. If markets
were keyed by pair_id alone, discovering next cycle's market would
silently overwrite the previous cycle's still-open entry — breaking
lookups for any position still held on the outgoing market and
leaking its WebSocket subscription forever. To avoid this, markets
are keyed by their unique `slug` (one per cycle). `pair_id` is kept
as a secondary index (pair_id -> current slug) so callers that only
care about "the current BTC_5MIN market" can still ask for it simply.
"""
import asyncio
import json
import time
import requests
from datetime import datetime, timezone
from config import Config


class MarketDiscovery:
    def __init__(self):
        self.active_markets  = {}    # slug (unique) → market dict
        self.pair_to_slug     = {}   # pair_id → current slug (latest cycle)
        self.known_tokens    = set() # Already subscribed token IDs
        self.token_to_slug   = {}    # token_id → slug (fast lookup)
        self.listener        = None  # Set after listener is created

    # ── Main discovery loop ──────────────────────────────────
    async def run_discovery_loop(self, listener):
        """
        Runs forever. Every 10 seconds:
        1. Polls Gamma API for active crypto markets
        2. Finds new markets not yet tracked
        3. Extracts YES/NO token IDs
        4. Subscribes them to WebSocket instantly
        5. Unsubscribes and finalizes expired markets
        """
        self.listener = listener
        print("[DISCOVERY] Starting market discovery (every 10s)")

        while True:
            try:
                await self._discover_and_subscribe()
            except Exception as e:
                print(f"[DISCOVERY ERROR] {e}")

            await asyncio.sleep(Config.DISCOVERY_INTERVAL_SECONDS)

    async def _discover_and_subscribe(self):
        raw_markets = await asyncio.to_thread(self._fetch_active_markets)
        new_tokens  = []
        new_market_params = []

        for raw in raw_markets:
            parsed = self._parse_market(raw)
            if not parsed:
                continue

            slug      = parsed["slug"]
            pair_id   = parsed["pair_id"]
            yes_token = parsed["yes_token"]

            # Skip if already tracking this exact market cycle
            if slug in self.active_markets:
                continue

            # NEW market cycle found — does NOT overwrite the
            # outgoing cycle's entry, since the key (slug) is unique.
            self.active_markets[slug] = parsed
            self.pair_to_slug[pair_id] = slug  # This is now "current"
            self.known_tokens.add(yes_token)
            self.known_tokens.add(parsed["no_token"])
            self.token_to_slug[yes_token] = slug
            self.token_to_slug[parsed["no_token"]] = slug
            new_tokens.extend([yes_token, parsed["no_token"]])
            new_market_params.append(parsed)  # For tick_size pre-warming

            print(
                f"[DISCOVERY] NEW market: {pair_id} | "
                f"Slug: {slug} | "
                f"Expires: {parsed['expiry_str']} | "
                f"YES token: {yes_token[:10]}..."
            )

        # Subscribe new tokens to WebSocket immediately
        if new_tokens and self.listener:
            await self.listener.subscribe_tokens(new_tokens)
            print(f"[DISCOVERY] Subscribed {len(new_tokens)} "
                  f"new tokens to WebSocket")

        # Pre-warm the executor's tick_size cache for every new
        # market NOW, while there's no time pressure, instead of
        # paying that network round-trip (~15-50ms) on the first
        # actual trade — which matters most in exactly the
        # FINAL/CAUTIOUS stages near expiry where every millisecond
        # of margin against the cutoff counts.
        if new_market_params and self.listener:
            await self.listener.prewarm_market_params(new_market_params)

        # Clean up expired markets (does not touch the incoming cycle)
        await self._cleanup_expired()

    def _fetch_active_markets(self) -> list:
        """Fetch all active crypto up/down markets from Gamma.
        Retries transient failures with short backoff since this
        runs on a 10s cycle and a single dropped request shouldn't
        stall discovery."""
        url = f"{Config.GAMMA_API}/markets"
        params = {
            "active":   "true",
            "closed":   "false",
            "tag_slug": "crypto",
            "limit":    100,
            "order":    "volume24hr",
        }
        last_error = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=8)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        print(f"[DISCOVERY] Gamma API fetch failed after 3 attempts: "
              f"{last_error}")
        return []

    def _parse_market(self, raw: dict) -> dict | None:
        """Extract all needed fields from a Gamma market object."""
        slug = raw.get("slug", "")
        if not slug:
            return None

        # Match slug to one of our pair IDs
        pair_id = None
        for pid, pattern in Config.SLUG_PATTERNS.items():
            if slug.startswith(pattern):
                if pid in Config.ACTIVE_PAIRS:
                    pair_id = pid
                    break

        if not pair_id:
            return None

        # clobTokenIds comes as a JSON string — must parse it
        raw_ids = raw.get("clobTokenIds", "[]")
        token_ids = (
            json.loads(raw_ids)
            if isinstance(raw_ids, str)
            else raw_ids
        )

        if len(token_ids) < 2:
            return None

        # Parse expiry timestamp
        end_date = raw.get("endDate", "")
        expiry   = self._parse_expiry(end_date)

        # Skip already-expired markets
        if expiry and expiry < time.time():
            return None

        return {
            "pair_id":    pair_id,
            "slug":       slug,
            "market_id":  raw.get("id"),
            "condition_id": raw.get("conditionId"),
            "yes_token":  token_ids[0],   # Index 0 = YES always
            "no_token":   token_ids[1],   # Index 1 = NO always
            "expiry":     expiry,
            "expiry_str": end_date,
            "volume_24h": float(raw.get("volume24hr", 0)),
            "active":     raw.get("active", False),
            "discovered_at": time.time(),
        }

    def _parse_expiry(self, end_date: str) -> float:
        """Convert ISO date string to Unix timestamp."""
        if not end_date:
            return 0.0
        try:
            dt = datetime.fromisoformat(
                end_date.replace("Z", "+00:00")
            )
            return dt.timestamp()
        except Exception:
            return 0.0

    async def _cleanup_expired(self):
        """
        Remove, unsubscribe, and finalize markets whose grace period
        has passed. Finalization calls into the position manager (if
        wired via the listener) so per-cycle hit/profit stats aren't
        silently lost, and so PositionManager doesn't grow forever.
        """
        now     = time.time()
        expired = [
            slug for slug, m in self.active_markets.items()
            if m["expiry"] and m["expiry"] < now - 30
        ]
        for slug in expired:
            market = self.active_markets.pop(slug, None)
            if not market:
                continue
            pair_id = market["pair_id"]
            print(f"[DISCOVERY] Market expired: {pair_id} "
                  f"({slug}) — unsubscribing")
            self.known_tokens.discard(market["yes_token"])
            self.known_tokens.discard(market["no_token"])
            self.token_to_slug.pop(market["yes_token"], None)
            self.token_to_slug.pop(market["no_token"], None)

            # Only clear the pair->slug pointer if it still points
            # at THIS expiring slug (a newer cycle may have already
            # taken over the pointer, which is correct — don't erase
            # a newer market's pointer just because an older one for
            # the same pair_id is being cleaned up).
            if self.pair_to_slug.get(pair_id) == slug:
                self.pair_to_slug.pop(pair_id, None)

            if self.listener:
                await self.listener.unsubscribe_tokens([
                    market["yes_token"], market["no_token"]
                ])
                # Finalize any open position tracking for this cycle
                await self.listener.finalize_market(pair_id, slug)

    def get_market_by_token(self, token_id: str) -> dict | None:
        """Find market data from a token ID — O(1) via the token
        index, works correctly even for an outgoing cycle whose
        pair_id has already been claimed by the next cycle."""
        slug = self.token_to_slug.get(token_id)
        if not slug:
            return None
        return self.active_markets.get(slug)

    def get_current_market_for_pair(self, pair_id: str) -> dict | None:
        """Find the CURRENT (latest-discovered) market for a pair_id.
        Use this when you specifically want 'the active BTC_5MIN
        market' rather than looking up by a known token."""
        slug = self.pair_to_slug.get(pair_id)
        if not slug:
            return None
        return self.active_markets.get(slug)

    def get_side_by_token(self, token_id: str, market: dict) -> str:
        """Returns 'YES' or 'NO' for a given token ID."""
        return "YES" if token_id == market["yes_token"] else "NO"
