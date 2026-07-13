"""
POLYBOT — Market Lifecycle Capture (Termux-friendly, standalone)

Records EVERY price tick for the 6 tracked assets' 5-min and 15-min
markets, spanning the FULL lifecycle: from the moment a market is
first discoverable (which may be BEFORE it starts accepting orders
— see PRE-OPEN DETECTION below) straight through to close, with no
gap at the open transition. Built specifically to give real,
ground-truth data on how the pre-open-to-open transition behaves,
since that's the exact window a hedge-completion strategy would
depend on.

WHY THIS IS A SEPARATE, STANDALONE SCRIPT (not reusing
core/market_discovery.py or core/websocket_listener.py directly):
those modules are built for LIVE TRADING and carry assumptions this
capture explicitly should NOT inherit — e.g. they skip markets
already past certain safety thresholds, they stop tracking a market
the moment it's flagged as expired/closed, and they're tuned for
"is there an edge to trade" rather than "record everything,
unconditionally, for later analysis." This script deliberately
duplicates the proven connection/reconnection/parsing PATTERN from
those modules (same WebSocket message shapes, same Gamma API
discovery approach) but with none of the trading-specific filtering,
so nothing you need for the pre-open transition gets silently
dropped.

PRE-OPEN DETECTION:
A market can appear in Gamma's `active=true,closed=false` results
before it actually accepts orders — Polymarket exposes this via
separate `acceptingOrders` and `enableOrderBook` fields, which can
disagree with `active`/`closed` (confirmed via current Polymarket
API documentation and a real Polymarket/rs-clob-client GitHub issue
showing these flags can be inconsistent with what polymarket.com's
own UI treats as live). This script records ALL of these flags on
every snapshot specifically so you can determine AFTERWARD, from
the data itself, exactly when trading actually became possible —
rather than assuming it lines up with when the market was first
discovered.

OUTCOME / RESOLUTION TRACKING:
A market's outcome does not exist while the market is open — it
can only be determined after the window closes and the price feed
(Chainlink Data Streams, for these specific short-duration markets)
settles it. This means outcome capture is necessarily a SEPARATE,
LATER check per market, not something the live tick stream can
provide. When a market's expiry passes, it moves into a pending-
resolution queue (rather than being dropped from memory immediately
— an earlier version of this script had exactly that bug, which
would have made outcome lookup impossible since the market's own
metadata would already be gone) and gets polled on Gamma's
`/markets/{id}` endpoint every RESOLUTION_CHECK_INTERVAL_SECONDS,
checking the `outcomePrices` field (settles to ["1","0"] or ["0","1"]
for a resolved binary market — whichever side won pays $1, the loser
$0). Confirmed via research that these specific short-duration
crypto markets resolve via Chainlink automatically, WITHOUT the
~2-hour UMA dispute window other Polymarket market categories go
through — so resolution should appear on Gamma soon after expiry,
not hours later. Still polled with retries (up to
RESOLUTION_CHECK_MAX_ATTEMPTS) rather than assumed instant. A market
that never resolves within that retry budget is explicitly recorded
as "resolution_unconfirmed" rather than silently vanishing from the
data with no trace.

HEARTBEAT — PROVING "NO GAP" RATHER THAN ASSUMING IT:
The tick stream is event-driven: no WebSocket event arrives when
nothing has changed, which is CORRECT behavior (a quiet market
genuinely produces no ticks), not a gap. But that also means a
genuinely quiet market and a silently dropped WebSocket connection
would look IDENTICAL when reviewing the output file afterward. A
separate heartbeat record is written on a fixed timer
(independent of tick activity) specifically to remove that
ambiguity: if heartbeats are present throughout a quiet stretch,
that stretch was a real quiet market. If heartbeats are ALSO
missing, the connection was actually down.

TERMUX / MOBILE-SPECIFIC NOTES (read before running a 12-hour
capture):
- Run `termux-wake-lock` BEFORE starting this script, and keep your
  phone plugged in. Without a wake lock, Android will very likely
  throttle or kill the background process once the screen locks,
  silently truncating your capture with no error — you'd only find
  out from a gap in the data afterward.
- Uses ONLY the standard library plus `websockets` and `requests` —
  nothing that's painful to `pip install` on Termux (no pandas, no
  heavy scientific stack; do that analysis afterward on a full
  machine, not on-device).
- Output is JSONL (one JSON object per line, appended continuously,
  flushed after every write) specifically because it's crash-safe —
  a killed process mid-write only loses the single incomplete line,
  never the whole file, unlike a single large JSON array that would
  be corrupted by a truncated write.
- Reconnects with exponential backoff on any WebSocket drop, mobile
  network changes (WiFi<->cellular handoff) being the most likely
  real-world cause during a 12-hour run.

USAGE:
    pip install websockets requests --break-system-packages
    termux-wake-lock
    python3 capture/capture_market_lifecycle.py --hours 12

Output: capture/data/lifecycle_capture_<start-timestamp>.jsonl
"""
import argparse
import asyncio
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import requests
    import websockets
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install websockets requests --break-system-packages")
    sys.exit(1)

GAMMA_API = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

