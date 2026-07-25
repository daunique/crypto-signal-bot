from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .config import get_settings, BUILD_VERSION
from .db import init_db
from .api import router

settings = get_settings()
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
    # Cache-bust static assets with the build version so a redeploy can
    # never leave a browser (or an intermediate CDN) serving a stale
    # styles.css/app.js against a fresh index.html -- a mismatch like that
    # previously left brand-new markup (e.g. nav icons) completely
    # unstyled, rendering at raw browser-default size, while classes that
    # happened to exist in both the old and new stylesheet still looked
    # fine. Different query strings are different cache keys everywhere,
    # so this forces a fresh fetch only when the version actually changes.
    html = (frontend / "index.html").read_text()
    html = html.replace('href="/static/styles.css"', f'href="/static/styles.css?v={BUILD_VERSION}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={BUILD_VERSION}"')
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok", "build": BUILD_VERSION}
