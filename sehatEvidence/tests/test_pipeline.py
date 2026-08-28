"""
Offline test for pipeline.py -- no network, no real LLM, no pytest.

Run from the project root (sehatEvidence/):
    python -m tests.test_pipeline

Plain-script convention (same as tests/test_verifier.py). Every
collaborator the pipeline takes is injected, so all ten cases drive the
real orchestration with scripted fakes:

  t01 happy path (S-id minting, funnel, red-team flag merge, disclaimer)
  t02 synthesizer abstention (INSUFFICIENT_EVIDENCE)
  t03 dead LLM end-to-end with the REAL agents (no key survives)
  t04 mock mode (replays demo/mock_response.json, touches nothing else)
  t05 pool < 2 retrieved records
  t06 fewer than 2 records at relevance >= 60
  t07 verifier abstains (reasons propagate, red team skipped)
  t08 red team raises (fail-open, no flags)
  t09 top-level LLMError net ("LLM unavailable", empty report)
  t10 strategist LLMError -> raw question used as the only query
"""

import contextlib
import io
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DISCLAIMER, FailoverLLMClient, Settings
from agents.appraiser import Appraiser, AppraisedRecord
from agents.red_team import RedTeam, RedTeamFlag
from agents.strategist import Strategist
from agents.synthesizer import Sentence, Synthesizer, SynthesisResult
from agents.verifier import Claim, ClaimCheck, VerificationReport, Verifier
from core.llm import LLMError
from core.schema import EvidenceRecord, SourceDB, StudyDesign

import pipeline as pipeline_module
from pipeline import (
    ABSTAIN_LLM_DEAD,
    ABSTAIN_LLM_SYNTHESIS,
    ABSTAIN_LOW_RELEVANCE,
    ABSTAIN_SYNTH_INSUFFICIENT,
    ABSTAIN_THIN_POOL,
    EvidencePipeline,
    build_default_pipeline,
)

QUESTION = "Does drug X reduce mortality in adults with condition Y?"
QUERIES = [
    "drug X condition Y mortality outcomes",
    "condition Y treatment systematic review",
    "drug X adverse effects adults",
]

SETTINGS = Settings(pool_cap=30, enable_supersession=False)


# --- record fixtures ----------------------------------------------------------


def make_record(
    native_id: str,
    design: StudyDesign,
    *,
    source: SourceDB = SourceDB.PUBMED,
    title: str = "",
    abstract: str = "",
    journal: str = "Journal of Evidence",
    pub: date = date(2024, 3, 1),
    doi: str = None,
    url: str = None,
    is_preprint: bool = False,
    is_retracted: bool = False,
    trial_status: str = None,
) -> EvidenceRecord:
    """One EvidenceRecord with sane defaults; only what a test cares about
    is ever passed explicitly."""
    return EvidenceRecord(
        record_id=f"rec-{native_id}",
        source=source,
        native_id=native_id,
        doi=doi or f"10.1000/{native_id}",
        title=title or f"Study {native_id} of drug X in condition Y",
        abstract=abstract or f"Abstract for study {native_id}: drug X versus placebo.",
        journal=journal,
        publication_date=pub,
        study_design=design,
        is_preprint=is_preprint,
        is_retracted=is_retracted,
        trial_status=trial_status,
        url=url or f"https://pubmed.ncbi.nlm.nih.gov/{native_id}/",
    )


def five_records() -> list[EvidenceRecord]:
    """A varied 5-record pool: SR, meta-analysis, RCT, cohort, trial record."""
    return [
        make_record("11111", StudyDesign.SYSTEMATIC_REVIEW),
        make_record("22222", StudyDesign.META_ANALYSIS),
        make_record("33333", StudyDesign.RCT),
        make_record("44444", StudyDesign.COHORT),
        make_record(
            "NCT55555",
            StudyDesign.CLINICAL_TRIAL_RECORD,
            source=SourceDB.CLINICAL_TRIALS,
            trial_status="RECRUITING",
            url="https://clinicaltrials.gov/study/NCT55555",
        ),
    ]


# --- fakes for every injected collaborator -----------------------------------


