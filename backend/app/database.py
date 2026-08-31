"""
Database setup: SQLAlchemy engine, session factory, declarative Base, and
the `get_db` FastAPI dependency.
"""
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

# Use an absolute path for the SQLite file so the app works no matter what
# directory `uvicorn` is launched from (a relative "./app.db" would silently
# create a different file depending on your current working directory).
BASE_DIR = Path(__file__).resolve().parent
SQLALCHEMY_DATABASE_URL = f"sqlite:///{BASE_DIR / 'app.db'}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # SQLite only allows a connection to be used by the thread that created
    # it by default. FastAPI can call a dependency from a different thread
    # than the one that opened the connection, so this flag is required.
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite does not enforce FOREIGN KEY constraints unless this pragma is
    set on every connection. Without it, e.g. a Report could silently point
    at a conversation_id that doesn't exist."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


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
