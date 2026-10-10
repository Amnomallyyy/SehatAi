"""
App entrypoint. Run with:  uvicorn app.main:app --reload
"""
import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .database import Base, engine
from .dependencies import AVATAR_DIR, UPLOAD_DIR
from .routers import (
    ai_summaries,
    appointment_slots,
    appointments,
    auth,
    connections,
    conversations,
    emergency,
    intake,
    lab_reports,
    medicines,
    messages,
    prescriptions,
    report_access,
    report_comments,
    reports,
    sehatai_bridge,
    structured_reports,
    users,
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("carelink")


def _enable_row_level_security() -> None:
    """Supabase publishes every public table through its REST API, where the
    browser-safe publishable/anon key can read any table with RLS off --
    found live: the 13 tables create_all() makes (users with password
    hashes, messages, reports, ...) were all readable that way. RLS with no
    policies closes that: this API connects as the table owner and is
    unaffected, while the public key reads nothing. Idempotent; on plain
    Postgres (CI, local QA) it's harmless."""
    if engine.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))
        except Exception as exc:  # e.g. a bridge table owned by another role
            logger.warning("Could not enable RLS on %s: %s", table.name, exc)


async def _medicine_expiry_loop() -> None:
    """Once an hour, flip finished medicines to inactive so the bots (which
    only read `active`) stop treating them as current."""
    from .database import SessionLocal
    from .routers.medicines import expire_finished_medicines

    def sweep() -> None:
        db = SessionLocal()
        try:
            expire_finished_medicines(db)
        except Exception:
            logger.exception("Medicine expiry sweep failed")
        finally:
            db.close()

    while True:
        await asyncio.to_thread(sweep)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No migrations tool in this stack (Alembic isn't in requirements.txt,
    # and wasn't asked for) -- create_all is the right amount of ceremony
    # for a hackathon MVP. It's a no-op for tables that already exist.
    Base.metadata.create_all(bind=engine)
    _enable_row_level_security()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    sweeper = asyncio.create_task(_medicine_expiry_loop())
    try:
        yield
    finally:
        sweeper.cancel()


app = FastAPI(
    title="Doctor-Patient Communication Portal API",
    description="Backend for a doctor-patient chat, report sharing, and AI-summary review portal.",
    version="1.0.0",
    lifespan=lifespan,
)

# The frontend is a separate app on its own origin, built independently, so
# CORS has to be wide open. This is safe to leave permissive here (rather
# than the usual "lock down allow_origins in production" caveat) because
# auth is a Bearer token in an Authorization header, not a cookie --
# allow_credentials stays False, so there's no CSRF-relevant credential
# state for a wildcard origin to expose.
#
# CORS_ALLOW_ORIGINS narrows it (comma-separated) when the deployment wants
# to -- behind the gateway everything is same-origin and CORS never fires.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
# JSON lists (conversations, markers, history) compress ~5-10x.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Request id + one access-log line with latency for every request, and
    a clean JSON 500 (with that id) instead of a bare stack trace if a
    route raises something unexpected."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error [%s] %s %s", request_id, request.method, request.url.path)
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info("%s %s -> %s %.0fms [%s]", request.method, request.url.path, response.status_code, elapsed_ms, request_id)
    return response

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(connections.router)
app.include_router(conversations.router)
app.include_router(messages.router)
app.include_router(reports.router)
app.include_router(ai_summaries.router)
app.include_router(report_comments.router)
app.include_router(report_access.router)
app.include_router(prescriptions.router)
app.include_router(appointments.router)
app.include_router(sehatai_bridge.router)
app.include_router(lab_reports.router)
app.include_router(structured_reports.router)
app.include_router(medicines.router)
app.include_router(intake.router)
app.include_router(appointment_slots.router)
app.include_router(emergency.router)


@app.get("/health/live", tags=["health"])
def liveness():
    """Process is up -- no dependencies checked (container liveness)."""
    return {"status": "ok"}


@app.get("/health", tags=["health"])
def health_check():
    """Readiness: the database answers. 503 when it doesn't, so the
    gateway/orchestrator stops routing here instead of serving errors."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Health check: database unreachable: %s", exc)
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "unreachable"})
    return {"status": "ok", "database": "ok"}
