"""
Offline test for agents/strategist.py -- no network required.

Run from the project root (sehatEvidence/):
    python -m tests.test_strategist

Covers the proposer<->critic design: query count is decided by the models,
not a fixed k. A ScriptedLLM feeds one response per call in order (draft,
critique, revision, critique, ...) so tests can assert on the exact
sequence and content of that back-and-forth.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm import LLMError, LLMClient
from agents.strategist import Strategist, _CRITIC_SYSTEM_PROMPT, _PROPOSER_SYSTEM_PROMPT

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

_SUFFICIENT = {"sufficient": True, "feedback": ""}


def _insufficient(feedback: str) -> dict:
    return {"sufficient": False, "feedback": feedback}


class ScriptedLLM:
    """Returns scripted JSON responses in call order (proposer and critic
    share this one object, distinguished by which `system` prompt a given
    call used -- exactly how the real Strategist calls the same LLMClient
    for both roles)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls.append({"prompt": prompt, "system": system, "temperature": temperature})
        idx = len(self.calls) - 1
        if idx >= len(self.responses):
            raise AssertionError(
                f"ScriptedLLM ran out of responses at call {idx + 1} "
                f"(only {len(self.responses)} scripted); prompt={prompt!r}"
            )
        response = self.responses[idx]
        if isinstance(response, Exception):
            raise response
        return response

    def roles(self) -> list[str]:
        """'propose' or 'critique' per call, for asserting call order."""
        out = []
        for call in self.calls:
            if call["system"] == _PROPOSER_SYSTEM_PROMPT:
                out.append("propose")
            elif call["system"] == _CRITIC_SYSTEM_PROMPT:
                out.append("critique")
            else:
                out.append("unknown")
        return out


