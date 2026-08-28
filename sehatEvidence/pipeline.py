"""
pipeline.py -- EvidenceBoard end-to-end orchestrator (Phase 3a).

Wires the five agents and the retrieval layer into ONE deterministic
sequence and returns a single JSON-ready report dict that the API layer
(api/server.py) can serve verbatim:

    Strategist  -> 3-5 database-ready queries (agents/strategist.py)
    retrieval   -> the FROZEN evidence pool (retrieval/retrieve.py)
    Appraiser   -> ranked, capped pool, retracted records excluded
                   (agents/appraiser.py; OCEBM 2011 / SORT / GRADE rubric)
    Synthesizer -> citation-forced draft answer, every sentence [S#]-tagged
                   (agents/synthesizer.py; ALCE arXiv:2305.14627)
    Verifier    -> existence / entailment / standing gate, deletes claims
                   (agents/verifier.py; SAFE arXiv:2403.18802,
                   SciFact arXiv:2004.14974, VerifAI arXiv:2604.08549)
    Red Team    -> flag-only adversarial pass over the survivors
                   (agents/red_team.py)

Who owns what
-------------
The pipeline owns exactly two things the agents deliberately do not:

1. The S-id namespace. Stable ids "S1".."Sn" are assigned HERE over the
   appraised (best-first) pool, and the evidence-item dicts every
   downstream agent consumes are built HERE. The Synthesizer may only
   cite an S-id it was shown; the Verifier's existence check is "is this
   S-id in the frozen pool?" -- both depend on one authority minting the
   ids once, before synthesis begins.
2. Abstention. Retrieval and appraisal are honest but silent about
   sufficiency, so the pipeline refuses to answer when the pool is too
   thin (< 2 records, or < 2 records at relevance >= 60), when the
   Synthesizer emits INSUFFICIENT_EVIDENCE, when the LLM is unavailable,
   or when the Verifier's own abstention rules fire. An abstention is a
   successful outcome, never an error: the report shape is identical,
   with abstained=True and the reasons spelled out.

Failure posture
---------------
Every optional stage fails open (Strategist -> [question]; Red Team ->
no flags) and every stage that cannot honestly produce an answer fails
closed into abstention (Synthesizer, Verifier). run() never raises: a
completely dead LLM (FailoverLLMClient exhausting every key) is caught
at the top level and reported as an "LLM unavailable" abstention.
"""

from __future__ import annotations

import json
import pathlib
from typing import Callable, Optional

from config import DISCLAIMER, FailoverLLMClient, Settings, get_settings
from agents.appraiser import Appraiser, AppraisedRecord
from agents.red_team import RedTeam, RedTeamFlag
from agents.strategist import Strategist
from agents.synthesizer import Synthesizer, SynthesisResult
from agents.verifier import Claim, Verifier, VerificationReport
from core.llm import LLMError
from core.schema import EvidenceRecord
from retrieval.retrieve import gather_evidence

__all__ = [
    "EvidencePipeline",
    "MOCK_RESPONSE_PATH",
    "build_default_pipeline",
    "serialize_claim",
]

#: Canned demo answer, resolved relative to THIS file so the working
#: directory never matters (the API may be started from anywhere).
MOCK_RESPONSE_PATH = pathlib.Path(__file__).parent / "demo" / "mock_response.json"

#: A pool smaller than this cannot support a cross-checked answer.
MIN_POOL_RECORDS = 2
#: ... and neither can a pool without at least this many on-topic records.
MIN_HIGH_RELEVANCE_RECORDS = 2
#: Appraiser score at or above which a record counts as "high relevance"
#: (the Appraiser's own 60-89 anchor: same condition/intervention).
HIGH_RELEVANCE_SCORE = 60

# --- abstention reasons (single source of truth for the UI copy) -------------

ABSTAIN_THIN_POOL = "fewer than 2 records retrieved"
ABSTAIN_LOW_RELEVANCE = "fewer than 2 high-relevance records"
ABSTAIN_LLM_SYNTHESIS = "LLM unavailable during synthesis"
ABSTAIN_SYNTH_INSUFFICIENT = "synthesizer judged evidence insufficient"
ABSTAIN_LLM_DEAD = "LLM unavailable"


