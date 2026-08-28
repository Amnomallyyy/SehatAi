"""
core/store.py -- SQLite-backed history + response cache.

One table, two views of it: "history" is "list runs by time," "cache" is
"look up the newest run for this question." A repeat question replays a
stored report instead of re-running the whole pipeline -- deliberately
useful given how slow/flaky the free-tier LLM backend can be (see
api/server.py's _handle_ask_stream, which checks the cache BEFORE calling
EvidencePipeline.run() and never fakes stage events for a cache hit).

Design choices, stated explicitly (see the implementation plan):
  - Cache forever, no TTL. Literature for a fixed question doesn't change
    minute-to-minute; a silent background expiry would undermine the
    whole point of avoiding the slow-LLM tax. Callers pass force_refresh
    to bypass the cache explicitly.
  - Every live/mock run INSERTS a new row rather than upserting -- full
    history survives even across repeated identical questions.
  - All SQL uses ? placeholders exclusively. Never string-interpolate a
    caller-supplied value into a query.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

__all__ = [
    "RUN_ID_PATTERN",
    "connect",
    "normalize_question",
    "find_cached",
    "record_run",
    "list_runs",
    "get_run",
    "delete_run",
    "clear_all",
]

#: uuid4 hex, no dashes -- what record_run() mints and every /api/history/{id}
#: route validates a path segment against before it ever reaches SQLite.
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    question      TEXT NOT NULL,
    cache_key     TEXT NOT NULL,
    report_json   TEXT NOT NULL,
    abstained     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    source        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_cache_key ON runs (cache_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);
"""

_write_lock = threading.Lock()


def connect(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the history/cache database.

    check_same_thread=False because EvidenceHandler serves requests on a
    ThreadingHTTPServer's worker threads, all sharing one connection;
    writes still serialize through `_write_lock` (same pattern as
    core/cache.py's TokenBucket) since sqlite3 connections are not
    inherently thread-safe for concurrent writes.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def normalize_question(question: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    A pragmatic cache-key heuristic, not semantic matching -- "Does X
    help Y??" and "does x help y" collide deliberately (the pipeline's
    output is the same either way); a near-miss just re-runs live, which
    is a cheap failure mode, never a wrong answer.
    """
    lowered = question.strip().lower()
    stripped = re.sub(r"[^\w\s]", "", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "question": row["question"],
        "abstained": bool(row["abstained"]),
        "created_at": row["created_at"],
        "source": row["source"],
    }


def find_cached(conn: sqlite3.Connection, cache_key: str) -> Optional[dict]:
    """Most recent run for this cache_key, or None. Full report included."""
    row = conn.execute(
        "SELECT * FROM runs WHERE cache_key = ? ORDER BY created_at DESC LIMIT 1",
        (cache_key,),
    ).fetchone()
    if row is None:
        return None
    summary = _row_to_summary(row)
    summary["report"] = json.loads(row["report_json"])
    return summary


def record_run(
    conn: sqlite3.Connection,
    *,
    question: str,
    cache_key: str,
    report: dict,
    abstained: bool,
    source: str,
) -> str:
    """Insert one new row (never an upsert -- see the module docstring)."""
    run_id = uuid.uuid4().hex
    with _write_lock:
        conn.execute(
            "INSERT INTO runs (id, question, cache_key, report_json, abstained, created_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, question, cache_key, json.dumps(report, ensure_ascii=False), int(abstained), _now_iso(), source),
        )
        conn.commit()
    return run_id


def list_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    abstained: Optional[bool] = None,
) -> tuple[list[dict], int]:
    """Summaries only (no report_json) -- keeps this endpoint cheap even
    with a large history. Returns (page, total_matching_count)."""
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    where = ""
    params: list[Any] = []
    if abstained is not None:
        where = "WHERE abstained = ?"
        params.append(int(abstained))

    total = conn.execute(f"SELECT COUNT(*) FROM runs {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, question, abstained, created_at, source FROM runs {where} "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_summary(r) for r in rows], total


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    summary = _row_to_summary(row)
    summary["report"] = json.loads(row["report_json"])
    return summary


def delete_run(conn: sqlite3.Connection, run_id: str) -> bool:
    with _write_lock:
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        conn.commit()
    return cur.rowcount > 0


def clear_all(conn: sqlite3.Connection) -> int:
    with _write_lock:
        cur = conn.execute("DELETE FROM runs")
        conn.commit()
    return cur.rowcount
