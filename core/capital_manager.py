"""
POLYBOT — Capital Manager
Two modes:
  FIXED      — $1 per side per pair, with idle-capital borrowing
  AUTONOMOUS — wallet_balance / 4, floats freely across all markets
Trades 6 assets (BTC, ETH, XRP, SOL, BNB, DOGE) x 2 durations
(5min, 15min) = 12 pairs total.
RULE: YES size always equals NO size. No exceptions.
"""
import time
from enum import Enum
from dataclasses import dataclass
from config import Config


class CapitalMode(Enum):
    FIXED = "FIXED"
    AUTONOMOUS = "AUTONOMOUS"


@dataclass
class PairAllocation:
    pair_id: str
    yes_allocated: float
    no_allocated: float
    yes_filled: float = 0.0
    no_filled: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0
    last_fill_time: float = 0.0

    @property
    def yes_idle(self):
        return max(0.0, self.yes_allocated - self.yes_filled)

    @property
    def no_idle(self):
        return max(0.0, self.no_allocated - self.no_filled)

    @property
    def total_idle(self):
        return self.yes_idle + self.no_idle


class CapitalEngine:
    def __init__(self, mode: CapitalMode, wallet_balance: float,
                 unit_size: float = 1.0):
        self.mode           = mode
        self.wallet_balance = wallet_balance
        self.unit_size      = unit_size
        self.deployed       = 0.0
        # pair_id (borrower) -> [(source_pair_id, amount), ...]
        # Uncommitted borrows awaiting either commit (record_fill)
        # or discard (unlock, on any failure path) — see
        # _get_fixed_size() for why this two-phase design exists.
        self._pending_borrows = {}

        if mode == CapitalMode.FIXED:
            self.pairs = {
                pid: PairAllocation(pid, unit_size, unit_size)
                for pid in Config.ACTIVE_PAIRS
            }
            total = unit_size * 2 * len(Config.ACTIVE_PAIRS)
            print(f"[CAPITAL] Mode=FIXED | "
                  f"${unit_size}/side × {len(Config.ACTIVE_PAIRS)} pairs "
                  f"= ${total:.2f} total")
        else:
            self.pool = wallet_balance / 4
            print(f"[CAPITAL] Mode=AUTONOMOUS | "
                  f"Pool=${self.pool:.2f} (1/4 of ${wallet_balance:.2f})")

    # ── Size calculation ─────────────────────────────────────
    def get_size(self, pair_id: str, edge: float,
                 yes_ask: float, no_ask: float, stage: dict) -> float:
        """
        Returns the TOTAL dollar budget available for this trade
        (covers both legs combined). The caller converts this into
        a single share count via shares = budget / combined_cost,
        which is then used identically on both legs — see
        websocket_listener.py._check_opportunity().
        """
        if self.mode == CapitalMode.FIXED:
            size = self._get_fixed_size(pair_id)
        else:
            size = self._get_autonomous_size(edge, yes_ask, no_ask)

        # Apply stage-based size reduction near expiry
        multiplier = {
            "ACTIVE":   1.0,
            "CAUTIOUS": 0.5,
            "FINAL":    0.25,
        }.get(stage["stage"], 0.0)

        size = size * multiplier

        # Apply timeframe weighting (data-justified — see config.py
        # TIMEFRAME_WEIGHT comment for the reasoning). Weights are
        # relative, normalized against their own average so an even
        # split (the default) never changes sizing at all — only a
        # deliberate tilt away from 1.0:1.0 has any effect.
        size = size * self._timeframe_multiplier(pair_id)

        return round(size, 4)

    def _timeframe_multiplier(self, pair_id: str) -> float:
        """
        Returns a normalized multiplier from Config.TIMEFRAME_WEIGHT
        based on whether pair_id is a 5MIN or 15MIN pair. Normalized
        against the average of both weights so an even split (the
        default {5MIN: 1.0, 15MIN: 1.0}) always yields multiplier
        1.0 for both — i.e. no behavior change unless you actually
        tilt the weights away from equal.
        """
        weights = Config.TIMEFRAME_WEIGHT
        avg_weight = (weights.get("5MIN", 1.0) + weights.get("15MIN", 1.0)) / 2
        if avg_weight <= 0:
            return 1.0  # Defensive fallback — never zero out sizing entirely

        if pair_id.endswith("_5MIN"):
            return weights.get("5MIN", 1.0) / avg_weight
        elif pair_id.endswith("_15MIN"):
            return weights.get("15MIN", 1.0) / avg_weight
        return 1.0  # Unrecognized suffix — no change, fail safe

    def _get_fixed_size(self, pair_id: str) -> float:
        """
        Returns available budget for this pair. If borrowing from
        another idle pair, the borrow is RECORDED (self._pending_borrows)
        but not yet committed to the source pair's filled totals —
        commitment happens in record_fill() (on success) and the
        borrow record is discarded in unlock() (on any failure path).
        This two-phase approach avoids both the original bug (two
        pairs both seeing the same idle capital as available under
        concurrent load) and a capital leak (borrowed-but-never-used
        capital being permanently deducted from the source pair when
        the trade fails to fill, which a naive immediate-reservation
        fix would cause).
        """
        pair = self.pairs.get(pair_id)
        if not pair:
            return 0.0

        own_idle = min(pair.yes_idle, pair.no_idle)
        if own_idle > 0:
            return own_idle

        borrowed = 0.0
        needed = self.unit_size * 2
        sources = []  # [(source_pair_id, amount), ...]
        for pid, p in self.pairs.items():
            if pid == pair_id or borrowed >= needed:
                continue
            idle_time = time.time() - p.last_fill_time
            # Subtract any already-pending (uncommitted) borrows
            # against this source so concurrent checks in the same
            # tick don't double-count it either.
            pending_against_source = self._pending_borrow_total(pid)
            source_idle = min(p.yes_idle, p.no_idle) - pending_against_source
            if source_idle > 0.1 and idle_time > 30:
                take = min(source_idle, needed - borrowed)
                sources.append((pid, take))
                borrowed += take

        if borrowed > 0:
            # Stash the pending borrow, keyed by the BORROWING pair,
            # so record_fill()/unlock() for THIS pair's trade can
            # find and resolve it precisely.
            self._pending_borrows[pair_id] = sources

        return borrowed

    def _pending_borrow_total(self, source_pair_id: str) -> float:
        """Sum of all currently-pending (uncommitted) borrows drawn
        FROM a given source pair, across any other pair's in-flight
        trade attempt."""
        total = 0.0
        for borrower_sources in self._pending_borrows.values():
            for pid, amount in borrower_sources:
                if pid == source_pair_id:
                    total += amount
        return total

    def _get_autonomous_size(self, edge: float,
                              yes_ask: float, no_ask: float) -> float:
        available = self.pool - self.deployed
        if available <= 0:
            return 0.0

        # Half-Kelly inspired sizing scaled by edge strength
        base_fraction = edge / max(yes_ask + no_ask, 0.01)
        kelly_size    = self.pool * base_fraction * 0.5

        max_per_trade = self.pool * 0.10  # Never more than 10% per trade
        min_per_trade = 1.0

        size = max(min_per_trade, min(kelly_size, max_per_trade, available))
        return round(size, 4)

    # ── Lock / unlock (Autonomous mode capital reservation) ──
    def lock(self, pair_id: str, dollar_cost: float,
              yes_ask: float, no_ask: float) -> bool:
        """
        dollar_cost = the TOTAL dollar cost already computed by the
        caller (shares × combined price) — NOT a per-leg size to be
        multiplied again. Passing a per-leg dollar amount here would
        double-count price.
        """
        if self.mode == CapitalMode.AUTONOMOUS:
            if dollar_cost > (self.pool - self.deployed):
                return False
            self.deployed += dollar_cost
        return True

    def unlock(self, pair_id: str, dollar_cost: float,
               yes_ask: float, no_ask: float):
        if self.mode == CapitalMode.AUTONOMOUS:
            self.deployed = max(0.0, self.deployed - dollar_cost)
        else:
            # FIXED mode: discard any pending (uncommitted) borrow
            # this pair made for the trade that just failed. Nothing
            # was ever actually deducted from the source pair, so
            # there's nothing to "give back" — just forget the
            # pending record so it stops being counted against the
            # source pair's availability for future checks.
            self._pending_borrows.pop(pair_id, None)

    # ── Record fills ──────────────────────────────────────────
    def record_fill(self, pair_id: str, shares: float, cost: float):
        """
        shares = identical share count filled on both legs (used
                 for position-manager-level accounting elsewhere)
        cost   = total dollar cost across both legs

        FIXED mode tracks yes_filled/no_filled in DOLLARS (matching
        yes_allocated/no_allocated, which are dollar budgets like
        "$1 per side") — so idle-capital comparisons stay unit-
        consistent. Each leg's dollar share of the total cost is
        approximated as half the combined cost, which is exact when
        YES and NO prices are symmetric around 0.50 and a close
        approximation otherwise (the small residual doesn't affect
        trading decisions, only the idle-capital display).

        If this pair borrowed capital to make this trade (see
        _get_fixed_size), COMMIT that borrow now by actually
        deducting it from the source pair(s) — this only happens
        on a confirmed successful fill, never speculatively.
        """
        if self.mode == CapitalMode.FIXED:
            pair = self.pairs.get(pair_id)
            if pair:
                pair.yes_filled += cost / 2
                pair.no_filled  += cost / 2
                pair.yes_cost   += cost / 2
                pair.no_cost    += cost / 2
                pair.last_fill_time = time.time()

            # Commit any pending borrow now that the trade succeeded
            sources = self._pending_borrows.pop(pair_id, None)
            if sources:
                for source_pid, amount in sources:
                    source_pair = self.pairs.get(source_pid)
                    if source_pair:
                        source_pair.yes_filled += amount
                        source_pair.no_filled  += amount
        else:
            self.deployed += cost

    def record_profit(self, profit: float):
        """Autonomous mode: profit compounds back into the pool."""
        if self.mode == CapitalMode.AUTONOMOUS:
            self.pool += profit
            self.wallet_balance += profit

    def reset_pair(self, pair_id: str):
        """Called when a market resolves — reset allocation."""
        if self.mode == CapitalMode.FIXED and pair_id in self.pairs:
            self.pairs[pair_id] = PairAllocation(
                pair_id, self.unit_size, self.unit_size
            )

    def sync_wallet(self, new_balance: float):
        """Periodic wallet sync — keeps autonomous pool accurate."""
        self.wallet_balance = new_balance
        if self.mode == CapitalMode.AUTONOMOUS:
            self.pool = new_balance / 4

    def get_status(self) -> dict:
        if self.mode == CapitalMode.FIXED:
            total_idle = sum(p.total_idle for p in self.pairs.values())
            return {
                "mode": "FIXED",
                "total_capital": self.unit_size * 2 * len(self.pairs),
                "idle": round(total_idle, 4),
            }
        return {
            "mode": "AUTONOMOUS",
            "pool": round(self.pool, 4),
            "deployed": round(self.deployed, 4),
            "available": round(self.pool - self.deployed, 4),
        }