def _empty_funnel() -> dict:
    """A fresh zeroed verification funnel (never share a mutable dict)."""
    return {
        "claims_generated": 0,
        "claims_deleted": 0,
        "claims_kept": 0,
        "by_reason": {},
    }


def serialize_claim(claim: Claim) -> dict:
    """Flatten one verified Claim into a JSON-ready dict.

    The three check outcomes are nested under "checks" so the UI can render
    the verification story (existence / entailment / standing) next to the
    claim, exactly as the Verifier recorded it.
    """
    return {
        "claim_id": claim.claim_id,
        "text": claim.text,
        "status": claim.status,
        "deletion_reason": claim.deletion_reason,
        "flags": list(claim.flags),
        "checks": {
            "existence": claim.checks.existence,
            "entailment": claim.checks.entailment,
            "standing": claim.checks.standing,
        },
        "verdict": claim.verdict,
        "confidence": claim.confidence,
        "evidence_quote": claim.evidence_quote,
        "citations": claim.citations,
    }


def _build_evidence_items(appraised: list[AppraisedRecord]) -> list[dict]:
    """Mint the S-id namespace over the appraised pool.

    `appraised` arrives best-first from the Appraiser, so "S1" is always
    the highest-ranked record. Each item carries everything the
    Synthesizer needs to write a cited sentence AND everything the
    Verifier needs to check it (citation_key, native_id, doi for the
    registry lookups; is_retracted for the standing check; title +
    abstract for the entailment judge).
    """
    items: list[dict] = []
    for i, ap in enumerate(appraised, 1):
        r = ap.record
        items.append(
            {
                "sid": f"S{i}",
                "citation_key": r.citation_key(),
                "source": r.source,
                "native_id": r.native_id,
                "doi": r.doi,
                "url": r.url,
                "title": r.title,
                "journal": r.journal,
                "publication_date": r.publication_date,
                "study_design": r.study_design.value if r.study_design else None,
                "trial_status": r.trial_status,
                "is_preprint": r.is_preprint,
                "is_retracted": r.is_retracted,
                "relevance_score": ap.score,
                "rationale": ap.rationale,
                "abstract": r.abstract,
            }
        )
    return items


