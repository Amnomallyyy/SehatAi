"""
frontend_main.py — serves the CareLink web UI.
Run with: uvicorn frontend_main:app --reload --port 3002
(3002 per docker-compose.yml/Dockerfile's port assignment -- 3000 is
sehatai's own webserver, a different service entirely.)

This mounts the Jinja2 template + static files so the browser can reach:
  GET /          → serves templates/index.html
  GET /static/*  → serves static/css/app.css, static/js/app.js
  GET /healthz   → liveness probe for Docker/the gateway

Where the browser finds the APIs is configured here, not hardcoded in
app.js: index.html renders window.SEHAT_CONFIG from these env vars.
  - Local dev (no gateway): the defaults -- each service on its own port.
  - Behind the gateway (docker-compose): same-origin paths (/api,
    /sehatai, /evidence), so the app works on any host name with no CORS.
"""

import hashlib
import os
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = pathlib.Path(__file__).parent

SEHAT_CONFIG = {
    "apiBase": os.environ.get("PUBLIC_API_BASE", "http://localhost:8000"),
    "sehataiBase": os.environ.get("PUBLIC_SEHATAI_BASE", "http://localhost:3000"),
    "evidenceBase": os.environ.get("PUBLIC_EVIDENCE_BASE", "http://localhost:8002"),
}


def _asset_version() -> str:
    """Content hash of the static bundle, appended as ?v= to the CSS/JS
    URLs. Lets the gateway cache /static/* aggressively while a new deploy
    still reaches every browser immediately (the URL changes with the
    content)."""
    digest = hashlib.sha256()
    for path in sorted((BASE / "static").rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return JSONResponse({"status": "ok"})


@app.get("/{full_path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, full_path: str):
    """Serve index.html for all routes — the frontend handles its own navigation."""
    response = templates.TemplateResponse(
        request,
        "index.html",
        {"sehat_config": SEHAT_CONFIG, "asset_version": ASSET_VERSION},
    )
    # The shell itself must never be cached -- it's what points browsers
    # at the current (versioned) assets.
    response.headers["Cache-Control"] = "no-cache"
    return response
