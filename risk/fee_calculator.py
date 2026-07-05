"""
POLYBOT — Fee Calculator
Exact Polymarket crypto-category fee math.
fee = shares × rate × price × (1 - price)
"""
from config import Config


class FeeCalculator:
    def __init__(self):
        self.rate     = Config.CRYPTO_TAKER_RATE
        self.gas      = Config.POLYGON_GAS
        self.slippage = Config.SLIPPAGE_PER_LEG

    def taker_fee(self, shares: float, price: float) -> float:
        return shares * self.rate * price * (1 - price)

    def total_cost(self, shares: float, yes_price: float,
                    no_price: float) -> dict:
        """
        shares = the SAME share count used on both legs (this is
        what guarantees the hedge — see websocket_listener.py for
        why share count, not dollar amount, must be identical
        across YES and NO).
        Returns full cost breakdown.
        """
        yes_fee  = self.taker_fee(shares, yes_price)
        no_fee   = self.taker_fee(shares, no_price)
        slip     = self.slippage * 2   # Both legs
        gas      = self.gas * 2        # Two transactions

        total = yes_fee + no_fee + slip + gas

        return {
            "yes_fee":  round(yes_fee, 6),
            "no_fee":   round(no_fee, 6),
            "slippage": round(slip, 6),
            "gas":      round(gas, 6),
            "total":    round(total, 6),
        }

    def minimum_combined_for_profit(self, shares: float,
                                     target_profit: float,
                                     yes_price: float = 0.50,
                                     no_price: float = 0.50) -> float:
        """
        What must YES+NO combined be to guarantee target_profit
        after all fees, for a given share count at given (worst-
        case) prices?
        """
        costs = self.total_cost(shares, yes_price, no_price)
        gross_edge_needed = (target_profit + costs["total"]) / shares
        return round(1.00 - gross_edge_needed, 4)
