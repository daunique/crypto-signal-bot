from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "deriv-higher-lower-bot"
    environment: str = "production"
    host: str = "0.0.0.0"
    port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    # Current Deriv API authentication
    deriv_app_id: str = ""
    deriv_pat: str = ""
    deriv_account_id: str = ""
    bot_mode: str = "demo"

    market_symbol: str = "R_25"
    timeframe_seconds: int = 180
    stake: float = 1.0
    currency: str = "USD"
    market_barriers: str = ""
    min_confluence_score: int = 6
    auto_trade: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @property
    def barrier_map(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for item in self.market_barriers.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            symbol, value = item.split("=", 1)
            try:
                result[symbol.strip()] = float(value.strip())
            except ValueError:
                raise ValueError(f"Invalid barrier value for {symbol.strip()}")
        return result

    def barrier_for(self, symbol: str) -> float:
        barriers = self.barrier_map
        if symbol not in barriers:
            raise RuntimeError(
                f"No market-specific barrier configured for {symbol}. "
                "Set MARKET_BARRIERS=SYMBOL=value before trading."
            )
        return barriers[symbol]


@lru_cache
def get_settings() -> Settings:
    return Settings()
