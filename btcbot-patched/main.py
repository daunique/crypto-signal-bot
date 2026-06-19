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

threading.Thread(target=_bg, daemon=True).start()
_sched()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
