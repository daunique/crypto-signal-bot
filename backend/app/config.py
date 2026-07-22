from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "deriv-higher-lower-bot"
    environment: str = "production"
    host: str = "0.0.0.0"
    port: int = 8080

    database_url: str = "sqlite+aiosqlite:///./data/bot.db"

    # New Deriv API authentication
    deriv_app_id: str = ""
    deriv_api_token: str = ""
    deriv_pat: str = ""
    deriv_token: str = ""  # backward-compatible alias
    deriv_demo_token: str = ""  # backward-compatible alias
    deriv_live_token: str = ""  # backward-compatible alias
    deriv_account_id: str = ""
    bot_mode: str = "demo"

    market_symbol: str = "R_25"
    timeframe_seconds: int = 180
    history_count: int = 120
    stake: float = 1.0
    currency: str = "USD"
    # No universal barrier. Configure each symbol explicitly.
    market_barriers: str = ""
    min_confluence_score: int = 6
    auto_trade: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    def barrier_for_symbol(self, symbol: str) -> float:
        """Return the explicitly configured barrier for a symbol.

        MARKET_BARRIERS format: R_25=0.375,R_10=0.20,R_100=1.00
        No fallback is allowed because barrier values are market-specific.
        """
        raw = self.market_barriers.strip()
        if not raw:
            raise RuntimeError(
                f"No market barrier configuration found. Configure MARKET_BARRIERS for {symbol}."
            )
        values = {}
        for item in raw.split(','):
            item = item.strip()
            if not item:
                continue
            if '=' not in item:
                raise RuntimeError(
                    f"Invalid MARKET_BARRIERS entry '{item}'. Expected SYMBOL=VALUE."
                )
            key, value = item.split('=', 1)
            values[key.strip().upper()] = float(value.strip())
        key = symbol.strip().upper()
        if key not in values:
            raise RuntimeError(
                f"No barrier configured for {symbol}. Refusing to trade with an unknown barrier."
            )
        barrier = values[key]
        if barrier <= 0:
            raise RuntimeError(f"Barrier for {symbol} must be greater than zero.")
        return barrier

    @property
    def auth_token(self) -> str:
        # Prefer the clean PAT name, while supporting names used by earlier builds.
        return (
            self.deriv_pat.strip()
            or self.deriv_api_token.strip()
            or self.deriv_token.strip()
            or (self.deriv_live_token if self.bot_mode.lower() == "live" else self.deriv_demo_token).strip()
        )

    @property
    def account_type(self) -> str:
        return "real" if self.bot_mode.lower() == "live" else "demo"


@lru_cache
def get_settings() -> Settings:
    return Settings()
