"""
POLYBOT — Expiry Guard
Hard cutoff at 10 seconds before market resolution.
Tiered edge requirements as expiry approaches.
"""
import time
from config import Config


class ExpiryGuard:
    def seconds_remaining(self, expiry: float) -> float:
        return max(0.0, expiry - time.time())

    def lifecycle_stage(self, expiry: float) -> dict:
        remaining = self.seconds_remaining(expiry)

        if remaining > Config.CAUTIOUS_SECONDS:
            stage = "ACTIVE"
            tradeable = True
        elif remaining > Config.FINAL_SECONDS:
            stage = "CAUTIOUS"
            tradeable = True
        elif remaining > Config.CUTOFF_SECONDS:
            stage = "FINAL"
            tradeable = True
        else:
            stage = "CLOSED"
            tradeable = False

        return {
            "stage": stage,
            "tradeable": tradeable,
            "seconds_remaining": remaining,
        }

    def min_edge_for_stage(self, stage: str) -> float:
        """Maximum allowed YES+NO combined cost for this stage."""
        return {
            "ACTIVE":   Config.MIN_EDGE_ACTIVE,
            "CAUTIOUS": Config.MIN_EDGE_CAUTIOUS,
            "FINAL":    Config.MIN_EDGE_FINAL,
            "CLOSED":   0.0,
        }.get(stage, 0.0)