class EvidencePipeline:
    """Runs one clinical question through every EvidenceBoard stage.

    Every collaborator is injected (no agent is constructed here), so the
    offline tests drive the whole orchestration with scripted fakes and
    never touch the network. Use :func:`build_default_pipeline` for the
    production wiring, where all five agents share ONE
    :class:`config.FailoverLLMClient` and therefore one rotating pool of
    API keys.
    """

    def __init__(
        self,
        strategist: Strategist,
        gather_fn: Callable[[list[str]], list[EvidenceRecord]],
        appraiser: Appraiser,
        synthesizer: Synthesizer,
        red_team: RedTeam,
        verifier: Verifier,
        settings: Settings,
    ) -> None:
        """
        strategist / appraiser / synthesizer / red_team / verifier: the five
        agents, duck-typed -- only the documented method of each is called.
        gather_fn: the frozen-pool retrieval callable
        (retrieval.retrieve.gather_evidence, or a fake in tests).
        settings: the configuration snapshot; the pool cap lives on the
        Appraiser, so the pipeline itself only keeps it for reference.
        """
        self.strategist = strategist
        self.gather_fn = gather_fn
        self.appraiser = appraiser
        self.synthesizer = synthesizer
        self.red_team = red_team
        self.verifier = verifier
        self.settings = settings

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, question: str, use_mock: bool = False) -> dict:
        """Answer one clinical question; return the full report dict.

        Report shape (identical for answers and abstentions):
        question, abstained, abstain_reasons, funnel, answer_text, claims,
        evidence, disclaimer.

        use_mock=True short-circuits the whole pipeline and replays
        demo/mock_response.json -- the offline demo fallback, so a dead
        network or a dead LLM key can never break a live presentation.

        Never raises: a totally unavailable LLM is reported as an
        "LLM unavailable" abstention.
        """
        if use_mock:
            return self._load_mock(question)

        print(f"[pipeline] starting: {question[:80]}")
        try:
            return self._run_stages(question)
        except LLMError as exc:
            # Last-resort net: every stage that CAN degrade already did,
            # so reaching here means no LLM key survived at all.
            print(f"[pipeline] LLM unavailable ({exc}); abstaining")
            return self._abstain(question, ABSTAIN_LLM_DEAD, [])

    # ------------------------------------------------------------------
    # The staged run
    # ------------------------------------------------------------------

    def _run_stages(self, question: str) -> dict:
        """Stages 1-6 of one run; see the module docstring for the lineage."""
        # --- Stage 1: query planning (fails open to the raw question) ----
        try:
            queries = self.strategist.plan_queries(question)
        except LLMError as exc:
            print(f"[pipeline] strategist unavailable ({exc}); using raw question")
            queries = [question]
        if not queries:
            queries = [question]

        # --- Stage 2: retrieval -- this pool is FROZEN from here on -------
        pool = self.gather_fn(queries)
        print(f"[pipeline] retrieved {len(pool)} records")
        if len(pool) < MIN_POOL_RECORDS:
            return self._abstain(question, ABSTAIN_THIN_POOL, [])

        # --- Stage 3: appraisal (ranks, caps, drops retracted records) ----
        # The FULL pool -- retracted records included -- stays in `pool` so
        # a citation to a retracted record still resolves for the
        # Verifier's existence check and is deleted by the standing check
        # (an honest "source retracted") rather than by a bogus
        # "citation unresolvable".
        appraised = self.appraiser.appraise(pool, question)
        print(f"[pipeline] appraised {len(appraised)} records")

        evidence_items = _build_evidence_items(appraised)

        high_relevance = [
            item
            for item in evidence_items
            if (item["relevance_score"] or 0) >= HIGH_RELEVANCE_SCORE
        ]
        if len(high_relevance) < MIN_HIGH_RELEVANCE_RECORDS:
            return self._abstain(question, ABSTAIN_LOW_RELEVANCE, evidence_items)

        # --- Stage 4: synthesis (citation-forced; fails closed) -----------
        try:
            synthesis: SynthesisResult = self.synthesizer.synthesize(
                question, evidence_items
            )
        except LLMError as exc:
            print(f"[pipeline] synthesis failed ({exc}); abstaining")
            return self._abstain(question, ABSTAIN_LLM_SYNTHESIS, evidence_items)
        print(
            f"[pipeline] synthesizer produced {len(synthesis.sentences)} sentences"
        )
        if synthesis.abstained:
            return self._abstain(
                question, ABSTAIN_SYNTH_INSUFFICIENT, evidence_items
            )

        # --- Stage 5: verification (the only stage that may delete) -------
        report: VerificationReport = self.verifier.verify(
            question, synthesis, evidence_items, queries
        )
        funnel = report.funnel or _empty_funnel()
        print(
            f"[pipeline] verification: {funnel.get('claims_generated', 0)} generated "
            f"-> {funnel.get('claims_deleted', 0)} deleted "
            f"-> {funnel.get('claims_kept', 0)} kept"
        )

        # --- Stage 6: red team (flag-only, fail-open) ---------------------
        claims = list(report.claims)
        if report.abstained:
            # An abstained answer is never shown, so there is nothing to
            # audit rhetorically -- skip the LLM call entirely.
            print(f"[pipeline] abstained: {report.abstain_reasons}")
        else:
            self._apply_red_team(question, claims)

        result = {
            "question": question,
            "abstained": report.abstained,
            "abstain_reasons": list(report.abstain_reasons),
            "funnel": funnel,
            "answer_text": report.answer_text,
            "claims": [serialize_claim(c) for c in claims],
            "evidence": evidence_items,
            "disclaimer": DISCLAIMER,
        }
        print("[pipeline] complete")
        return result

    # ------------------------------------------------------------------
    # Stage 6 helper
    # ------------------------------------------------------------------

    def _apply_red_team(self, question: str, claims: list[Claim]) -> None:
        """Annotate the surviving claims in place with Red Team flags.

        Only kept/flagged claims are audited (a deleted claim is already
        out of the answer). Flags are merged into Claim.flags as display
        strings alongside the Verifier's own flags ("weakly supported",
        "expression of concern"), keeping the note the UI shows. Any
        failure means zero flags -- the auditor never blocks an answer.
        """
        survivors = [c for c in claims if c.status in ("kept", "flagged")]
        if not survivors:
            return
        try:
            flags: list[RedTeamFlag] = self.red_team.audit(question, survivors)
        except Exception as exc:  # fail-open: audit() should not raise, but
            print(f"[pipeline] red team failed ({exc}); no flags applied")
            return
        flags = flags or []
        print(f"[pipeline] red team: {len(flags)} flags")

        by_id = {c.claim_id: c for c in survivors}
        for flag in flags:
            claim = by_id.get(flag.claim_id)
            if claim is None:
                continue  # RedTeam already guards this; stay defensive
            label = f"{flag.flag}: {flag.note}" if flag.note else flag.flag
            if label not in claim.flags:
                claim.flags.append(label)

    # ------------------------------------------------------------------
    # Abstention and mock replay
    # ------------------------------------------------------------------

    def _abstain(
        self, question: str, reason: str, evidence_items: list[dict]
    ) -> dict:
        """Build the abstention report: same shape, no answer, one reason.

        `evidence_items` is whatever the run got as far as building -- the
        UI still shows the pool it found, which is exactly what makes an
        abstention auditable rather than a dead end.
        """
        print(f"[pipeline] abstained: {[reason]}")
        return {
            "question": question,
            "abstained": True,
            "abstain_reasons": [reason],
            "funnel": _empty_funnel(),
            "answer_text": "",
            "claims": [],
            "evidence": evidence_items or [],
            "disclaimer": DISCLAIMER,
        }

    def _load_mock(self, question: str) -> dict:
        """Replay demo/mock_response.json (offline demo fallback).

        A missing or unparseable file abstains with the reason instead of
        raising -- the demo path must never crash the API either.
        """
        print(f"[pipeline] mock mode: replaying {MOCK_RESPONSE_PATH.name}")
        try:
            with MOCK_RESPONSE_PATH.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"[pipeline] mock response unavailable ({exc})")
            return self._abstain(
                question, f"mock response unavailable: {exc}", []
            )
        if not isinstance(data, dict):
            return self._abstain(
                question,
                "mock response unavailable: expected a JSON object, got "
                f"{type(data).__name__}",
                [],
            )
        print("[pipeline] complete")
        return data


