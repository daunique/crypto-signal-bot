"""Fallback entrypoint — identical to wsgi.py."""
from gevent import monkey
monkey.patch_all()

import os, logging, threading
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from app import app, socketio

def _bg():
    with app.app_context():
        try:
            from signal_engine import retrain_all
            retrain_all(limit=300)
            logger.info("[INIT] Models ready")
        except Exception as e:
            logger.error(f"[INIT] {e}")

def _sched():
    try:
        from scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        logger.error(f"[INIT] Scheduler: {e}")

def _ensure_new_columns():
    import os
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or "postgresql" not in db_url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, options="-c statement_timeout=60000")
        conn.autocommit = True
        cur = conn.cursor()
        for col, coldef in [
            ("placed_at",             "TIMESTAMP"),
            ("limitless_executed_at", "TIMESTAMP"),
            ("limitless_fill_price",  "REAL"),
        ]:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='signals' AND column_name=%s", (col,)
            )
            if cur.fetchone():
                continue
            cur.execute(f"ALTER TABLE signals ADD COLUMN IF NOT EXISTS {col} {coldef}")
            logger.info("[INIT] signals.%s added OK", col)
        cur.close(); conn.close()
    except Exception as _e:
        logger.warning("[INIT] _ensure_new_columns: %s", _e)

_ensure_new_columns()
threading.Thread(target=_bg, daemon=True).start()
_sched()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
