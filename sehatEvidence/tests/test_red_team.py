"""
Offline test for agents/red_team.py -- no network required.

Run from the project root (sehatEvidence/):
    python -m tests.test_red_team

Coverage inventory: happy path (exact flags, exactly one LLM call at
temperature 0.1), hallucinated claim_id guard, invalid flag value guard,
(claim_id, flag) dedup, note normalization to "", empty-flags envelope,
fail-open on LLMError AND on non-LLMError, empty-question / no-claims
short-circuit (LLM never called), bad response shapes, and prompt /
system-prompt content.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.red_team import RedTeam, RedTeamFlag
from core.llm import LLMError

_QUESTION = "Is drug X more effective than placebo for condition Y?"


class FakeClaim:
    """Duck-typed claim stand-in. The Red Team may only touch .claim_id and
    .text -- reaching for anything else is a contract violation."""

    def __init__(self, claim_id, text):
        self.claim_id = claim_id
        self.text = text


class FakeLLM:
    """Scriptable LLM stand-in.

    `script` is a list consumed in order; each item is either a JSON value
    to return or an exception instance to raise. Every complete_json call
    is recorded (prompt, system, temperature). A call made after the script
    runs out raises AssertionError -- combined with the recorded-calls
    checks below, tests that must NOT hit the LLM fail loudly.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature}
        )
        if not self.script:
            raise AssertionError("FakeLLM was called with no scripted item left")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def two_claims():
    """The standard two-claim input used by most cases."""
    return [
        FakeClaim("s0-c1", "Drug X halves mortality in condition Y."),
        FakeClaim("s1-c1", "Drug X is safe for all patients."),
    ]


# --- test cases ---------------------------------------------------------------


def t01_happy_path():
    llm = FakeLLM(
        [
            {
                "flags": [
                    {
                        "claim_id": "s0-c1",
                        "flag": "overstatement",
                        "note": "Effect size overstated",
                    },
                    {
                        "claim_id": "s1-c1",
                        "flag": "missing_caveat",
                        "note": "Small sample",
                    },
                ]
            }
        ]
    )
    flags = RedTeam(llm=llm).audit(_QUESTION, two_claims())
    assert len(flags) == 2, f"expected 2 flags, got {len(flags)}: {flags}"
    assert all(isinstance(f, RedTeamFlag) for f in flags), (
        f"must return RedTeamFlag objects: {flags}"
    )
    assert flags[0] == RedTeamFlag("s0-c1", "overstatement", "Effect size overstated"), (
        f"first flag mismatch: {flags[0]}"
    )
    assert flags[1] == RedTeamFlag("s1-c1", "missing_caveat", "Small sample"), (
        f"second flag mismatch: {flags[1]}"
    )
    assert len(llm.calls) == 1, f"exactly one LLM call expected, got {len(llm.calls)}"
    assert llm.calls[0]["temperature"] == 0.1, (
        f"LLM must be called at temperature 0.1: {llm.calls[0]['temperature']}"
    )
    print("PASS 01: happy path returns exact flags; one LLM call at temperature 0.1")


def t02_hallucination_guard():
    llm = FakeLLM(
        [
            {
                "flags": [
                    {
                        "claim_id": "s9-c9",  # not in the input claims
                        "flag": "overstatement",
                        "note": "hallucinated claim",
                    },
                    {
                        "claim_id": "s0-c1",
                        "flag": "absolute_claim",
                        "note": "unguarded 'all patients'",
                    },
                ]
            }
        ]
    )
    flags = RedTeam(llm=llm).audit(_QUESTION, two_claims())
    assert flags == [RedTeamFlag("s0-c1", "absolute_claim", "unguarded 'all patients'")], (
        f"hallucinated claim_id must be dropped, valid kept: {flags}"
    )
    assert len(llm.calls) == 1
    print("PASS 02: flag on unknown claim_id 's9-c9' dropped; valid flag kept")


def t03_invalid_flag_value():
    llm = FakeLLM(
        [
            {
                "flags": [
                    {
                        "claim_id": "s0-c1",
                        "flag": "bad_flag",  # not one of the four allowed values
                        "note": "not a real flag type",
                    },
                    {
                        "claim_id": "s1-c1",
                        "flag": "population_mismatch",
                        "note": "only adults studied",
                    },
                ]
            }
        ]
    )
    flags = RedTeam(llm=llm).audit(_QUESTION, two_claims())
    assert flags == [
        RedTeamFlag("s1-c1", "population_mismatch", "only adults studied")
    ], f"invalid flag value must be dropped, valid kept: {flags}"
    print("PASS 03: invalid flag value 'bad_flag' dropped; valid flag kept")


def t04_dedup():
    llm = FakeLLM(
        [
            {
                "flags": [
                    {
                        "claim_id": "s0-c1",
                        "flag": "overstatement",
                        "note": "first occurrence",
                    },
                    {
                        "claim_id": "s0-c1",
                        "flag": "overstatement",  # identical (claim_id, flag)
                        "note": "second occurrence",
                    },
                    {
                        "claim_id": "s0-c1",
                        "flag": "missing_caveat",  # same claim, different flag
                        "note": "different flag type",
                    },
                ]
            }
        ]
    )
    flags = RedTeam(llm=llm).audit(_QUESTION, two_claims())
    assert flags == [
        RedTeamFlag("s0-c1", "overstatement", "first occurrence"),
        RedTeamFlag("s0-c1", "missing_caveat", "different flag type"),
    ], f"identical (claim_id, flag) pairs must dedupe, first wins: {flags}"
    print("PASS 04: duplicate (claim_id, flag) collapsed to one; distinct flag types kept")


