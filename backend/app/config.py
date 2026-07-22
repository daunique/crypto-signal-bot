from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "deriv-higher-lower-bot"
    app_version: str = "3.0.0"
    environment: str = "production"
    host: str = "0.0.0.0"
    port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    # Current Deriv API: PAT app + PAT token.
    deriv_app_id: str = ""
    deriv_pat: str = ""
    # Compatibility aliases for older deployments.
    deriv_api_token: str = ""
    deriv_token: str = ""
    deriv_demo_token: str = ""
    deriv_live_token: str = ""
    deriv_account_id: str = ""
    bot_mode: str = "demo"

    market_symbol: str = "R_25"
    timeframe_seconds: int = 180
    history_count: int = 120
    stake: float = 1.0
    currency: str = "USD"
    market_barriers: str = ""
    min_confluence_score: int = 6
    auto_trade: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @property
    def auth_token(self) -> str:
        # PAT is preferred. Legacy aliases are supported only to ease migration.
        candidates = [
            self.deriv_pat,
            self.deriv_api_token,
            self.deriv_token,
            self.deriv_live_token if self.bot_mode.lower() == "live" else self.deriv_demo_token,
        ]
        return next((x.strip() for x in candidates if x and x.strip()), "")

    @property
    def account_type(self) -> str:
        return "real" if self.bot_mode.lower() == "live" else "demo"

    def barrier_for_symbol(self, symbol: str) -> float:
        raw = self.market_barriers.strip()
        if not raw:
            raise RuntimeError(f"No market barrier configuration found for {symbol}.")
        values: dict[str, float] = {}
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                raise RuntimeError(f"Invalid MARKET_BARRIERS entry '{item}'. Expected SYMBOL=VALUE.")
            key, value = item.split("=", 1)
            try:
                barrier = float(value.strip())
            except ValueError as exc:
                raise RuntimeError(f"Invalid barrier value for {key.strip()}: {value.strip()}") from exc
            if barrier <= 0:
                raise RuntimeError(f"Barrier for {key.strip()} must be greater than zero.")
            values[key.strip().upper()] = barrier
        key = symbol.strip().upper()
        if key not in values:
            raise RuntimeError(f"No barrier configured for {symbol}. Refusing to trade with an unknown barrier.")
        return values[key]


@lru_cache
def get_settings() -> Settings:
    return Settings()
