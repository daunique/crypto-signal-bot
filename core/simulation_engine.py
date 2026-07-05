"""
POLYBOT — Simulation Mode
Answers "what would a $50 account have earned?" using REAL,
LIVE market data (WebSocket prices, real fee math, real edge
detection) but placing ZERO live orders and requiring NO funded
wallet or even a private key at all.

WHY THIS EXISTS (read before using):
main.py's live trading path requires authentication AND places
real orders against Polymarket's live CLOB. Running that with an
unfunded wallet does NOT give you a safe profit projection — it
authenticates fine (auth checks signing, not balance), discovers
real opportunities, and then every single order attempt gets
rejected by Polymarket's server for insufficient funds, exhausts
MAX_CONSECUTIVE_MISSES almost immediately, and halts the bot. That
is real network load against Polymarket's live infrastructure for
no useful signal back — not a safe simulation.

This module is the correct tool for "show me what $50 would have
earned": it reuses the SAME real-time WebSocket price feed, SAME
fee calculator, SAME expiry-stage edge thresholds, and SAME two-
sided sizing math as the live system (core/websocket_listener.py,
risk/fee_calculator.py, core/expiry_guard.py) — but every "trade"
is purely a bookkeeping entry against a simulated $50 balance. No
OrderExecutor is constructed. No PRIVATE_KEY or FUNDER_ADDRESS is
required. No network call reaches the CLOB's order endpoints at
all — only the read-only WebSocket price feed and Gamma API
discovery, exactly like observing the market without an account.

HONEST LIMITS OF WHAT THIS CAN TELL YOU:
- It assumes every simulated FOK/FAK order fills at the observed
  price. Real execution can fail to fill (thin books, latency,
  a stale quote) — this simulation cannot model that, so its
  numbers are a BEST CASE, not a guarantee of what live trading
  would actually produce.
- It runs against CURRENT live market conditions for whatever
  period you let it run — it is not a historical backtest across
  the wallet data analyzed in earlier sessions, and results from
  a few hours or days should not be treated as a reliable long-
  run expectation.
"""
import time
from dataclasses import dataclass, field
from config import Config
from risk.fee_calculator import FeeCalculator
from core.expiry_guard import ExpiryGuard


@dataclass
class SimulatedTrade:
    timestamp: float
    pair_id: str
    slug: str
    combined_cost: float
    gross_edge: float
    shares: float
    cost: float
    fees: float
    net_profit: float
    stage: str


class SimulationCapital:
    """
    A self-contained $50 (or whatever you configure) capital
    tracker. Mirrors AUTONOMOUS mode's "float freely, size
    proportionally, compound profit back in" behavior from
    core/capital_manager.py, since a fixed total budget floating
    across all pairs is the correct match for "fixed amount of $50
    total fund" — the FIXED mode's $1-per-side-per-pair design
    would need $24 minimum (12 pairs × $1 × 2 sides) before it
    could even place its first trade.
    """
    def __init__(self, starting_balance: float):
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.deployed = 0.0
        self.peak_balance = starting_balance
        self.trough_balance = starting_balance

    @property
    def available(self) -> float:
        return self.balance - self.deployed

    def calculate_position_size(self, edge: float,
                                 yes_ask: float, no_ask: float) -> float:
        """Same half-Kelly-inspired sizing as CapitalEngine's
        AUTONOMOUS mode (_get_autonomous_size), scaled to THIS
        simulation's own balance rather than a real wallet's pool."""
        if self.available <= 0:
            return 0.0
        base_fraction = edge / max(yes_ask + no_ask, 0.01)
        kelly_size = self.balance * base_fraction * 0.5
        max_per_trade = self.balance * 0.10
        min_per_trade = min(1.0, self.balance)  # Don't demand more than we have
        size = max(min_per_trade,
                   min(kelly_size, max_per_trade, self.available))
        return round(size, 4)

    def deploy(self, cost: float):
        self.deployed += cost

    def settle(self, cost: float, net_profit: float):
        """A simulated trade resolves — release the deployed capital
        and apply the (positive or negative) net profit."""
        self.deployed = max(0.0, self.deployed - cost)
        self.balance += net_profit
        self.peak_balance = max(self.peak_balance, self.balance)
        self.trough_balance = min(self.trough_balance, self.balance)


