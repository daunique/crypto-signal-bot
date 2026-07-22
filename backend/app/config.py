from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    def model_post_init(self, __context):
        mode = self.bot_mode.lower().strip()
        if mode not in {"demo", "live"}:
            raise ValueError("BOT_MODE must be either 'demo' or 'live'")
        if self.timeframe_seconds != 180:
            raise ValueError("This bot is intentionally locked to 3-minute candles (180 seconds)")
        if self.stake <= 0:
            raise ValueError("STAKE must be greater than zero")


@lru_cache
def get_settings() -> Settings:
    return Settings()
