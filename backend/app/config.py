from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# Kept here (rather than main.py) so api.py can import it too without a
# main.py <-> api.py circular import (main.py imports the router from api.py).
BUILD_VERSION = "2026-07-26-tick-ema-strategy-1"


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
    # Contract duration in ticks (Deriv's tick-duration contracts are valid
    # for 1-10 ticks). The strategy/backtest this bot ships with (see
    # strategy.py) was validated specifically at 10.
    trade_duration_ticks: int = 10
    stake: float = 1.0
    currency: str = "USD"
    auto_trade: bool = True
    request_timeout_seconds: float = 15.0
    # Deriv's Higher/Lower product requires a signed offset `barrier` (see
    # README). Sized dynamically as a fraction of the rolling tick
    # volatility (stdev of the last `tick_vol_window` tick-to-tick price
    # changes) rather than a fixed point value, so it scales with R_25's
    # actual current volatility instead of going stale. Larger = harder to
    # win but bigger payout; smaller = closer to Rise/Fall odds. 0.25 is the
    # value the shipped strategy was backtested at (see strategy.py) -- not
    # a default guess.
    barrier_vol_fraction: float = 0.25
    tick_vol_window: int = 20

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
        if not (1 <= self.trade_duration_ticks <= 10):
            raise ValueError("TRADE_DURATION_TICKS must be between 1 and 10 (Deriv's tick-duration contract limit)")
        if self.stake <= 0:
            raise ValueError("STAKE must be greater than zero")
        if self.barrier_vol_fraction <= 0:
            raise ValueError("BARRIER_VOL_FRACTION must be greater than zero")
        if self.tick_vol_window < 2:
            raise ValueError("TICK_VOL_WINDOW must be at least 2")


@lru_cache
def get_settings() -> Settings:
    return Settings()
