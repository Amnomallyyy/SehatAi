"""
Offline test for agents/strategist.py -- no network required.

Run from the project root (sehatEvidence/):
    python -m tests.test_strategist
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm import LLMError, LLMClient
from agents.strategist import Strategist, DEFAULT_K

# Output hygiene: every query must be a plain keyword phrase -- word
# characters, whitespace and common punctuation only. NO brackets (which
# would signal database field tags like "aspirin[MeSH]").
_QUERY_RE = re.compile(r"^[\w\s\-.,()/'\"]+$")
_BOOLEAN_RE = re.compile(r"\b(?:and|or|not)\b", re.IGNORECASE)

_SEMAGLUTIDE_Q = "Is semaglutide effective for weight loss in patients with obesity?"
_ASPIRIN_Q = "Does aspirin prevent stroke in elderly patients?"

_GOOD_QUERIES = [
    "semaglutide obesity cardiovascular outcomes",
    "semaglutide weight management review",
    "GLP-1 receptor agonist adverse effects",
]


class FakeLLM:
    """Returns one scripted JSON response and tracks every call."""

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.last_prompt = None
        self.last_system = None
        self.last_temperature = None

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        self.last_prompt = prompt
        self.last_system = system
        self.last_temperature = temperature
        return self.response


class FailingLLM:
    """Every call raises LLMError (dead endpoint / malformed JSON)."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        raise LLMError("simulated LLM failure: invalid JSON")


# --- helpers ----------------------------------------------------------------


def assert_valid_queries(queries, k=None):
    """Every query: non-empty str, plain phrase, 4-12 words, no booleans."""
    for q in queries:
        assert isinstance(q, str) and q, f"query must be a non-empty str: {q!r}"
        assert _QUERY_RE.match(q), f"query fails hygiene regex: {q!r}"
        assert not _BOOLEAN_RE.search(q), f"boolean operator in query: {q!r}"
        assert 4 <= len(q.split()) <= 12, f"query outside 4-12 words: {q!r}"
    if k is not None:
        assert len(queries) == k, f"expected {k} queries, got {len(queries)}: {queries}"


# --- test cases ---------------------------------------------------------------


def t01_valid_json():
    assert DEFAULT_K == 3, f"DEFAULT_K must be 3, got {DEFAULT_K}"
    llm = FakeLLM({"queries": list(_GOOD_QUERIES)})
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q, k=3)
    assert out == _GOOD_QUERIES, f"expected the scripted queries in order, got {out}"
    assert llm.calls == 1, f"LLM must be called exactly once, got {llm.calls}"
    assert _SEMAGLUTIDE_Q in llm.last_prompt, "prompt must embed the question"
    assert "exactly 3 queries" in llm.last_prompt, llm.last_prompt
    assert llm.last_system and "PubMed" in llm.last_system, "system prompt missing"
    assert llm.last_temperature == 0.1, "planning must run at temperature 0.1"

    # default k (DEFAULT_K) takes the same path
    llm = FakeLLM({"queries": list(_GOOD_QUERIES)})
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q)
    assert out == _GOOD_QUERIES and llm.calls == 1
    print("PASS 01: valid JSON -> 3 scripted queries in order, exactly one LLM call")


def t02_heuristic_fallback():
    llm = FailingLLM()
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q, k=3)
    assert llm.calls == 1, f"LLM must be attempted exactly once, got {llm.calls}"
    assert_valid_queries(out, k=3)
    significant = {"semaglutide", "effective", "weight", "loss", "patients", "obesity"}
    for q in out:
        assert any(w.lower() in significant for w in q.split()), (
            f"query shares no significant term with the question: {q!r}"
        )
    print(f"PASS 02: LLMError -> 3 heuristic queries, no exception: {out}")


