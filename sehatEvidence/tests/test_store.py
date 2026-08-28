"""
Offline test for core/store.py -- no network, no LLM, no pytest.

Run from the project root (sehatEvidence/):
    python -m tests.test_store

Plain-script convention (same as tests/test_pipeline.py). Uses an
in-memory SQLite database (":memory:") -- fast, and every test starts
from a fresh connection so nothing leaks between cases.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def run() -> None:
    t01_normalize_question()
    t02_record_and_find_cached()
    t03_repeat_question_inserts_new_row_not_upsert()
    t04_list_runs_pagination_and_filter()
    t05_get_run_and_delete()
    t06_clear_all()
    t07_source_tagging_mock_vs_live()
    print("All store tests passed.")


if __name__ == "__main__":
    run()
