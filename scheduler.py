"""
POLYBOT — Scheduler
Background jobs:
  - Wallet sync every 60s
  - Position reconciliation every 10 min
  - Health check every 30s
  - Daily report at midnight UTC
(Market discovery runs in its own loop in market_discovery.py at 10s)
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import Config


class BotScheduler:
    def __init__(self, listener, discovery, executor,
                 journal, tracker, circuit_breaker, capital=None,
                 positions=None):
        self.listener   = listener
        self.discovery  = discovery
        self.executor   = executor
        self.journal    = journal
        self.tracker    = tracker
        self.breaker    = circuit_breaker
        self.capital    = capital
        self.positions  = positions
        self.scheduler  = AsyncIOScheduler()

    def setup_jobs(self):
        self.scheduler.add_job(
            self.sync_wallet, "interval",
            seconds=Config.WALLET_SYNC_INTERVAL_SECONDS,
            id="wallet_sync"
        )
        self.scheduler.add_job(
            self.reconcile_positions, "interval",
            minutes=Config.RECONCILE_INTERVAL_MINUTES,
            id="reconcile"
        )
        self.scheduler.add_job(
            self.health_check, "interval",
            seconds=Config.HEALTH_CHECK_INTERVAL_SECONDS,
            id="health_check"
        )
        self.scheduler.add_job(
            self.daily_report, "cron",
            hour=0, minute=0, id="daily_report"
        )

    async def sync_wallet(self):
        try:
            balance = await self.executor.get_wallet_balance()
            self.tracker.update_balance(balance)

            # Update deployed capital from the real capital engine
            # state — without this, tracker.deployed stays at its
            # initial 0.0 forever and the dashboard's "Capital
            # Deployed" card never reflects actual open positions.
            if self.capital is not None:
                self.capital.sync_wallet(balance)
                status = self.capital.get_status()
                if status["mode"] == "FIXED":
                    deployed = status["total_capital"] - status["idle"]
                else:
                    deployed = status["deployed"]
                self.tracker.update_deployed(deployed)

            self.journal.log_wallet(
                balance=balance,
                deployed=self.tracker.deployed,
                pnl_today=self.journal.get_daily_pnl(),
                pnl_total=self.journal.get_total_pnl()
            )
            # Safety check on every sync
            safe = self.breaker.check_all(balance)
            self.tracker.set_status(
                "HALTED" if not safe else "RUNNING"
            )
        except Exception as e:
            print(f"[SCHEDULER] Wallet sync error: {e}")
            self.breaker.record_api_error()

    async def reconcile_positions(self):
        """
        Compares on-chain held positions (Data API — actual filled
        shares) against this bot's internal PositionManager state,
        to catch drift: a missed fill confirmation, a network blip
        during trade logging, or any other case where the bot's own
        bookkeeping has silently diverged from reality.

        A small tolerance (DUST_THRESHOLD) is used rather than exact
        equality — confirmed via research that Polymarket's protocol
        itself introduces sub-cent rounding drift on fills (CLOB
        rounds matched fills to integer cent ticks; the SDK truncates
        taker amounts to USDC/pUSD scale), so a few-microshare
        mismatch is expected and NOT a real discrepancy.
        """
        DUST_THRESHOLD = 0.05  # shares — deliberately looser than the
                                 # protocol's own ~0.01 dust threshold,
                                 # to avoid false alarms from timing
                                 # (a fill that landed between our
                                 # last snapshot and the API's).
        try:
            on_chain = await self.executor.get_data_api_positions()

            if self.positions is None or self.discovery is None:
                print(f"[SCHEDULER] Reconciliation check: "
                      f"{len(on_chain)} on-chain positions found "
                      f"(internal comparison unavailable — "
                      f"positions/discovery not wired)")
                return

            # Aggregate on-chain shares by pair_id via the token->pair
            # index discovery already maintains.
            on_chain_by_pair: dict[str, float] = {}
            for entry in on_chain:
                token_id = entry.get("token_id") or entry.get("asset")
                size = float(entry.get("size", 0) or 0)
                if not token_id or size <= 0:
                    continue
                market = self.discovery.get_market_by_token(token_id)
                if not market:
                    continue  # Position on a market we're not tracking
                pair_id = market["pair_id"]
                on_chain_by_pair[pair_id] = (
                    on_chain_by_pair.get(pair_id, 0) + size
                )

            # Compare against internal state. Internal tracks
            # yes_shares/no_shares separately; on-chain aggregation
            # above sums BOTH sides per pair, so compare against the
            # internal sum too for a like-for-like check.
            discrepancies = []
            for pair_id, pos in self.positions.positions.items():
                internal_total = pos.yes_shares + pos.no_shares
                onchain_total = on_chain_by_pair.get(pair_id, 0.0)
                diff = abs(internal_total - onchain_total)
                if diff > DUST_THRESHOLD:
                    discrepancies.append({
                        "pair_id": pair_id,
                        "internal": round(internal_total, 4),
                        "on_chain": round(onchain_total, 4),
                        "diff": round(diff, 4),
                    })

            # Also check for pairs on-chain that internal tracking
            # has no record of at all — e.g. a directional hold that
            # somehow never got logged.
            for pair_id, onchain_total in on_chain_by_pair.items():
                if pair_id not in self.positions.positions and onchain_total > DUST_THRESHOLD:
                    discrepancies.append({
                        "pair_id": pair_id,
                        "internal": 0.0,
                        "on_chain": round(onchain_total, 4),
                        "diff": round(onchain_total, 4),
                    })

            if discrepancies:
                print(f"[SCHEDULER] ⚠ RECONCILIATION MISMATCH — "
                      f"{len(discrepancies)} pair(s) diverged from "
                      f"on-chain reality:")
                for d in discrepancies:
                    print(f"    {d['pair_id']}: internal={d['internal']} "
                          f"on_chain={d['on_chain']} diff={d['diff']}")
                self.journal.log_circuit_break(
                    f"Reconciliation mismatch: {len(discrepancies)} "
                    f"pair(s) diverged (see logs for detail)"
                )
            else:
                print(f"[SCHEDULER] Reconciliation OK — "
                      f"{len(on_chain)} on-chain positions match "
                      f"internal tracking (within {DUST_THRESHOLD} "
                      f"share tolerance)")

        except Exception as e:
            print(f"[SCHEDULER] Reconcile error: {e}")

    async def health_check(self):
        if not self.listener.ws_connected:
            print("[SCHEDULER] WebSocket down — forcing reconnect")
            await self.listener.reconnect()

    async def daily_report(self):
        pnl   = self.journal.get_daily_pnl()
        total = self.journal.get_total_pnl()
        trades = self.journal.get_trades_today()
        win_rate = self.journal.get_win_rate()
        print(f"\n{'='*50}")
        print(f"  DAILY REPORT")
        print(f"  Trades today:  {trades}")
        print(f"  Win rate:      {win_rate:.1f}%")
        print(f"  Today's PnL:   ${pnl:.4f}")
        print(f"  Total PnL:     ${total:.4f}")
        print(f"{'='*50}\n")

    def start(self):
        self.scheduler.start()
        print(f"[SCHEDULER] Jobs running | "
              f"Wallet sync: {Config.WALLET_SYNC_INTERVAL_SECONDS}s | "
              f"Reconcile: {Config.RECONCILE_INTERVAL_MINUTES}m | "
              f"Health: {Config.HEALTH_CHECK_INTERVAL_SECONDS}s")

    def stop(self):
        self.scheduler.shutdown()
