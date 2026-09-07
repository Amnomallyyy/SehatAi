"""
Database setup: SQLAlchemy engine, session factory, declarative Base, and
the `get_db` FastAPI dependency.

MIGRATED (architecture doc §02) -- this used to be a local SQLite file,
isolated from every other service in the system. It now points at the same
Supabase Postgres project SehatAI and DataFetch already use, so CareLink's
own tables (users, connections, conversations, ...) and the bridge tables
this migration adds (see models.py's BRIDGE TABLES section) live in one
place. Confirmed with the team before this switch, per the architecture
doc's own warning -- this is live, shared data now, not a throwaway local
file.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# database.py is imported first, before any router (including ai.py, the
# only other place that used to call this) -- load .env here so
# DATABASE_URL is available no matter what import order pulls this module
# in first. Safe to call more than once; python-dotenv no-ops if the
# environment is already populated (e.g. real env vars in production).
load_dotenv()

# Get this from the Supabase dashboard: Settings -> Database -> Connection
# string -> URI (use the "Session pooler" URI, port 5432/6543 -- NOT the
# anon/service_role API keys in the main .env, which are for Supabase's
# REST API, not a direct Postgres connection). Put it in backend/.env as:
#   DATABASE_URL=postgresql+psycopg2://postgres.xxxx:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
# Fails loudly rather than silently falling back to the old SQLite file --
# a quiet fallback here would look like the migration succeeded when it
# didn't, and the whole point of this change is that everyone reads/writes
# the same database.
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL")
if not SQLALCHEMY_DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. CareLink's backend now requires the shared Supabase "
        "Postgres connection string (Settings -> Database -> Connection string -> URI in "
        "the Supabase dashboard) -- see this file's doc comment. The old local-SQLite "
        "fallback was removed on purpose: this app's tables are meant to live alongside "
        "SehatAI's and DataFetch's now, not in an isolated file again."
    )

# Postgres, unlike SQLite, enforces FOREIGN KEY constraints natively on
# every connection -- the old _enable_sqlite_foreign_keys PRAGMA listener
# this file used to need is gone, not because it stopped mattering, but
# because Postgres never needed it in the first place.
engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Yields a request-scoped SQLAlchemy session and guarantees it's closed
    afterwards, even if the request raises."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
