from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .config import get_settings
from .db import init_db
from .api import router

settings = get_settings()
BUILD_VERSION = "2026-07-23-invalid-barrier-adaptive-retry-2-widened"
app = FastAPI(title=settings.app_name)
app.include_router(router)

frontend = Path(__file__).resolve().parents[2] / "frontend"
if frontend.exists():
    app.mount("/static", StaticFiles(directory=frontend), name="static")


@app.on_event("startup")
async def startup():
    Path("data").mkdir(exist_ok=True)
    await init_db()
    import logging
    logging.getLogger(__name__).info("BUILD_VERSION=%s", BUILD_VERSION)


@app.get("/")
async def index():
    return FileResponse(frontend / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "build": BUILD_VERSION}