class FakeStrategist:
    """plan_queries() returns scripted queries, or raises `error`."""

    def __init__(self, queries: list = None, error: Exception = None) -> None:
        self.queries = list(QUERIES) if queries is None else list(queries)
        self.error = error
        self.calls: list = []

    def plan_queries(self, question: str, k: int = 3) -> list:
        self.calls.append((question, k))
        if self.error is not None:
            raise self.error
        return list(self.queries)


class FakeGather:
    """gather_fn: records a call, returns a scripted pool or raises."""

    def __init__(self, records: list = None, error: Exception = None) -> None:
        self.records = list(records or [])
        self.error = error
        self.calls: list = []

    def __call__(self, queries: list) -> list:
        self.calls.append(list(queries))
        if self.error is not None:
            raise self.error
        return list(self.records)


class FakeAppraiser:
    """Scripted scores, best-first, retracted records excluded (as the real
    Appraiser does). Records beyond the script get 50."""

    def __init__(self, scores: list) -> None:
        self.scores = list(scores)
        self.calls: list = []

    def appraise(self, records: list, question: str) -> list:
        self.calls.append((list(records), question))
        live = [r for r in records if not r.is_retracted]
        out = [
            AppraisedRecord(
                record=record,
                score=self.scores[i] if i < len(self.scores) else 50,
                rationale=f"scripted rationale for {record.citation_key()}",
            )
            for i, record in enumerate(live)
        ]
        out.sort(key=lambda ap: -ap.score)
        return out


class FakeSynthesizer:
    """Returns a scripted SynthesisResult, or raises `error`."""

    def __init__(self, result: SynthesisResult = None, error: Exception = None) -> None:
        self.result = result
        self.error = error
        self.calls: list = []

    def synthesize(self, question: str, evidence: list) -> SynthesisResult:
        self.calls.append((question, evidence))
        if self.error is not None:
            raise self.error
        return self.result


class FakeVerifier:
    """Returns a pre-built VerificationReport and remembers its inputs."""

    def __init__(self, report: VerificationReport) -> None:
        self.report = report
        self.calls: list = []

    def verify(
        self, question: str, synthesis, evidence: list, queries: list
    ) -> VerificationReport:
        self.calls.append((question, synthesis, evidence, queries))
        return self.report


class FakeRedTeam:
    """Returns scripted flags, or raises `error` (fail-open exercise)."""

    def __init__(self, flags: list = None, error: Exception = None) -> None:
        self.flags = list(flags or [])
        self.error = error
        self.calls: list = []

    def audit(self, question: str, kept_claims: list) -> list:
        self.calls.append((question, list(kept_claims)))
        if self.error is not None:
            raise self.error
        return list(self.flags)


