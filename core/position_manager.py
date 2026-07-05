"""
POLYBOT — Position Manager
Tracks every hit per market, hedged vs unhedged exposure,
and enforces per-market safety limits.

Includes same-side re-entry cap (added after analyzing real trade
data from an active wallet): a market where the bot holds an
unhedged directional position and re-enters the SAME side
repeatedly, without ever getting hedged, is a specific observed
failure pattern — one real example lost $6.52 across five
consecutive same-side entries (0.40 → 0.43 → 0.31 → 0.31 → 0.21)
that never once got matched with the opposite side before the
market resolved against it. MAX_SAME_SIDE_STREAK forces a hedge-
or-cut decision after N consecutive same-side unhedged entries,
rather than allowing indefinite doubling-down on a losing side.
"""
import time
from dataclasses import dataclass, field
from config import Config


@dataclass
class MarketPosition:
    pair_id: str
    expiry: float
    yes_shares: float = 0.0
    no_shares: float = 0.0
    total_cost: float = 0.0
    hit_count: int = 0
    last_hit_time: float = 0.0
    directional: list = field(default_factory=list)
    # Tracks consecutive directional holds on the SAME side without
    # an intervening hedge (i.e. without yes_shares/no_shares ever
    # becoming balanced again). Reset to 0 the moment the position
    # is hedged (either side catches up) or a hedge/cut is forced.
    same_side_streak: int = 0
    streak_side: str = ""  # "YES" or "NO" — which side the streak is on

    @property
    def is_balanced(self) -> bool:
        return abs(self.yes_shares - self.no_shares) < 0.001

    @property
    def unhedged_exposure(self) -> float:
        return abs(self.yes_shares - self.no_shares)

    @property
    def guaranteed_profit(self) -> float:
        hedged_pairs = min(self.yes_shares, self.no_shares)
        return (hedged_pairs * 1.00) - self.total_cost


class PositionManager:
    def __init__(self):
        self.positions: dict[str, MarketPosition] = {}

    def _get_or_create(self, pair_id: str, expiry: float) -> MarketPosition:
        if pair_id not in self.positions:
            self.positions[pair_id] = MarketPosition(
                pair_id=pair_id, expiry=expiry
            )
        return self.positions[pair_id]

    def can_hit(self, pair_id: str, expiry: float) -> bool:
        """Check all safety limits before allowing a new hit."""
        pos = self._get_or_create(pair_id, expiry)

        if pos.hit_count >= Config.MAX_HITS_PER_MARKET:
            return False
        if pos.total_cost >= Config.MAX_COST_PER_MARKET:
            return False
        if pos.unhedged_exposure >= Config.MAX_UNHEDGED_EXPOSURE:
            return False
        if (time.time() - pos.last_hit_time) < Config.HIT_COOLDOWN:
            return False
        return True

    def record_hit(self, pair_id: str, shares: float,
                    cost: float, expiry: float):
        pos = self._get_or_create(pair_id, expiry)
        pos.yes_shares    += shares
        pos.no_shares     += shares
        pos.total_cost    += cost
        pos.hit_count     += 1
        pos.last_hit_time  = time.time()
        # A fully-hedged hit (both legs filled equally) is the
        # clearest possible reset signal for the streak — this
        # market is balanced again, so any prior directional streak
        # is no longer a standing risk.
        pos.same_side_streak = 0
        pos.streak_side = ""

    def can_hold_directional(self, pair_id: str, side: str) -> bool:
        """
        Checks whether ANOTHER same-side directional hold is allowed
        on this market, based on Config.MAX_SAME_SIDE_STREAK. Call
        this BEFORE add_directional() when deciding whether to hold
        or force a hedge/cut — see websocket_listener._handle_one_leg.
        """
        pos = self.positions.get(pair_id)
        if not pos:
            return True  # No position yet — first hold is always allowed
        if pos.streak_side != side:
            return True  # Different side — not a repeat, streak doesn't apply
        return pos.same_side_streak < Config.MAX_SAME_SIDE_STREAK

    def add_directional(self, pair_id: str, side: str,
                         shares: float, price: float,
                         expiry: float = 0.0):
        """
        expiry is only used if this pair has no existing position
        entry yet (needed for _get_or_create's dataclass). In the
        real trading flow this never actually happens — can_hit()
        always runs first and registers the entry as a side effect
        — but making this method self-sufficient rather than silently
        depending on that side effect avoids a subtle bug: calling
        add_directional() before any can_hit()/record_hit() call
        would previously no-op completely (self.positions.get()
        returning None), silently losing the directional hold and
        its share count from position tracking entirely.
        """
        pos = self._get_or_create(pair_id, expiry)
        pos.directional.append({
            "side": side, "shares": shares,
            "price": price, "time": time.time()
        })
        if side == "YES":
            pos.yes_shares += shares
        else:
            pos.no_shares += shares

        # Update the same-side streak counter
        if pos.streak_side == side:
            pos.same_side_streak += 1
        else:
            pos.streak_side = side
            pos.same_side_streak = 1

    def get_same_side_streak(self, pair_id: str) -> int:
        pos = self.positions.get(pair_id)
        return pos.same_side_streak if pos else 0

    def get_hit_count(self, pair_id: str) -> int:
        pos = self.positions.get(pair_id)
        return pos.hit_count if pos else 0

    def resolve_market(self, pair_id: str) -> dict:
        """Called when a market expires — finalize and remove."""
        pos = self.positions.pop(pair_id, None)
        if not pos:
            return {}
        return {
            "pair_id": pair_id,
            "hits": pos.hit_count,
            "profit": round(pos.guaranteed_profit, 4),
            "unhedged": round(pos.unhedged_exposure, 4),
            "same_side_streak_at_close": pos.same_side_streak,
        }

    def get_total_unhedged(self) -> float:
        return sum(p.unhedged_exposure for p in self.positions.values())
