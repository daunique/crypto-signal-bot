"""
POLYBOT — WebSocket Listener
Connects to Polymarket real-time feed.
Every price event triggers opportunity check and trade execution.
"""
import asyncio
import json
import time
import websockets
from config import Config


class WebSocketListener:
    def __init__(self, discovery, capital, positions,
                 executor, expiry_guard, fee_calc,
                 circuit_breaker, journal, tracker,
                 runtime_settings=None):
        self.discovery       = discovery
        self.capital         = capital
        self.positions       = positions
        self.executor        = executor
        self.expiry_guard    = expiry_guard
        self.fee_calc        = fee_calc
        self.breaker         = circuit_breaker
        self.journal         = journal
        self.tracker         = tracker
        self.runtime_settings = runtime_settings

        # Live orderbook state — updated on every WebSocket event
        # Structure: { token_id: {"bid": float, "ask": float} }
        self.book = {}

        self.ws           = None
        self.ws_connected = False
        self.pending_subs = []  # Tokens to subscribe when WS ready
        self._reconnect_attempt = 0

    # ── Main run loop ────────────────────────────────────────
    async def run(self):
        """Connect and listen forever with auto-reconnect.
        Uses exponential backoff (capped at 30s) so a sustained
        outage doesn't hammer Polymarket's servers — and resets
        to immediate retry the moment a connection succeeds."""
        while True:
            try:
                await self._connect_and_listen()
            except Exception as e:
                self.ws_connected = False
                delay = min(30, 2 ** self._reconnect_attempt)
                self._reconnect_attempt += 1
                print(f"[WS] Disconnected: {e} — "
                      f"reconnecting in {delay}s "
                      f"(attempt {self._reconnect_attempt})")
                await asyncio.sleep(delay)

    async def _connect_and_listen(self):
        """Establish WebSocket connection and process messages."""
        print(f"[WS] Connecting to Polymarket WebSocket...")

        async with websockets.connect(
            Config.WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=10_000_000
        ) as ws:
            self.ws           = ws
            self.ws_connected = True
            self._reconnect_attempt = 0
            print("[WS] Connected — listening for price events")

            # Subscribe any tokens that were discovered before WS connected
            if self.pending_subs:
                await self._send_subscription(self.pending_subs)
                self.pending_subs = []

            async for raw_message in ws:
                await self._on_message(raw_message)

    async def _on_message(self, raw: str):
        """
        Called on EVERY WebSocket message — must be fast.
        Average processing time: < 1ms
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = data.get("event_type") or data.get("type")
        if not event_type:
            return

        # ── Real-time top-of-book update ──────────────────
        if event_type == "best_bid_ask":
            token_id = data.get("asset_id")
            best_bid = data.get("best_bid")
            best_ask = data.get("best_ask")

            if not token_id or best_ask is None:
                return

            # Update local book state
            if token_id not in self.book:
                self.book[token_id] = {}
            self.book[token_id]["bid"] = float(best_bid or 0)
            self.book[token_id]["ask"] = float(best_ask)

            # Immediately check for opportunity
            await self._check_opportunity(token_id)

        # ── Full book snapshot (on subscribe) ─────────────
        elif event_type == "book":
            token_id = data.get("asset_id")
            if token_id and data.get("asks"):
                best_ask = float(data["asks"][0]["price"]) \
                           if data["asks"] else None
                best_bid = float(data["bids"][0]["price"]) \
                           if data.get("bids") else None
                if best_ask:
                    self.book[token_id] = {
                        "bid": best_bid or 0,
                        "ask": best_ask
                    }

        # ── Price change (individual level update) ─────────
        elif event_type == "price_change":
            token_id = data.get("asset_id")
            if token_id:
                await self._check_opportunity(token_id)

    async def _check_opportunity(self, updated_token_id: str):
        """
        Core logic — runs after EVERY price event.
        Checks if YES+NO combined cost < threshold.
        Executes trade if edge found.
        Fast path: all checks in < 1ms before any I/O.
        """
        # Circuit breaker check first (instant)
        if self.breaker.is_halted:
            return

        # Find which market this token belongs to
        market = self.discovery.get_market_by_token(updated_token_id)
        if not market:
            return

        pair_id      = market["pair_id"]
        yes_token    = market["yes_token"]
        no_token     = market["no_token"]
        expiry       = market["expiry"]
        condition_id = market.get("condition_id")

        # Duration toggle — discovery tracks ALL pairs regardless,
        # so the comparison stats below still get populated even for
        # the "off" duration. This only gates whether we go on to
        # actually SIZE and EXECUTE a trade for this pair right now.
        live = True
        if self.runtime_settings is not None:
            live = self.runtime_settings.is_pair_live(pair_id)

        # Get current asks from local book
        yes_data = self.book.get(yes_token)
        no_data  = self.book.get(no_token)

        if not yes_data or not no_data:
            return  # Don't have both sides yet

        yes_ask = yes_data.get("ask")
        no_ask  = no_data.get("ask")

        if not yes_ask or not no_ask:
            return

        # Expiry check (instant math)
        stage = self.expiry_guard.lifecycle_stage(expiry)
        if not stage["tradeable"]:
            return

        # Edge threshold for this stage
        threshold   = self.expiry_guard.min_edge_for_stage(stage["stage"])
        combined    = yes_ask + no_ask
        gross_edge  = 1.00 - combined

        if combined > threshold:
            return  # No edge — most common path, returns instantly

        # Fee check — will this actually profit after fees?
        dollar_budget = self.capital.get_size(
            pair_id, gross_edge, yes_ask, no_ask, stage
        )
        if dollar_budget <= 0:
            return

        # ── CRITICAL: size by SHARES, not dollars ──────────────
        # Spending the same DOLLAR amount on each leg buys DIFFERENT
        # share counts (since YES and NO have different prices),
        # which breaks the "YES shares == NO shares" guarantee that
        # makes this trade risk-free. Instead, compute ONE share
        # count from the total budget and combined cost, then use
        # that identical share count for both legs.
        shares = round(dollar_budget / combined, 2)
        if shares <= 0:
            return

        actual_cost_estimate = shares * combined
        costs = self.fee_calc.total_cost(shares, yes_ask, no_ask)
        net_profit = (gross_edge * shares) - costs["total"]

        if net_profit < Config.MIN_NET_PROFIT:
            return  # Edge eaten by fees

        # Duration toggle gate — checked BEFORE can_hit deliberately.
        # position_manager.can_hit() has a side effect of creating a
        # MarketPosition entry on first check (via _get_or_create),
        # even if the trade never executes. If this ran before the
        # toggle check, a toggled-off duration would still leave a
        # phantom (empty) position entry behind. Checking live first
        # means a toggled-off pair never touches position_manager
        # state at all — it only ever gets logged as observed.
        if not live:
            observed_market = self.discovery.get_current_market_for_pair(pair_id)
            self.journal.log_observed_opportunity({
                "pair_id":       pair_id,
                "slug":          observed_market.get("slug", "") if observed_market else "",
                "combined_cost": combined,
                "gross_edge":    gross_edge,
                "shares":        shares,
                "net_profit":    net_profit,
                "time_remaining": stage["seconds_remaining"],
            })
            return

        # Position limit check (only reached for LIVE pairs now)
        if not self.positions.can_hit(pair_id, expiry):
            return

        # All checks passed AND this duration is live — EXECUTE
        print(
            f"[EDGE] {pair_id} | "
            f"YES={yes_ask:.3f} + NO={no_ask:.3f} = {combined:.4f} | "
            f"Edge={gross_edge:.4f} | "
            f"Shares={shares:.2f} (~${actual_cost_estimate:.2f}) | "
            f"Net≈${net_profit:.4f} | "
            f"Stage={stage['stage']} | "
            f"T-{stage['seconds_remaining']:.0f}s"
        )

        # Execute in background — don't block next WS message
        asyncio.create_task(
            self._execute_trade(
                pair_id, yes_token, no_token,
                yes_ask, no_ask, shares, net_profit,
                gross_edge, costs, expiry, stage, condition_id
            )
        )

    async def _execute_trade(
        self, pair_id, yes_token, no_token,
        yes_ask, no_ask, shares, net_profit,
        gross_edge, costs, expiry, stage, condition_id=None
    ):
        """
        Two-legged FOK execution.
        Both legs use the IDENTICAL share count (computed upstream
        from total budget / combined cost) so YES shares == NO
        shares exactly — this is what makes the hedge guaranteed.
        YES fills first → then NO immediately. If either fails →
        safe rollback.
        """
        dollar_estimate = shares * (yes_ask + no_ask)

        # Lock capital before submitting
        if not self.capital.lock(pair_id, dollar_estimate, yes_ask, no_ask):
            return

        # ── LEG 1: Buy YES (FOK) ──────────────────────────
        yes_result = await self.executor.place_fok(
            token_id=yes_token,
            price=yes_ask,
            shares=shares,
            condition_id=condition_id
        )

        if not yes_result["filled"]:
            self.capital.unlock(pair_id, dollar_estimate, yes_ask, no_ask)
            self.breaker.record_miss()
            return

        # ── Re-verify edge before NO leg ──────────────────
        current_no_ask = self.book.get(no_token, {}).get("ask")
        if not current_no_ask:
            # NO price unknown — sell YES back
            await self._sell_back(yes_token, yes_ask,
                                   yes_result["shares"], pair_id,
                                   condition_id)
            self.capital.unlock(pair_id, dollar_estimate, yes_ask, no_ask)
            return

        re_combined = yes_result["fill_price"] + current_no_ask
        if re_combined >= 0.99:
            print(f"[ABORT] {pair_id} — edge gone after YES fill "
                  f"({re_combined:.4f}), selling YES back")
            await self._sell_back(yes_token, yes_ask,
                                   yes_result["shares"], pair_id,
                                   condition_id)
            self.capital.unlock(pair_id, dollar_estimate, yes_ask, no_ask)
            return

        # ── LEG 2: Buy NO (FOK) — IDENTICAL SHARE COUNT ───
        # Using yes_result["shares"] (the ACTUAL filled amount, not
        # the requested amount) ensures the hedge is exact even if
        # the YES fill was partially different from what we asked.
        no_result = await self.executor.place_fok(
            token_id=no_token,
            price=current_no_ask,
            shares=yes_result["shares"],
            condition_id=condition_id
        )

        if not no_result["filled"]:
            # Only YES filled — handle unhedged position
            await self._handle_one_leg(
                pair_id, yes_token, yes_result, expiry,
                condition_id
            )
            self.capital.unlock(pair_id, dollar_estimate, yes_ask, no_ask)
            return

        # ── BOTH LEGS FILLED — SUCCESS ────────────────────
        # Correct per-leg dollar cost: each leg's OWN fill price
        # times the (now-identical) share count.
        filled_shares = no_result["shares"]
        actual_cost = (
            (yes_result["fill_price"] * filled_shares) +
            (no_result["fill_price"] * filled_shares)
        )
        locked_profit = (filled_shares * 1.00) - actual_cost

        self.positions.record_hit(pair_id, filled_shares, actual_cost, expiry)
        self.capital.record_fill(pair_id, filled_shares, actual_cost)
        self.breaker.record_success()

        # Log to database
        trade_market = self.discovery.get_current_market_for_pair(pair_id)
        self.journal.log_trade({
            "pair_id":        pair_id,
            "slug":           trade_market.get("slug", "") if trade_market else "",
            "yes_token":      yes_token,
            "no_token":       no_token,
            "yes_ask":        yes_ask,
            "no_ask":         no_ask,
            "combined_cost":  yes_ask + no_ask,
            "gross_edge":     gross_edge,
            "size":           filled_shares,
            "yes_fill_price": yes_result["fill_price"],
            "no_fill_price":  no_result["fill_price"],
            "actual_cost":    actual_cost,
            "taker_fee_yes":  costs["yes_fee"],
            "taker_fee_no":   costs["no_fee"],
            "slippage":       costs["slippage"],
            "gas_fee":        costs["gas"],
            "total_fee":      costs["total"],
            "net_profit":     locked_profit - costs["total"],
            "status":         "HEDGED",
            "hit_number":     self.positions.get_hit_count(pair_id),
            "time_remaining": stage["seconds_remaining"],
            "capital_mode":   Config.CAPITAL_MODE,
            "expiry":         expiry,
        })

        print(
            f"[SUCCESS] {pair_id} | "
            f"YES@{yes_result['fill_price']:.3f} + "
            f"NO@{no_result['fill_price']:.3f} | "
            f"Locked profit: ${locked_profit:.4f} | "
            f"Hit #{self.positions.get_hit_count(pair_id)}"
        )

    async def _sell_back(self, token_id, price, size, pair_id,
                          condition_id=None):
        """Emergency sell-back if NO leg fails."""
        sell_price = max(0.01, price - 0.03)
        result = await self.executor.place_ioc(
            token_id=token_id,
            price=sell_price,
            size=size,
            side="SELL",
            condition_id=condition_id
        )
        if result["filled"]:
            print(f"[ROLLBACK] {pair_id} YES sold back "
                  f"@ {result['fill_price']:.3f}")
        else:
            print(f"[ROLLBACK FAILED] {pair_id} — "
                  f"directional YES position held")

    async def _handle_one_leg(self, pair_id, yes_token,
                               yes_result, expiry,
                               condition_id=None):
        """
        YES filled but NO didn't.
        Decide: hold directional or cut immediately.
        """
        remaining = expiry - time.time()

        # Rule 1: < 30 seconds left → cut immediately
        if remaining < 30:
            print(f"[CUT] {pair_id} one-leg — "
                  f"too close to expiry ({remaining:.0f}s)")
            await self._sell_back(
                yes_token, yes_result["fill_price"],
                yes_result["shares"], pair_id, condition_id
            )
            return

        # Rule 2: Same-side re-entry cap. Added after analyzing real
        # trade data — a real market showed five consecutive same-
        # side unhedged holds at progressively worse prices with no
        # intervening hedge, losing $6.52 on that single market.
        # Rather than blindly holding again, force a cut once the
        # streak cap is reached, regardless of remaining time.
        if not self.positions.can_hold_directional(pair_id, "YES"):
            streak = self.positions.get_same_side_streak(pair_id)
            print(f"[STREAK CUT] {pair_id} — {streak} consecutive "
                  f"same-side (YES) unhedged holds reached the cap "
                  f"({Config.MAX_SAME_SIDE_STREAK}); cutting instead "
                  f"of holding again.")
            await self._sell_back(
                yes_token, yes_result["fill_price"],
                yes_result["shares"], pair_id, condition_id
            )
            return

        # Rule 3: Hold if meaningful time remains and the streak cap
        # hasn't been hit (Step 5: directional position)
        print(f"[DIRECTIONAL] {pair_id} holding YES "
              f"— {remaining:.0f}s remaining")
        self.positions.add_directional(
            pair_id, "YES", yes_result["shares"], yes_result["fill_price"],
            expiry
        )

    # ── WebSocket subscription management ───────────────────
    async def subscribe_tokens(self, token_ids: list):
        """Subscribe new token IDs to the live WebSocket."""
        if self.ws and self.ws_connected:
            await self._send_subscription(token_ids)
        else:
            # Queue for when WS connects
            self.pending_subs.extend(token_ids)

    async def prewarm_market_params(self, new_markets: list):
        """
        Pre-fetches and caches tick_size/neg_risk for every token
        in newly-discovered markets, BEFORE any trade is attempted.
        Run at discovery time (every 10s, no time pressure) instead
        of on the first trade (where every millisecond counts,
        especially near the FINAL/CAUTIOUS expiry stages). Failures
        here are non-fatal — if a lookup fails, place_fok/place_ioc
        still fall back to the safe 0.01 default at trade time.
        """
        if not hasattr(self.executor, "_get_market_params"):
            return  # Mock/test executors may not implement this

        tasks = []
        for market in new_markets:
            condition_id = market.get("condition_id")
            if not condition_id:
                continue
            tasks.append(self.executor._get_market_params(
                market["yes_token"], condition_id
            ))
            tasks.append(self.executor._get_market_params(
                market["no_token"], condition_id
            ))

        if tasks:
            # Run all lookups concurrently — this is background work
            # with no latency pressure, unlike trade-time lookups.
            await asyncio.gather(*tasks, return_exceptions=True)

    async def unsubscribe_tokens(self, token_ids: list):
        """Unsubscribe expired market tokens."""
        if self.ws and self.ws_connected:
            try:
                await self.ws.send(json.dumps({
                    "assets_ids": token_ids,
                    "operation":  "unsubscribe",
                }))
            except Exception:
                pass
        # Clean up local book
        for tid in token_ids:
            self.book.pop(tid, None)

    async def _send_subscription(self, token_ids: list):
        """Send subscription message to WebSocket."""
        try:
            await self.ws.send(json.dumps({
                "assets_ids":           token_ids,
                "type":                 "market",
                "custom_feature_enabled": True,  # Enables best_bid_ask
            }))
        except Exception as e:
            print(f"[WS] Subscription error: {e}")

    async def reconnect(self):
        """Force reconnection (called by health check)."""
        if self.ws:
            await self.ws.close()
        self.ws_connected = False

    async def finalize_market(self, pair_id: str, slug: str):
        """
        Called by market_discovery when a market cycle's grace
        period has passed (e.g. a 5-min BTC market has resolved
        and the next cycle has begun). Two things happen here
        regardless of whether a trade occurred on this cycle:
          1. Pulls final hit/profit/unhedged stats from the
             position manager (only meaningful if a trade happened)
          2. Resets this pair's FIXED-mode capital allocation back
             to its full $1-per-side budget — a new market cycle
             means a fresh budget, so carrying over a mostly-spent
             allocation from the OLD cycle would starve the pair of
             capital for no reason (confirmed as a real bug: without
             this, a pair's own capital is effectively exhausted by
             its 3rd trade and it becomes permanently dependent on
             borrowing from other pairs, even on a brand new market).
        """
        self.capital.reset_pair(pair_id)

        summary = self.positions.resolve_market(pair_id)
        if not summary:
            return  # No positions were opened this cycle — nothing more to log

        if summary.get("unhedged", 0) > 0.001:
            print(f"[CYCLE END] {pair_id} ({slug}) closed with "
                  f"UNHEDGED exposure: {summary['unhedged']:.4f} shares "
                  f"— this position rode to resolution without a "
                  f"matching hedge on one side.")
        else:
            print(f"[CYCLE END] {pair_id} ({slug}) | "
                  f"{summary['hits']} hits | "
                  f"guaranteed profit ${summary['profit']:.4f}")