SLUG_PATTERNS = {
    "BTC_5MIN":   "btc-updown-5m-",
    "BTC_15MIN":  "btc-updown-15m-",
    "ETH_5MIN":   "eth-updown-5m-",
    "ETH_15MIN":  "eth-updown-15m-",
    "XRP_5MIN":   "xrp-updown-5m-",
    "XRP_15MIN":  "xrp-updown-15m-",
    "SOL_5MIN":   "sol-updown-5m-",
    "SOL_15MIN":  "sol-updown-15m-",
    "BNB_5MIN":   "bnb-updown-5m-",
    "BNB_15MIN":  "bnb-updown-15m-",
    "DOGE_5MIN":  "doge-updown-5m-",
    "DOGE_15MIN": "doge-updown-15m-",
    # Discovered live in production data on 2026-07-10 — not part of
    # the original 6-asset set, but matches the same market family
    # and is currently active, so tracking it too.
    "HYPE_5MIN":  "hype-updown-5m-",
    "HYPE_15MIN": "hype-updown-15m-",
}

DISCOVERY_INTERVAL_SECONDS = 10

# Resolution checking (Gamma API, per market, after expiry).
# Short-duration crypto markets resolve via Chainlink Data Streams
# automatically, WITHOUT the ~2-hour UMA dispute window that other
# Polymarket markets go through (confirmed via current Polymarket
# documentation) — so outcome data should appear on Gamma soon
# after endDate passes, not hours later. Still polled with retries
# rather than assumed instant, since "soon" isn't "immediately."
RESOLUTION_CHECK_INTERVAL_SECONDS = 15
RESOLUTION_CHECK_MAX_ATTEMPTS = 40  # 40 x 15s = 10 minutes of retrying
                                       # before giving up on a market


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class LifecycleCapture:
    def __init__(self, output_path: str, run_seconds: float):
        self.output_path = output_path
        self.run_until = time.time() + run_seconds
        self.stop_requested = False

        # market_id (Gamma's numeric id, stable across a market's
        # whole life, unlike slug/token which are per-outcome) ->
        # market metadata dict
        self.tracked_markets: dict[str, dict] = {}
        # token_id -> market_id, for fast WebSocket event routing
        self.token_to_market: dict[str, str] = {}
        self.known_token_ids: set[str] = set()

        # Markets that have expired but whose outcome hasn't been
        # confirmed yet. market_id -> attempt count. A market moves
        # here from tracked_markets at expiry (instead of being
        # dropped immediately, which was the original bug — outcome
        # checking needs the market's metadata to still exist to
        # look it up), and is fully dropped only once resolved or
        # after RESOLUTION_CHECK_MAX_ATTEMPTS.
        self.pending_resolution: dict[str, int] = {}
        self.resolution_metadata: dict[str, dict] = {}
        self.resolved_market_ids: set[str] = set()

        self.ws = None
        self.ws_connected = False
        self.pending_subs: list[str] = []
        self._reconnect_attempt = 0

        self._out_fh = open(self.output_path, "a", buffering=1)  # line-buffered

        self.tick_count = 0
        self.market_count = 0
      
      # CRITICAL FIX: write_record() used to call os.fsync() directly,
        # inline, on every single tick — a blocking disk call executed
        # right on the asyncio event loop thread. Under low tick volume
        # (e.g. mobile captures where frequent WS reconnects were
        # inadvertently throttling throughput) this never caused
        # visible problems. But under sustained high volume (confirmed
        # via a real deployment: two [STATUS] lines 5 minutes apart
        # both showing an IDENTICAL tick count, while discovery and
        # resolution kept working normally), the event loop was
        # falling behind on reading new WebSocket frames because it
        # kept blocking on fsync — a classic case of one slow
        # synchronous call starving an entire async event loop.
        # Moving all disk I/O onto a dedicated background thread
        # removes this bottleneck entirely: write_record() now just
        # enqueues (effectively instant, never blocks), and a single
        # writer thread handles the actual write+fsync in the
        # background, still preserving the "survive a hard kill"
        # durability guarantee, just off the hot path.
        # BOUNDED — a real deployment on a 256MB machine hit the Linux
        # OOM killer because the previous unbounded queue let ticks
        # pile up in memory faster than disk writes could drain them,
        # eventually exhausting all available RAM. Capping this at a
        # modest size and dropping the newest tick (with a
        # rate-limited warning) when genuinely overwhelmed is a much
        # safer failure mode than an OOM crash-restart loop — losing
        # a handful of ticks during a rare overload spike is far
        # better than losing the entire session.
        self._write_queue: queue.Queue = queue.Queue(maxsize=5000)
        self._dropped_count = 0
        self._last_drop_warning = 0.0
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="disk-writer")
        self._writer_thread.start()

    def log(self, msg: str):
        print(f"[{now_iso()}] {msg}", flush=True)

    def write_record(self, record: dict):
        # Non-blocking: hands the record to the background writer
        # thread. If the queue is full (writer genuinely can't keep
        # up), drop this one record rather than blocking the event
        # loop OR growing memory unboundedly — losing an occasional
        # tick under rare overload is far better than an OOM kill
        # that loses the entire remaining session.
        try:
            self._write_queue.put_nowait(record)
        except queue.Full:
            self._dropped_count += 1
            now = time.time()
            if now - self._last_drop_warning > 30:  # rate-limit logging
                self.log(f"[WRITER] queue full — dropped "
                          f"{self._dropped_count} record(s) so far "
                          f"(writer can't keep up with current volume)")
                self._last_drop_warning = now

    def _writer_loop(self):
        """Runs on its own OS thread for the entire lifetime of the
        process, completely independent of the asyncio event loop.

        CRITICAL FIX #2: fsync'ing after every single record was
        confirmed (via a real deployment) to be far too slow for this
        machine's disk to sustain — over 100,000 ticks were silently
        dropped in ~6 minutes once the queue-full backpressure kicked
        in. Batching writes and fsync'ing once per batch (instead of
        once per record) keeps the same "survive a hard kill"
        durability guarantee — you can only ever lose the current
        batch, bounded to BATCH_INTERVAL seconds — while cutting disk
        I/O overhead by roughly the batch size, which is what was
        actually making the writer too slow to keep up.
        """
        BATCH_INTERVAL = 0.5   # max seconds of data at risk on a hard kill
        MAX_BATCH_SIZE = 2000  # safety cap so one batch can't grow unbounded

        while True:
            batch = []
            try:
                first = self._write_queue.get(timeout=1.0)
            except queue.Empty:
                continue  # nothing arrived recently, just wait again
            if first is None:  # shutdown sentinel
                break
            batch.append(first)

            deadline = time.time() + BATCH_INTERVAL
            while len(batch) < MAX_BATCH_SIZE and time.time() < deadline:
                try:
                    item = self._write_queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    self._flush_batch(batch)
                    return
                batch.append(item)

            self._flush_batch(batch)

    def _flush_batch(self, batch: list):
        try:
            for record in batch:
                self._out_fh.write(json.dumps(record) + "\n")
            self._out_fh.flush()
            os.fsync(self._out_fh.fileno())
        except Exception as e:
            # Never let a bad batch silently kill the writer thread —
            # log it and keep going, since losing the ability to
            # write ANY further records would be far worse.
            print(f"[{now_iso()}] [WRITER ERROR] {e}", flush=True)
        finally:
            for _ in batch:
                self._write_queue.task_done()

    def close(self):
        """Call on clean shutdown to make sure every queued record
        is actually flushed to disk before the process exits."""
        self._write_queue.join()  # block until all pending writes finish
        self._write_queue.put(None)  # sentinel to stop the writer thread
        self._writer_thread.join(timeout=5)
        self._out_fh.close()

    # ── Discovery loop (Gamma API, every 10s) ──────────────────
    async def run_discovery_loop(self):
        self.log("Discovery loop starting (every "
                  f"{DISCOVERY_INTERVAL_SECONDS}s)")
        while not self.stop_requested and time.time() < self.run_until:
            try:
                to_subscribe, to_unsubscribe = await asyncio.to_thread(
                    self._discover_once
                )
                # Scheduling happens HERE, on the event loop — NOT
                # inside _discover_once, which runs in a worker
                # thread via asyncio.to_thread() and has no running
                # event loop of its own. Calling asyncio.create_task()
                # from within that thread raises RuntimeError every
                # time (confirmed by a test catching this exact
                # failure before it shipped — this affected BOTH the
                # subscribe path, a pre-existing bug from when this
                # script was first written, and the new unsubscribe
                # path added for resolution tracking).
                if to_subscribe:
                    await self._subscribe(to_subscribe)
                if to_unsubscribe:
                    await self._send_unsubscribe(to_unsubscribe)
            except Exception as e:
                self.log(f"[DISCOVERY ERROR] {e}")
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)
        self.stop_requested = True

    # Per pair_id (e.g. BTC_5MIN), Polymarket always has exactly the
    # CURRENT live-trading window and the NEXT pre-open window listed
    # simultaneously (confirmed from live screenshots: a 9:30-9:45
    # market with real volume alongside a 9:45-10:00 market already
    # orderable at $0 volume) — never a whole day of future slots at
    # once in normal browsing, that only showed up here because
    # order=id sorting surfaced batch-created future markets mixed
    # in with genuinely current ones. Selecting the 2 nearest-expiry
    # markets per pair reliably targets exactly current+next,
    # matching what a person actually sees in the app.
    TARGET_MARKETS_PER_PAIR = 2

    def _discover_once(self) -> tuple[list, list]:
        """
        Runs inside asyncio.to_thread() — no event loop here. Must
        return work to be scheduled rather than scheduling it
        directly. Returns (tokens_to_subscribe, tokens_to_unsubscribe).
        """
        raw_markets = self._fetch_markets()
        to_subscribe = []

        # Parse everything first, then group by pair_id so we can
        # pick the nearest-expiry N per pair — a single Gamma page
        # mixes many pairs and many future slots together, so this
        # can't be decided market-by-market as they're seen.
        parsed_by_pair: dict[str, list[tuple[dict, dict]]] = {}
        for raw in raw_markets:
            parsed = self._parse_market(raw)
            if not parsed:
                continue
            parsed_by_pair.setdefault(parsed["pair_id"], []).append((parsed, raw))

        target_market_ids = set()
        for pair_id, entries in parsed_by_pair.items():
            entries.sort(key=lambda pr: pr[0]["expiry"] or float("inf"))
            for parsed, raw in entries[:self.TARGET_MARKETS_PER_PAIR]:
                target_market_ids.add(parsed["market_id"])
                self._process_target_market(parsed, raw, to_subscribe)

        tokens_to_unsubscribe = self._cleanup_expired()
        return to_subscribe, tokens_to_unsubscribe

    def _process_target_market(self, parsed: dict, raw: dict, to_subscribe: list):
        market_id = parsed["market_id"]
        if market_id in self.tracked_markets:
            # Already tracking — but flags like acceptingOrders can
            # CHANGE over time (that's the whole point of this
            # capture), so record a snapshot every discovery cycle,
            # not just on first sight.
            self._record_metadata_snapshot(parsed, raw)
        else:
            # NEW market — this is the pre-open moment we care about.
            self.tracked_markets[market_id] = parsed
            self.token_to_market[parsed["yes_token"]] = market_id
            self.token_to_market[parsed["no_token"]] = market_id
            self.market_count += 1

            self.log(f"NEW market discovered: {parsed['pair_id']} | "
                      f"slug={parsed['slug']} | "
                      f"acceptingOrders={parsed['accepting_orders']} | "
                      f"enableOrderBook={parsed['enable_order_book']} | "
                      f"expiry={parsed['expiry_str']}")

            self._record_metadata_snapshot(parsed, raw, is_first_sight=True)

        # Subscribe ONLY once a market has genuinely opened — no
        # pre-open lead window. The "next" market is still discovered
        # and metadata-snapshotted each cycle (so we know it exists
        # and when it will open), but its WebSocket subscription is
        # deferred until seconds_to_start <= 0, i.e. it has actually
        # started. From that point it's tracked continuously, tick by
        # tick, straight through to its own close.
        now = time.time()
        seconds_to_start = (parsed["start_ts"] - now) if parsed["start_ts"] else None
        is_open = seconds_to_start is None or seconds_to_start <= 0

        if is_open and parsed["yes_token"] not in self.known_token_ids:
            self.known_token_ids.add(parsed["yes_token"])
            self.known_token_ids.add(parsed["no_token"])
            to_subscribe.extend([parsed["yes_token"], parsed["no_token"]])
            self.log(f"Subscribing (OPEN): "
                      f"{parsed['pair_id']} | {parsed['slug']} | "
                      f"seconds_to_start={seconds_to_start}")



    # The asset+timeframe pairs this capture tracks. Every listing/
    # filtering endpoint we tried (tag_slug, order=id, series_slug,
    # /events) turned out to be polluted by markets from as far back
    # as December 2025 that were never marked closed=true — there is
    # no reliable way to filter those out of a LIST. But these slugs
    # are deterministic: {asset}-updown-{tf}-{unix_close_timestamp},
    # aligned to fixed 5-min/15-min boundaries. So instead of listing
    # and filtering, we COMPUTE the exact slug for the current and
    # next round of every pair and fetch those directly by exact
    # match — confirmed clean against live data on 2026-07-10.
    ASSETS = ["btc", "eth", "xrp", "sol", "bnb", "doge", "hype"]
    TIMEFRAMES = {"5m": 300, "15m": 900}

    def _compute_target_slugs(self) -> dict[str, str]:
        """Returns {slug: pair_id} for the current + next round of
        every asset/timeframe pair, computed directly from the clock
        rather than discovered via any search/list endpoint."""
        now = time.time()
        slugs = {}
        for asset in self.ASSETS:
            for tf_label, tf_seconds in self.TIMEFRAMES.items():
                pair_id = f"{asset.upper()}_{tf_label.upper()}"
                # IMPORTANT: the slug's embedded timestamp is the
                # round's START time (eventStartTime), NOT its close
                # — confirmed against live data where a slug's own
                # timestamp matched eventStartTime while endDate was
                # 300/900s LATER. The currently in-progress round
                # started at the most recent boundary at or before
                # now; the next round starts exactly one period
                # later. Using (floor+1) here (the close-time
                # boundary) was an off-by-one that silently skipped
                # the genuinely-live round every cycle.
                current_start = int(now // tf_seconds) * tf_seconds
                next_start = current_start + tf_seconds
                slugs[f"{asset}-updown-{tf_label}-{current_start}"] = pair_id
                slugs[f"{asset}-updown-{tf_label}-{next_start}"] = pair_id
        return slugs

    def _fetch_markets(self) -> list:
        """Batch-fetch the current+next market for every tracked
        pair by exact slug match in a single request. Retries on
        transient failure since a single dropped request during a
        12-hour run shouldn't cost an entire discovery cycle."""
        url = f"{GAMMA_API}/markets"
        target_slugs = self._compute_target_slugs()
        params = [("slug", s) for s in target_slugs]
        last_error = None
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=8)
                resp.raise_for_status()
                data = resp.json()
                # Tag each returned market with the pair_id we computed
                # for its slug, since _parse_market's own SLUG_PATTERNS
                # matching is no longer the source of truth here.
                for m in data:
                    m["_computed_pair_id"] = target_slugs.get(m.get("slug"))
                return data
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        self.log(f"[DISCOVERY] Gamma fetch failed after 3 attempts: "
                  f"{last_error}")
        return []


    def _parse_market(self, raw: dict) -> dict | None:
        slug = raw.get("slug", "")
        if not slug:
            return None

        # pair_id now comes from the slug we deliberately computed
        # and requested — not from matching against SLUG_PATTERNS,
        # which required scanning a polluted, unfiltered listing.
        pair_id = raw.get("_computed_pair_id")
        if not pair_id:
            return None

        raw_ids = raw.get("clobTokenIds", "[]")
        token_ids = (
            json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        )
        if len(token_ids) < 2:
            return None

        end_date = raw.get("endDate", "")
        expiry = self._parse_iso(end_date)

        # IMPORTANT: startDate is just when Gamma's DB row was
        # created, NOT when the round actually opens — confirmed by
        # inspecting a live market where startDate was "today" but
        # the round itself (per its own question text and
        # eventStartTime) didn't open until 24h later. eventStartTime
        # is the field that actually matches the round's real open
        # time, so that's what start_ts must be derived from.
        start_str = raw.get("eventStartTime", "")
        start_ts = self._parse_iso(start_str)

        return {
            "pair_id": pair_id,
            "slug": slug,
            "market_id": str(raw.get("id")),
            "condition_id": raw.get("conditionId"),
            "yes_token": token_ids[0],
            "no_token": token_ids[1],
            "expiry": expiry,
            "expiry_str": end_date,
            "start_ts": start_ts,
            "start_str": start_str,
            # THE key fields for pre-open detection — captured raw,
            # unfiltered, exactly as Gamma reports them, since these
            # can disagree with active/closed (confirmed via current
            # Polymarket docs and a real GitHub issue showing exactly
            # this kind of inconsistency for other market categories).
            "active": raw.get("active"),
            "closed": raw.get("closed"),
            "accepting_orders": raw.get("acceptingOrders"),
            "enable_order_book": raw.get("enableOrderBook"),
              }
      def _parse_iso(self, s: str) -> float:
        if not s:
            return 0.0
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    def _record_metadata_snapshot(self, parsed: dict, raw: dict,
                                   is_first_sight: bool = False):
        self.write_record({
            "record_type": "metadata_snapshot",
            "captured_at": time.time(),
            "captured_at_iso": now_iso(),
            "is_first_sight": is_first_sight,
            "pair_id": parsed["pair_id"],
            "slug": parsed["slug"],
            "market_id": parsed["market_id"],
            "condition_id": parsed["condition_id"],
            "expiry": parsed["expiry"],
            "start_ts": parsed["start_ts"],
            "active": parsed["active"],
            "closed": parsed["closed"],
            "accepting_orders": parsed["accepting_orders"],
            "enable_order_book": parsed["enable_order_book"],
            "seconds_to_expiry": round(parsed["expiry"] - time.time(), 2)
                                  if parsed["expiry"] else None,
            "seconds_to_start": round(parsed["start_ts"] - time.time(), 2)
                                 if parsed["start_ts"] else None,
        })

    def _cleanup_expired(self) -> list:
        """
        Moves expired markets to pending_resolution instead of
        dropping them immediately — this was the original gap: a
        market's outcome can't be looked up if its metadata
        (condition_id, slug, pair_id) has already been discarded.

        Returns a flat list of token_ids that should be unsubscribed
        — this method itself does NOT schedule the unsubscribe
        directly (a real bug caught during testing: this runs inside
        asyncio.to_thread() via _discover_once, i.e. in a worker
        thread with no running event loop, so calling
        asyncio.create_task() here raises RuntimeError every time —
        the caller, which IS on the event loop, does the scheduling
        instead).
        """
        now = time.time()
        newly_expired = [
            mid for mid, m in self.tracked_markets.items()
            if m["expiry"] and m["expiry"] < now
        ]
        tokens_to_unsubscribe = []
        for mid in newly_expired:
            m = self.tracked_markets.pop(mid)
            self.token_to_market.pop(m["yes_token"], None)
            self.token_to_market.pop(m["no_token"], None)
            self.known_token_ids.discard(m["yes_token"])
            self.known_token_ids.discard(m["no_token"])
            tokens_to_unsubscribe.extend([m["yes_token"], m["no_token"]])
            self.pending_resolution[mid] = 0
            self.resolution_metadata[mid] = m
            self.log(f"Market window closed, checking for outcome: "
                      f"{m['pair_id']} ({m['slug']})")
        return tokens_to_unsubscribe

    async def run_resolution_check_loop(self):
        """
        Separate timer-driven loop (NOT the tick stream — resolution
        doesn't arrive as a price event; it has to be polled from
        Gamma per-market after expiry) that checks every pending
        market's outcomePrices field. A resolved binary market
        settles outcomePrices to ["1","0"] or ["0","1"] — whichever
        side won pays $1, the loser $0. Retries with backoff-free
        fixed interval since short-duration crypto markets resolve
        via Chainlink automatically (no ~2hr UMA dispute window),
        so "not resolved yet" should be a brief, not indefinite,
        state for these specific markets.
        """
        self.log("Resolution check loop starting (every "
                  f"{RESOLUTION_CHECK_INTERVAL_SECONDS}s)")
        while not self.stop_requested and time.time() < self.run_until:
            await asyncio.sleep(RESOLUTION_CHECK_INTERVAL_SECONDS)
            pending_ids = list(self.pending_resolution.keys())
            for mid in pending_ids:
                await asyncio.to_thread(self._check_resolution, mid)

    def _check_resolution(self, market_id: str):
        m = self.resolution_metadata.get(market_id)
        if not m:
            self.pending_resolution.pop(market_id, None)
            return

        self.pending_resolution[market_id] += 1
        attempt = self.pending_resolution[market_id]

        try:
            url = f"{GAMMA_API}/markets/{market_id}"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            self.log(f"[RESOLUTION] Fetch error for {m['pair_id']} "
                      f"({m['slug']}), attempt {attempt}: {e}")
            if attempt >= RESOLUTION_CHECK_MAX_ATTEMPTS:
                self._give_up_on_resolution(market_id, m)
            return

        outcome_prices_raw = raw.get("outcomePrices")
        closed = raw.get("closed")

        outcome_prices = None
        if outcome_prices_raw:
            try:
                outcome_prices = (
                    json.loads(outcome_prices_raw)
                    if isinstance(outcome_prices_raw, str)
                    else outcome_prices_raw
                )
            except Exception:
                outcome_prices = None

        is_resolved = (
            outcome_prices is not None
            and len(outcome_prices) >= 2
            and (float(outcome_prices[0]) in (0.0, 1.0))
            and (float(outcome_prices[1]) in (0.0, 1.0))
      )

