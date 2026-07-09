"""Fallback entrypoint — identical to wsgi.py."""
from gevent import monkey
monkey.patch_all()

import os, logging, threading
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from app import app, socketio

# (No model-training warm-up needed — the deterministic V2 signal_engine has
# no model to train. signal_engine imports cleanly and is ready immediately.)

def _sched():
    try:
        from scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        logger.error(f"[INIT] Scheduler: {e}")

_sched()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