class FailingLLM:
    """Every call raises LLMError (dead endpoint / malformed JSON)."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        raise LLMError("simulated LLM failure: invalid JSON")


# --- helpers ----------------------------------------------------------------


def assert_valid_queries(queries):
    """Every query: non-empty str, plain phrase, 4-12 words, no booleans."""
    for q in queries:
        assert isinstance(q, str) and q, f"query must be a non-empty str: {q!r}"
        assert _QUERY_RE.match(q), f"query fails hygiene regex: {q!r}"
        assert not _BOOLEAN_RE.search(q), f"boolean operator in query: {q!r}"
        assert 4 <= len(q.split()) <= 12, f"query outside 4-12 words: {q!r}"


# --- test cases ---------------------------------------------------------------


def t01_sufficient_on_first_try():
    llm = ScriptedLLM([{"queries": list(_GOOD_QUERIES)}, _SUFFICIENT])
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q)
    assert out == _GOOD_QUERIES, f"expected the scripted queries in order, got {out}"
    assert llm.roles() == ["propose", "critique"], llm.roles()
    assert _SEMAGLUTIDE_Q in llm.calls[0]["prompt"], "proposer prompt must embed the question"
    assert "PubMed" in llm.calls[0]["system"], "proposer system prompt missing"
    assert "reviewer" in llm.calls[1]["system"].lower(), "critic system prompt missing"
    assert llm.calls[0]["temperature"] == 0.1 and llm.calls[1]["temperature"] == 0.1
    print("PASS 01: critic approves on first pass -> proposer's draft used verbatim, 2 calls")


def t02_model_decides_count_freely():
    # A single-facet question: the model returns just ONE query, and the
    # critic accepts it. Nothing in plan_queries() should force a minimum.
    llm = ScriptedLLM([{"queries": ["aspirin stroke prevention elderly"]}, _SUFFICIENT])
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q)
    assert out == ["aspirin stroke prevention elderly"], out

    # A dense question: the model returns FIVE queries, critic accepts.
    five = [
        "semaglutide obesity cardiovascular outcomes",
        "semaglutide weight management review",
        "GLP-1 receptor agonist adverse effects",
        "obesity pharmacotherapy guidelines adults",
        "semaglutide renal outcomes trial",
    ]
    llm2 = ScriptedLLM([{"queries": five}, _SUFFICIENT])
    out2 = Strategist(llm=llm2).plan_queries(_SEMAGLUTIDE_Q)
    assert out2 == five, out2
    print("PASS 02: no fixed count -- 1 query and 5 queries both pass through untouched")


def t03_critic_requests_revision_then_approves():
    draft = ["semaglutide obesity weight loss outcomes"]
    revised = [
        "semaglutide obesity weight loss outcomes",
        "semaglutide cardiovascular safety adverse effects",
    ]
    llm = ScriptedLLM(
        [
            {"queries": list(draft)},
            _insufficient("missing a harms/adverse-effects angle"),
            {"queries": list(revised)},
            _SUFFICIENT,
        ]
    )
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q)
    assert out == revised, out
    assert llm.roles() == ["propose", "critique", "propose", "critique"], llm.roles()
    # The revision prompt must carry the previous draft AND the critic's feedback.
    revision_prompt = llm.calls[2]["prompt"]
    assert "semaglutide obesity weight loss outcomes" in revision_prompt, revision_prompt
    assert "missing a harms/adverse-effects angle" in revision_prompt, revision_prompt
    print("PASS 03: critic flags a gap -> proposer revises with feedback -> critic re-approves")


def t04_loop_continues_across_multiple_revisions():
    # Three full rounds before the critic is finally satisfied -- the loop
    # is unbounded by design, so this must not stop early or raise.
    llm = ScriptedLLM(
        [
            {"queries": ["round one query text here"]},
            _insufficient("round one gap"),
            {"queries": ["round two query text here"]},
            _insufficient("round two gap"),
            {"queries": ["round three query text here"]},
            _SUFFICIENT,
        ]
    )
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q)
    assert out == ["round three query text here"], out
    assert llm.roles() == ["propose", "critique"] * 3, llm.roles()
    print("PASS 04: multiple critic rounds all run to completion, no artificial cap")


def t05_proposer_unreachable_falls_back_to_heuristic():
    llm = FailingLLM()
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q)
    assert llm.calls == 1, f"proposer must be attempted exactly once, got {llm.calls}"
    assert_valid_queries(out)
    assert len(out) >= 1, "heuristic must never return an empty plan for a real question"
    significant = {"semaglutide", "effective", "weight", "loss", "patients", "obesity"}
    for q in out:
        assert any(w.lower() in significant for w in q.split()), (
            f"query shares no significant term with the question: {q!r}"
        )
    print(f"PASS 05: proposer unreachable -> heuristic queries, no critic call, no exception: {out}")


def t06_critic_unreachable_accepts_draft_as_is():
    llm = ScriptedLLM([{"queries": list(_GOOD_QUERIES)}, LLMError("critic endpoint dead")])
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q)
    assert out == _GOOD_QUERIES, out
    assert llm.roles() == ["propose", "critique"], llm.roles()
    print("PASS 06: critic unreachable -> proposer's draft accepted as-is, no exception")


def t07_revision_unreachable_keeps_last_good_draft():
    llm = ScriptedLLM(
        [
            {"queries": list(_GOOD_QUERIES)},
            _insufficient("needs one more angle"),
            LLMError("proposer endpoint dead on revision"),
        ]
    )
    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q)
    assert out == _GOOD_QUERIES, "revision failure must keep the last accepted draft, not fail"
    print("PASS 07: revision call unreachable -> keeps the last accepted (pre-revision) draft")


def t08_bad_shapes_fall_back_to_heuristic_or_accept():
    # (a) proposer response is a list, not a dict -> heuristic fallback.
    llm = ScriptedLLM([["not", "a", "dict"]])
    out = Strategist(llm=llm).plan_queries(_ASPIRIN_Q)
    assert_valid_queries(out)
    assert all("aspirin" in q.lower() for q in out), out

    # (b) proposer dict without "queries" -> heuristic fallback.
    llm2 = ScriptedLLM([{"result": ["aspirin stroke prevention elderly"]}])
    out2 = Strategist(llm=llm2).plan_queries(_ASPIRIN_Q)
    assert_valid_queries(out2)

    # (c) proposer's queries are all invalid (e.g. a boolean operator) ->
    # zero survive validation -> treated as an unusable proposal -> heuristic.
    llm3 = ScriptedLLM([{"queries": ["drug AND disease treatment outcomes"]}])
    out3 = Strategist(llm=llm3).plan_queries(_ASPIRIN_Q)
    assert_valid_queries(out3)
    assert "drug AND disease treatment outcomes" not in out3

    # (d) critic response missing "sufficient" -> treated as unreachable,
    # draft accepted as-is (no heuristic -- the draft itself was fine).
    llm4 = ScriptedLLM([{"queries": list(_GOOD_QUERIES)}, {"feedback": "no verdict key"}])
    out4 = Strategist(llm=llm4).plan_queries(_SEMAGLUTIDE_Q)
    assert out4 == _GOOD_QUERIES, out4
    print("PASS 08: malformed proposer shapes -> heuristic; malformed critic shape -> accept draft")


def t09_dedupe_within_one_proposal():
    llm = ScriptedLLM(
        [
            {
                "queries": [
                    "Query one here now",
                    "query one here now",
                    "Query two also here now",
                ]
            },
            _SUFFICIENT,
        ]
    )
    out = Strategist(llm=llm).plan_queries("Is metformin safe in pregnancy?")
    assert out[0] == "Query one here now", (
        f"first occurrence (original casing) must win: {out}"
    )
    lowered = [q.lower() for q in out]
    assert lowered.count("query one here now") == 1, (
        f"case-insensitive duplicate survived: {out}"
    )
    assert lowered == ["query one here now", "query two also here now"], out
    print(f"PASS 09: case-insensitive dedupe within a single proposal: {out}")


def t10_empty_question():
    llm = ScriptedLLM([{"queries": ["some valid query here"]}, _SUFFICIENT])
    assert Strategist(llm=llm).plan_queries("   ") == [], (
        "whitespace-only question must return []"
    )
    assert Strategist(llm=llm).plan_queries("") == [], (
        "empty question must return []"
    )
    assert llm.calls == [], "LLM must NOT be called for a blank question"
    print("PASS 10: blank question -> [] with zero LLM calls")


def t11_duck_typing():
    llm = ScriptedLLM([{"queries": ["metformin type two diabetes outcomes"]}, _SUFFICIENT])
    strategist = Strategist(llm=llm)
    assert strategist.llm is llm, "injected LLM must be used as-is (duck-typed)"
    out = strategist.plan_queries("Does metformin help type 2 diabetes?")
    assert out == ["metformin type two diabetes outcomes"], out

    # Default constructor builds a real LLMClient; its constructor does no
    # network I/O, so merely constructing it is offline-safe.
    default = Strategist()
    assert isinstance(default.llm, LLMClient), type(default.llm)
    print("PASS 11: duck-typed injection works; default constructor is offline-safe")


def t12_regex_hygiene():
    samples: list[str] = []
    samples += Strategist(llm=ScriptedLLM([{"queries": list(_GOOD_QUERIES)}, _SUFFICIENT])).plan_queries(
        _SEMAGLUTIDE_Q
    )
    samples += Strategist(llm=FailingLLM()).plan_queries(_ASPIRIN_Q)
    samples += Strategist(llm=ScriptedLLM([{"queries": ["short"]}])).plan_queries(
        "Is metformin safe in pregnancy?"
    )
    samples += Strategist(llm=ScriptedLLM([["bad shape"]])).plan_queries(_SEMAGLUTIDE_Q)
    assert len(samples) >= 6, f"expected a healthy sample spread, got {len(samples)}"
    for q in samples:
        assert _QUERY_RE.match(q), f"query fails hygiene regex: {q!r}"
    print(f"PASS 12: all {len(samples)} sampled outputs match the plain-phrase regex")


def t13_generic_and_internals():
    # All stop words / pure numbers -> no significant terms -> the single
    # spec'd generic query (deliberately exempt from the 4-word floor).
    out = Strategist(llm=FailingLLM()).plan_queries("What is the best for a 2023?")
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

    # single-term question: every template padded to >= 4 words, in order,
    # ALL 5 templates returned (no k to slice by any more).
    assert _heuristic_queries("aspirin") == [
        "aspirin review clinical study",
        "aspirin randomized trial evidence",
        "aspirin adverse effects evidence",
        "aspirin guideline clinical study",
        "aspirin clinical study evidence",
    ], _heuristic_queries("aspirin")
    print("PASS 13: generic no-terms fallback; term extraction; heuristic returns all 5 templates")


def t14_on_round_fires_once_per_proposer_draft():
    # Same 3-round script as t04: on_round must fire once per DRAFT (not
    # per critique), in order, 1-based, with the exact queries shown to
    # the critic at that round -- this is what lets pipeline.py stream
    # real mid-loop progress instead of the UI sitting on "start" for
    # however many rounds a dense question needs (mirrors the Appraiser/
    # Verifier's own on_progress hooks).
    llm = ScriptedLLM(
        [
            {"queries": ["round one query text here"]},
            _insufficient("round one gap"),
            {"queries": ["round two query text here"]},
            _insufficient("round two gap"),
            {"queries": ["round three query text here"]},
            _SUFFICIENT,
        ]
    )
    rounds: list[tuple[int, list]] = []
    out = Strategist(llm=llm).plan_queries(
        _ASPIRIN_Q, on_round=lambda i, qs: rounds.append((i, list(qs)))
    )
    assert out == ["round three query text here"], out
    assert rounds == [
        (1, ["round one query text here"]),
        (2, ["round two query text here"]),
        (3, ["round three query text here"]),
    ], rounds
    print("PASS 14: on_round fires once per proposer draft, in order, 1-based")


def t15_on_round_failure_is_fail_open():
    # A broken callback must never break query planning -- same fail-open
    # rule as every other injected callback in this codebase.
    llm = ScriptedLLM([{"queries": list(_GOOD_QUERIES)}, _SUFFICIENT])

    def boom(i, qs):
        raise RuntimeError("boom")

    out = Strategist(llm=llm).plan_queries(_SEMAGLUTIDE_Q, on_round=boom)
    assert out == _GOOD_QUERIES, out
    print("PASS 15: a broken on_round callback never breaks query planning")


def run():
    t01_sufficient_on_first_try()
    t02_model_decides_count_freely()
    t03_critic_requests_revision_then_approves()
    t04_loop_continues_across_multiple_revisions()
    t05_proposer_unreachable_falls_back_to_heuristic()
    t06_critic_unreachable_accepts_draft_as_is()
    t07_revision_unreachable_keeps_last_good_draft()
    t08_bad_shapes_fall_back_to_heuristic_or_accept()
    t09_dedupe_within_one_proposal()
    t10_empty_question()
    t11_duck_typing()
    t12_regex_hygiene()
    t13_generic_and_internals()
    t14_on_round_fires_once_per_proposer_draft()
    t15_on_round_failure_is_fail_open()
    print("\nAll strategist tests passed.")


if __name__ == "__main__":
    run()
