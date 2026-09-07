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

BUG FIXED HERE (2026-08-31): "cache forever" above was never meant to
cover a run that abstained because the LLM backend itself was unreachable
mid-question (pipeline.py's ABSTAIN_LLM_SYNTHESIS / ABSTAIN_LLM_DEAD) --
that is a transient infrastructure hiccup, not a fact about the literature.
Confirmed against a real stored run: a question that hit a total NIM
outage during synthesis was recorded as an abstention, and every later ask
of that EXACT question silently replayed that same "no answer" forever --
looking exactly like "this question doesn't work" -- because find_cached()
had no way to tell a transient failure apart from a legitimate cached
answer, and nothing in the UI hints that the "force a fresh run" checkbox
is the way out. record_run()'s new `cacheable` flag (set by the caller --
api/server.py knows the pipeline's abstain-reason vocabulary, this module
deliberately does not) marks that kind of row un-servable from the cache;
find_cached() skips straight past it to the next real result, or to None
(a fresh live run) if there isn't one. The row is NOT deleted -- it still
shows up in plain history, funnel and all, exactly as it happened.
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
    source        TEXT NOT NULL,
    cacheable     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_runs_cache_key ON runs (cache_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs (created_at DESC);
"""

#: Substrings of pipeline.py's ABSTAIN_LLM_SYNTHESIS / ABSTAIN_LLM_DEAD --
#: duplicated here (not imported) so this module stays dependency-free of
#: pipeline.py; used ONLY by the one-time migration below to retroactively
#: un-stick any transient-failure row that predates the `cacheable` column.
#: New rows get their `cacheable` flag from the caller (api/server.py),
#: which is the single source of truth going forward.
_TRANSIENT_ABSTAIN_MARKERS = ("LLM unavailable",)

_write_lock = threading.Lock()


def _migrate_cacheable_column(conn: sqlite3.Connection) -> None:
    """Add the `cacheable` column to a pre-existing DB that predates it,
    then retroactively mark any already-stored transient-LLM-failure
    abstention as not cacheable -- otherwise that exact question would stay
    silently stuck replaying "no answer" forever, which is the bug this
    migration exists to fix for databases that already hit it.

    A no-op (early return) once the column exists -- ALTER TABLE only runs
    the one time a pre-migration DB is opened.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "cacheable" in columns:
        return
    conn.execute("ALTER TABLE runs ADD COLUMN cacheable INTEGER NOT NULL DEFAULT 1")
    like_clauses = " OR ".join(["report_json LIKE ?"] * len(_TRANSIENT_ABSTAIN_MARKERS))
    params = [f"%{marker}%" for marker in _TRANSIENT_ABSTAIN_MARKERS]
    changed = conn.execute(
        f"UPDATE runs SET cacheable = 0 WHERE abstained = 1 AND ({like_clauses})",
        params,
    ).rowcount
    conn.commit()
    if changed:
        print(f"[store] migration: marked {changed} pre-existing transient-failure row(s) as not cacheable")


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
    _migrate_cacheable_column(conn)
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
    """Most recent CACHEABLE run for this cache_key, or None. Full report
    included.

    Skips past any row recorded with cacheable=0 (a transient-LLM-failure
    abstention -- see the module docstring's BUG FIXED note): that kind of
    row is real history, but replaying it as "the answer" to this question
    forever would be wrong, so a cache miss here correctly triggers a fresh
    live run instead of resurrecting an infrastructure hiccup.
    """
    row = conn.execute(
        "SELECT * FROM runs WHERE cache_key = ? AND cacheable = 1 "
        "ORDER BY created_at DESC LIMIT 1",
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
    cacheable: bool = True,
) -> str:
    """Insert one new row (never an upsert -- see the module docstring).

    cacheable: False marks this row as real history but never servable by
    find_cached() -- for a transient-infrastructure abstention that
    shouldn't be memorized as "the answer" to this question (see the
    module docstring). The caller decides this (api/server.py knows the
    pipeline's abstain-reason vocabulary); defaults to True so every
    existing caller keeps today's "cache every result" behavior unless it
    opts out.
    """
    run_id = uuid.uuid4().hex
    with _write_lock:
        conn.execute(
            "INSERT INTO runs (id, question, cache_key, report_json, abstained, created_at, source, cacheable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, question, cache_key, json.dumps(report, ensure_ascii=False),
                int(abstained), _now_iso(), source, int(cacheable),
            ),
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
