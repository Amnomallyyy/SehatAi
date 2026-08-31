"""
tests/test_clinicaltrials_relaxation.py -- offline, scripted-session test
for ClinicalTrialsClient.search_relaxed().

Run: python -m tests.test_clinicaltrials_relaxation

Reproduces the exact collapse-then-recovery scenario found live on
2026-08-30 (ClinicalTrials.gov's `query.term` implicitly ANDs every word,
same failure class as PubMed's Automatic Term Mapping): the query
"andexanet alfa dosing regimen apixaban reversal FDA approved" returned 0
studies, while "andexanet alfa dosing" returned 9. No network required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.clinicaltrials import ClinicalTrialsClient


def make_study(nct_id: str, title: str = "A study") -> dict:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": title},
            "statusModule": {"overallStatus": "COMPLETED"},
            "descriptionModule": {"briefSummary": "A brief summary."},
        }
    }


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class ScriptedSession:
    """Routes on the exact `query.term` sent; every other param ignored
    for routing purposes."""

    def __init__(self, routes: dict[str, list[dict]]):
        self.routes = routes
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        term = (params or {}).get("query.term")
        if term not in self.routes:
            raise AssertionError(f"ScriptedSession got an unscripted query.term: {term!r}")
        return FakeResponse({"studies": self.routes[term]})


def make_client(session):
    return ClinicalTrialsClient(session=session)


def t01_collapse_recovers_by_dropping_trailing_words():
    q = "andexanet alfa dosing regimen apixaban reversal FDA approved"
    session = ScriptedSession(
        {
            "andexanet alfa dosing regimen apixaban reversal FDA approved": [],
            "andexanet alfa dosing regimen apixaban reversal FDA": [],
            "andexanet alfa dosing regimen apixaban reversal": [make_study("NCT00000001")],
            "andexanet alfa dosing regimen apixaban": [make_study("NCT00000001")],
            "andexanet alfa dosing regimen": [make_study("NCT00000001"), make_study("NCT00000002")],
            "andexanet alfa dosing": [
                make_study("NCT00000001"), make_study("NCT00000002"), make_study("NCT00000003"),
            ],
        }
    )
    client = make_client(session)
    results = client.search_relaxed(q, page_size=3, max_pages=1, statuses=None)
    ids = [r.native_id for r in results]
    assert "NCT00000001" in ids and "NCT00000002" in ids and "NCT00000003" in ids, ids
    print(f"PASS 01: 0 hits -> progressively drops trailing words -> recovers {len(results)} studies: {ids}")


def t02_healthy_query_never_relaxes():
    q = "apixaban"
    session = ScriptedSession({q: [make_study(f"NCT{i:08d}") for i in range(5)]})
    client = make_client(session)
    results = client.search_relaxed(q, page_size=5, max_pages=1, statuses=None)
    assert len(results) == 5
    assert len(session.calls) == 1, f"a healthy query must issue exactly one call, got {len(session.calls)}"
    print("PASS 02: a healthy (already-above-floor) query issues zero extra probe calls")


def t03_min_words_guard():
    q = "one two"
    session = ScriptedSession({q: []})
    client = make_client(session)
    results = client.search_relaxed(q, page_size=5, max_pages=1, statuses=None, min_words=3)
    assert results == []
    assert len(session.calls) == 1, "a 2-word query must never be relaxed below min_words=3"
    print("PASS 03: min_words guard prevents relaxing a query that is already at the floor")


def t04_floor_unreachable_returns_best_effort():
    session = ScriptedSession(
        {
            "one two three four": [],
            "one two three": [make_study("NCT00000001")],
        }
    )
    client = make_client(session)
    results = client.search_relaxed(
        "one two three four", page_size=100, max_pages=1, statuses=None, min_words=3, max_rounds=1
    )
    assert len(results) == 1, "must return the best-effort widened result, never raise"
    print("PASS 04: an unreachable floor returns the best-effort widened result without raising")


def t05_merge_never_loses_or_duplicates_studies():
    q = "a b c d"
    session = ScriptedSession(
        {
            "a b c d": [make_study("NCT00000001")],
            "a b c": [make_study("NCT00000001"), make_study("NCT00000002")],
        }
    )
    client = make_client(session)
    results = client.search_relaxed(q, page_size=5, max_pages=1, statuses=None, min_words=1, max_rounds=1)
    ids = [r.native_id for r in results]
    assert ids.count("NCT00000001") == 1, f"a study present in both rounds must not be duplicated: {ids}"
    assert "NCT00000002" in ids, ids
    print("PASS 05: merging across rounds neither duplicates nor drops studies")


def run():
    t01_collapse_recovers_by_dropping_trailing_words()
    t02_healthy_query_never_relaxes()
    t03_min_words_guard()
    t04_floor_unreachable_returns_best_effort()
    t05_merge_never_loses_or_duplicates_studies()
    print("\nAll clinicaltrials relaxation tests passed.")


if __name__ == "__main__":
    run()
