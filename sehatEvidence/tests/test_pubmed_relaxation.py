"""
tests/test_pubmed_relaxation.py -- offline, scripted-session test for
PubMedClient.esearch_relaxed() / search_and_fetch_traced().

Run: python -m tests.test_pubmed_relaxation

Reproduces the exact collapse-then-recovery scenario found live on
2026-08-29 (apixaban/warfarin/CKD-stage-3b query: strict count 1, drop
"3b" -> count recovers) end to end against a scripted HTTP session -- no
network required, but every scripted payload is either the real captured
translation or a direct, deliberately simplified analog of it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

os.environ.setdefault("NCBI_TOOL_NAME", "evidenceboard_test")
os.environ.setdefault("NCBI_EMAIL", "test@example.com")

from retrieval.pubmed import PubMedClient

_TRANSLATION = (
    '("apixaban"[Supplementary Concept] OR "apixaban"[All Fields]) '
    'AND ("warfarin"[MeSH Terms] OR "warfarin"[All Fields]) '
    'AND "3b"[All Fields]'
)


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def esearch_payload(count, ids, translation):
    return {
        "esearchresult": {
            "count": str(count),
            "idlist": list(ids),
            "querytranslation": translation,
        }
    }


class ScriptedSession:
    """Routes on the exact `term` param sent, since that's what changes
    round to round; every other param is ignored for routing purposes."""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        term = (params or {}).get("term")
        if term not in self.routes:
            raise AssertionError(f"ScriptedSession got an unscripted term: {term!r}")
        return FakeResponse(self.routes[term])


def make_client(session):
    return PubMedClient(tool_name="t", email="e@example.com", session=session)


def t01_collapse_recovers():
    strict_term = "apixaban warfarin stroke 3b"
    dropped_term = _TRANSLATION.replace(' AND "3b"[All Fields]', "")
    session = ScriptedSession(
        {
            strict_term: esearch_payload(1, ["1111"], _TRANSLATION),
            dropped_term: esearch_payload(73, [], dropped_term),  # count-only probe
        }
    )
    client = make_client(session)
    ids, trace = client.esearch_relaxed(strict_term, retmax=15)

    assert trace.dropped == ['"3b"[All Fields]'], trace.dropped
    assert trace.counts == [1, 73], trace.counts
    assert trace.cleared_floor is True
    assert trace.floor == 15
    print(f"PASS 01: 1 hit -> drop '3b' -> 73 hits, cleared floor: {trace.counts}")


def t02_strict_ids_are_preserved_and_first():
    strict_term = "apixaban warfarin stroke 3b"
    dropped_term = _TRANSLATION.replace(' AND "3b"[All Fields]', "")
    session = ScriptedSession(
        {
            strict_term: esearch_payload(1, ["1111"], _TRANSLATION),
            dropped_term: esearch_payload(3, ["2222", "3333"], dropped_term),
        }
    )
    client = make_client(session)
    ids, trace = client.esearch_relaxed(strict_term, retmax=15, yield_floor=2)
    assert ids[0] == "1111", "the strict-search id must be first"
    assert "1111" in ids and "2222" in ids and "3333" in ids
    print(f"PASS 02: strict ids preserved and ordered first: {ids}")


def t03_healthy_query_never_relaxes():
    term = "metformin type two diabetes"
    session = ScriptedSession({term: esearch_payload(387, [str(i) for i in range(15)], "")})
    client = make_client(session)
    ids, trace = client.esearch_relaxed(term, retmax=15)
    assert len(session.calls) == 1, f"a healthy query must issue exactly one HTTP call, got {len(session.calls)}"
    assert trace.cleared_floor is True
    assert trace.dropped == []
    print("PASS 03: a healthy (already-above-floor) query issues zero extra probe calls")


def t04_floor_unreachable_returns_best_effort():
    term = "one AND two"
    session = ScriptedSession(
        {
            term: esearch_payload(1, ["1111"], '"one"[All Fields] AND "two"[All Fields]'),
            '"one"[All Fields]': esearch_payload(2, [], '"one"[All Fields]'),
        }
    )
    client = make_client(session)
    ids, trace = client.esearch_relaxed(term, retmax=100, yield_floor=100, min_concepts=1)
    assert trace.cleared_floor is False, "2 hits can never clear a floor of 100"
    assert trace.counts[-1] == 2
    print("PASS 04: an unreachable floor returns the best-effort widened result, never raises")


def t05_min_concepts_guard():
    term = "one AND two"
    translation = '"one"[All Fields] AND "two"[All Fields]'
    session = ScriptedSession({term: esearch_payload(0, [], translation)})
    client = make_client(session)
    ids, trace = client.esearch_relaxed(term, retmax=10, min_concepts=2)
    assert trace.dropped == [], "a 2-concept query must never be relaxed below min_concepts=2"
    assert len(session.calls) == 1
    print("PASS 05: min_concepts guard prevents relaxing a query down to a single bare term")


def t06_sort_relevance_is_sent_only_on_real_fetches():
    strict_term = "apixaban warfarin stroke 3b"
    dropped_term = _TRANSLATION.replace(' AND "3b"[All Fields]', "")
    session = ScriptedSession(
        {
            strict_term: esearch_payload(1, ["1111"], _TRANSLATION),
            dropped_term: esearch_payload(50, ["2222"], dropped_term),
        }
    )
    client = make_client(session)
    client.esearch_relaxed(strict_term, retmax=15)
    assert session.calls[0].get("sort") == "relevance", "the strict real fetch must be relevance-sorted"
    assert "sort" not in session.calls[1], "the count-only probe must not request a sort at all"
    assert session.calls[2].get("sort") == "relevance", "the final relaxed real fetch must be relevance-sorted"
    print("PASS 06: sort=relevance sent on real fetches, omitted on count-only probes")


def t07_no_progress_drop_is_reverted():
    # Dropping "3b" doesn't help (still 1) -- the drop must be reverted and
    # relaxation must fall through to trying "warfarin" next rather than
    # keeping a drop that bought nothing.
    strict_term = "apixaban warfarin 3b"
    translation = '"apixaban"[All Fields] AND "warfarin"[MeSH Terms] AND "3b"[All Fields]'
    no_progress_term = '"apixaban"[All Fields] AND "warfarin"[MeSH Terms]'
    session = ScriptedSession(
        {
            strict_term: esearch_payload(1, ["1111"], translation),
            no_progress_term: esearch_payload(1, [], no_progress_term),  # NO improvement
        }
    )
    client = make_client(session)
    ids, trace = client.esearch_relaxed(strict_term, retmax=15, min_concepts=1, max_rounds=1)
    assert '"3b"[All Fields]' not in trace.dropped, (
        "a drop that doesn't increase the count must be reverted, not kept"
    )
    assert trace.dropped == [], "with max_rounds=1 consumed by the no-progress probe, nothing should stick"
    print("PASS 07: a no-progress drop is reverted rather than accepted for free")


def run():
    t01_collapse_recovers()
    t02_strict_ids_are_preserved_and_first()
    t03_healthy_query_never_relaxes()
    t04_floor_unreachable_returns_best_effort()
    t05_min_concepts_guard()
    t06_sort_relevance_is_sent_only_on_real_fetches()
    t07_no_progress_drop_is_reverted()
    print("\nAll pubmed relaxation tests passed.")


if __name__ == "__main__":
    run()
