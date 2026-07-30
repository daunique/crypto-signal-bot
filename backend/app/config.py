from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# Kept here (rather than main.py) so api.py can import it too without a
# main.py <-> api.py circular import (main.py imports the router from api.py).
BUILD_VERSION = "2026-07-30-main-sub-pnl-track-1"


class Settings(BaseSettings):
    app_name: str = "deriv-higher-lower-bot"
    environment: str = "production"
    host: str = "0.0.0.0"
    port: int = 8080
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    deriv_app_id: str = ""
    deriv_pat: str = ""
    deriv_account_id: str = ""
    bot_mode: str = "demo"

    market_symbol: str = "R_25"
    stake: float = 1.0
    currency: str = "USD"
    auto_trade: bool = True
    request_timeout_seconds: float = 15.0

    # --- Strategy: backtested volatility-timing signal (see PnL note below) ---
    # Contract duration in ticks. This is fixed at 10 because that is what was
    # backtested (deriv-data 2026-01-01 to 2026-07-18, 8.55M ticks/symbol) --
    # changing it means trading something that has not been validated.
    contract_duration_ticks: int = 10
    # Fixed, signed-offset barrier magnitude (see deriv.py: sign is applied
    # automatically from bet_direction -- +barrier for HIGHER, -barrier for
    # LOWER). NOT ATR-derived. 0.30 was chosen because, of every barrier and
    # signal combination tested, it came closest to the live quoted payout's
    # breakeven win rate (still ~1.0-1.6 points short -- see PnL note below).
    barrier: float = 0.30
    # Which side every qualifying signal trades. Backtesting found Higher and
    # Lower statistically indistinguishable on this instrument (no directional
    # edge exists -- see report), so this is fixed rather than computed; it
    # only needs to be *a* consistent choice, not the "right" one.
    bet_direction: str = "LOWER"
    # Rolling window (in ticks) used to estimate current realized volatility.
    vol_window_ticks: int = 100
    # The volatility-percentile threshold is recalculated once per day from
    # only the preceding N days of data (never future data) -- this is the
    # "adaptive, non-lookahead" version validated in the backtest, not the
    # more optimistic fixed-threshold-on-the-whole-sample version.
    vol_trailing_days: int = 90
    # A signal fires when current rolling volatility is at or above this
    # percentile of the trailing window (90 = "top 10% most volatile
    # moments"). This is a *timing* filter only -- it has no bearing on
    # direction.
    vol_target_percentile: float = 90.0

    # --- Main/sub PnL-track risk-management overlay (see pnl_tracker.py) ---
    # This sits on top of the strategy above; it doesn't change when a
    # signal fires, only which PnL bucket a trade is recorded against, and
    # (live accounts only) whether that trade is actually placed live or
    # diverted to demo.
    pnl_track_profit_target: float = 10.0
    pnl_track_loss_streak_limit: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    def model_post_init(self, __context):
        mode = self.bot_mode.lower().strip()
        if mode not in {"demo", "live"}:
            raise ValueError("BOT_MODE must be either 'demo' or 'live'")
        # Normalize in place. Without this, a value that passes this
        # case-insensitive check (e.g. "Demo", "DEMO", or "demo" with
        # trailing whitespace) would still fail deriv.py's exact `==
        # "demo"` comparison during account selection, silently falling
        # through to the *real* (live) account instead of demo.
        self.bot_mode = mode
        if self.stake <= 0:
            raise ValueError("STAKE must be greater than zero")
        if self.contract_duration_ticks <= 0:
            raise ValueError("CONTRACT_DURATION_TICKS must be greater than zero")
        if self.barrier <= 0:
            raise ValueError("BARRIER must be greater than zero")
        direction = self.bet_direction.upper().strip()
        if direction not in {"HIGHER", "LOWER"}:
            raise ValueError("BET_DIRECTION must be either 'HIGHER' or 'LOWER'")
        self.bet_direction = direction
        if self.vol_window_ticks <= 1:
            raise ValueError("VOL_WINDOW_TICKS must be greater than 1")
        if self.vol_trailing_days <= 0:
            raise ValueError("VOL_TRAILING_DAYS must be greater than zero")
        if not (0 < self.vol_target_percentile < 100):
            raise ValueError("VOL_TARGET_PERCENTILE must be between 0 and 100 (exclusive)")
        if self.pnl_track_profit_target <= 0:
            raise ValueError("PNL_TRACK_PROFIT_TARGET must be greater than zero")
        if self.pnl_track_loss_streak_limit <= 0:
            raise ValueError("PNL_TRACK_LOSS_STREAK_LIMIT must be greater than zero")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Honest performance note (carried forward from the backtest report, not
# reproduced from optimism): at the live payout quoted for R_25 barrier 0.30
# ($3.00 back per $1 staked -> breakeven win rate 33.33%), the validated,
# out-of-sample, non-lookahead win rate for this exact configuration was
# 31.9-32.0% -- short of breakeven by roughly 1.0-1.6 percentage points. This
# is the closest to breakeven found across an extensive search (pullback
# patterns, multi-signal ensembles, RNG/periodicity checks, and this
# volatility-timing signal). It is not known to be profitable. Nothing in
# this codebase should be read as claiming otherwise.
# ---------------------------------------------------------------------------
