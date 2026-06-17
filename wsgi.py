"""
Render/Gunicorn entrypoint.
gevent monkey_patch MUST be first before any other import.
psycogreen makes psycopg2 gevent-compatible (patches its wait callback).
"""
from gevent import monkey
monkey.patch_all()

# Make psycopg2 cooperate with gevent — must come right after monkey.patch_all()
try:
    from psycogreen.gevent import patch_psycopg
    patch_psycopg()
except ImportError:
    pass  # psycogreen not installed — will work but may block briefly on DB calls

import os
import logging
import threading

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from app import app, socketio


def _background_init():
    with app.app_context():
        try:
            logger.info("[INIT] Training ML models for all 6 pairs...")
            from signal_engine import retrain_all
            retrain_all(limit=960)
            logger.info("[INIT] All models ready — signals enabled")
        except Exception as e:
            logger.error(f"[INIT] Model training failed: {e}")


def _start_scheduler():
    try:
        from scheduler import start_scheduler
        start_scheduler()
        logger.info("[INIT] Scheduler started")
    except Exception as e:
        logger.error(f"[INIT] Scheduler failed: {e}")


def _ensure_new_columns():
    """
    Direct psycopg2 DDL — runs BEFORE the scheduler starts.

    Adds the three columns introduced in v3.4 (placed_at,
    limitless_executed_at, limitless_fill_price) to an existing DB that
    was deployed before those columns existed.

    Why direct psycopg2 and not SQLAlchemy _add_column()?
      • SQLAlchemy's connection pool inherits the server's statement_timeout
        (often 5-10 s on Render free tier).  A fresh psycopg2 connection with
        options='-c statement_timeout=60s' overrides that at the TCP handshake,
        before any SQL is sent.
      • autocommit=True means each ALTER TABLE is its own transaction — a
        timeout on one column cannot abort the others.
      • The existence check (information_schema.columns) is a zero-lock
        catalog read that returns in <5 ms — so re-deploys where columns
        already exist cost nothing.
    """
    import os
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or "postgresql" not in db_url:
        return  # SQLite / no DB — skip
    try:
        import psycopg2
        # dsn: swap postgres:// → postgresql:// if needed, then strip the scheme
        # psycopg2 accepts a full DSN string directly
        dsn = db_url.replace("postgresql://", "postgres://", 1)
        conn = psycopg2.connect(
            dsn,
            options="-c statement_timeout=60000"  # 60 s per statement
        )
        conn.autocommit = True
        cur = conn.cursor()

        NEW_COLS = [
            ("placed_at",             "TIMESTAMP"),
            ("limitless_executed_at", "TIMESTAMP"),
            ("limitless_fill_price",  "REAL"),
        ]
        for col, coldef in NEW_COLS:
            # Instant catalog check — no locks
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='signals' AND column_name=%s",
                (col,)
            )
            if cur.fetchone():
                logger.info("[INIT] signals.%s already exists — skip", col)
                continue
            cur.execute(
                f"ALTER TABLE signals ADD COLUMN IF NOT EXISTS {col} {coldef}"
            )
            logger.info("[INIT] signals.%s added OK", col)

        cur.close()
        conn.close()
    except Exception as _e:
        # Non-fatal — scheduler still starts; columns will be added on next
        # successful migration cycle.
        logger.warning("[INIT] _ensure_new_columns failed: %s", _e)


_ensure_new_columns()
threading.Thread(target=_background_init, daemon=True).start()
_start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
