# Render sometimes auto-detects main.py and tries uvicorn.
# This stub redirects everything to the real app in wsgi.py.
from wsgi import app  # noqa: F401