def t03_bad_shapes():
    # (a) response is a list, not a dict -> full heuristic fallback
    llm = FakeLLM(["not", "a", "dict"])
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q, k=3)
    assert llm.calls == 1
    assert_valid_queries(out, k=3)
    assert all("aspirin" in q.lower() for q in out), out

    # (b) dict without "queries" -> full heuristic fallback
    llm = FakeLLM({"result": ["aspirin stroke prevention elderly"]})
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q, k=3)
    assert llm.calls == 1
    assert_valid_queries(out, k=3)

    # (c) "queries" is not a list -> full heuristic fallback
    llm = FakeLLM({"queries": "aspirin stroke prevention elderly"})
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q, k=3)
    assert llm.calls == 1
    assert_valid_queries(out, k=3)

    # (d) mixed valid/invalid entries: invalid dropped, valid kept in order,
    # one heuristic filler tops the plan back up to k -- with NO second call.
    mixed = {
        "queries": [
            123,                                       # non-string entry
            None,                                      # non-string entry
            "drug AND disease treatment outcomes",     # boolean operator
            "aspirin[MeSH] stroke prevention elderly",  # field tag
            "aspirin stroke",                          # only 2 words
            "aspirin stroke prevention myocardial infarction recurrent "
            "events secondary cardiovascular mortality pooled analysis outcomes",  # 13 words
            "aspirin stroke prevention elderly",       # valid
            "aspirin secondary prevention guideline",  # valid
        ]
    }
    llm = FakeLLM(mixed)
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q, k=3)
    assert llm.calls == 1, "topping up must NOT trigger a second LLM call"
    assert out[:2] == [
        "aspirin stroke prevention elderly",
        "aspirin secondary prevention guideline",
    ], f"valid entries must be kept in order: {out}"
    assert len(out) == 3, f"shortfall must be topped up to k=3: {out}"
    assert_valid_queries(out)
    assert out[2] == "aspirin prevent stroke elderly systematic review", out
    for bad in (
        "drug AND disease treatment outcomes",
        "aspirin[MeSH] stroke prevention elderly",
        "aspirin stroke",
    ):
        assert bad not in out, f"invalid entry leaked through: {bad!r}"
    print(f"PASS 03: bad shapes / invalid entries dropped, valid kept, topped up: {out}")


def t04_dedupe():
    llm = FakeLLM(
        {
            "queries": [
                "Query one here now",
                "query one here now",
                "Query two also here now",
            ]
        }
    )
    out = Strategist(llm=llm).plan_queries("Is metformin safe in pregnancy?", k=3)
    assert out[0] == "Query one here now", (
        f"first occurrence (original casing) must win: {out}"
    )
    lowered = [q.lower() for q in out]
    assert lowered.count("query one here now") == 1, (
        f"case-insensitive duplicate survived: {out}"
    )
    assert "query two also here now" in lowered, out
    assert len(out) == 3, f"duplicate removal shortfall must be topped up: {out}"
    print(f"PASS 04: case-insensitive dedupe keeps first occurrence: {out}")


def t05_empty_question():
    llm = FakeLLM({"queries": ["some valid query here"]})
    assert Strategist(llm=llm).plan_queries("   ", k=3) == [], (
        "whitespace-only question must return []"
    )
    assert Strategist(llm=llm).plan_queries("", k=3) == [], (
        "empty question must return []"
    )
    assert llm.calls == 0, "LLM must NOT be called for a blank question"
    print("PASS 05: blank question -> [] with zero LLM calls")


def t06_k_clamping():
    six = [
        "alpha query one here",
        "beta query two here",
        "gamma query three here",
        "delta query four here",
        "epsilon query five here",
        "zeta query six here",
    ]
    llm = FakeLLM({"queries": six})
    out = Strategist(llm=llm).plan_queries("any clinical question here", k=99)
    assert len(out) == 5, f"k=99 must clamp to 5, got {len(out)}"
    assert out == six[:5], f"first 5 of the valid queries expected: {out}"

    llm = FakeLLM({"queries": six})
    out = Strategist(llm=llm).plan_queries("any clinical question here", k=0)
    assert len(out) >= 1, f"k=0 must clamp to at least 1, got {len(out)}"
    assert out == six[:1], out
    print("PASS 06: k clamped to [1, 5] (k=0 -> 1 query, k=99 -> 5 queries)")


