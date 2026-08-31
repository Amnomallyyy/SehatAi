"""
App entrypoint. Run with:  uvicorn app.main:app --reload
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .dependencies import AVATAR_DIR, UPLOAD_DIR
from .routers import (
    ai_summaries,
    appointments,
    auth,
    connections,
    conversations,
    messages,
    prescriptions,
    report_access,
    report_comments,
    reports,
    users,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No migrations tool in this stack (Alembic isn't in requirements.txt,
    # and wasn't asked for) -- create_all is the right amount of ceremony
    # for a hackathon MVP. It's a no-op for tables that already exist.
    Base.metadata.create_all(bind=engine)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    yield


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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}