class DeadLLM:
    """Every call fails -- the "no key survives" case."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt, system=None, temperature=0.2):
        self.calls += 1
        raise LLMError("simulated transport failure")

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        raise LLMError("simulated transport failure")


class BoomStrategist:
    """Any use is a test failure (mock mode must call nothing)."""

    def plan_queries(self, question: str, k: int = 3) -> list:
        raise AssertionError("strategist must not run in mock mode")


def boom_gather(queries: list) -> list:
    raise AssertionError("retrieval must not run in mock mode")


class BoomAgent:
    """Any method call is a test failure."""

    def appraise(self, records, question):
        raise AssertionError("appraiser must not run")

    def synthesize(self, question, evidence):
        raise AssertionError("synthesizer must not run")

    def verify(self, question, synthesis, evidence, queries):
        raise AssertionError("verifier must not run")

    def audit(self, question, kept_claims):
        raise AssertionError("red team must not run")


# --- scripted synthesis / verification -----------------------------------------


def happy_synthesis() -> SynthesisResult:
    return SynthesisResult(
        raw_text=(
            "Drug X reduces mortality in adults with condition Y [S1]. "
            "It is well tolerated [S2]. "
            "It eliminates the need for monitoring [S3]."
        ),
        sentences=[
            Sentence(0, "Drug X reduces mortality in adults with condition Y", ["S1"]),
            Sentence(1, "It is well tolerated", ["S2"]),
            Sentence(2, "It eliminates the need for monitoring", ["S3"]),
        ],
        abstained=False,
        parse_deletions=[],
    )


def make_claim(
    claim_id: str,
    text: str,
    status: str,
    sid: str,
    *,
    deletion_reason: str = None,
    flags: list = None,
    entailment: str = "supports",
    standing: str = "pass",
    verdict: str = "SUPPORTS",
    confidence: float = 0.9,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        status=status,
        deletion_reason=deletion_reason,
        flags=list(flags or []),
        checks=ClaimCheck(existence="pass", entailment=entailment, standing=standing),
        verdict=verdict,
        confidence=confidence,
        evidence_quote="drug X versus placebo",
        citations=[
            {
                "sid": sid,
                "citation_key": "MED/11111",
                "title": "Study 11111 of drug X in condition Y",
                "url": "https://pubmed.ncbi.nlm.nih.gov/11111/",
            }
        ],
    )


def happy_report() -> VerificationReport:
    """2 kept, 1 deleted -- the funnel the UI renders."""
    kept_a = make_claim(
        "s0-c1", "Drug X reduces mortality in adults with condition Y", "kept", "S1"
    )
    kept_b = make_claim("s1-c1", "It is well tolerated", "kept", "S2")
    deleted = make_claim(
        "s2-c1",
        "It eliminates the need for monitoring",
        "deleted",
        "S3",
        deletion_reason="unsupported by cited evidence",
        entailment="nei",
        standing="skipped",
        verdict="NOT_ENOUGH_INFO",
        confidence=0.1,
    )
    return VerificationReport(
        claims=[kept_a, kept_b, deleted],
        funnel={
            "claims_generated": 3,
            "claims_deleted": 1,
            "claims_kept": 2,
            "by_reason": {"unsupported by cited evidence": 1},
        },
        abstained=False,
        abstain_reasons=[],
        answer_text=f"{kept_a.text} {kept_b.text}",
    )


# --- harness ------------------------------------------------------------------


def run_captured(pipeline: EvidencePipeline, question: str, use_mock: bool = False):
    """Run the pipeline with its [pipeline] logging captured.

    Returns (report, log_text). The captured log is printed before any
    exception escapes, so a failure never hides the stage trace.
    """
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            report = pipeline.run(question, use_mock=use_mock)
    except BaseException:
        print(buffer.getvalue())
        raise
    return report, buffer.getvalue()


def build_pipeline(
    *,
    strategist=None,
    gather=None,
    appraiser=None,
    synthesizer=None,
    red_team=None,
    verifier=None,
    settings: Settings = SETTINGS,
) -> EvidencePipeline:
    """EvidencePipeline over fakes, with the happy-path default for each."""
    return EvidencePipeline(
        strategist=strategist or FakeStrategist(),
        gather_fn=gather or FakeGather(five_records()),
        appraiser=appraiser or FakeAppraiser([90, 85, 75, 65, 60]),
        synthesizer=synthesizer or FakeSynthesizer(happy_synthesis()),
        red_team=red_team or FakeRedTeam(),
        verifier=verifier or FakeVerifier(happy_report()),
        settings=settings,
    )


def assert_report_shape(report: dict) -> None:
    """Every report -- answer or abstention -- carries the same keys."""
    expected = {
        "question",
        "abstained",
        "abstain_reasons",
        "funnel",
        "answer_text",
        "claims",
        "evidence",
        "disclaimer",
    }
    assert set(report) == expected, f"report keys: {sorted(report)}"
    assert report["disclaimer"] == DISCLAIMER, "disclaimer must be verbatim"
    funnel = report["funnel"]
    assert set(funnel) == {
        "claims_generated",
        "claims_deleted",
        "claims_kept",
        "by_reason",
    }, f"funnel keys: {sorted(funnel)}"


def assert_abstained(report: dict, reason: str) -> None:
    assert_report_shape(report)
    assert report["abstained"] is True, "expected an abstention"
    assert report["abstain_reasons"] == [reason], report["abstain_reasons"]
    assert report["answer_text"] == "", report["answer_text"]
    assert report["claims"] == [], report["claims"]
    assert report["funnel"] == {
        "claims_generated": 0,
        "claims_deleted": 0,
        "claims_kept": 0,
        "by_reason": {},
    }, report["funnel"]


# --- t01 happy path ------------------------------------------------------------


def test_happy_path() -> None:
    strategist = FakeStrategist()
    gather = FakeGather(five_records())
    appraiser = FakeAppraiser([90, 85, 75, 65, 60])
    synthesizer = FakeSynthesizer(happy_synthesis())
    verifier = FakeVerifier(happy_report())
    red_team = FakeRedTeam(
        [
            RedTeamFlag(
                claim_id="s0-c1",
                flag="overstatement",
                note="Effect is stated more strongly than the pooled estimate supports.",
            )
        ]
    )
    pipe = build_pipeline(
        strategist=strategist,
        gather=gather,
        appraiser=appraiser,
        synthesizer=synthesizer,
        verifier=verifier,
        red_team=red_team,
    )

    report, log = run_captured(pipe, QUESTION)
    assert_report_shape(report)

    assert report["question"] == QUESTION
    assert report["abstained"] is False, report["abstain_reasons"]
    assert report["abstain_reasons"] == []
    assert report["answer_text"], "answer_text must be non-empty"
    assert report["answer_text"] == happy_report().answer_text
    assert report["funnel"] == {
        "claims_generated": 3,
        "claims_deleted": 1,
        "claims_kept": 2,
        "by_reason": {"unsupported by cited evidence": 1},
    }, report["funnel"]

    # Stages ran once each, on the frozen pool and the planned queries.
    assert gather.calls == [QUERIES], gather.calls
    assert len(appraiser.calls) == 1
    assert len(appraiser.calls[0][0]) == 5, "appraiser sees the FULL pool"
    assert verifier.calls[0][0] == QUESTION
    assert verifier.calls[0][3] == QUERIES, "verifier gets the same queries"

    # Evidence: one item per appraised record, S-ids best-first.
    evidence = report["evidence"]
    assert len(evidence) == 5, len(evidence)
    assert [item["sid"] for item in evidence] == ["S1", "S2", "S3", "S4", "S5"]
    assert [item["relevance_score"] for item in evidence] == [90, 85, 75, 65, 60]
    assert evidence[0]["citation_key"] == "MED/11111", evidence[0]["citation_key"]
    assert evidence[0]["study_design"] == "systematic_review"
    assert evidence[4]["citation_key"] == "NCT/NCT55555"
    assert evidence[4]["trial_status"] == "RECRUITING"
    assert evidence[0]["abstract"], "the judge needs the abstract text"
    assert evidence[0]["rationale"].startswith("scripted rationale")
    # The synthesizer and the verifier see the SAME evidence items.
    assert synthesizer.calls[0][1] is evidence
    assert verifier.calls[0][2] is evidence

    # Claims: serialized in generation order, deleted one included.
    claims = report["claims"]
    assert [c["claim_id"] for c in claims] == ["s0-c1", "s1-c1", "s2-c1"]
    assert [c["status"] for c in claims] == ["kept", "kept", "deleted"]
    assert claims[2]["deletion_reason"] == "unsupported by cited evidence"
    assert claims[0]["checks"] == {
        "existence": "pass",
        "entailment": "supports",
        "standing": "pass",
    }, claims[0]["checks"]
    assert claims[2]["checks"]["entailment"] == "nei"
    assert claims[0]["verdict"] == "SUPPORTS"
    assert claims[0]["confidence"] == 0.9
    assert claims[0]["citations"][0]["sid"] == "S1"

    # Red team audited only the survivors and its flag was merged.
    assert len(red_team.calls) == 1
    audited = red_team.calls[0][1]
    assert [c.claim_id for c in audited] == ["s0-c1", "s1-c1"], "survivors only"
    assert len(claims[0]["flags"]) == 1, claims[0]["flags"]
    assert claims[0]["flags"][0].startswith("overstatement: "), claims[0]["flags"]
    assert claims[1]["flags"] == []
    assert claims[2]["flags"] == []

    for expected_line in (
        "[pipeline] starting: ",
        "[pipeline] retrieved 5 records",
        "[pipeline] appraised 5 records",
        "[pipeline] synthesizer produced 3 sentences",
        "[pipeline] verification: 3 generated -> 1 deleted -> 2 kept",
        "[pipeline] red team: 1 flags",
        "[pipeline] complete",
    ):
        assert expected_line in log, f"missing log line: {expected_line!r}"

    # The report is what api/server.py hands straight to json.dumps() -- a
    # dict looking fine in Python (e.g. a raw datetime.date slipping into a
    # field) is not the same as it being JSON-serializable, and nothing
    # above actually crosses that boundary. Regression coverage for
    # exactly that gap: EvidenceRecord.publication_date is a real `date`
    # (see make_record's default), and _build_evidence_items() once copied
    # it straight into the evidence dict unconverted.
    json.dumps(report)
    assert isinstance(evidence[0]["publication_date"], str), (
        "publication_date must be serialized to a string, not left as a "
        f"{type(evidence[0]['publication_date']).__name__}"
    )

    print("PASS 1: happy path -- 5 evidence items, funnel 3/1/2, flag merged")


# --- t02 synthesizer abstention -------------------------------------------------


def test_synthesizer_abstains() -> None:
    synthesizer = FakeSynthesizer(
        SynthesisResult(
            raw_text="INSUFFICIENT_EVIDENCE",
            sentences=[],
            abstained=True,
            parse_deletions=[],
        )
    )
    verifier = BoomAgent()
    red_team = BoomAgent()
    pipe = build_pipeline(
        synthesizer=synthesizer, verifier=verifier, red_team=red_team
    )

    report, log = run_captured(pipe, QUESTION)
    assert_abstained(report, ABSTAIN_SYNTH_INSUFFICIENT)
    assert "synthesizer judged evidence insufficient" in report["abstain_reasons"][0]
    # The pool it found is still reported, so the abstention is auditable.
    assert len(report["evidence"]) == 5, report["evidence"]
    assert "[pipeline] abstained:" in log

    print("PASS 2: INSUFFICIENT_EVIDENCE -- abstains, no claims, pool still shown")


# --- t03 dead LLM, real agents ---------------------------------------------------


def test_llm_totally_dead() -> None:
    dead = DeadLLM()
    llm = FailoverLLMClient(clients=[dead])
    pipe = EvidencePipeline(
        strategist=Strategist(llm=llm),
        gather_fn=FakeGather(five_records()),
        appraiser=Appraiser(pool_cap=SETTINGS.pool_cap, llm=llm),
        synthesizer=Synthesizer(llm=llm),
        red_team=RedTeam(llm=llm),
        verifier=Verifier(llm=llm, enable_supersession=False),
        settings=SETTINGS,
    )

    report, _log = run_captured(pipe, QUESTION)
    assert_report_shape(report)
    assert report["abstained"] is True
    assert len(report["abstain_reasons"]) == 1, report["abstain_reasons"]
    reason = report["abstain_reasons"][0]
    assert reason == ABSTAIN_LLM_SYNTHESIS, reason
    assert "LLM unavailable" in reason, reason
    assert report["answer_text"] == ""
    assert report["claims"] == []
    assert report["funnel"]["claims_generated"] == 0
    assert dead.calls > 0, "the failover client did try the dead key"

    print("PASS 3: dead LLM (real agents) -- abstains with 'LLM unavailable'")


# --- t04 mock mode ---------------------------------------------------------------


def test_mock_mode() -> None:
    path = pipeline_module.MOCK_RESPONSE_PATH
    original = path.read_bytes() if path.exists() else None
    canned = {
        "question": "canned demo question",
        "abstained": False,
        "abstain_reasons": [],
        "funnel": {
            "claims_generated": 4,
            "claims_deleted": 1,
            "claims_kept": 3,
            "by_reason": {"unsupported by cited evidence": 1},
        },
        "answer_text": "Canned answer sentence one. Canned answer sentence two.",
        "claims": [{"claim_id": "s0-c1", "text": "Canned claim", "status": "kept"}],
        "evidence": [{"sid": "S1", "citation_key": "MED/99999"}],
        "disclaimer": DISCLAIMER,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(canned, indent=2), encoding="utf-8")
        # Every collaborator is a booby trap: mock mode must touch none.
        pipe = EvidencePipeline(
            strategist=BoomStrategist(),
            gather_fn=boom_gather,
            appraiser=BoomAgent(),
            synthesizer=BoomAgent(),
            red_team=BoomAgent(),
            verifier=BoomAgent(),
            settings=SETTINGS,
        )
        report, log = run_captured(pipe, QUESTION, use_mock=True)
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)

    assert report == canned, report
    assert report["question"] == "canned demo question", "replayed verbatim"
    assert "[pipeline] mock mode:" in log

    print("PASS 4: mock mode -- replays demo/mock_response.json verbatim")


# --- t05 thin pool ---------------------------------------------------------------


def test_pool_below_minimum() -> None:
    gather = FakeGather([make_record("11111", StudyDesign.RCT)])
    appraiser = BoomAgent()
    pipe = build_pipeline(
        gather=gather, appraiser=appraiser, synthesizer=BoomAgent(), verifier=BoomAgent()
    )

    report, log = run_captured(pipe, QUESTION)
    assert_abstained(report, ABSTAIN_THIN_POOL)
    assert report["evidence"] == [], "nothing was appraised, so no evidence items"
    assert "[pipeline] retrieved 1 records" in log

    print("PASS 5: pool < 2 records -- abstains before appraisal")


# --- t06 not enough high-relevance records ---------------------------------------


def test_few_high_relevance() -> None:
    appraiser = FakeAppraiser([59, 55, 40, 30, 20])
    synthesizer = FakeSynthesizer(happy_synthesis())
    pipe = build_pipeline(
        appraiser=appraiser, synthesizer=synthesizer, verifier=BoomAgent()
    )

    report, _log = run_captured(pipe, QUESTION)
    assert_abstained(report, ABSTAIN_LOW_RELEVANCE)
    assert len(report["evidence"]) == 5, "the low-relevance pool is still reported"
    assert synthesizer.calls == [], "synthesis never runs on a low-relevance pool"

    # One record at 60 is still not two: the threshold is inclusive but the
    # count is not met.
    pipe = build_pipeline(appraiser=FakeAppraiser([60, 59, 40, 30, 20]))
    report, _log = run_captured(pipe, QUESTION)
    assert_abstained(report, ABSTAIN_LOW_RELEVANCE)

    print("PASS 6: fewer than 2 records at relevance >= 60 -- abstains")


# --- t07 verifier abstains --------------------------------------------------------


def test_verifier_abstains() -> None:
    deleted_a = make_claim(
        "s0-c1",
        "Drug X reduces mortality in adults with condition Y",
        "deleted",
        "S1",
        deletion_reason="unsupported by cited evidence",
        entailment="nei",
        standing="skipped",
        verdict="NOT_ENOUGH_INFO",
        confidence=0.2,
    )
    deleted_b = make_claim(
        "s1-c1",
        "It is well tolerated",
        "deleted",
        "S2",
        deletion_reason="source retracted (pubmed)",
        entailment="supports",
        standing="fail",
    )
    reasons = [
        "all claims deleted during verification",
        "post-verification collapse: only 0/2 claims survived",
    ]
    verifier = FakeVerifier(
        VerificationReport(
            claims=[deleted_a, deleted_b],
            funnel={
                "claims_generated": 2,
                "claims_deleted": 2,
                "claims_kept": 0,
                "by_reason": {
                    "unsupported by cited evidence": 1,
                    "source retracted (pubmed)": 1,
                },
            },
            abstained=True,
            abstain_reasons=list(reasons),
            answer_text="",
        )
    )
    red_team = FakeRedTeam()
    pipe = build_pipeline(verifier=verifier, red_team=red_team)

    report, log = run_captured(pipe, QUESTION)
    assert_report_shape(report)
    assert report["abstained"] is True
    assert report["abstain_reasons"] == reasons, report["abstain_reasons"]
    assert report["answer_text"] == ""
    # The verifier's own funnel and claims survive the abstention: this is
    # exactly the "9 generated -> 9 deleted" story the UI must be able to tell.
    assert report["funnel"]["claims_deleted"] == 2, report["funnel"]
    assert [c["status"] for c in report["claims"]] == ["deleted", "deleted"]
    assert red_team.calls == [], "no answer is shown, so no rhetorical audit"
    assert "[pipeline] abstained:" in log

    print("PASS 7: verifier abstention -- reasons and funnel propagate verbatim")


# --- t08 red team fail-open -------------------------------------------------------


def test_red_team_fail_open() -> None:
    red_team = FakeRedTeam(error=RuntimeError("adversarial reviewer exploded"))
    pipe = build_pipeline(red_team=red_team)

    report, log = run_captured(pipe, QUESTION)
    assert_report_shape(report)
    assert report["abstained"] is False, report["abstain_reasons"]
    assert report["answer_text"] == happy_report().answer_text
    assert report["funnel"]["claims_kept"] == 2
    assert all(c["flags"] == [] for c in report["claims"]), report["claims"]
    assert len(red_team.calls) == 1, "it was called -- and its failure absorbed"
    assert "[pipeline] red team failed" in log
    assert "[pipeline] complete" in log

    print("PASS 8: red team raises -- fail-open, answer intact, no flags")


# --- t09 top-level LLMError net ----------------------------------------------------


def test_top_level_llm_net() -> None:
    gather = FakeGather(error=LLMError("All 3 LLM keys failed"))
    pipe = build_pipeline(gather=gather, appraiser=BoomAgent(), verifier=BoomAgent())

    report, log = run_captured(pipe, QUESTION)
    assert_abstained(report, ABSTAIN_LLM_DEAD)
    assert report["abstain_reasons"] == ["LLM unavailable"]
    assert report["evidence"] == []
    assert "[pipeline] LLM unavailable" in log

    print("PASS 9: top-level LLMError net -- 'LLM unavailable', empty report")


# --- t10 strategist fail-open -------------------------------------------------------


def test_strategist_fail_open() -> None:
    strategist = FakeStrategist(error=LLMError("planner key dead"))
    gather = FakeGather(five_records())
    pipe = build_pipeline(strategist=strategist, gather=gather)

    report, log = run_captured(pipe, QUESTION)
    assert report["abstained"] is False, report["abstain_reasons"]
    assert gather.calls == [[QUESTION]], gather.calls
    assert "[pipeline] strategist unavailable" in log

    # An empty plan (blank question path) degrades the same way.
    strategist = FakeStrategist(queries=[])
    gather = FakeGather(five_records())
    pipe = build_pipeline(strategist=strategist, gather=gather)
    report, _log = run_captured(pipe, QUESTION)
    assert gather.calls == [[QUESTION]], gather.calls

    print("PASS 10: strategist unavailable -- raw question used as the query")


# --- factory smoke test ---------------------------------------------------------------


def test_factory_is_importable() -> None:
    """build_default_pipeline wires all five agents over ONE failover client.

    Constructing clients does no network I/O, so this stays offline; with no
    keys configured FailoverLLMClient raises ValueError, which is the honest
    "nothing to wire" outcome rather than a silent half-built pipeline.
    """
    settings = Settings(llm_api_keys=["k1", "k2"], pool_cap=7)
    pipe = build_default_pipeline(settings=settings)
    assert isinstance(pipe, EvidencePipeline)
    assert pipe.settings is settings
    assert pipe.appraiser.pool_cap == 7
    assert pipe.gather_fn is pipeline_module.gather_evidence
    assert pipe.strategist.llm is pipe.synthesizer.llm, "one shared LLM client"
    assert pipe.strategist.llm.client_count == 2

    keyless = Settings(llm_api_keys=[])
    try:
        build_default_pipeline(settings=keyless)
    except ValueError:
        pass
    else:
        raise AssertionError("a keyless build must raise ValueError")

    print("PASS 11: build_default_pipeline -- shared failover client, pool cap wired")


def run() -> None:
    test_happy_path()
    test_synthesizer_abstains()
    test_llm_totally_dead()
    test_mock_mode()
    test_pool_below_minimum()
    test_few_high_relevance()
    test_verifier_abstains()
    test_red_team_fail_open()
    test_top_level_llm_net()
    test_strategist_fail_open()
    test_factory_is_importable()
    print("All pipeline tests passed.")


if __name__ == "__main__":
    run()