def t07_duck_typing():
    llm = FakeLLM({"queries": ["metformin type two diabetes outcomes"]})
    strategist = Strategist(llm=llm)
    assert strategist.llm is llm, "injected LLM must be used as-is (duck-typed)"
    out = strategist.plan_queries("Does metformin help type 2 diabetes?", k=1)
    assert llm.calls == 1 and out == ["metformin type two diabetes outcomes"], out

    # Default constructor builds a real LLMClient; its constructor does no
    # network I/O, so merely constructing it is offline-safe.
    default = Strategist()
    assert isinstance(default.llm, LLMClient), type(default.llm)
    print("PASS 07: duck-typed injection works; default constructor is offline-safe")


def t08_more_than_k():
    five = [
        "semaglutide obesity cardiovascular outcomes",
        "semaglutide weight management review",
        "GLP-1 receptor agonist adverse effects",
        "obesity pharmacotherapy guidelines adults",
        "semaglutide renal outcomes trial",
    ]
    llm = FakeLLM({"queries": five})
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q, k=3)
    assert out == five[:3], f"only the first k queries must be used: {out}"
    for extra in five[3:]:
        assert extra not in out, f"oversupplied query leaked into the plan: {extra!r}"
    print("PASS 08: LLM oversupply -> exactly the first k queries, no fallback mixed in")


def t09_regex_hygiene():
    samples: list[str] = []
    samples += Strategist(llm=FakeLLM({"queries": list(_GOOD_QUERIES)})).plan_queries(
        _SEMAGLUTIDE_Q
    )
    samples += Strategist(llm=FailingLLM()).plan_queries(_ASPIRIN_Q)
    samples += Strategist(llm=FakeLLM({"queries": ["short"]})).plan_queries(
        "Is metformin safe in pregnancy?"
    )
    samples += Strategist(llm=FakeLLM(["bad shape"])).plan_queries(_SEMAGLUTIDE_Q)
    assert len(samples) >= 10, f"expected a healthy sample spread, got {len(samples)}"
    for q in samples:
        assert _QUERY_RE.match(q), f"query fails hygiene regex: {q!r}"
    print(f"PASS 09: all {len(samples)} sampled outputs match the plain-phrase regex")


def t10_generic_and_internals():
    # All stop words / pure numbers -> no significant terms -> the single
    # spec'd generic query (deliberately exempt from the 4-word floor).
    out = Strategist(llm=FailingLLM()).plan_queries("What is the best for a 2023?", k=3)
    assert out == ["clinical evidence review"], f"expected the generic query, got {out}"

    from agents.strategist import _heuristic_queries, _significant_terms

    assert _significant_terms("Is semaglutide effective for weight loss?") == [
        "semaglutide",
        "effective",
        "weight",
        "loss",
    ], _significant_terms("Is semaglutide effective for weight loss?")

    # duplicates keep first occurrence; pure numbers dropped
    terms = _significant_terms("aspirin aspirin 100 mg stroke stroke")
    assert terms == ["aspirin", "mg", "stroke"], terms

    # capped at 6 terms, original order
    assert _significant_terms("one two three four five six seven eight") == [
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
    ]

    # single-term question: every template padded to >= 4 words, in order
    assert _heuristic_queries("aspirin", 5) == [
        "aspirin review clinical study",
        "aspirin randomized trial evidence",
        "aspirin adverse effects evidence",
        "aspirin guideline clinical study",
        "aspirin clinical study evidence",
    ], _heuristic_queries("aspirin", 5)
    print("PASS 10: generic no-terms fallback; term extraction; short-query padding")


def run():
    t01_valid_json()
    t02_heuristic_fallback()
    t03_bad_shapes()
    t04_dedupe()
    t05_empty_question()
    t06_k_clamping()
    t07_duck_typing()
    t08_more_than_k()
    t09_regex_hygiene()
    t10_generic_and_internals()
    print("\nAll strategist tests passed.")


if __name__ == "__main__":
    run()
