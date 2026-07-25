from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

# Kept here (rather than main.py) so api.py can import it too without a
# main.py <-> api.py circular import (main.py imports the router from api.py).
BUILD_VERSION = "2026-07-24-dashboard-redesign-and-mode-toggle-1"


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
    timeframe_seconds: int = 180
    stake: float = 1.0
    currency: str = "USD"
    min_confluence_score: int = 6
    auto_trade: bool = True
    request_timeout_seconds: float = 15.0
    # Deriv's Higher/Lower product requires a signed offset `barrier` (see
    # README). This is sized dynamically as a fraction of the recent average
    # candle range (ATR) rather than a fixed point value, so it scales with
    # R_25's actual current volatility instead of going stale if the
    # volatility regime shifts. Larger = harder to win but bigger payout;
    # smaller = closer to Rise/Fall odds. Tune to taste.
    barrier_atr_fraction: float = 0.25

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
        if self.timeframe_seconds != 180:
            raise ValueError("This bot is intentionally locked to 3-minute candles (180 seconds)")
        if self.stake <= 0:
            raise ValueError("STAKE must be greater than zero")
        if self.barrier_atr_fraction <= 0:
            raise ValueError("BARRIER_ATR_FRACTION must be greater than zero")


@lru_cache
def get_settings() -> Settings:
    return Settings()