class SimulationEngine:
    """
    Wraps the real edge-detection math into a no-execution,
    no-wallet-needed simulation. Fed live WebSocket book state the
    same way the real WebSocketListener is — see
    dashboard/app.py's /api/simulation endpoints for how this gets
    driven from live price ticks.
    """
    def __init__(self, starting_balance: float = 50.0):
        self.capital = SimulationCapital(starting_balance)
        self.fee_calc = FeeCalculator()
        self.expiry_guard = ExpiryGuard()
        self.trades: list[SimulatedTrade] = []
        self.started_at = time.time()

    def evaluate_tick(self, pair_id: str, slug: str,
                       yes_ask: float, no_ask: float,
                       expiry: float) -> SimulatedTrade | None:
        """
        Runs the SAME edge/fee/stage logic as
        websocket_listener._check_opportunity, but only ever
        records a bookkeeping entry — never places a real order.
        Returns the SimulatedTrade if one was recorded, else None.
        """
        combined = yes_ask + no_ask
        gross_edge = 1.00 - combined

        stage = self.expiry_guard.lifecycle_stage(expiry)
        if not stage["tradeable"]:
            return None

        threshold = self.expiry_guard.min_edge_for_stage(stage["stage"])
        if combined > threshold:
            return None

        dollar_budget = self.capital.calculate_position_size(
            gross_edge, yes_ask, no_ask
        )
        if dollar_budget <= 0:
            return None

        shares = round(dollar_budget / combined, 2)
        if shares <= 0:
            return None

        costs = self.fee_calc.total_cost(shares, yes_ask, no_ask)
        net_profit = (gross_edge * shares) - costs["total"]

        if net_profit < Config.MIN_NET_PROFIT:
            return None

        actual_cost = shares * combined
        self.capital.deploy(actual_cost)
        # Simulation simplification: settle immediately rather than
        # waiting for real market resolution, since both sides are
        # bought at a genuinely guaranteed-profitable combined cost
        # (see the README's two-sided hedge explanation) — the
        # payout is fixed at trade time regardless of which side
        # the market ultimately resolves to. net_profit already has
        # fees subtracted (computed above), so pass it directly —
        # do NOT add costs["total"] back in, or every trade
        # silently overstates profit by exactly the fee amount.
        self.capital.settle(actual_cost, net_profit)

        trade = SimulatedTrade(
            timestamp=time.time(), pair_id=pair_id, slug=slug,
            combined_cost=combined, gross_edge=gross_edge,
            shares=shares, cost=actual_cost, fees=costs["total"],
            net_profit=net_profit, stage=stage["stage"],
        )
        self.trades.append(trade)
        return trade

    def get_summary(self) -> dict:
        total_profit = sum(t.net_profit for t in self.trades)
        wins = sum(1 for t in self.trades if t.net_profit > 0)
        return {
            "starting_balance": self.capital.starting_balance,
            "current_balance": round(self.capital.balance, 4),
            "total_profit": round(total_profit, 4),
            "return_pct": round(
                total_profit / self.capital.starting_balance * 100, 2
            ) if self.capital.starting_balance > 0 else 0,
            "trade_count": len(self.trades),
            "win_rate": round(wins / len(self.trades) * 100, 2)
                        if self.trades else 0.0,
            "peak_balance": round(self.capital.peak_balance, 4),
            "trough_balance": round(self.capital.trough_balance, 4),
            "running_since": self.started_at,
            "elapsed_hours": round((time.time() - self.started_at) / 3600, 2),
        }

    def get_recent_trades(self, limit: int = 20) -> list:
        return [
            {
                "time": t.timestamp, "pair": t.pair_id,
                "combined_cost": round(t.combined_cost, 4),
                "shares": t.shares, "profit": round(t.net_profit, 4),
                "stage": t.stage,
            }
            for t in self.trades[-limit:][::-1]
        ]
