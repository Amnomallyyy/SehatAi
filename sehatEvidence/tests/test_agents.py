"""
tests/test_agents.py -- offline tests for agents/appraiser.py.

Plain-script convention (no pytest): run with
    python -m tests.test_agents
All LLM interaction is faked; nothing here touches the network.

Coverage inventory: t01-t12 original suite (empty input, hierarchy,
retraction, preprint cap, recency, blend math, hallucination, pool cap,
sorting, question types, misc rules, rationales); t13-t24 code-review fix
suite (B1-B11: low-coverage fallback, weak bound for missing abstract,
future date band, batching, sort tie-breaks, GRADE/evidence-based regex
guards, question-classifier guards, accuracy-marker guard, ScriptedLLM
validation, pool_cap validation, half-up rounding -- plus A6 LLM transport
failure memory); t25 evidence-recalibration case (C1: preprint cap 45 -> 65,
per BMJ Medicine 2023 PMC9951374; C2: case-control diagnosis bonus +10 -> +5,
per OCEBM Level 4).
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm import LLMError
from core.schema import EvidenceRecord, SourceDB, StudyDesign
from agents.appraiser import (
    Appraiser,
    AppraisedRecord,
    DEFAULT_POOL_CAP,
    _BLEND_W_HEURISTIC,
    _BLEND_W_LLM,
)

_THERAPY_Q = "Is drug X more effective than placebo for condition Y?"
_PROGNOSIS_Q = "What is the long-term prognosis for patients with condition Y?"
_ETIOLOGY_Q = "Does exposure X cause condition Y?"
_DIAGNOSIS_Q = "How is condition Y diagnosed?"

_ABSTRACT = "We enrolled 500 patients and followed them for two years."

_today = date.today()


# --- helpers ----------------------------------------------------------------


def make_record(
    native_id="1",
    design=StudyDesign.RCT,
    title=None,
    abstract=_ABSTRACT,
    pub_date=None,
    is_preprint=False,
    is_retracted=False,
    retraction_source=None,
    trial_status=None,
    source=SourceDB.PUBMED,
):
    """Build a minimal EvidenceRecord with sensible test defaults."""
    return EvidenceRecord(
        record_id=f"rec-{source.value}-{native_id}",
        source=source,
        native_id=str(native_id),
        title=title if title is not None else f"Study {native_id}",
        abstract=abstract if abstract is not None else "",
        publication_date=pub_date,
        study_design=design,
        is_preprint=is_preprint,
        is_retracted=is_retracted,
        retraction_source=retraction_source,
        trial_status=trial_status,
    )


def recent(off_years=0):
    """A publication date `off_years` back; only the year matters (M1)."""
    return date(_today.year - off_years, 6, 1)


def scores_by_key(out):
    return {ar.record.citation_key(): ar.score for ar in out}


class FailingLLM:
    """Simulates a dead LLM endpoint: every call raises LLMError."""

    def __init__(self):
        self.calls = 0

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        raise LLMError("simulated endpoint failure")


class ScriptedLLM:
    """Returns scripted JSON envelopes in order (last one repeats)."""

    def __init__(self, responses):
        if not responses:
            # B9: fail fast with a clear message instead of letting the first
            # complete_json() call die on responses[-1] -> IndexError.
            raise ValueError(
                "ScriptedLLM was constructed with no scripted responses; "
                "its first complete_json() call would otherwise crash with "
                "an unhelpful IndexError. Pass at least one response."
            )
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature}
        )
        idx = min(len(self.calls), len(self.responses)) - 1
        return self.responses[idx]


# --- test cases ---------------------------------------------------------------


def t01_empty_input():
    default_app = Appraiser()  # constructs LLMClient(); no network at init
    assert default_app.pool_cap == DEFAULT_POOL_CAP == 30
    app = Appraiser(llm=FailingLLM())
    assert app.appraise([], _THERAPY_Q) == [], "empty input must return []"
    print("PASS 01: empty input returns []")


def t02_hierarchy():
    app = Appraiser(llm=FailingLLM())
    designs = [
        StudyDesign.SYSTEMATIC_REVIEW,
        StudyDesign.META_ANALYSIS,
        StudyDesign.RCT,
        StudyDesign.GUIDELINE,
        StudyDesign.COHORT,
        StudyDesign.CASE_CONTROL,
        StudyDesign.REVIEW,
        StudyDesign.CASE_SERIES,
        StudyDesign.CASE_REPORT,
        StudyDesign.UNKNOWN,
    ]
    records = [
        make_record(str(i + 1), design=d, pub_date=recent(0))
        for i, d in enumerate(designs)
    ]
    s = {}
    for ar in app.appraise(records, _THERAPY_Q):
        idx = int(ar.record.native_id) - 1
        s[designs[idx]] = ar.score

    assert (
        s[StudyDesign.SYSTEMATIC_REVIEW] >= s[StudyDesign.META_ANALYSIS]
        > s[StudyDesign.RCT]
        > s[StudyDesign.GUIDELINE]
        > s[StudyDesign.COHORT]
        > s[StudyDesign.CASE_CONTROL]
        > s[StudyDesign.REVIEW]
        > s[StudyDesign.CASE_SERIES]
        > s[StudyDesign.CASE_REPORT]
        > s[StudyDesign.UNKNOWN]
    ), f"hierarchy not monotone: {s}"

    # hand-computed exact scores: base + recent(+5), therapy question
    assert s[StudyDesign.SYSTEMATIC_REVIEW] == 100, s[StudyDesign.SYSTEMATIC_REVIEW]
    assert s[StudyDesign.RCT] == 90, s[StudyDesign.RCT]  # 85 + 5
    assert s[StudyDesign.COHORT] == 70, s[StudyDesign.COHORT]  # 65 + 5

    # extra design-table entries
    extra = [
        make_record("21", design=StudyDesign.PREPRINT, pub_date=recent(0)),
        make_record("22", design=StudyDesign.CLINICAL_TRIAL_RECORD, pub_date=recent(0)),
    ]
    by_key = scores_by_key(app.appraise(extra, _THERAPY_Q))
    assert by_key["MED/21"] == 25, by_key["MED/21"]  # 20 + 5
    assert by_key["MED/22"] == 45, by_key["MED/22"]  # 40 + 5, no M8 penalty
    print("PASS 02: heuristic hierarchy monotone; exact hand-computed scores match")


def t03_retraction():
    app = Appraiser(llm=FailingLLM())
    records = [
        make_record("1", design=StudyDesign.RCT, pub_date=recent(0)),
        make_record(
            "2",
            design=StudyDesign.RCT,
            pub_date=recent(0),
            is_retracted=True,
            retraction_source="pubmed",
        ),
        make_record("3", design=StudyDesign.COHORT, pub_date=recent(0)),
    ]
    out = app.appraise(records, _THERAPY_Q)
    keys = [ar.record.citation_key() for ar in out]
    assert "MED/2" not in keys, "retracted record must be excluded"
    assert set(keys) == {"MED/1", "MED/3"}, f"siblings must remain: {keys}"
    print("PASS 03: retracted record excluded; non-retracted siblings remain")


def t04_preprint_cap():
    app = Appraiser(llm=FailingLLM())
    rec = make_record("1", design=StudyDesign.RCT, pub_date=recent(0), is_preprint=True)
    out = app.appraise([rec], _THERAPY_Q)
    assert len(out) == 1 and out[0].score == 65, f"preprint RCT must cap at 65: {out}"
    assert "preprint cap 65" in out[0].rationale, out[0].rationale

    # cap survives the LLM blend too (applied last)
    llm = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/1", "relevance": 100, "rationale": "PICO match"}]}]
    )
    out2 = Appraiser(llm=llm).appraise(
        [make_record("1", design=StudyDesign.RCT, pub_date=recent(0), is_preprint=True)],
        _THERAPY_Q,
    )
    assert out2[0].score == 65, f"cap must apply after blending: {out2[0].score}"

    # PREPRINT design keeps its low base: cap is a ceiling, not a floor
    out3 = app.appraise(
        [make_record("2", design=StudyDesign.PREPRINT, pub_date=recent(0))], _THERAPY_Q
    )
    assert out3[0].score == 25, f"PREPRINT design: 20 + 5 expected: {out3[0].score}"
    print("PASS 04: preprint cap 65 applied last (heuristic and blended paths)")


def t05_recency():
    app = Appraiser(llm=FailingLLM())
    # cohort base 65, abstract present, therapy question -> 65 + band modifier
    cases = {0: 70, 4: 65, 7: 60, 15: 55, 25: 50, None: 60}
    records = []
    next_id = 1
    for off in cases:
        records.append(
            make_record(
                str(next_id),
                design=StudyDesign.COHORT,
                pub_date=recent(off) if off is not None else None,
            )
        )
        next_id += 1
    by_key = scores_by_key(app.appraise(records, _THERAPY_Q))
    for rec, (off, expected) in zip(records, cases.items()):
        got = by_key[rec.citation_key()]
        assert got == expected, f"recency offset {off}: expected {expected}, got {got}"
    print("PASS 05: recency bands +5/0/-5/-10/-15 and -5 for missing date")


def t06_blend_math():
    # H=80 (guideline 75 + recent 5), R=100 -> B = 0.65*80 + 0.35*100 = 87
    llm = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/1", "relevance": 100, "rationale": "PICO match"}]}]
    )
    out = Appraiser(llm=llm).appraise(
        [
            make_record(
                "1",
                design=StudyDesign.GUIDELINE,
                title="Clinical practice guideline for X",
                pub_date=recent(0),
            )
        ],
        _THERAPY_Q,
    )
    assert out[0].score == 87, f"expected blended 87, got {out[0].score}"
    assert "LLM relevance 100" in out[0].rationale and "PICO match" in out[0].rationale
    assert llm.calls[0]["temperature"] == 0.0, "LLM must be called at temperature 0.0"

    # over-bound, +/-25: H=20 (case report 25 - no date 5), R=100 -> B=48 -> H+25 = 45
    llm2 = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/2", "relevance": 100, "rationale": "direct"}]}]
    )
    out2 = Appraiser(llm=llm2).appraise(
        [make_record("2", design=StudyDesign.CASE_REPORT, pub_date=None)], _THERAPY_Q
    )
    assert out2[0].score == 45, f"expected bound-clamped 45, got {out2[0].score}"
    assert "blend capped" in out2[0].rationale, out2[0].rationale

    # +/-15 bound (UNKNOWN design): H=20 (15 + 5), R=100 -> B=48 -> H+15 = 35
    llm3 = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/3", "relevance": 100, "rationale": "direct"}]}]
    )
    out3 = Appraiser(llm=llm3).appraise(
        [make_record("3", design=StudyDesign.UNKNOWN, pub_date=recent(0))], _THERAPY_Q
    )
    assert out3[0].score == 35, f"expected weak-bound clamp 35, got {out3[0].score}"

    # +/-15 bound (missing abstract): H=75 (85 - 5 no date - 5 no abstract),
    # R=100 -> B=83.75 -> round -> 84
    llm4 = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/4", "relevance": 100, "rationale": "direct"}]}]
    )
    out4 = Appraiser(llm=llm4).appraise(
        [make_record("4", design=StudyDesign.RCT, pub_date=None, abstract="   ")],
        _THERAPY_Q,
    )
    assert out4[0].score == 84, f"expected 84, got {out4[0].score}"

    # malformed LLM payload -> heuristic-only for the whole batch, never raises
    llm5 = ScriptedLLM([{"rankings": "not-a-list"}])
    out5 = Appraiser(llm=llm5).appraise(
        [make_record("5", design=StudyDesign.RCT, pub_date=recent(0))], _THERAPY_Q
    )
    assert out5[0].score == 90 and "llm_unavailable" in out5[0].rationale
    print("PASS 06: blend math, +/-25 and +/-15 bounds, malformed-response fallback")


def t07_hallucination():
    records = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 13)
    ]
    rankings = [
        {"citation_key": f"MED/{i}", "relevance": 95, "rationale": "partial PICO"}
        for i in range(1, 12)
    ]
    rankings.append(
        {"citation_key": "MED/99999999", "relevance": 100, "rationale": "hallucinated"}
    )
    llm = ScriptedLLM([{"rankings": rankings}])
    out = Appraiser(llm=llm).appraise(records, _THERAPY_Q)

    assert len(out) == 12, f"expected all 12 input records, got {len(out)}"
    keys = [ar.record.citation_key() for ar in out]
    assert set(keys) == {r.citation_key() for r in records}, "input keys must map 1:1"
    assert len(set(keys)) == 12, "each record exactly once"
    assert "MED/99999999" not in keys, "hallucinated key must be dropped"

    by_key = scores_by_key(out)
    # blended: B = 0.65*90 + 0.35*95 = 91.75 -> 92
    assert by_key["MED/1"] == 92, f"blended score expected 92, got {by_key['MED/1']}"
    # omitted key (1/12 missed = 8.3% <= 10% tolerance) -> heuristic-only
    assert by_key["MED/12"] == 90, f"skipped record must fall back to H=90: {by_key['MED/12']}"
    skipped = [ar for ar in out if ar.record.citation_key() == "MED/12"][0]
    assert "llm_skipped" in skipped.rationale, skipped.rationale

    # determinism: prompt lists records sorted by citation_key, temperature 0.0
    prompt = llm.calls[0]["prompt"]
    listed = [
        ln.split("citation_key:")[1].strip()
        for ln in prompt.splitlines()
        if ln.startswith("- citation_key:")
    ]
    assert listed == sorted(listed), "prompt must list records in citation_key order"
    assert llm.calls[0]["temperature"] == 0.0
    print("PASS 07: hallucinated key dropped; skipped key heuristic-only; prompt sorted")


def t08_pool_cap():
    app = Appraiser(llm=FailingLLM())
    keepers = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 31)
    ]  # score 90 each
    losers = [
        make_record(f"9{i:02d}", design=StudyDesign.UNKNOWN, pub_date=recent(25))
        for i in range(1, 6)
    ]  # 15 - 15 = 0 each
    out = app.appraise(keepers + losers, _THERAPY_Q)
    assert len(out) == 30, f"pool cap must truncate 35 -> 30, got {len(out)}"
    keys = {ar.record.citation_key() for ar in out}
    assert all(f"MED/{i}" in keys for i in range(1, 31)), "top records must be kept"
    assert not any(f"MED/9{i:02d}" in keys for i in range(1, 6)), "bottom must drop"

    small = Appraiser(pool_cap=5, llm=FailingLLM())
    out2 = small.appraise(
        [make_record(str(i), design=StudyDesign.COHORT, pub_date=recent(0)) for i in range(1, 11)],
        _THERAPY_Q,
    )
    assert len(out2) == 5, f"custom pool_cap=5 must return 5, got {len(out2)}"
    print("PASS 08: pool cap truncates to 30 keeping top scores; custom cap works")


def t09_sorting():
    app = Appraiser(llm=FailingLLM())
    records = [
        make_record("2", design=StudyDesign.RCT, pub_date=recent(0)),
        make_record("10", design=StudyDesign.RCT, pub_date=recent(0)),
        make_record("3", design=StudyDesign.SYSTEMATIC_REVIEW, pub_date=recent(0)),
    ]
    out = app.appraise(records, _THERAPY_Q)
    scores = [ar.score for ar in out]
    assert all(
        isinstance(s, int) and not isinstance(s, bool) and 0 <= s <= 100 for s in scores
    ), f"scores must be ints in [0,100]: {scores}"
    assert scores == sorted(scores, reverse=True), f"not descending: {scores}"
    keys = [ar.record.citation_key() for ar in out]
    # tie (score+design+date equal) breaks by citation_key ascending:
    # "MED/10" < "MED/2" lexicographically
    assert keys == ["MED/3", "MED/10", "MED/2"], f"bad tie-break order: {keys}"
    print("PASS 09: int scores in [0,100], descending order, deterministic tie-break")


def t10_question_type():
    app = Appraiser(llm=FailingLLM())

    def cohort():
        return make_record(
            "1",
            design=StudyDesign.COHORT,
            title="Cohort study of disease course",
            pub_date=recent(0),
        )

    therapy = scores_by_key(app.appraise([cohort()], _THERAPY_Q))["MED/1"]
    prognosis = scores_by_key(app.appraise([cohort()], _PROGNOSIS_Q))["MED/1"]
    diagnosis = scores_by_key(app.appraise([cohort()], _DIAGNOSIS_Q))["MED/1"]
    assert therapy == 70, f"therapy baseline expected 70, got {therapy}"
    assert prognosis == therapy + 10, (
        f"prognosis must boost cohort by +10: {therapy} -> {prognosis}"
    )
    assert diagnosis == therapy, (
        f"diagnosis without accuracy markers must not boost: {diagnosis}"
    )

    # etiology/harm: case-control gets +5
    def case_control():
        return make_record("2", design=StudyDesign.CASE_CONTROL, pub_date=recent(0))

    cc_therapy = scores_by_key(app.appraise([case_control()], _THERAPY_Q))["MED/2"]
    cc_etio = scores_by_key(app.appraise([case_control()], _ETIOLOGY_Q))["MED/2"]
    assert cc_therapy == 60 and cc_etio == 65, (
        f"case-control etiology boost expected 60 -> 65, got {cc_therapy} -> {cc_etio}"
    )

    # diagnosis WITH accuracy markers: cohort gets +10
    acc = make_record(
        "3",
        design=StudyDesign.COHORT,
        title="Accuracy of test T for condition Y",
        abstract="Sensitivity and specificity of test T were 0.9.",
        pub_date=recent(0),
    )
    got = scores_by_key(app.appraise([acc], _DIAGNOSIS_Q))["MED/3"]
    assert got == 80, f"diagnosis with accuracy markers expected 80, got {got}"
    print("PASS 10: question-type adjustments (prognosis/etiology/diagnosis)")


def t11_misc_rules():
    app = Appraiser(llm=FailingLLM())
    consensus = make_record(
        "1",
        design=StudyDesign.GUIDELINE,
        title="Expert consensus statement on management of X",
        pub_date=recent(0),
    )
    evidence_based = make_record(
        "2",
        design=StudyDesign.GUIDELINE,
        title="Evidence-based guideline for X informed by a systematic review",
        pub_date=recent(0),
    )
    sr_observational = make_record(
        "3",
        design=StudyDesign.SYSTEMATIC_REVIEW,
        title="Systematic review of cohort studies of X",
        pub_date=recent(0),
    )
    recruiting = make_record(
        "01234567",
        design=StudyDesign.CLINICAL_TRIAL_RECORD,
        trial_status="RECRUITING",
        pub_date=recent(0),
        source=SourceDB.CLINICAL_TRIALS,
    )
    out = app.appraise([consensus, evidence_based, sr_observational, recruiting], _THERAPY_Q)
    by_key = scores_by_key(out)

    # consensus guideline: 75 - 15 + 5 = 65
    assert by_key["MED/1"] == 65, f"consensus penalty expected 65, got {by_key['MED/1']}"
    cons_rat = [ar for ar in out if ar.record.citation_key() == "MED/1"][0].rationale
    assert "consensus" in cons_rat.lower(), cons_rat
    # evidence-based guideline: 75 + 10 + 5 = 90
    assert by_key["MED/2"] == 90, f"evidence-based bonus expected 90, got {by_key['MED/2']}"
    # SR of observational evidence: base 75 (OCEBM 2a) + 5 = 80
    assert by_key["MED/3"] == 80, f"SR-of-observational expected 80, got {by_key['MED/3']}"
    # recruiting CT.gov record: 40 - 10 + 5 = 35
    assert by_key["NCT/01234567"] == 35, (
        f"recruiting protocol penalty expected 35, got {by_key['NCT/01234567']}"
    )
    print("PASS 11: consensus -15, evidence-based +10, SR-of-observational 75, recruiting -10")


def t12_rationales_identity():
    app = Appraiser(llm=FailingLLM())
    records = [
        make_record("1", design=StudyDesign.RCT, pub_date=recent(0)),
        make_record("2", design=StudyDesign.COHORT, pub_date=None),
        make_record("3", design=StudyDesign.UNKNOWN, pub_date=recent(4), abstract=""),
    ]
    out = app.appraise(records, _THERAPY_Q)
    assert len(out) == 3
    for ar in out:
        assert isinstance(ar, AppraisedRecord)
        assert isinstance(ar.rationale, str) and ar.rationale.strip(), (
            f"rationale must be non-empty: {ar!r}"
        )
        assert any(ar.record is r for r in records), (
            "AppraisedRecord.record must be the SAME object as the input record"
        )
    cohort_row = [ar for ar in out if ar.record.citation_key() == "MED/2"][0]
    assert (
        "cohort (base 65)" in cohort_row.rationale
        and "no date (-5)" in cohort_row.rationale
        and "llm_unavailable" in cohort_row.rationale
    ), cohort_row.rationale
    print("PASS 12: rationales non-empty and auditable; record identity preserved")


def t13_low_coverage_fallback():
    # B1: >10% missing keys -> heuristic-only for the WHOLE batch.
    # 10 records, 8 valid entries + 1 hallucinated key = 2/10 = 20% missing.
    records = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 11)
    ]
    pure = scores_by_key(
        Appraiser(llm=FailingLLM()).appraise(records, _THERAPY_Q)
    )
    rankings = [
        {"citation_key": f"MED/{i}", "relevance": 100, "rationale": "direct"}
        for i in range(1, 9)
    ]
    rankings.append(
        {"citation_key": "MED/99999999", "relevance": 100, "rationale": "hallucinated"}
    )
    llm = ScriptedLLM([{"rankings": rankings}])
    out = Appraiser(llm=llm).appraise(records, _THERAPY_Q)

    assert len(out) == 10, f"all 10 records must be scored, got {len(out)}"
    for ar in out:
        key = ar.record.citation_key()
        assert ar.score == pure[key], (
            f"{key}: low-coverage batch must fall back to pure heuristic "
            f"({pure[key]}), got {ar.score}"
        )
        assert "llm_low_coverage" in ar.rationale, ar.rationale
        assert "llm_skipped" not in ar.rationale, (
            f"{key}: batch tag must be distinct from llm_skipped: {ar.rationale}"
        )
    print("PASS 13: >10% missing keys -> whole batch heuristic-only (llm_low_coverage)")


def t14_weak_bound_missing_abstract():
    # B2: pin the +/-15 bound for MISSING ABSTRACT (t06 only pinned UNKNOWN
    # design). CASE_REPORT, no date, blank abstract -> H = 25 - 5 - 5 = 15.
    # R = 100 -> blended 44.75, clamped by the weak bound to H + 15 = 30.
    # (With the solid +/-25 bound it would be 40 -- that is the point.)
    llm = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/1", "relevance": 100, "rationale": "direct"}]}]
    )
    out = Appraiser(llm=llm).appraise(
        [make_record("1", design=StudyDesign.CASE_REPORT, pub_date=None, abstract="   ")],
        _THERAPY_Q,
    )
    assert out[0].score == 30, (
        f"missing-abstract weak bound must clamp 44.75 to 30, got {out[0].score}"
    )
    print("PASS 14: +/-15 bound pinned for missing abstract (44.75 -> 30, not 40)")


def t15_future_date_band():
    # B3: a future publication date lands in the +5 recency band.
    app = Appraiser(llm=FailingLLM())
    future = make_record(
        "1", design=StudyDesign.COHORT, pub_date=date(_today.year + 1, 6, 1)
    )
    out = app.appraise([future], _THERAPY_Q)
    assert out[0].score == 70, f"future date must earn +5 (65 + 5), got {out[0].score}"
    assert "future-dated (+5)" in out[0].rationale, out[0].rationale
    print("PASS 15: future publication date lands in the +5 recency band")


def t16_batching():
    # B4: 21 records -> >= 3 LLM calls (batch size 10, halved from 20 -- see
    # _LLM_BATCH_SIZE's comment), each prompt holds <= 10 citation_keys,
    # every input key appears in exactly one prompt, all records scored.
    records = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 22)
    ]
    rankings = [
        {"citation_key": r.citation_key(), "relevance": 80, "rationale": "partial"}
        for r in records
    ]
    llm = ScriptedLLM([{"rankings": rankings}])
    out = Appraiser(llm=llm).appraise(records, _THERAPY_Q)

    assert len(out) == 21, f"all 21 records must be scored, got {len(out)}"
    assert len(llm.calls) >= 3, f"expected >= 3 LLM calls, got {len(llm.calls)}"

    seen: dict[str, int] = {}
    for call in llm.calls:
        keys = [
            ln.split("citation_key:")[1].strip()
            for ln in call["prompt"].splitlines()
            if ln.startswith("- citation_key:")
        ]
        assert len(keys) <= 10, f"prompt holds {len(keys)} citation_keys, max is 10"
        assert len(keys) == len(set(keys)), "no duplicate keys within one prompt"
        for k in keys:
            assert k not in seen, f"{k} appears in more than one prompt"
            seen[k] = 1
    expected = {r.citation_key() for r in records}
    assert set(seen) == expected, (
        f"every input key must appear in exactly one prompt: "
        f"missing={expected - set(seen)}, extra={set(seen) - expected}"
    )
    print("PASS 16: 21 records -> disjoint LLM batches of <= 10 keys; all scored")


def t17_sort_tiebreaks():
    # B5: pin the tie-breaks beyond score/base/date/abstract/citation_key.
    app = Appraiser(llm=FailingLLM())

    # (i) same score + design, different dates -> newer first.
    recs = [
        make_record("1", design=StudyDesign.RCT, pub_date=recent(2)),  # 85+5 = 90
        make_record("2", design=StudyDesign.RCT, pub_date=recent(1)),  # 85+5 = 90
    ]
    keys = [ar.record.citation_key() for ar in app.appraise(recs, _THERAPY_Q)]
    assert keys == ["MED/2", "MED/1"], f"newer date must sort first: {keys}"

    # (ii) same score + design, one undated -> dated first, None last.
    recs = [
        make_record("1", design=StudyDesign.COHORT, pub_date=None),  # 65-5 = 60
        make_record("2", design=StudyDesign.COHORT, pub_date=recent(7)),  # 65-5 = 60
    ]
    keys = [ar.record.citation_key() for ar in app.appraise(recs, _THERAPY_Q)]
    assert keys == ["MED/2", "MED/1"], f"undated record must sort last: {keys}"

    # (iii) same score + design + date, one with abstract -> abstract first.
    # Both are preprint RCTs, so the preprint cap 65 erases the -5 abstract
    # penalty and the scores tie; only the abstract tie-break separates them.
    recs = [
        make_record("1", design=StudyDesign.RCT, pub_date=recent(2), is_preprint=True),
        make_record(
            "2",
            design=StudyDesign.RCT,
            pub_date=recent(2),
            is_preprint=True,
            abstract="",
        ),
    ]
    out = app.appraise(recs, _THERAPY_Q)
    scores = [ar.score for ar in out]
    assert scores == [65, 65], f"both must hit the preprint cap 65: {scores}"
    keys = [ar.record.citation_key() for ar in out]
    assert keys == ["MED/1", "MED/2"], f"record with abstract must sort first: {keys}"
    print("PASS 17: tie-breaks: newer date, dated-over-undated, abstract-over-none")


def t18_regex_guards():
    # B6: GRADE / evidence-based / SR-of-non-randomized regex guards.
    app = Appraiser(llm=FailingLLM())

    def guideline(native_id, title):
        return make_record(
            native_id, design=StudyDesign.GUIDELINE, title=title, pub_date=recent(0)
        )

    def sr(native_id, title):
        return make_record(
            native_id,
            design=StudyDesign.SYSTEMATIC_REVIEW,
            title=title,
            pub_date=recent(0),
        )

    cases = [
        # (record, expected score, expect the evidence-based note?)
        (guideline("1", "Management of high-grade gliomas"), 80, False),
        (guideline("2", "Guideline developed based on a systematic review"), 90, True),
        (sr("3", "Systematic reviews and meta-analyses of observational studies"), 80, None),
        (sr("4", "Systematic review of case\u2013control studies"), 80, None),
        (guideline("5", "Guideline using the GRADE framework"), 90, True),
    ]
    out = app.appraise([c[0] for c in cases], _THERAPY_Q)
    by_key = {ar.record.citation_key(): ar for ar in out}
    for rec, expected, expect_note in cases:
        ar = by_key[rec.citation_key()]
        assert ar.score == expected, (
            f"{rec.title!r}: expected score {expected}, got {ar.score}"
        )
        if expect_note is True:
            assert "evidence-based" in ar.rationale, ar.rationale
        if expect_note is False:
            assert "evidence-based" not in ar.rationale, (
                f"plain 'grade' words must not earn the +10 bonus: {ar.rationale}"
            )
    print("PASS 18: high-grade/grade-3/Gleason stay plain; GRADE framework earns +10")


def t19_classifier_guards():
    # B7: substring false positives must classify as therapy (no boost).
    app = Appraiser(llm=FailingLLM())

    def cohort():
        return make_record("1", design=StudyDesign.COHORT, pub_date=recent(0))

    guard_questions = [
        "Do patients adhere to drug X better because of fewer side effects?",
        "Is the response to drug X unpredictable?",
        "What does the American Heart Association recommend for condition Y?",
    ]
    therapy = scores_by_key(app.appraise([cohort()], _THERAPY_Q))["MED/1"]
    prognosis = scores_by_key(app.appraise([cohort()], _PROGNOSIS_Q))["MED/1"]
    assert therapy == 70 and prognosis == 80, (therapy, prognosis)
    for q in guard_questions:
        got = scores_by_key(app.appraise([cohort()], q))["MED/1"]
        assert got == therapy, (
            f"question misrouted (expected therapy baseline {therapy}): "
            f"{q!r} -> {got}"
        )
    print("PASS 19: 'because'/'unpredictable'/'Association' questions stay therapy")


def t20_accuracy_marker_guard():
    # B8: "specifically enrolled" / "nonspecific symptoms" are prose, not
    # diagnostic-accuracy markers -> no +10 under a diagnosis question.
    app = Appraiser(llm=FailingLLM())
    rec = make_record(
        "1",
        design=StudyDesign.COHORT,
        title="Cohort study of patients with condition Y",
        abstract="We specifically enrolled 500 patients with nonspecific symptoms.",
        pub_date=recent(0),
    )
    got = scores_by_key(app.appraise([rec], _DIAGNOSIS_Q))["MED/1"]
    assert got == 70, (
        f"'specifically'/'nonspecific' must not count as accuracy markers: {got}"
    )
    print("PASS 20: 'specifically enrolled'/'nonspecific' earn no diagnostic bonus")


def t21_scripted_llm_empty():
    # B9: ScriptedLLM([]) must fail with a clear ValueError, not IndexError.
    try:
        ScriptedLLM([])
    except ValueError as exc:
        assert "scripted" in str(exc).lower(), f"unclear message: {exc}"
        print(f"PASS 21: ScriptedLLM([]) raises ValueError: {exc}")
    else:
        raise AssertionError("ScriptedLLM([]) must raise ValueError, not IndexError")


def t22_pool_cap_validation():
    # B10: negative pool_cap -> [] (not rows[:-3]); None -> DEFAULT_POOL_CAP.
    records = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 6)
    ]
    negative = Appraiser(pool_cap=-3, llm=FailingLLM())
    assert negative.pool_cap == 0, f"-3 must clamp to 0, got {negative.pool_cap}"
    assert negative.appraise(records, _THERAPY_Q) == [], (
        "negative pool_cap must yield [] (old code returned the top 2 via rows[:-3])"
    )

    none_cap = Appraiser(pool_cap=None, llm=FailingLLM())
    assert none_cap.pool_cap == DEFAULT_POOL_CAP == 30, none_cap.pool_cap
    many = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 36)
    ]
    out = none_cap.appraise(many, _THERAPY_Q)
    assert len(out) == 30, (
        f"pool_cap=None must cap at DEFAULT_POOL_CAP ({DEFAULT_POOL_CAP}): {len(out)}"
    )
    print("PASS 22: pool_cap=-3 -> []; pool_cap=None -> DEFAULT_POOL_CAP")


def t23_half_up_rounding():
    # B11: blended 36.5 must round half-up to 37, not banker's-round to 36.
    # H = 40 (CLINICAL_TRIAL_RECORD base 40, 3-5y old -> 0, abstract present);
    # R = 30 -> blend = 0.65*40 + 0.35*30 = 36.5 exactly (verified below).
    blended = _BLEND_W_HEURISTIC * 40 + _BLEND_W_LLM * 30
    assert blended == 36.5, f"hand-computed blend must be exactly 36.5: {blended}"
    llm = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/1", "relevance": 30, "rationale": "related"}]}]
    )
    out = Appraiser(llm=llm).appraise(
        [make_record("1", design=StudyDesign.CLINICAL_TRIAL_RECORD, pub_date=recent(4))],
        _THERAPY_Q,
    )
    assert out[0].score == 37, (
        f"blended 36.5 must round half-up to 37 (round() would give 36): "
        f"{out[0].score}"
    )
    print("PASS 23: blend rounding is half-up (36.5 -> 37, not banker's 36)")


def t24_llm_failure_memory():
    # A6: a dead endpoint is probed at most ONCE per appraise() call (not
    # once per batch), and the memory does not stick across calls.
    llm = FailingLLM()
    records = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 22)
    ]  # 21 records -> 2 batches
    out = Appraiser(llm=llm).appraise(records, _THERAPY_Q)
    assert len(out) == 21, f"all 21 records must still be scored, got {len(out)}"
    assert llm.calls == 1, (
        f"dead endpoint must be probed exactly once, not per batch: {llm.calls}"
    )
    assert all("llm_unavailable" in ar.rationale for ar in out)

    out2 = Appraiser(llm=llm).appraise(records[:3], _THERAPY_Q)
    assert llm.calls == 2, (
        f"failure memory is per-call; a new appraise() must probe again: {llm.calls}"
    )
    assert len(out2) == 3
    print("PASS 24: transport failure probed once per appraise() call, not sticky")


def t25_evidence_recalibration():
    # C1: recent preprint RCT with abstract, heuristic-only path:
    # H = 85 (RCT base) + 5 (recent) = 90 -> preprint hard cap 65 exactly.
    app = Appraiser(llm=FailingLLM())
    preprint_rct = make_record(
        "1", design=StudyDesign.RCT, pub_date=recent(0), is_preprint=True
    )
    out = app.appraise([preprint_rct], _THERAPY_Q)
    assert out[0].score == 65, (
        f"recent preprint RCT must land exactly on cap 65 (85+5=90 -> 65): "
        f"{out[0].score}"
    )
    assert "preprint cap 65" in out[0].rationale, out[0].rationale

    # C2: case-control under a diagnosis question with accuracy markers:
    # 55 (base) + 5 (OCEBM Level 4 diagnosis bonus) + 0 (3-year recency band)
    # = 60. Clean record: dated, abstract present, no other modifiers fire.
    cc = make_record(
        "2",
        design=StudyDesign.CASE_CONTROL,
        title="Case-control study of the accuracy of test T",
        abstract="Sensitivity and specificity of test T were 0.9.",
        pub_date=recent(3),
    )
    out_cc = app.appraise([cc], _DIAGNOSIS_Q)
    assert out_cc[0].score == 60, (
        f"case-control diagnosis bonus is +5: expected 55+5=60, got {out_cc[0].score}"
    )
    assert "OCEBM Level 4" in out_cc[0].rationale, out_cc[0].rationale
    print("PASS 25: preprint cap exactly 65; case-control diagnosis +5 -> 60")


def t26_on_progress_callback():
    # 21 records -> 3 batches (batch size 10, B4's own split); on_progress
    # must fire once per batch with 1-based (batch_index, batch_count), in
    # order.
    records = [
        make_record(str(i), design=StudyDesign.RCT, pub_date=recent(0))
        for i in range(1, 22)
    ]
    rankings = [
        {"citation_key": r.citation_key(), "relevance": 80, "rationale": "partial"}
        for r in records
    ]
    llm = ScriptedLLM([{"rankings": rankings}])
    calls: list[tuple[int, int]] = []
    Appraiser(llm=llm).appraise(
        records, _THERAPY_Q, on_progress=lambda i, n: calls.append((i, n))
    )
    assert calls == [(1, 3), (2, 3), (3, 3)], calls

    # A broken callback must never break appraisal itself (fail-open, same
    # rule as pipeline._emit).
    def boom(i, n):
        raise RuntimeError("simulated callback failure")

    out = Appraiser(llm=llm).appraise(records, _THERAPY_Q, on_progress=boom)
    assert len(out) == 21, "a raising on_progress must not stop appraisal"
    print("PASS 26: on_progress fires once per batch with (index, count); fails open")


def t27_raw_relevance_is_preserved():
    # Same bound-clamped case as t06: H=20, R=100 -> blended/capped B=45,
    # but AppraisedRecord.relevance must carry the RAW, un-blended R=100 --
    # this is the field pipeline._topical_score() needs to tell a
    # well-supported case report from an off-topic review.
    llm = ScriptedLLM(
        [{"rankings": [{"citation_key": "MED/1", "relevance": 100, "rationale": "direct"}]}]
    )
    out = Appraiser(llm=llm).appraise(
        [make_record("1", design=StudyDesign.CASE_REPORT, pub_date=None)], _THERAPY_Q
    )
    assert out[0].score == 45, f"blended score unaffected by this change: {out[0].score}"
    assert out[0].relevance == 100, f"relevance must be the raw LLM value, not blended: {out[0].relevance}"

    # A record the LLM's batch never covered (llm_skipped) -> relevance None.
    llm2 = ScriptedLLM([{"rankings": []}])
    out2 = Appraiser(llm=llm2).appraise(
        [make_record("2", design=StudyDesign.RCT, pub_date=recent(0))], _THERAPY_Q
    )
    assert out2[0].relevance is None, out2[0].relevance

    # Dead LLM -> heuristic-only -> relevance None for every record.
    out3 = Appraiser(llm=FailingLLM()).appraise(
        [make_record("3", design=StudyDesign.RCT, pub_date=recent(0))], _THERAPY_Q
    )
    assert out3[0].relevance is None, out3[0].relevance
    print("PASS 27: AppraisedRecord.relevance carries the raw LLM score; None when skipped/unavailable")


def run():
    t01_empty_input()
    t02_hierarchy()
    t03_retraction()
    t04_preprint_cap()
    t05_recency()
    t06_blend_math()
    t07_hallucination()
    t08_pool_cap()
    t09_sorting()
    t10_question_type()
    t11_misc_rules()
    t12_rationales_identity()
    t13_low_coverage_fallback()
    t14_weak_bound_missing_abstract()
    t15_future_date_band()
    t16_batching()
    t17_sort_tiebreaks()
    t18_regex_guards()
    t19_classifier_guards()
    t20_accuracy_marker_guard()
    t21_scripted_llm_empty()
    t22_pool_cap_validation()
    t23_half_up_rounding()
    t24_llm_failure_memory()
    t25_evidence_recalibration()
    t26_on_progress_callback()
    t27_raw_relevance_is_preserved()
    print("\nAll appraiser tests passed.")


if __name__ == "__main__":
    run()
