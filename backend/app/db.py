import logging
from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, Text, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Column name predates the tick strategy (2026-07-26) and is kept
    # as-is to avoid a rename migration -- it now holds the epoch of the
    # tick that triggered the decision, not a 180s candle-boundary epoch.
    # Still unique per decision point; see engine.py's on_decision_tick().
    # Exposed to the API/frontend as "decision_epoch".
    candle_epoch: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    contract_type: Mapped[str] = mapped_column(String(32))
    score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="QUALIFIED")
    reason: Mapped[str] = mapped_column(Text, default="")
    barrier_offset: Mapped[float | None] = mapped_column(Float, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    contract_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(16))
    stake: Mapped[float] = mapped_column(Float)
    payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    entry_spot: Mapped[float | None] = mapped_column(Float, nullable=True)
    barrier: Mapped[str | None] = mapped_column(String(16), nullable=True)


class BotEvent(Base):
    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    level: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)


class RuntimeSetting(Base):
    """Generic key-value store for settings changed from the dashboard
    (currently just bot_mode) that need to persist across restarts without
    a redeploy. BOT_MODE the env var remains the fallback default when no
    override has ever been saved here."""
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


async def get_runtime_setting(key: str, default: str | None = None) -> str | None:
    async with session() as db:
        row = await db.get(RuntimeSetting, key)
        return row.value if row else default


async def set_runtime_setting(key: str, value: str) -> None:
    async with session() as db:
        row = await db.get(RuntimeSetting, key)
        if row:
            row.value = value
        else:
            db.add(RuntimeSetting(key=key, value=value))
        await db.commit()


async def get_effective_bot_mode(settings) -> str:
    """The mode actually in effect: a saved dashboard override if one
    exists, otherwise the BOT_MODE env var's default."""
    override = await get_runtime_setting("bot_mode")
    if override in ("demo", "live"):
        return override
    return settings.bot_mode


async def set_bot_mode_override(mode: str) -> str:
    mode = mode.lower().strip()
    if mode not in ("demo", "live"):
        raise ValueError("mode must be 'demo' or 'live'")
    await set_runtime_setting("bot_mode", mode)
    return mode


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all() only creates brand-new tables -- it won't add a
        # newly-introduced column (barrier_offset, barrier) to a table that
        # already exists from a prior deploy. This project has no migration
        # framework, so do the minimal additive migration by hand. Column
        # existence is checked *before* attempting ALTER TABLE, rather than
        # attempting-and-ignoring-the-error, because on Postgres a failed
        # statement (e.g. "column already exists") poisons the rest of the
        # transaction, unlike on SQLite.
        await _add_column_if_missing(conn, "signals", "barrier_offset", "FLOAT")
        await _add_column_if_missing(conn, "trades", "barrier", "VARCHAR(16)")


async def _add_column_if_missing(conn, table: str, column: str, ddl_type: str):
    def _has_column(sync_conn):
        return column in {c["name"] for c in inspect(sync_conn).get_columns(table)}
    if not await conn.run_sync(_has_column):
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        log.info("DB_MIGRATION added %s.%s", table, column)


def session() -> AsyncSession:
    return SessionLocal()
