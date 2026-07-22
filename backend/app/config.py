from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "deriv-higher-lower-bot"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    deriv_app_id: str = ""
    deriv_demo_token: str = ""
    deriv_live_token: str = ""
    bot_mode: str = "demo"

    market_symbol: str = "R_25"
    timeframe_seconds: int = 180
    stake: float = 1.0
    currency: str = "USD"
    barrier_distance: float = 0.375
    min_confluence_score: int = 6
    auto_trade: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def deriv_token(self) -> str:
        return self.deriv_live_token if self.bot_mode.lower() == "live" else self.deriv_demo_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
