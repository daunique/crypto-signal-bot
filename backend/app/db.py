import logging
from datetime import datetime, timezone, date
from sqlalchemy import String, Float, Integer, DateTime, Text, Date, inspect, text, delete, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from .config import get_settings
from .pnl_tracker import PnLTrackState

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
    # Historically named for the old 180s-candle strategy (candle boundary
    # epoch); repurposed, unchanged, to hold the entry TICK epoch for the
    # volatility-timing strategy. Left as-is rather than renamed to avoid an
    # unnecessary destructive migration -- it is still a unique epoch marking
    # one signal opportunity, which is all the de-duplication check in
    # engine.py actually needs.
    candle_epoch: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    contract_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="QUALIFIED")
    reason: Mapped[str] = mapped_column(Text, default="")
    barrier_offset: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The rolling realized-volatility reading that qualified this signal, and
    # the trailing-percentile threshold it cleared -- kept for auditability
    # (the old EMA strategy's integer "score" column is gone; these are its
    # replacement, and are more directly meaningful for this strategy).
    current_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    vol_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    # Which PnL bucket this trade was recorded against, set at the moment
    # the trade opened -- see pnl_tracker.py. "main" for any trade not
    # using the main/sub overlay (kept nullable so pre-existing rows from
    # before this feature don't need backfilling).
    pnl_track: Mapped[str | None] = mapped_column(String(16), nullable=True)


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


class VolSample(Base):
    """A once-per-30-ticks sample of rolling realized volatility, kept for
    `vol_trailing_days` (config) so the volatility-percentile threshold can be
    recomputed from a *persisted* trailing window across restarts, instead of
    only from whatever has accumulated in-process since the bot last started.

    Sampling every ~30 ticks (not every tick) keeps this table's size sane --
    ~90 days x ~1440 samples/day is a few hundred thousand rows, not the ~3.9M
    ticks/symbol that a full-resolution version would need for 90 days at R_25's
    ~2s tick rate.
    """
    __tablename__ = "vol_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    vol: Mapped[float] = mapped_column(Float)


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


async def prune_old_vol_samples(symbol: str, keep_days: int) -> None:
    """Delete VolSample rows older than `keep_days` (plus a small buffer) so
    this table doesn't grow unbounded over months of uptime."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=keep_days + 5)
    async with session() as db:
        await db.execute(delete(VolSample).where(VolSample.symbol == symbol, VolSample.day < cutoff))
        await db.commit()


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all() only creates brand-new tables -- it won't add a
        # newly-introduced column (barrier_offset, barrier, current_vol,
        # vol_threshold) to a table that already exists from a prior deploy.
        # This project has no migration framework, so do the minimal
        # additive migration by hand. Column existence is checked *before*
        # attempting ALTER TABLE, rather than attempting-and-ignoring-the-
        # error, because on Postgres a failed statement (e.g. "column
        # already exists") poisons the rest of the transaction, unlike on
        # SQLite.
        await _add_column_if_missing(conn, "signals", "barrier_offset", "FLOAT")
        await _add_column_if_missing(conn, "signals", "current_vol", "FLOAT")
        await _add_column_if_missing(conn, "signals", "vol_threshold", "FLOAT")
        await _add_column_if_missing(conn, "trades", "barrier", "VARCHAR(16)")
        await _add_column_if_missing(conn, "trades", "pnl_track", "VARCHAR(16)")
        # The reverse problem: a column that's IN an existing live database
        # (from a schema this table had before) but is no longer in this
        # code's model at all -- e.g. `signals.score`, an Integer NOT NULL
        # column from the pre-2026-07-29 EMA-strategy schema. Adding columns
        # never breaks anything, but a leftover NOT NULL column with no
        # default does: every INSERT that doesn't mention it fails outright
        # (sqlite3.IntegrityError / IntegrityError on Postgres) -- which is
        # exactly what happened here (2026-07-30 22:00 UTC: every qualifying
        # signal crashed on insert, which uncaught propagated out of
        # on_tick() and looked like a connection failure, repeatedly kicking
        # the engine into RECONNECTING). Dropped rather than made nullable
        # because it's not just unused by accident -- nothing in this
        # strategy has a "score" concept to put there.
        for table_name in ("signals", "trades"):
            await _drop_unknown_columns(conn, table_name)


_PNL_TRACK_KEYS = ("pnl_track", "pnl_track_delta", "pnl_track_consec_losses", "pnl_track_auto_demo")


async def load_pnl_track_state():
    """Loads the persisted main/sub PnL-track state (see pnl_tracker.py),
    defaulting to a fresh "main" state if this is the first time it's been
    read (e.g. right after this feature is first deployed)."""
    async with session() as db:
        rows = {}
        for key in _PNL_TRACK_KEYS:
            row = await db.get(RuntimeSetting, key)
            rows[key] = row.value if row else None
    return PnLTrackState(
        track=rows["pnl_track"] or "main",
        delta=float(rows["pnl_track_delta"]) if rows["pnl_track_delta"] is not None else 0.0,
        consec_losses=int(rows["pnl_track_consec_losses"]) if rows["pnl_track_consec_losses"] is not None else 0,
        auto_demo_active=(rows["pnl_track_auto_demo"] == "true"),
    )


async def save_pnl_track_state(state) -> None:
    await set_runtime_setting("pnl_track", state.track)
    await set_runtime_setting("pnl_track_delta", repr(state.delta))
    await set_runtime_setting("pnl_track_consec_losses", str(state.consec_losses))
    await set_runtime_setting("pnl_track_auto_demo", "true" if state.auto_demo_active else "false")


async def get_track_pnl_totals() -> dict:
    """All-time (not daily) PnL summed per track, from settled trades only --
    this is a lifetime/cumulative view by design, since the $-profit target
    that moves a trade between tracks is an absolute dollar amount, not a
    daily one."""
    async with session() as db:
        rows = (await db.execute(
            select(Trade.pnl_track, func.sum(Trade.profit))
            .where(Trade.status.in_(("WON", "LOST")))
            .group_by(Trade.pnl_track)
        )).all()
    totals = {"main": 0.0, "sub": 0.0}
    for track, total in rows:
        if track in totals and total is not None:
            totals[track] = float(total)
    return totals


async def _add_column_if_missing(conn, table: str, column: str, ddl_type: str):
    def _has_column(sync_conn):
        return column in {c["name"] for c in inspect(sync_conn).get_columns(table)}
    if not await conn.run_sync(_has_column):
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        log.info("DB_MIGRATION added %s.%s", table, column)


async def _drop_unknown_columns(conn, table: str):
    """Drops any column present in the live database but absent from this
    table's current model -- see the long comment in init_db() for why this
    matters (a leftover NOT NULL column with no default breaks every
    INSERT, not just ones that happen to touch it). Requires SQLite
    3.35+ (2021) or Postgres, both of which support DROP COLUMN directly;
    if it fails for any reason, this logs and leaves the column in place
    rather than raising, since a redundant unused column existing is far
    less bad than the migration step itself crashing startup.
    """
    known_columns = set(Base.metadata.tables[table].columns.keys())
    def _get_columns(sync_conn):
        return [c["name"] for c in inspect(sync_conn).get_columns(table)]
    existing_columns = await conn.run_sync(_get_columns)
    for column in existing_columns:
        if column in known_columns:
            continue
        try:
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
            log.info("DB_MIGRATION dropped stale column %s.%s", table, column)
        except Exception:
            log.exception("Could not drop stale column %s.%s -- leaving it in place", table, column)


def session() -> AsyncSession:
    return SessionLocal()