def build_default_pipeline(settings: Optional[Settings] = None) -> EvidencePipeline:
    """Production wiring: five agents over ONE key-rotating LLM client.

    Sharing a single FailoverLLMClient means a key that dies mid-question
    is retired once, for every agent, instead of being re-probed per
    stage (config.FailoverLLMClient rotation is sticky).

    When ``LLM_SENSITIVE_MODEL`` is set, the Verifier (entailment judge)
    uses the heavier model for maximum accuracy on NLI tasks, while all
    other agents use the faster default model.
    """
    if settings is None:
        settings = get_settings()
    llm = FailoverLLMClient(settings=settings)
    # Use heavier model for verifier (entailment/NLI) when configured
    if settings.llm_sensitive_model:
        from config import build_llm_clients
        sensitive_llm = FailoverLLMClient(
            clients=build_llm_clients(settings, model_override=settings.llm_sensitive_model)
        )
    else:
        sensitive_llm = llm
    return EvidencePipeline(
        strategist=Strategist(llm=llm),
        gather_fn=gather_evidence,
        appraiser=Appraiser(pool_cap=settings.pool_cap, llm=llm),
        synthesizer=Synthesizer(llm=llm),
        red_team=RedTeam(llm=llm),
        verifier=Verifier(llm=sensitive_llm, enable_supersession=settings.enable_supersession),
        settings=settings,
    )
