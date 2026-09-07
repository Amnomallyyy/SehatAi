"""
frontend_main.py — Drop this into the project root alongside app/.
Run with: uvicorn frontend_main:app --reload --port 3002
(3002 per docker-compose.yml/Dockerfile's port assignment -- 3000 is
sehatai's own webserver, a different service entirely.)

This mounts the Jinja2 template + static files so the browser can reach:
  GET /          → serves templates/index.html
  GET /static/*  → serves static/css/app.css, static/js/app.js

The backend API must be running separately on port 8000 (uvicorn app.main:app --reload).
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pathlib

BASE = pathlib.Path(__file__).parent

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.get("/{full_path:path}", response_class=HTMLResponse)
async def catch_all(request: Request, full_path: str):
    """Serve index.html for all routes — the frontend handles its own navigation."""
    return templates.TemplateResponse(request, "index.html")