def t05_note_defaults():
    llm = FakeLLM(
        [
            {
                "flags": [
                    {  # note key missing entirely
                        "claim_id": "s0-c1",
                        "flag": "overstatement",
                    },
                    {  # note present but junk (non-string)
                        "claim_id": "s1-c1",
                        "flag": "missing_caveat",
                        "note": 12345,
                    },
                ]
            }
        ]
    )
    flags = RedTeam(llm=llm).audit(_QUESTION, two_claims())
    assert len(flags) == 2, f"both entries must survive note normalization: {flags}"
    assert flags[0].note == "", f"missing note must default to '': {flags[0].note!r}"
    assert flags[1].note == "", f"junk note must normalize to '': {flags[1].note!r}"
    assert flags[0].claim_id == "s0-c1" and flags[1].claim_id == "s1-c1", flags
    print("PASS 05: missing note and junk note both normalize to ''")


def t06_empty_flags():
    llm = FakeLLM([{"flags": []}])
    flags = RedTeam(llm=llm).audit(_QUESTION, two_claims())
    assert flags == [], f"empty flags envelope must yield []: {flags}"
    assert len(llm.calls) == 1, "LLM is still called once when claims exist"
    print("PASS 06: {'flags': []} -> []")


def t07_fail_open():
    claims = two_claims()
    # LLMError -- the contract's declared failure mode
    llm = FakeLLM([LLMError("simulated endpoint failure")])
    flags = RedTeam(llm=llm).audit(_QUESTION, claims)
    assert flags == [], f"LLMError must fail open to []: {flags}"
    assert len(llm.calls) == 1
    # non-LLMError -- the broad except must still fail open, never raise
    llm2 = FakeLLM([ValueError("unexpected boom")])
    flags2 = RedTeam(llm=llm2).audit(_QUESTION, claims)
    assert flags2 == [], f"non-LLMError must also fail open to []: {flags2}"
    assert len(llm2.calls) == 1
    print("PASS 07: LLMError and ValueError both fail open to [] without raising")


def t08_no_claims_no_question():
    claims = two_claims()
    llm = FakeLLM()  # empty script: any call raises AssertionError inside the fake
    assert RedTeam(llm=llm).audit(_QUESTION, []) == [], "no claims must return []"
    assert RedTeam(llm=llm).audit("", claims) == [], "empty question must return []"
    assert RedTeam(llm=llm).audit(None, claims) == [], "None question must return []"
    assert RedTeam(llm=llm).audit("", []) == [], "empty everything must return []"
    assert llm.calls == [], f"LLM must not be called without work: {llm.calls}"
    print("PASS 08: no claims / empty question -> [] with zero LLM calls")


def t09_bad_shapes():
    claims = two_claims()
    bad_shapes = [
        [{"claim_id": "s0-c1", "flag": "overstatement", "note": "list envelope"}],  # list, not dict
        {"no_flags_here": True},  # dict without "flags"
        {"flags": "not-a-list"},  # flags is a string
        {"flags": {"s0-c1": "overstatement"}},  # flags is a dict
        None,  # JSON null
        42,  # bare number
    ]
    for bad in bad_shapes:
        llm = FakeLLM([bad])
        flags = RedTeam(llm=llm).audit(_QUESTION, claims)
        assert flags == [], f"bad shape {bad!r} must fail open to [], got {flags}"
        assert len(llm.calls) == 1, (
            f"bad shape {bad!r}: LLM must still have been called exactly once"
        )
    # junk entries inside an otherwise valid flags list are skipped, not fatal
    llm = FakeLLM(
        [
            {
                "flags": [
                    "junk",
                    None,
                    {"claim_id": "s0-c1", "flag": "overstatement", "note": "ok"},
                ]
            }
        ]
    )
    flags = RedTeam(llm=llm).audit(_QUESTION, claims)
    assert flags == [RedTeamFlag("s0-c1", "overstatement", "ok")], (
        f"non-object entries must be skipped, valid kept: {flags}"
    )
    print("PASS 09: bad envelope shapes -> []; junk entries inside flags skipped")


def t10_prompt_content():
    question = "Does drug X prevent stroke in adults with atrial fibrillation?"
    claims = [
        FakeClaim("s0-c1", "Drug X cuts stroke risk in half versus warfarin."),
        FakeClaim("s1-c1", "Drug X eliminates the need for routine monitoring."),
    ]
    llm = FakeLLM([{"flags": []}])
    RedTeam(llm=llm).audit(question, claims)
    assert len(llm.calls) == 1, "exactly one LLM call expected"
    prompt = llm.calls[0]["prompt"]
    assert question in prompt, f"prompt must contain the clinical question: {prompt!r}"
    for claim in claims:
        assert claim.claim_id in prompt, f"claim_id {claim.claim_id} missing from prompt"
        assert claim.text in prompt, f"claim text {claim.text!r} missing from prompt"
    assert "1. [s0-c1]" in prompt and "2. [s1-c1]" in prompt, (
        f"claims must be numbered with ids in brackets: {prompt!r}"
    )
    system = llm.calls[0]["system"]
    assert isinstance(system, str) and system, "system prompt must be a non-empty string"
    for flag_type in (
        "overstatement",
        "absolute_claim",
        "missing_caveat",
        "population_mismatch",
    ):
        assert flag_type in system, f"system prompt must mention {flag_type}"
    print("PASS 10: prompt carries question + numbered claims; system names all four flag types")


def run():
    t01_happy_path()
    t02_hallucination_guard()
    t03_invalid_flag_value()
    t04_dedup()
    t05_note_defaults()
    t06_empty_flags()
    t07_fail_open()
    t08_no_claims_no_question()
    t09_bad_shapes()
    t10_prompt_content()
    print("\nAll red team tests passed.")


if __name__ == "__main__":
    run()
