"""
benchmark/run_benchmark.py -- EvidenceBoard three-arm benchmark (Phase 4a).

Answers ONE question with three different levels of machinery and measures
what changes:

    bare-LLM      one LLM call, "answer with PubMed IDs". No retrieval, no
                  verification -- the fabrication baseline.
    naive-RAG     Strategist -> retrieval -> Appraiser -> Synthesizer
                  (citation-forced). Stops BEFORE the Verifier: citations
                  are real records, but nothing checks that they say what
                  the answer claims.
    full-pipeline pipeline.build_default_pipeline().run(question) -- every
                  stage including the existence / entailment / standing gate
                  and the Red Team pass.

Measurement, not verification
-----------------------------
Only the full pipeline is allowed to DELETE anything. To get comparable
numbers for the two weaker arms, their output is passed through a
MEASUREMENT-ONLY Verifier afterwards: the claims it would have deleted are
counted for the metrics table but never removed from the arm's answer. That
is the whole point -- naive-RAG still shows the doctor a claim its own
citation does not support; the benchmark just counts how often.

Seeded failures
---------------
Two records are injected into the retrieval pool of the FULL arm only
(documented in the results file as well):

    MED/99999999   a fabricated PMID -- tests the existence check. It never
                   resolves against PubMed esummary, so any claim citing it
                   must be deleted as "citation unresolvable".
    MED/21177010   Wakefield's retracted MMR/autism paper, fetched LIVE so
                   its retraction flag comes from PubMed's own curation --
                   tests the standing check (the Appraiser must exclude it
                   from the ranked pool, or the Verifier must delete every
                   claim citing it).

The retracted seed is verified to exist (and to actually be flagged
retracted) with one PubMed efetch before the run; if PubMed is unreachable
the seeded tests are skipped and the affected metric is reported as n/a
rather than as a zero.

Reproducibility
---------------
Fixed temperature 0.1 for the bare arm, evidence pools sorted by
citation_key before appraisal, and questions processed in file order. The
agents' own internal temperatures (synthesis 0.2, judge 0.0) are their
contract and are deliberately not overridden here.

Usage
-----
    python -m benchmark.run_benchmark [--max N] [--arm bare|naive|full|all]
                                      [--offline-sample]

--offline-sample replaces the LLM, the retrieval fan-out and every registry
HTTP call with deterministic fixtures, so the whole harness can be smoke-
tested (or run in CI) with no network and no API keys. Offline numbers
describe the FIXTURES, not the real world.

Results are written to benchmark/results.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import FailoverLLMClient, Settings, get_settings
from agents.appraiser import Appraiser
from agents.red_team import RedTeam
from agents.strategist import Strategist
from agents.synthesizer import Sentence, Synthesizer, SynthesisResult
from agents.verifier import Verifier
from core.llm import LLMError
from core.schema import EvidenceRecord, SourceDB, StudyDesign
from pipeline import (
    MIN_POOL_RECORDS,
    EvidencePipeline,
    build_default_pipeline,
    serialize_claim,
)
# The S-id namespace is minted by the pipeline, and the naive-RAG arm must
# mint it EXACTLY the same way for its numbers to be comparable -- so the
# pipeline's own helper is reused rather than reimplemented here.
from pipeline import _build_evidence_items as build_evidence_items
from retrieval.retrieve import gather_evidence

QUESTIONS_PATH = Path(__file__).parent / "questions.json"
RESULTS_PATH = Path(__file__).parent / "results.md"

#: Every arm the runner knows, in report order.
ARMS = ("bare", "naive", "full")
ARM_LABELS = {"bare": "Bare LLM", "naive": "Naive RAG", "full": "Full Pipeline"}

#: Fixed for reproducibility (the bare arm is the only call this runner owns).
TEMPERATURE = 0.1

#: Seeded failure identifiers -- see the module docstring.
FABRICATED_PMID = "99999999"
RETRACTED_PMID = "21177010"

#: Categories whose questions have real literature behind them. Abstaining
#: on these is a miss; abstaining on "no_evidence" is a hit.
EVIDENCE_CATEGORIES = frozenset(
    {"therapy", "diagnosis", "prognosis", "harm", "supersession"}
)
NO_EVIDENCE_CATEGORY = "no_evidence"

_BARE_SYSTEM = (
    "You are a clinical evidence assistant answering a physician's question. "
    "Cite the PubMed IDs of the studies you rely on, in the form PMID: "
    "12345678, immediately after the sentence that uses them."
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
#: PMIDs as a bare LLM writes them: "PMID: 12345678", "PMID 12345678",
#: "[12345678]", or a pubmed.ncbi.nlm.nih.gov link.
_PMID_RE = re.compile(
    r"PMID[:\s#]*(\d{4,8})"
    r"|pubmed\.ncbi\.nlm\.nih\.gov/(\d{4,8})"
    r"|\[(\d{5,8})\]",
    re.IGNORECASE,
)
#: An unverified arm "abstained" when it says so in prose (the pipeline
#: arms report abstention structurally instead).
_REFUSAL_RE = re.compile(
    r"INSUFFICIENT_EVIDENCE"
    r"|no (?:published |reliable |good |high[- ]quality |clinical )?evidence"
    r"|cannot (?:be )?(?:answer|answered|reliably answer)"
    r"|not enough (?:evidence|information|data)"
    r"|there is no evidence",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def load_questions(limit: Optional[int] = None) -> list[dict]:
    """Load benchmark/questions.json in file order, optionally truncated."""
    with QUESTIONS_PATH.open(encoding="utf-8") as handle:
        questions = json.load(handle)
    if not isinstance(questions, list):
        raise ValueError(
            f"questions.json must contain a JSON array, got "
            f"{type(questions).__name__}"
        )
    if limit is not None and limit > 0:
        questions = questions[:limit]
    return questions


# ---------------------------------------------------------------------------
# One question, one arm
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """Everything the metrics need about one (arm, question) run.

    actual: "answer" | "abstain" | "error" -- the only three things an arm
    can do. `cited_keys` are citation_keys (MED/12345678, NCT/NCT01234567)
    the arm's ANSWER relies on; `resolution` maps each of them to True
    (resolves), False (does not) or None (could not be checked -- fail-open,
    exactly like the Verifier's own registry policy).
    """

    qid: int
    category: str
    expected: str
    arm: str
    actual: str
    latency: float
    cited_keys: list[str] = field(default_factory=list)
    resolution: dict[str, Optional[bool]] = field(default_factory=dict)
    claims: list[dict] = field(default_factory=list)
    funnel: dict = field(default_factory=dict)
    error: Optional[str] = None
    seed_notes: list[str] = field(default_factory=list)

    @property
    def correct(self) -> bool:
        """Did the arm do what the question expects (answer vs abstain)?"""
        return self.actual == self.expected


@dataclass
class SeedStats:
    """Seeded-failure bookkeeping for the full arm."""

    fabricated_injected: int = 0
    fabricated_cited: int = 0
    fabricated_caught: int = 0
    retracted_injected: int = 0
    retracted_excluded: int = 0
    retracted_cited: int = 0
    retracted_caught: int = 0
    skipped_reason: Optional[str] = None

    @property
    def retraction_catch_rate(self) -> Optional[float]:
        """Fraction of injected retractions the pipeline kept out of the
        answer (excluded by the Appraiser, or deleted by the Verifier)."""
        if not self.retracted_injected:
            return None
        return self.retracted_caught / self.retracted_injected


# ---------------------------------------------------------------------------
# Offline fixtures (--offline-sample): no network, no keys, no API calls
# ---------------------------------------------------------------------------

#: PMIDs the offline registry resolves. Anything else is unknown (None),
#: except FABRICATED_PMID which is authoritatively missing.
_FIXTURE_PMIDS = ("30000001", "30000002", "30000003")
_FIXTURE_NCT = "NCT30000004"


def _fixture_record(
    native_id: str,
    design: StudyDesign,
    question: str,
    *,
    source: SourceDB = SourceDB.PUBMED,
    is_retracted: bool = False,
    trial_status: Optional[str] = None,
) -> EvidenceRecord:
    """One deterministic stand-in record for the offline sample."""
    topic = question.rstrip("?")
    return EvidenceRecord(
        record_id=f"fixture-{native_id}",
        source=source,
        native_id=native_id,
        doi=f"10.9999/fixture.{native_id}",
        title=f"[offline fixture, {design.value}] {topic}",
        abstract=(
            "OFFLINE FIXTURE ABSTRACT. In this synthetic record the "
            f"intervention was associated with the outcome of interest for "
            f"the question: {topic}. Effect estimate 0.82 (95% CI "
            "0.74-0.91) over 24 months of follow-up."
        ),
        journal="Journal of Offline Fixtures",
        publication_date=date(2024, 1, 15),
        study_design=design,
        is_retracted=is_retracted,
        retraction_source="pubmed" if is_retracted else None,
        trial_status=trial_status,
        url=f"https://example.invalid/{native_id}",
    )


class FixtureRetrieval:
    """Stand-in for retrieval.gather_evidence with a scripted pool.

    The pool depends on the CURRENT question's category, which the runner
    sets before each run: "no_evidence" questions get a single off-topic
    record so the pipeline's thin-pool abstention fires, every other
    category gets a four-record pool spanning the evidence hierarchy.
    """

    def __init__(self) -> None:
        self.question = ""
        self.category = "therapy"

    def set_question(self, question: str, category: str) -> None:
        self.question = question
        self.category = category

    def __call__(self, queries: Any, **kwargs: Any) -> list[EvidenceRecord]:
        if self.category == NO_EVIDENCE_CATEGORY:
            # One weakly related record: below MIN_POOL_RECORDS, so the
            # pipeline abstains before it ever reaches synthesis.
            return [
                _fixture_record(
                    _FIXTURE_PMIDS[0], StudyDesign.CASE_REPORT, self.question
                )
            ]
        return [
            _fixture_record(
                _FIXTURE_PMIDS[0], StudyDesign.SYSTEMATIC_REVIEW, self.question
            ),
            _fixture_record(_FIXTURE_PMIDS[1], StudyDesign.RCT, self.question),
            _fixture_record(_FIXTURE_PMIDS[2], StudyDesign.COHORT, self.question),
            _fixture_record(
                _FIXTURE_NCT,
                StudyDesign.CLINICAL_TRIAL_RECORD,
                self.question,
                source=SourceDB.CLINICAL_TRIALS,
                trial_status="RECRUITING",
            ),
        ]


class FixtureLLM:
    """Deterministic LLM stand-in, dispatched on each agent's system prompt.

    Only the shapes each agent actually validates are produced. The
    Appraiser deliberately gets an empty ranking list so the offline sample
    runs on its pure heuristic (OCEBM) scores -- fully reproducible, and it
    keeps this fixture out of the ranking business.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    # --- text completions -------------------------------------------------

    def complete(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.2
    ) -> str:
        system = system or ""
        if "clinical evidence synthesizer" in system:
            self.calls.append("synthesize")
            return self._synthesis(prompt)
        self.calls.append("bare")
        return self._bare_answer()

    def _synthesis(self, prompt: str) -> str:
        """One cited sentence per evidence item shown in the prompt (max 3),
        so every citation is valid by construction -- including the seeded
        fabricated record when it ranks into the shown set."""
        sids = list(dict.fromkeys(re.findall(r"\[(S\d+)\]", prompt)))[:3]
        if not sids:
            return "INSUFFICIENT_EVIDENCE"
        return " ".join(
            f"Offline fixture evidence reports an effect estimate of 0.82 "
            f"for this outcome [{sid}]."
            for sid in sids
        )

    @staticmethod
    def _bare_answer() -> str:
        """A canned bare-LLM answer with the failure profile this benchmark
        exists to expose: one resolvable citation, one fabricated PMID, one
        retracted paper."""
        return (
            f"Yes, the intervention is effective and reduces the outcome by "
            f"roughly 30 percent (PMID: {_FIXTURE_PMIDS[0]}). A large trial "
            f"confirmed the mortality benefit (PMID: {FABRICATED_PMID}). "
            f"An earlier study reported the same association (PMID: "
            f"{RETRACTED_PMID})."
        )

    # --- JSON completions -------------------------------------------------

    def complete_json(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.1
    ) -> Any:
        system = system or ""
        if "literature-search queries" in system:
            self.calls.append("plan")
            return {
                "queries": [
                    "offline fixture clinical evidence sample query",
                    "offline fixture systematic review sample query",
                    "offline fixture adverse effects sample query",
                ]
            }
        if "relevance judge" in system:
            self.calls.append("appraise")
            return {"rankings": []}  # heuristic-only, by design
        if "decompose" in system:
            self.calls.append("decompose")
            return {"claims": self._decompose(prompt)}
        if "entailment judge" in system:
            self.calls.append("judge")
            return {
                "verdict": "SUPPORTS",
                "confidence": 0.9,
                "evidence_quote": "the intervention was associated with the outcome",
                "reason": "offline fixture judge always supports fixture claims",
            }
        if "adversarial reviewer" in system:
            self.calls.append("red_team")
            return {"flags": []}
        self.calls.append("unknown")
        return {}

    @staticmethod
    def _decompose(prompt: str) -> list[dict]:
        """Echo the decomposition prompt back as one claim per sentence,
        keeping each sentence's own citations (the prompt lists them)."""
        claims: list[dict] = []
        pattern = re.compile(
            r"^\[(\d+)\] (.+)\n\s*citations: (.+)$", re.MULTILINE
        )
        for match in pattern.finditer(prompt):
            sids = [
                sid.strip()
                for sid in match.group(3).split(",")
                if sid.strip().startswith("S")
            ]
            if not sids:
                continue
            claims.append(
                {
                    "sentence_index": int(match.group(1)),
                    "claim": match.group(2).strip(),
                    "citations": sids,
                }
            )
        return claims


class _OfflineResponse:
    """Minimal requests.Response stand-in for the offline registry."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"offline fixture HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class OfflineSession:
    """requests.Session stand-in: answers every registry lookup locally.

    Injected into the Verifier so --offline-sample makes zero network calls
    while still exercising the real existence-check code path. The seeded
    fabricated PMID is the only identifier reported as missing.
    """

    def get(self, url: str, **kwargs: Any) -> _OfflineResponse:
        params = kwargs.get("params") or {}
        if "esummary" in url:
            ids = [i for i in str(params.get("id", "")).split(",") if i]
            uids = [i for i in ids if i != FABRICATED_PMID]
            result: dict = {"uids": uids}
            for uid in uids:
                result[uid] = {"uid": uid, "title": "offline fixture record"}
            return _OfflineResponse({"result": result})
        if "clinicaltrials.gov" in url:
            return _OfflineResponse({"protocolSection": {"identificationModule": {}}})
        return _OfflineResponse({"offline": True})  # doi.org


class FixtureFetcher:
    """PubMed efetch stand-in used by the bare arm's measurement pass."""

    def efetch(self, pmids: list[str]) -> list[EvidenceRecord]:
        return [
            _fixture_record(pmid, StudyDesign.RCT, "offline fixture question")
            for pmid in pmids
            if pmid in _FIXTURE_PMIDS or pmid == RETRACTED_PMID
        ]


class FixtureResolver:
    """Identifier resolution without the network (offline sample only)."""

    def resolve(self, keys: list[str]) -> dict[str, Optional[bool]]:
        out: dict[str, Optional[bool]] = {}
        for key in keys:
            native = key.split("/", 1)[1] if "/" in key else key
            if native == FABRICATED_PMID:
                out[key] = False
            elif native in _FIXTURE_PMIDS or native in (
                _FIXTURE_NCT,
                RETRACTED_PMID,
            ):
                out[key] = True
            else:
                out[key] = None  # unknown to the fixture: not counted
        return out


# ---------------------------------------------------------------------------
# Live identifier resolution (citation_resolution_rate)
# ---------------------------------------------------------------------------


class RegistryResolver:
    """Does a citation_key point at a record that actually exists?

    PubMed/Europe PMC numeric ids go to esummary in one batch,
    ClinicalTrials.gov ids to the v2 study endpoint. Anything else (or any
    transport failure) resolves to None -- "could not check" is reported as
    n/a and never counted as a fabrication, mirroring the Verifier's
    fail-open registry policy. Results are cached for the whole run.
    """

    _ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    _CTGOV = "https://clinicaltrials.gov/api/v2/studies/{nct_id}"

    def __init__(self, timeout: int = 15) -> None:
        import requests  # local import: offline mode never needs it here

        self.session = requests.Session()
        self.timeout = timeout
        self._cache: dict[str, Optional[bool]] = {}

    def resolve(self, keys: list[str]) -> dict[str, Optional[bool]]:
        pending = [k for k in dict.fromkeys(keys) if k not in self._cache]
        pmid_keys = {}
        nct_keys = {}
        for key in pending:
            native = key.split("/", 1)[1] if "/" in key else key
            if native.isdigit():
                pmid_keys[key] = native
            elif native.upper().startswith("NCT"):
                nct_keys[key] = native
            else:
                self._cache[key] = None
        if pmid_keys:
            found = self._esummary_batch(sorted(set(pmid_keys.values())))
            for key, pmid in pmid_keys.items():
                self._cache[key] = found.get(pmid)
        for key, nct_id in nct_keys.items():
            self._cache[key] = self._nct(nct_id)
        return {key: self._cache.get(key) for key in keys}

    def _esummary_batch(self, pmids: list[str]) -> dict[str, Optional[bool]]:
        try:
            resp = self.session.get(
                self._ESUMMARY,
                params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result = (resp.json() or {}).get("result") or {}
            uids = {str(u) for u in (result.get("uids") or [])}
        except Exception as exc:
            print(f"[benchmark] esummary check failed ({exc}); {len(pmids)} ids n/a")
            return {pmid: None for pmid in pmids}
        out: dict[str, Optional[bool]] = {}
        for pmid in pmids:
            if pmid not in uids:
                out[pmid] = False
                continue
            entry = result.get(pmid)
            out[pmid] = not (isinstance(entry, dict) and entry.get("error"))
        return out

    def _nct(self, nct_id: str) -> Optional[bool]:
        try:
            resp = self.session.get(
                self._CTGOV.format(nct_id=nct_id),
                params={"format": "json"},
                timeout=self.timeout,
            )
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return "protocolSection" in (resp.json() or {})
        except Exception as exc:
            print(f"[benchmark] ctgov check failed for {nct_id} ({exc}); n/a")
            return None


# ---------------------------------------------------------------------------
# Seeded failures (full arm only)
# ---------------------------------------------------------------------------


def fabricated_record(question: str) -> EvidenceRecord:
    """A plausible-looking record whose PMID does not exist.

    Deliberately on-topic and RCT-shaped so the Appraiser ranks it into the
    shown evidence set and the Synthesizer is tempted to cite it -- that is
    what makes the existence check measurable.
    """
    topic = question.rstrip("?")
    return EvidenceRecord(
        record_id=f"seed-fabricated-{FABRICATED_PMID}",
        source=SourceDB.PUBMED,
        native_id=FABRICATED_PMID,
        doi=None,
        title=f"Randomized controlled trial: {topic}",
        abstract=(
            "SEEDED BENCHMARK RECORD (fabricated identifier). METHODS: "
            f"multicentre randomized trial addressing the question: {topic} "
            "RESULTS: the intervention showed a statistically significant "
            "benefit versus control (hazard ratio 0.79, 95% CI 0.68-0.92). "
            "CONCLUSIONS: the intervention is effective for this outcome."
        ),
        journal="Seeded Benchmark Reports",
        publication_date=date(2023, 6, 1),
        study_design=StudyDesign.RCT,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{FABRICATED_PMID}/",
    )


def load_retracted_seed(offline: bool) -> tuple[Optional[EvidenceRecord], Optional[str]]:
    """Fetch the seeded retracted paper; returns (record, skip_reason).

    Live mode fetches PMID 21177010 through PubMedClient so the retraction
    flag comes from PubMed's own curation rather than from this file. A
    network failure, a missing record, or a record PubMed does NOT flag as
    retracted all mean "skip the seeded retraction test" -- reporting n/a is
    honest, reporting 0.0 would not be.
    """
    if offline:
        return (
            _fixture_record(
                RETRACTED_PMID,
                StudyDesign.CASE_SERIES,
                "offline fixture retracted paper",
                is_retracted=True,
            ),
            None,
        )
    try:
        from retrieval.pubmed import PubMedClient

        records = PubMedClient().efetch([RETRACTED_PMID]) or []
    except Exception as exc:
        return None, f"PubMed unreachable during seed check ({exc})"
    match = next((r for r in records if r.native_id == RETRACTED_PMID), None)
    if match is None:
        return None, f"seed PMID {RETRACTED_PMID} not returned by PubMed"
    if not match.is_retracted:
        return None, (
            f"seed PMID {RETRACTED_PMID} is no longer flagged retracted by PubMed"
        )
    return match, None


class SeedInjector:
    """Wraps a gather_fn and injects the seeded failure records.

    Also sorts every pool by citation_key, so the pool handed to the
    Appraiser is byte-identical across runs of the same question.
    """

    def __init__(
        self,
        gather_fn: Callable[..., list[EvidenceRecord]],
        retracted: Optional[EvidenceRecord],
        stats: SeedStats,
        enabled: bool = True,
    ) -> None:
        self.gather_fn = gather_fn
        self.retracted = retracted
        self.stats = stats
        self.enabled = enabled
        self.question = ""

    def set_question(self, question: str) -> None:
        self.question = question

    def __call__(self, queries: Any, **kwargs: Any) -> list[EvidenceRecord]:
        pool = list(self.gather_fn(queries, **kwargs))
        if self.enabled:
            pool.append(fabricated_record(self.question))
            self.stats.fabricated_injected += 1
            if self.retracted is not None:
                # Fresh copy per question: the Appraiser and Verifier both
                # mutate nothing, but a shared record across 30 runs would
                # still be a trap waiting to happen.
                import copy

                pool.append(copy.deepcopy(self.retracted))
                self.stats.retracted_injected += 1
        pool.sort(key=lambda record: record.citation_key())
        return pool


# ---------------------------------------------------------------------------
# Measurement-only verification (used by the bare and naive arms)
# ---------------------------------------------------------------------------


def measure(
    verifier: Verifier,
    question: str,
    sentences: list[Sentence],
    evidence_items: list[dict],
    queries: list[str],
) -> tuple[dict, list[dict]]:
    """Run the verification gate for METRICS ONLY.

    The returned funnel and claims describe what the Verifier WOULD have
    deleted. The arm's answer is untouched -- an unverified arm's whole
    characteristic is that it shows those claims anyway.
    """
    if not sentences:
        return {}, []
    synthesis = SynthesisResult(
        raw_text="", sentences=sentences, abstained=False, parse_deletions=[]
    )
    try:
        report = verifier.verify(question, synthesis, evidence_items, queries)
    except Exception as exc:  # measurement must never break the benchmark
        print(f"[benchmark] measurement pass failed ({exc}); metrics n/a")
        return {}, []
    return report.funnel, [serialize_claim(claim) for claim in report.claims]


def extract_pmids(text: str) -> list[str]:
    """PMIDs a bare answer cites, first-occurrence order, deduplicated."""
    pmids: list[str] = []
    for match in _PMID_RE.finditer(text or ""):
        pmid = next((group for group in match.groups() if group), None)
        if pmid and pmid not in pmids:
            pmids.append(pmid)
    return pmids


def bare_evidence_items(
    pmids: list[str], records: list[EvidenceRecord]
) -> list[dict]:
    """Mint S-ids over the records a bare answer cited.

    A PMID PubMed cannot return (fabricated, or simply nonexistent) still
    gets an evidence item -- with no title and no abstract -- so the
    existence check has something to fail on instead of the citation
    silently disappearing from the measurement.
    """
    by_pmid = {record.native_id: record for record in records}
    items: list[dict] = []
    for i, pmid in enumerate(pmids, 1):
        record = by_pmid.get(pmid)
        items.append(
            {
                "sid": f"S{i}",
                "citation_key": f"MED/{pmid}",
                "source": SourceDB.PUBMED,
                "native_id": pmid,
                "doi": record.doi if record else None,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "title": record.title if record else "",
                "journal": record.journal if record else None,
                "publication_date": record.publication_date if record else None,
                "study_design": record.study_design.value if record else None,
                "trial_status": None,
                "is_preprint": bool(record.is_preprint) if record else False,
                "is_retracted": bool(record.is_retracted) if record else False,
                "relevance_score": None,
                "rationale": "cited by the bare-LLM answer",
                "abstract": record.abstract if record else "",
            }
        )
    return items


def sentences_from_bare(text: str, pmid_to_sid: dict[str, str]) -> list[Sentence]:
    """Turn a bare answer into cited Sentences for the measurement pass.

    Sentences citing nothing are dropped: with no citation there is nothing
    to verify against. They still appear in the bare arm's answer, which is
    exactly why its citation metrics are not the whole story.
    """
    out: list[Sentence] = []
    for piece in _SENTENCE_SPLIT_RE.split((text or "").strip()):
        piece = piece.strip()
        if not piece:
            continue
        sids = [
            pmid_to_sid[pmid]
            for pmid in extract_pmids(piece)
            if pmid in pmid_to_sid
        ]
        if not sids:
            continue
        out.append(Sentence(index=len(out), text=piece, citations=sids))
    return out


# ---------------------------------------------------------------------------
# The three arms
# ---------------------------------------------------------------------------


def run_bare(harness: "Harness", question_row: dict) -> Outcome:
    """Arm 1: one LLM call, no retrieval, no verification."""
    question = question_row["question"]
    outcome = Outcome(
        qid=int(question_row["id"]),
        category=str(question_row["category"]),
        expected=str(question_row["expected"]),
        arm="bare",
        actual="error",
        latency=0.0,
    )
    prompt = (
        f"Answer this clinical question with citations to PubMed IDs: {question}"
    )
    started = time.perf_counter()
    try:
        text = harness.llm.complete(
            prompt, system=_BARE_SYSTEM, temperature=TEMPERATURE
        )
    except LLMError as exc:
        outcome.latency = time.perf_counter() - started
        outcome.error = str(exc)
        print(f"[benchmark] bare arm LLM error: {exc}")
        return outcome
    except Exception as exc:  # a 30-question run must not die on one question
        outcome.latency = time.perf_counter() - started
        outcome.error = f"{type(exc).__name__}: {exc}"
        print(f"[benchmark] bare arm failed: {exc}")
        return outcome
    outcome.latency = time.perf_counter() - started

    pmids = extract_pmids(text or "")
    outcome.cited_keys = [f"MED/{pmid}" for pmid in pmids]
    outcome.resolution = harness.resolver.resolve(outcome.cited_keys)
    refused = bool(_REFUSAL_RE.search(text or "")) or not (text or "").strip()
    outcome.actual = "abstain" if refused else "answer"
    if refused:
        return outcome

    records: list[EvidenceRecord] = []
    if pmids and harness.fetcher is not None:
        try:
            records = harness.fetcher.efetch(pmids) or []
        except Exception as exc:
            print(f"[benchmark] bare arm efetch failed ({exc}); titles unavailable")
    evidence_items = bare_evidence_items(pmids, records)
    pmid_to_sid = {item["native_id"]: item["sid"] for item in evidence_items}
    outcome.funnel, outcome.claims = measure(
        harness.measure_verifier,
        question,
        sentences_from_bare(text, pmid_to_sid),
        evidence_items,
        [question],
    )
    return outcome


def run_naive(harness: "Harness", question_row: dict) -> Outcome:
    """Arm 2: Strategist -> retrieval -> Appraiser -> Synthesizer.

    Citation-forced, so every shown sentence names a real record in the
    frozen pool -- but nothing checks that the record says what the sentence
    claims, and nothing checks its standing.
    """
    question = question_row["question"]
    outcome = Outcome(
        qid=int(question_row["id"]),
        category=str(question_row["category"]),
        expected=str(question_row["expected"]),
        arm="naive",
        actual="error",
        latency=0.0,
    )
    started = time.perf_counter()
    evidence_items: list[dict] = []
    queries: list[str] = [question]
    try:
        planned = harness.strategist.plan_queries(question)
        queries = planned or [question]
        pool = list(harness.naive_gather(queries))
        pool.sort(key=lambda record: record.citation_key())
        if len(pool) < MIN_POOL_RECORDS:
            outcome.latency = time.perf_counter() - started
            outcome.actual = "abstain"
            print(f"[benchmark] naive arm: thin pool ({len(pool)} records)")
            return outcome
        appraised = harness.appraiser.appraise(pool, question)
        evidence_items = build_evidence_items(appraised)
        synthesis = harness.synthesizer.synthesize(question, evidence_items)
    except LLMError as exc:
        outcome.latency = time.perf_counter() - started
        outcome.error = str(exc)
        print(f"[benchmark] naive arm LLM error: {exc}")
        return outcome
    except Exception as exc:
        outcome.latency = time.perf_counter() - started
        outcome.error = f"{type(exc).__name__}: {exc}"
        print(f"[benchmark] naive arm failed: {exc}")
        return outcome
    outcome.latency = time.perf_counter() - started

    if synthesis.abstained or not synthesis.sentences:
        outcome.actual = "abstain"
        return outcome
    outcome.actual = "answer"

    key_by_sid = {
        str(item["sid"]): str(item["citation_key"]) for item in evidence_items
    }
    cited: list[str] = []
    for sentence in synthesis.sentences:
        for sid in sentence.citations:
            key = key_by_sid.get(sid)
            if key and key not in cited:
                cited.append(key)
    outcome.cited_keys = cited
    outcome.resolution = harness.resolver.resolve(cited)
    outcome.funnel, outcome.claims = measure(
        harness.measure_verifier,
        question,
        list(synthesis.sentences),
        evidence_items,
        queries,
    )
    return outcome


def run_full(harness: "Harness", question_row: dict) -> Outcome:
    """Arm 3: the complete EvidenceBoard pipeline, seeded failures included."""
    question = question_row["question"]
    outcome = Outcome(
        qid=int(question_row["id"]),
        category=str(question_row["category"]),
        expected=str(question_row["expected"]),
        arm="full",
        actual="error",
        latency=0.0,
    )
    started = time.perf_counter()
    try:
        report = harness.pipeline.run(question)
    except Exception as exc:  # pipeline.run() is documented never to raise
        outcome.latency = time.perf_counter() - started
        outcome.error = f"{type(exc).__name__}: {exc}"
        print(f"[benchmark] full arm failed: {exc}")
        return outcome
    outcome.latency = time.perf_counter() - started

    claims = report.get("claims") or []
    outcome.claims = claims
    outcome.funnel = report.get("funnel") or {}
    outcome.actual = "abstain" if report.get("abstained") else "answer"

    # Only the citations the doctor is actually shown count towards
    # resolution -- a deleted claim is not part of the answer.
    shown: list[str] = []
    for claim in claims:
        if claim.get("status") not in ("kept", "flagged"):
            continue
        for citation in claim.get("citations") or []:
            key = citation.get("citation_key")
            if key and str(key) not in shown:
                shown.append(str(key))
    outcome.cited_keys = shown
    outcome.resolution = harness.resolver.resolve(shown)

    if harness.seeds_enabled:
        _score_seeds(harness.seed_stats, report, outcome)
    return outcome


def _score_seeds(stats: SeedStats, report: dict, outcome: Outcome) -> None:
    """Did the full pipeline catch the two seeded failures for this question?

    A seed is CAUGHT when it never reaches the answer: either the Appraiser
    kept it out of the ranked pool (retractions, per COPE/NLM) or every claim
    citing it was deleted by the Verifier.
    """
    pool_keys = {
        str(item.get("citation_key")) for item in (report.get("evidence") or [])
    }
    all_claims = report.get("claims") or []

    for key, label in (
        (f"MED/{FABRICATED_PMID}", "fabricated"),
        (f"MED/{RETRACTED_PMID}", "retracted"),
    ):
        if label == "retracted" and not stats.retracted_injected:
            continue  # seed unavailable: nothing to score
        citing = [
            claim
            for claim in all_claims
            if any(
                str(citation.get("citation_key")) == key
                for citation in (claim.get("citations") or [])
            )
        ]
        if label == "retracted" and key not in pool_keys:
            stats.retracted_excluded += 1
        if not citing:
            # Never cited, so it could not mislead the answer. For the
            # retraction seed that IS the catch (the Appraiser excluded it);
            # an uncited fabricated record proves nothing either way.
            if label == "retracted":
                stats.retracted_caught += 1
                outcome.seed_notes.append("retracted seed excluded before synthesis")
            continue
        survived = [c for c in citing if c.get("status") in ("kept", "flagged")]
        caught = not survived
        if label == "fabricated":
            stats.fabricated_cited += 1
            stats.fabricated_caught += int(caught)
            outcome.seed_notes.append(
                "fabricated PMID cited and deleted"
                if caught
                else "FABRICATED PMID SURVIVED"
            )
        else:
            stats.retracted_cited += 1
            stats.retracted_caught += int(caught)
            outcome.seed_notes.append(
                "retracted seed cited and deleted"
                if caught
                else "RETRACTED SOURCE SURVIVED"
            )


ARM_RUNNERS: dict[str, Callable[["Harness", dict], Outcome]] = {
    "bare": run_bare,
    "naive": run_naive,
    "full": run_full,
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

#: Report order. The last two are not in the Phase 4a spec but fall out of
#: the same data and are what actually answers "is the pipeline right?".
METRIC_KEYS = (
    "citation_resolution_rate",
    "entailment_pass_rate",
    "retraction_catch_rate",
    "claims_deleted_per_answer",
    "abstention_rate_no_evidence",
    "abstention_rate_evidence",
    "avg_latency_seconds",
    "expected_outcome_match_rate",
    "errors",
)


def _mean(values: list[float]) -> Optional[float]:
    """Mean, or None for an empty sample (None prints as n/a, not 0.00)."""
    return sum(values) / len(values) if values else None


def _rate(hits: int, total: int) -> Optional[float]:
    """hits/total, or None when the denominator is empty."""
    return hits / total if total else None


def aggregate(
    arm: str, outcomes: list[Outcome], seed_stats: SeedStats
) -> dict[str, Optional[float]]:
    """Roll one arm's per-question outcomes up into the metrics table.

    Questions that errored out are excluded from every rate (they are
    reported separately as `errors`) -- an LLM transport failure is a
    property of the endpoint, not of the arm's epistemics.
    """
    done = [o for o in outcomes if o.actual != "error"]

    # citation_resolution_rate: counted per cited IDENTIFIER of the answer
    # the arm actually showed. Lookups that could not be checked (None) are
    # excluded rather than scored as fabrications.
    resolved = unresolved = 0
    for outcome in done:
        for key in outcome.cited_keys:
            state = outcome.resolution.get(key)
            if state is True:
                resolved += 1
            elif state is False:
                unresolved += 1

    # entailment_pass_rate: over claims the entailment judge actually ruled
    # on ("skipped" claims -- deleted earlier by the existence or standing
    # check -- carry no entailment signal).
    supports = judged = 0
    for outcome in done:
        for claim in outcome.claims:
            verdict = str((claim.get("checks") or {}).get("entailment") or "")
            if verdict not in ("supports", "refutes", "nei"):
                continue
            judged += 1
            supports += int(verdict == "supports")

    deleted = [
        float((outcome.funnel or {}).get("claims_deleted", 0) or 0)
        for outcome in done
        if outcome.funnel
    ]

    no_evidence = [o for o in done if o.category == NO_EVIDENCE_CATEGORY]
    evidence_rich = [o for o in done if o.category in EVIDENCE_CATEGORIES]

    return {
        "citation_resolution_rate": _rate(resolved, resolved + unresolved),
        "entailment_pass_rate": _rate(supports, judged),
        # Seeds are injected into the full arm only, so this is n/a elsewhere
        # -- reporting 0.0 for an untested arm would be a lie.
        "retraction_catch_rate": (
            seed_stats.retraction_catch_rate if arm == "full" else None
        ),
        "claims_deleted_per_answer": _mean(deleted),
        "abstention_rate_no_evidence": _rate(
            sum(1 for o in no_evidence if o.actual == "abstain"), len(no_evidence)
        ),
        "abstention_rate_evidence": _rate(
            sum(1 for o in evidence_rich if o.actual == "abstain"),
            len(evidence_rich),
        ),
        "avg_latency_seconds": _mean([o.latency for o in outcomes]),
        "expected_outcome_match_rate": _rate(
            sum(1 for o in done if o.correct), len(done)
        ),
        "errors": float(len(outcomes) - len(done)),
    }


def _fmt(key: str, value: Optional[float]) -> str:
    """Table cell: n/a for "not measured", integer for counts."""
    if value is None:
        return "n/a"
    if key == "errors":
        return str(int(value))
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# results.md
# ---------------------------------------------------------------------------

_METRIC_NOTES = (
    "- **citation_resolution_rate** -- of every identifier cited by the answer "
    "the arm actually showed, the fraction that resolves against PubMed "
    "esummary / ClinicalTrials.gov. Identifiers that could not be checked "
    "(transport failure, non-registry key) are excluded, never counted as "
    "fabrications.",
    "- **entailment_pass_rate** -- of the claims the entailment judge ruled on, "
    "the fraction where the cited evidence SUPPORTS the claim (rather than "
    "REFUTES / NOT_ENOUGH_INFO).",
    "- **retraction_catch_rate** -- of the seeded retracted records, the "
    "fraction kept out of the shown answer (excluded by the Appraiser per "
    "COPE/NLM policy, or deleted by the Verifier's standing check). Seeds are "
    "injected into the **full-pipeline arm only**, so this reads n/a for the "
    "other two arms.",
    "- **claims_deleted_per_answer** -- mean claims deleted per question. For "
    "the bare and naive arms these deletions are *measured, not applied*: "
    "their answers still contain the claims, which is exactly the finding.",
    "- **abstention_rate_no_evidence** -- abstention rate on the 5 `no_evidence` "
    "questions. **Higher is better** (abstaining is the correct answer).",
    "- **abstention_rate_evidence** -- abstention rate on the evidence-rich "
    "categories. **Lower is better** (over-abstention is a failure mode too).",
    "- **avg_latency_seconds** -- mean wall-clock seconds per question for the "
    "arm's own work. The measurement-only verification pass is excluded from "
    "the bare and naive timings.",
    "- **expected_outcome_match_rate** -- fraction of questions where "
    "answer-vs-abstain matched `expected` in questions.json.",
    "- **errors** -- questions that raised (LLMError or transport). Excluded "
    "from every rate above.",
)


def write_results(
    path: Path,
    arms: tuple[str, ...],
    metrics: dict[str, dict[str, Optional[float]]],
    outcomes_by_arm: dict[str, list[Outcome]],
    seed_stats: SeedStats,
    offline: bool,
    question_count: int,
) -> None:
    """Render the metrics table, the metric definitions, the seeded-failure
    audit and one per-question table per arm into benchmark/results.md."""
    lines: list[str] = []
    lines.append("# EvidenceBoard benchmark results")
    lines.append("")
    lines.append(f"- Questions: {question_count} (from `benchmark/questions.json`)")
    lines.append(f"- Arms run: {', '.join(ARM_LABELS[a] for a in arms)}")
    lines.append(f"- Bare-arm temperature: {TEMPERATURE} (fixed for reproducibility)")
    lines.append(
        "- Mode: **offline sample (fixtures, no network)**"
        if offline
        else "- Mode: live (real LLM + real retrieval)"
    )
    if offline:
        lines.append(
            "- These numbers describe the FIXTURES, not the real world; "
            "offline mode exists to smoke-test the harness."
        )
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Bare LLM | Naive RAG | Full Pipeline |")
    lines.append("|--------|----------|-----------|---------------|")
    for key in METRIC_KEYS:
        cells = []
        for arm in ARMS:
            if arm not in metrics:
                cells.append("not run")
            else:
                cells.append(_fmt(key, metrics[arm].get(key)))
        lines.append(f"| {key} | {' | '.join(cells)} |")
    lines.append("")

    lines.append("### How each metric is measured")
    lines.append("")
    lines.extend(_METRIC_NOTES)
    lines.append("")
    lines.append(
        "> The bare and naive arms are passed through a **measurement-only** "
        "Verifier: it counts what would have been deleted and removes nothing. "
        "Only the full pipeline actually deletes claims."
    )
    lines.append("")

    lines.append("## Seeded failures")
    lines.append("")
    lines.append(
        f"Injected into the retrieval pool of the **full-pipeline arm only** "
        f"(the bare and naive arms never see them, so their "
        f"`retraction_catch_rate` is n/a by construction):"
    )
    lines.append("")
    lines.append(
        f"- `MED/{FABRICATED_PMID}` -- fabricated PMID, on-topic and "
        f"RCT-shaped, tests the existence check."
    )
    lines.append(
        f"- `MED/{RETRACTED_PMID}` -- Wakefield's retracted MMR/autism paper, "
        + (
            "stubbed by the offline fixture as retracted."
            if offline
            else "fetched live so the retraction flag comes from PubMed's own "
            "curation; tests the standing check."
        )
    )
    lines.append("")
    if seed_stats.skipped_reason:
        lines.append(
            f"**Retraction seed skipped:** {seed_stats.skipped_reason}. "
            f"`retraction_catch_rate` is reported as n/a rather than 0.00."
        )
    else:
        lines.append(
            f"| Seed | Injected | Cited by synthesis | Caught |\n"
            f"|------|----------|--------------------|--------|\n"
            f"| fabricated PMID | {seed_stats.fabricated_injected} | "
            f"{seed_stats.fabricated_cited} | {seed_stats.fabricated_caught} |\n"
            f"| retracted paper | {seed_stats.retracted_injected} | "
            f"{seed_stats.retracted_cited} | {seed_stats.retracted_caught} |"
        )
        lines.append("")
        lines.append(
            f"The retracted seed was kept out of the ranked pool by the "
            f"Appraiser on {seed_stats.retracted_excluded} of "
            f"{seed_stats.retracted_injected} injections."
        )
    lines.append("")

    for arm in arms:
        lines.append(f"## Per-question results -- {ARM_LABELS[arm]}")
        lines.append("")
        lines.append(
            "| id | category | expected | actual | correct? | latency (s) | notes |"
        )
        lines.append(
            "|----|----------|----------|--------|----------|-------------|-------|"
        )
        for outcome in outcomes_by_arm.get(arm, []):
            notes = list(outcome.seed_notes)
            if outcome.error:
                notes.append(f"error: {outcome.error}")
            correct = (
                "-" if outcome.actual == "error" else ("yes" if outcome.correct else "NO")
            )
            lines.append(
                f"| {outcome.qid} | {outcome.category} | {outcome.expected} "
                f"| {outcome.actual} | {correct} | {outcome.latency:.2f} "
                f"| {'; '.join(notes) if notes else ''} |"
            )
        lines.append("")

    while lines and not lines[-1]:
        lines.pop()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[benchmark] wrote {path}")


# ---------------------------------------------------------------------------
# Harness wiring
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    """Everything the three arms share for one run.

    The naive arm borrows the pipeline's OWN agent instances, so it differs
    from the full arm in exactly one respect: it stops before verification.
    """

    offline: bool
    llm: Any
    strategist: Strategist
    appraiser: Appraiser
    synthesizer: Synthesizer
    measure_verifier: Verifier
    pipeline: EvidencePipeline
    naive_gather: Callable[..., list[EvidenceRecord]]
    resolver: Any
    fetcher: Any
    injector: Optional[SeedInjector]
    seed_stats: SeedStats
    seeds_enabled: bool
    fixture_retrieval: Optional[FixtureRetrieval] = None

    def set_question(self, question: str, category: str) -> None:
        """Tell the per-question collaborators which question is running."""
        if self.fixture_retrieval is not None:
            self.fixture_retrieval.set_question(question, category)
        if self.injector is not None:
            self.injector.set_question(question)


def build_harness(offline: bool) -> Harness:
    """Wire the harness for offline fixtures or for live endpoints."""
    seed_stats = SeedStats()

    if offline:
        print("[benchmark] offline sample: fixtures only, no network, no keys")
        settings = Settings(pool_cap=30, enable_supersession=False)
        llm = FixtureLLM()
        fixture_retrieval = FixtureRetrieval()
        retracted, skip_reason = load_retracted_seed(True)
        seed_stats.skipped_reason = skip_reason
        injector = SeedInjector(
            fixture_retrieval, retracted, seed_stats, enabled=True
        )
        pipeline = EvidencePipeline(
            strategist=Strategist(llm=llm),
            gather_fn=injector,
            appraiser=Appraiser(pool_cap=settings.pool_cap, llm=llm),
            synthesizer=Synthesizer(llm=llm),
            red_team=RedTeam(llm=llm),
            # OfflineSession answers every registry lookup locally, so the
            # real existence-check code path runs with zero HTTP.
            verifier=Verifier(
                llm=llm, session=OfflineSession(), enable_supersession=False
            ),
            settings=settings,
        )
        return Harness(
            offline=True,
            llm=llm,
            strategist=pipeline.strategist,
            appraiser=pipeline.appraiser,
            synthesizer=pipeline.synthesizer,
            measure_verifier=Verifier(
                llm=llm, session=OfflineSession(), enable_supersession=False
            ),
            pipeline=pipeline,
            naive_gather=fixture_retrieval,  # seeds are full-arm only
            resolver=FixtureResolver(),
            fetcher=FixtureFetcher(),
            injector=injector,
            seed_stats=seed_stats,
            seeds_enabled=True,
            fixture_retrieval=fixture_retrieval,
        )

    settings = get_settings()
    if not settings.has_llm_keys:
        raise SystemExit(
            "[benchmark] no LLM API keys configured. Set LLM_API_KEYS in .env, "
            "or run with --offline-sample."
        )
    llm = FailoverLLMClient(settings=settings)
    pipeline = build_default_pipeline(settings)

    # Seeded failures: verified to exist before the run, and wired into the
    # FULL arm's gather_fn only.
    retracted, skip_reason = load_retracted_seed(False)
    seed_stats.skipped_reason = skip_reason
    if skip_reason:
        print(f"[benchmark] seeded retraction test skipped: {skip_reason}")
    else:
        print(f"[benchmark] seed check ok: PMID {RETRACTED_PMID} is flagged retracted")
    injector = SeedInjector(pipeline.gather_fn, retracted, seed_stats, enabled=True)
    pipeline.gather_fn = injector

    fetcher: Any = None
    try:
        from retrieval.pubmed import PubMedClient

        fetcher = PubMedClient()
    except Exception as exc:
        # Only the bare arm's measurement pass needs titles; without them
        # its entailment numbers are reported as n/a.
        print(f"[benchmark] PubMed client unavailable ({exc}); bare-arm entailment n/a")

    return Harness(
        offline=False,
        llm=llm,
        strategist=pipeline.strategist,
        appraiser=pipeline.appraiser,
        synthesizer=pipeline.synthesizer,
        measure_verifier=Verifier(
            llm=llm, enable_supersession=settings.enable_supersession
        ),
        pipeline=pipeline,
        naive_gather=gather_evidence,
        resolver=RegistryResolver(),
        fetcher=fetcher,
        injector=injector,
        seed_stats=seed_stats,
        seeds_enabled=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.run_benchmark",
        description="Three-arm EvidenceBoard benchmark (bare LLM / naive RAG / full pipeline).",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="only run the first N questions (default: all of them)",
    )
    parser.add_argument(
        "--arm",
        choices=("bare", "naive", "full", "all"),
        default="all",
        help="which arm to run (default: all)",
    )
    parser.add_argument(
        "--offline-sample",
        action="store_true",
        help="use deterministic fixtures instead of live network/LLM calls",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    questions = load_questions(args.max)
    if not questions:
        print("[benchmark] no questions to run")
        return 1
    arms: tuple[str, ...] = ARMS if args.arm == "all" else (args.arm,)
    total = len(questions)
    print(
        f"[benchmark] {total} question(s), arms={','.join(arms)}, "
        f"mode={'offline-sample' if args.offline_sample else 'live'}"
    )

    harness = build_harness(args.offline_sample)
    outcomes_by_arm: dict[str, list[Outcome]] = {}
    for arm in arms:
        runner = ARM_RUNNERS[arm]
        outcomes: list[Outcome] = []
        for i, row in enumerate(questions, 1):
            question = str(row["question"])
            print(
                f"[benchmark] Running arm={arm} question {i}/{total}: "
                f"{question[:50]}..."
            )
            harness.set_question(question, str(row.get("category", "")))
            outcome = runner(harness, row)
            outcomes.append(outcome)
            print(
                f"[benchmark]   -> {outcome.actual} "
                f"({outcome.latency:.2f}s, {len(outcome.cited_keys)} citation(s))"
            )
        outcomes_by_arm[arm] = outcomes

    metrics = {
        arm: aggregate(arm, outcomes_by_arm[arm], harness.seed_stats) for arm in arms
    }
    write_results(
        RESULTS_PATH,
        arms,
        metrics,
        outcomes_by_arm,
        harness.seed_stats,
        args.offline_sample,
        total,
    )
    for arm in arms:
        summary = ", ".join(
            f"{key}={_fmt(key, metrics[arm].get(key))}" for key in METRIC_KEYS
        )
        print(f"[benchmark] {ARM_LABELS[arm]}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