if is_resolved:
            winning_side = "YES" if float(outcome_prices[0]) == 1.0 else "NO"
            self.write_record({
                "record_type": "resolution",
                "captured_at": time.time(),
                "captured_at_iso": now_iso(),
                "pair_id": m["pair_id"],
                "slug": m["slug"],
                "market_id": market_id,
                "condition_id": m["condition_id"],
                "outcome_prices": outcome_prices,
                "winning_side": winning_side,
                "closed": closed,
                "resolution_check_attempts": attempt,
                "seconds_after_expiry": round(time.time() - m["expiry"], 2)
                                          if m["expiry"] else None,
            })
            self.log(f"[RESOLUTION] {m['pair_id']} ({m['slug']}) "
                      f"resolved: {winning_side} wins "
                      f"(after {attempt} check(s))")
            self.resolved_market_ids.add(market_id)
            self.pending_resolution.pop(market_id, None)
            self.resolution_metadata.pop(market_id, None)
            return

        if attempt >= RESOLUTION_CHECK_MAX_ATTEMPTS:
            self._give_up_on_resolution(market_id, m)

    def _give_up_on_resolution(self, market_id: str, m: dict):
        """
        After RESOLUTION_CHECK_MAX_ATTEMPTS (10 minutes of retrying)
        with no resolved outcomePrices, record that explicitly
        rather than silently dropping the market with no trace —
        an analysis later should be able to tell "this market's
        outcome was never confirmed" apart from "this market simply
        wasn't captured."
        """
        self.write_record({
            "record_type": "resolution_unconfirmed",
            "captured_at": time.time(),
            "captured_at_iso": now_iso(),
            "pair_id": m["pair_id"],
            "slug": m["slug"],
            "market_id": market_id,
            "condition_id": m["condition_id"],
            "attempts_made": RESOLUTION_CHECK_MAX_ATTEMPTS,
        })
        self.log(f"[RESOLUTION] GIVING UP on {m['pair_id']} "
                  f"({m['slug']}) after "
                  f"{RESOLUTION_CHECK_MAX_ATTEMPTS} attempts — "
                  f"recorded as unconfirmed, not silently dropped")
        self.pending_resolution.pop(market_id, None)
        self.resolution_metadata.pop(market_id, None)

    # ── WebSocket price feed ────────────────────────────────────
    async def run_websocket_loop(self):
        while not self.stop_requested and time.time() < self.run_until:
            try:
                await self._connect_and_listen()
            except Exception as e:
                self.ws_connected = False
                delay = min(30, 2 ** self._reconnect_attempt)
                self._reconnect_attempt += 1
                self.log(f"[WS] Disconnected: {e} — "
                          f"reconnecting in {delay}s "
                          f"(attempt {self._reconnect_attempt})")
                await asyncio.sleep(delay)
        self.stop_requested = True

    async def _connect_and_listen(self):
        self.log("Connecting to Polymarket WebSocket...")
        async with websockets.connect(
            WS_URL, ping_interval=30, ping_timeout=20,
            close_timeout=5, max_size=10_000_000
        ) as ws:
            self.ws = ws
            self.ws_connected = True
            self._reconnect_attempt = 0
            self.log("Connected — capturing every price tick")

            # DEFENSE IN DEPTH: confirmed via a real deployment that
            # the WS connection can go silently stale (no close frame,
            # no exception, ping/pong keepalive not catching it on
            # this network path) — the receive loop just times out
            # every 5s forever, treating it as routine, while zero
            # messages actually arrive for HOURS. Track the last time
            # any message was received; if too long passes, force a
            # reconnect explicitly rather than trusting the library's
            # own keepalive to catch every failure mode.
            self._last_message_at = time.time()
            STALE_THRESHOLD = 60  # seconds with zero messages = treat as dead

            if self.known_token_ids:
                # CRITICAL: resubscribe to EVERYTHING currently
                # tracked, not just self.pending_subs. A token only
                # ever entered pending_subs at the moment it was
                # first discovered while disconnected — once
                # successfully subscribed via a live connection, it's
                # never re-queued. So on any LATER disconnect,
                # pending_subs is empty and every already-active
                # subscription was silently dropped, going unnoticed
                # until a coincidental new-market discovery happened
                # to trigger a fresh subscribe call. Confirmed via a
                # live run showing repeated "Connected" with no
                # matching "Subscribed" line, and tick counts frozen
                # for 60+ seconds despite the process being alive and
                # discovery/resolution loops still running normally.
                await self._send_subscription(list(self.known_token_ids))
                self.pending_subs = []
            elif self.pending_subs:
                await self._send_subscription(self.pending_subs)
                self.pending_subs = []

            while not self.stop_requested and time.time() < self.run_until:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    idle_for = time.time() - self._last_message_at
                    if idle_for > STALE_THRESHOLD:
                        raise ConnectionError(
                            f"no messages received in {idle_for:.0f}s — "
                            f"treating connection as silently stale")
                    continue  # Loop back to re-check stop/deadline conditions
                self._last_message_at = time.time()
                try:
                    await self._on_message(raw)
                except Exception as e:
                    # A single malformed/unexpected message shouldn't
                    # tear down the whole connection — that would
                    # force a full reconnect + resubscribe cycle over
                    # what might just be one bad frame. Log it and
                    # keep listening. Genuine connection failures are
                    # still caught by the staleness watchdog above and
                    # by the outer run_websocket_loop's exception
                    # handler for anything raised outside this loop.
                    self.log(f"[WS] Error processing message (non-fatal): {e}")

    async def _on_message(self, raw: str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Polymarket's feed sometimes sends a JSON array of events in
        # a single frame (e.g. an initial batch on subscribe) rather
        # than one object per frame — normalize to a list either way
        # so every event gets processed instead of crashing on the
        # first list-shaped message.
        messages = parsed if isinstance(parsed, list) else [parsed]

        for data in messages:
            if not isinstance(data, dict):
                continue
            self._handle_single_event(data)

    def _handle_single_event(self, data: dict):
        event_type = data.get("event_type") or data.get("type")
        if not event_type:
            return

      # Polymarket's price_change events don't carry a top-level
        # asset_id like best_bid_ask does — the asset_id is nested
        # inside a price_changes array (one entry per outcome token,
        # both belonging to the same market). Falling back to the
        # top-level field only, as before, left token_id/pair_id/slug
        # null on every single price_change tick — confirmed against
        # 69k+ recorded ticks that all had null pair_id. Either
        # entry's asset_id resolves to the same market_id via
        # token_to_market, so the first one is sufcient for lookup.
        token_id = data.get("asset_id")
        if not token_id and event_type == "price_change":
            changes = data.get("price_changes") or []
            if changes:
                token_id = changes[0].get("asset_id")
        market_id = self.token_to_market.get(token_id) if token_id else None
        market = self.tracked_markets.get(market_id) if market_id else None

        # Record EVERY relevant event type unconditionally — this
        # capture's purpose is completeness, not filtering to only
        # what a trading strategy would act on.
        if event_type == "best_bid_ask":
            self.tick_count += 1
            self.write_record({
                "record_type": "tick",
                "event_type": "best_bid_ask",
                "captured_at": time.time(),
                "captured_at_iso": now_iso(),
                "token_id": token_id,
                "pair_id": market["pair_id"] if market else None,
                "slug": market["slug"] if market else None,
                "side": self._side_for_token(token_id, market),
                "best_bid": data.get("best_bid"),
                "best_ask": data.get("best_ask"),
                "seconds_to_expiry": round(market["expiry"] - time.time(), 3)
                                      if market and market["expiry"] else None,
            })

        elif event_type == "price_change":
            self.tick_count += 1
            self.write_record({
                "record_type": "tick",
                "event_type": "price_change",
                "captured_at": time.time(),
                "captured_at_iso": now_iso(),
                "token_id": token_id,
                "pair_id": market["pair_id"] if market else None,
                "slug": market["slug"] if market else None,
                "side": self._side_for_token(token_id, market),
                "raw": data,  # price_change payloads vary in shape;
                                # keep the full raw event rather than
                                # guessing which sub-fields matter.
                "seconds_to_expiry": round(market["expiry"] - time.time(), 3)
                                      if market and market["expiry"] else None,
            })

        elif event_type == "last_trade_price":
            self.write_record({
                "record_type": "trade",
                "captured_at": time.time(),
                "captured_at_iso": now_iso(),
                "token_id": token_id,
                "pair_id": market["pair_id"] if market else None,
                "slug": market["slug"] if market else None,
                "side": self._side_for_token(token_id, market),
                "price": data.get("price"),
                "size": data.get("size"),
                "seconds_to_expiry": round(market["expiry"] - time.time(), 3)
                                      if market and market["expiry"] else None,
            })

    def _side_for_token(self, token_id, market) -> str | None:
        if not market or not token_id:
            return None
        if token_id == market["yes_token"]:
            return "YES"
        if token_id == market["no_token"]:
            return "NO"
        return None

    async def _subscribe(self, token_ids: list):
        if self.ws and self.ws_connected:
            await self._send_subscription(token_ids)
        else:
            self.pending_subs.extend(token_ids)

    async def _send_subscription(self, token_ids: list):
        try:
            await self.ws.send(json.dumps({
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }))
            self.log(f"Subscribed {len(token_ids)} tokens")
        except Exception as e:
            self.log(f"[WS] Subscription error: {e}")

    async def _send_unsubscribe(self, token_ids: list):
        """
        Called once a market's window has closed — no more price
        ticks are useful for a market that can no longer be traded.
        Failure here is non-fatal (worst case: a few extra ticks
        for a now-closed market keep arriving and get filtered out
        naturally since the market is no longer in tracked_markets).
        """
        if not (self.ws and self.ws_connected):
            return
        try:
            await self.ws.send(json.dumps({
                "assets_ids": token_ids,
                "operation": "unsubscribe",
            }))
        except Exception as e:
            self.log(f"[WS] Unsubscribe error (non-fatal): {e}")


async def main(hours: float, output_dir: str = None):
    # Defaults to the existing local path so nothing changes for
    # on-device Termux usage. Overridable for cloud deployment where
    # data needs to land on a mounted persistent volume instead.
    output_dir = output_dir or os.environ.get("CAPTURE_OUTPUT_DIR", "capture/data")
    os.makedirs(output_dir, exist_ok=True)
    start_ts = int(time.time())
    output_path = f"{output_dir}/lifecycle_capture_{start_ts}.jsonl"

    run_seconds = hours * 3600
    cap = LifecycleCapture(output_path=output_path, run_seconds=run_seconds)

    cap.log("=" * 55)
    cap.log("  POLYBOT — MARKET LIFECYCLE CAPTURE")
    cap.log(f"  Duration: {hours} hours")
    cap.log(f"  Output: {output_path}")
    cap.log("=" * 55)

    # Graceful shutdown on Ctrl+C or SIGTERM (e.g. Termux task kill)
    def handle_stop(signum, frame):
        cap.log(f"Stop signal received ({signum}) — shutting down cleanly")
        cap.stop_requested = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    try:
        await asyncio.gather(
            cap.run_discovery_loop(),
            cap.run_websocket_loop(),
            cap.run_resolution_check_loop(),
            _periodic_status(cap),
            _write_heartbeat(cap),
        )
    finally:
        cap.log("=" * 55)
        cap.log(f"  CAPTURE COMPLETE")
        cap.log(f"  Markets seen: {cap.market_count}")
        cap.log(f"  Ticks recorded: {cap.tick_count}")
        cap.log(f"  Outcomes resolved: {len(cap.resolved_market_ids)}")
        cap.log(f"  Still pending resolution at shutdown: "
                 f"{len(cap.pending_resolution)}")
        cap.log(f"  Output file: {output_path}")
        cap.log("=" * 55)
        cap.close()

async def _write_heartbeat(cap: LifecycleCapture, interval: int = 10):
    """
    Writes an explicit 'still connected, nothing changed' record to
    the OUTPUT FILE (not just the terminal log) every `interval`
    seconds. This exists specifically so that a quiet stretch in the
    data can be PROVEN to be a genuine quiet market rather than a
    silently dropped WebSocket connection you didn't notice — the
    tick stream itself is event-driven (nothing to report when
    nothing changes, which is correct behavior, not a gap), but
    that also means a real dropped connection would otherwise look
    identical to a quiet market when reviewing the file afterward.
    This heartbeat closes that ambiguity: if heartbeats are present
    but ticks are absent for a stretch, that stretch was genuinely
    quiet. If heartbeats are ALSO absent, the connection was down.
    """
    while not cap.stop_requested and time.time() < cap.run_until:
        cap.write_record({
            "record_type": "heartbeat",
            "captured_at": time.time(),
            "captured_at_iso": now_iso(),
            "ws_connected": cap.ws_connected,
            "markets_tracked": len(cap.tracked_markets),
            "markets_pending_resolution": len(cap.pending_resolution),
        })
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Capture full Polymarket market lifecycle "
                    "(pre-open through close) for later analysis"
    )
    parser.add_argument(
        "--hours", type=float, default=12.0,
        help="How long to run the capture, in hours (default: 12)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Directory to write output files to (default: capture/data, "
             "or $CAPTURE_OUTPUT_DIR if set)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.hours, args.output_dir))
