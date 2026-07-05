"""
POLYBOT — Circuit Breaker
Kill switches that halt trading when risk limits are breached.
"""
import time
from datetime import datetime
from config import Config


class CircuitBreaker:
    def __init__(self, journal, wallet_balance: float, positions=None):
        self.journal           = journal
        self.initial_balance   = wallet_balance
        self.consecutive_misses = 0
        self.api_errors        = 0
        self.is_halted         = False
        self.halt_reason       = None
        # Optional: set via set_positions() if not available at
        # construction time (main.py wires this up after both
        # objects exist).
        self.positions         = positions

    def set_positions(self, positions):
        """Late-bind the position manager if it wasn't available
        when the breaker was constructed."""
        self.positions = positions

    def check_all(self, current_balance: float) -> bool:
        """Run every safety check. Returns True if safe to continue."""
        if self.is_halted:
            return False

        daily_pnl = self.journal.get_daily_pnl()
        if daily_pnl < Config.MAX_DAILY_LOSS:
            self.halt(f"Daily loss limit hit: ${daily_pnl:.2f}")
            return False

        if self.initial_balance > 0:
            drop_pct = (self.initial_balance - current_balance) / \
                       self.initial_balance
            if drop_pct > Config.MAX_BALANCE_DROP_PCT:
                self.halt(f"Wallet dropped {drop_pct:.1%}")
                return False

        if self.consecutive_misses >= Config.MAX_CONSECUTIVE_MISSES:
            self.halt(f"Too many consecutive misses: "
                       f"{self.consecutive_misses}")
            return False

        if self.api_errors >= Config.MAX_API_ERRORS:
            self.halt(f"Too many API errors: {self.api_errors}")
            return False

        # Global unhedged exposure — per-market caps (in
        # position_manager.can_hit) stop any ONE market from
        # accumulating too much unhedged risk, but say nothing
        # about several markets each sitting near their own cap
        # simultaneously. This checks the aggregate across all
        # open positions.
        if self.positions is not None:
            total_unhedged = self.positions.get_total_unhedged()
            if total_unhedged >= Config.MAX_TOTAL_UNHEDGED_EXPOSURE:
                self.halt(f"Global unhedged exposure too high: "
                           f"{total_unhedged:.4f} shares across "
                           f"all open positions")
                return False

        return True

    def halt(self, reason: str):
        self.is_halted   = True
        self.halt_reason = reason
        print(f"\n{'='*50}")
        print(f"  KILL SWITCH ACTIVATED")
        print(f"  Reason: {reason}")
        print(f"{'='*50}\n")
        try:
            self.journal.log_circuit_break(reason)
        except Exception:
            pass

    def resume(self):
        """Manually resume trading after review."""
        self.is_halted = False
        self.halt_reason = None
        self.consecutive_misses = 0
        self.api_errors = 0
        print("[CIRCUIT BREAKER] Trading resumed manually")

    def record_miss(self):
        self.consecutive_misses += 1

    def record_success(self):
        self.consecutive_misses = 0

    def record_api_error(self):
        self.api_errors += 1

    def reset_api_errors(self):
        self.api_errors = 0

    def status(self) -> dict:
        return {
            "halted": self.is_halted,
            "reason": self.halt_reason,
            "consecutive_misses": self.consecutive_misses,
            "api_errors": self.api_errors,
        }
