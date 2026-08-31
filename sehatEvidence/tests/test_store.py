"""
Offline test for core/store.py -- no network, no LLM, no pytest.

Run from the project root (sehatEvidence/):
    python -m tests.test_store

Plain-script convention (same as tests/test_pipeline.py). Uses an
in-memory SQLite database (":memory:") -- fast, and every test starts
from a fresh connection so nothing leaks between cases.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.store as store_module
from core.store import (
    RUN_ID_PATTERN,
    clear_all,
    connect,
    delete_run,
    find_cached,
    get_run,
    list_runs,
    normalize_question,
    record_run,
)

SAMPLE_REPORT = {
    "question": "Does metformin reduce all-cause mortality in type 2 diabetes?",
    "abstained": False,
    "abstain_reasons": [],
    "funnel": {"claims_generated": 3, "claims_deleted": 1, "claims_kept": 2, "by_reason": {}},
    "answer_text": "Metformin was associated with lower mortality [S1].",
    "claims": [],
    "evidence": [],
    "disclaimer": "not a medical device",
    "queries": ["metformin mortality type 2 diabetes"],
    "synthesizer_parse_deletions": [],
}


def t01_normalize_question():
    assert normalize_question("Does Metformin reduce mortality??") == normalize_question(
        "does metformin   reduce mortality"
    )
    assert normalize_question("  Trailing space  ") == "trailing space"
    print("PASS 01: normalize_question collapses case/punctuation/whitespace")


def t02_record_and_find_cached():
    conn = connect(":memory:")
    key = normalize_question(SAMPLE_REPORT["question"])
    run_id = record_run(
        conn, question=SAMPLE_REPORT["question"], cache_key=key,
        report=SAMPLE_REPORT, abstained=False, source="live",
    )
    assert RUN_ID_PATTERN.match(run_id), run_id

    hit = find_cached(conn, key)
    assert hit is not None
    assert hit["report"]["answer_text"] == SAMPLE_REPORT["answer_text"]
    assert hit["source"] == "live"
    assert hit["abstained"] is False

    miss = find_cached(conn, "some other question entirely")
    assert miss is None
    print("PASS 02: record_run -> find_cached hit; unrelated key -> miss")


def t03_repeat_question_inserts_new_row_not_upsert():
    conn = connect(":memory:")
    key = normalize_question("Q?")
    id1 = record_run(conn, question="Q?", cache_key=key, report=SAMPLE_REPORT, abstained=False, source="live")
    id2 = record_run(conn, question="Q?", cache_key=key, report=SAMPLE_REPORT, abstained=False, source="live")
    assert id1 != id2, "each run must get its own row, never an upsert"
    runs, total = list_runs(conn)
    assert total == 2, total
    # find_cached returns the MOST RECENT row for that key.
    hit = find_cached(conn, key)
    assert hit["id"] == id2
    print("PASS 03: repeat question inserts a new row; cache serves the most recent")


def t04_list_runs_pagination_and_filter():
    conn = connect(":memory:")
    for i in range(5):
        record_run(
            conn, question=f"Q{i}", cache_key=f"q{i}",
            report=SAMPLE_REPORT, abstained=(i % 2 == 0), source="live",
        )
    page1, total = list_runs(conn, limit=2, offset=0)
    assert total == 5
    assert len(page1) == 2
    page2, _ = list_runs(conn, limit=2, offset=2)
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})

    abstained_only, abstained_total = list_runs(conn, abstained=True)
    assert abstained_total == 3, abstained_total  # i=0,2,4
    assert all(r["abstained"] for r in abstained_only)

    # Summaries never carry the full report -- keeps the list endpoint cheap.
    assert "report" not in page1[0]
    print("PASS 04: list_runs paginates, filters by abstained, summaries omit report_json")


def t05_get_run_and_delete():
    conn = connect(":memory:")
    run_id = record_run(
        conn, question="Q", cache_key="q", report=SAMPLE_REPORT, abstained=False, source="live",
    )
    fetched = get_run(conn, run_id)
    assert fetched is not None
    assert fetched["report"] == SAMPLE_REPORT

    assert get_run(conn, "0" * 32) is None  # well-formed but unknown id

    assert delete_run(conn, run_id) is True
    assert get_run(conn, run_id) is None
    assert delete_run(conn, run_id) is False  # already gone
    print("PASS 05: get_run round-trips the full report; delete_run removes it, is idempotent-safe")


def t06_clear_all():
    conn = connect(":memory:")
    for i in range(4):
        record_run(conn, question=f"Q{i}", cache_key=f"q{i}", report=SAMPLE_REPORT, abstained=False, source="live")
    deleted = clear_all(conn)
    assert deleted == 4
    _, total = list_runs(conn)
    assert total == 0
    assert clear_all(conn) == 0
    print("PASS 06: clear_all wipes every row and reports the count")


def t07_source_tagging_mock_vs_live():
    conn = connect(":memory:")
    record_run(conn, question="A", cache_key="a", report=SAMPLE_REPORT, abstained=False, source="mock")
    record_run(conn, question="B", cache_key="b", report=SAMPLE_REPORT, abstained=False, source="live")
    runs, _ = list_runs(conn)
    sources = {r["question"]: r["source"] for r in runs}
    assert sources["A"] == "mock"
    assert sources["B"] == "live"
    print("PASS 07: mock and live runs are tagged distinctly, never conflated")


def t08_cacheable_flag_controls_find_cached():
    """Regression test for the "stuck history question" bug: a transient
    LLM-outage abstention (cacheable=False) must be real, visible history
    but must NEVER be replayed by find_cached() -- otherwise every later
    ask of the exact same question silently gets the same "no answer"
    forever, indistinguishable from "this question is broken"."""
    conn = connect(":memory:")
    key = normalize_question("Transient Q")
    stuck_report = dict(SAMPLE_REPORT, abstained=True, abstain_reasons=["LLM unavailable during synthesis"])
    stuck_id = record_run(
        conn, question="Transient Q", cache_key=key, report=stuck_report,
        abstained=True, source="live", cacheable=False,
    )
    assert find_cached(conn, key) is None, "a non-cacheable row must never be served as a cache hit"
    runs, total = list_runs(conn)
    assert total == 1 and runs[0]["id"] == stuck_id, "the stuck row still shows up in plain history"

    good_id = record_run(
        conn, question="Transient Q", cache_key=key, report=SAMPLE_REPORT,
        abstained=False, source="live", cacheable=True,
    )
    hit = find_cached(conn, key)
    assert hit is not None and hit["id"] == good_id, "a later cacheable row must be served"
    print("PASS 08: cacheable=False rows are real history but never served by find_cached; default stays True")


def t09_migration_backfills_preexisting_transient_failure_rows():
    """Simulate a real pre-existing DB (created before the `cacheable`
    column existed) that already has a stuck transient-LLM-failure row --
    the EXACT shape of the bug found live in this project's own
    evidenceboard.db -- and confirm connect()'s one-time migration
    retroactively un-sticks it without touching any other row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, question TEXT NOT NULL, cache_key TEXT NOT NULL,
            report_json TEXT NOT NULL, abstained INTEGER NOT NULL,
            created_at TEXT NOT NULL, source TEXT NOT NULL
        );
        """
    )
    stuck_report = dict(SAMPLE_REPORT, abstained=True, abstain_reasons=["LLM unavailable during synthesis"])
    conn.execute(
        "INSERT INTO runs (id, question, cache_key, report_json, abstained, created_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("a" * 32, "Stuck Q", "stuck q", json.dumps(stuck_report), 1, "2026-08-30T00:00:00+00:00", "live"),
    )
    conn.execute(
        "INSERT INTO runs (id, question, cache_key, report_json, abstained, created_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("b" * 32, "Fine Q", "fine q", json.dumps(SAMPLE_REPORT), 0, "2026-08-30T00:00:00+00:00", "live"),
    )
    conn.commit()

    store_module._migrate_cacheable_column(conn)  # what connect() runs internally

    assert find_cached(conn, "stuck q") is None, "pre-existing stuck row must be un-cacheable after migration"
    assert find_cached(conn, "fine q") is not None, "an unrelated pre-existing row must stay cacheable"
    print("PASS 09: connect()'s migration retroactively un-sticks a pre-existing transient-failure row")


def run() -> None:
    t01_normalize_question()
    t02_record_and_find_cached()
    t03_repeat_question_inserts_new_row_not_upsert()
    t04_list_runs_pagination_and_filter()
    t05_get_run_and_delete()
    t06_clear_all()
    t07_source_tagging_mock_vs_live()
    t08_cacheable_flag_controls_find_cached()
    t09_migration_backfills_preexisting_transient_failure_rows()
    print("All store tests passed.")


if __name__ == "__main__":
    run()
