"""
agents/appraiser.py -- evidence appraisal and ranked-pool selection.

Turns a retrieved pool of EvidenceRecords into a deterministic, capped,
ranked pool of AppraisedRecords that downstream agents (Synthesizer,
Verifier) can trust to be "best first".

The rubric and its sources
--------------------------
* Base scores per study design follow the Oxford Centre for Evidence-Based
  Medicine 2011 Levels of Evidence (OCEBM: 1a = systematic reviews /
  meta-analyses, 1b = individual RCTs, 2b = cohort, 3b = case-control,
  4 = case series), the SORT Strength-of-Recommendation Taxonomy (level 1 =
  SR/MA + RCT, level 3 = consensus / expert opinion) and DynaMed's
  level-of-evidence scheme. GRADE's core insight -- design implies a
  starting certainty that other domains can only move so far -- is what
  motivates the bounded blend below.
* Syntheses of non-randomized evidence (e.g. "systematic review of cohort
  studies") are re-based to OCEBM 2a, not 1a.
* Consensus / expert-opinion guidelines are penalized (SORT level 3);
  guidelines explicitly built on systematic-review methodology get a bonus
  (AGREE II rigor domain).
* Retracted records are EXCLUDED outright, per COPE/NLM policy: retracted
  work must never be surfaced as usable clinical evidence.
* Preprints are hard-capped at 65 -- one GRADE level below the peer-reviewed
  equivalent, at or below the cohort tier (65) and above case-control (55).
  Peer-review status warrants a one-level penalty, empirically anchored to
  BMJ Medicine 2023 (PMC9951374: across 356 COVID trials, preprint results
  were rarely inconsistent with the published version) and NLM preprint
  labeling policy (not certified by peer review).
* Recency, missing-abstract and recruiting-protocol penalties are simple
  deterministic metadata modifiers (M1, M7, M8).

Bounded LLM-as-judge blend
--------------------------
If an LLM endpoint is reachable it judges ONLY topical relevance to the
question's PICO -- never study design or rigor, which stay with the
deterministic rubric. The blend

    B = 0.65 * H + 0.35 * R      (H = heuristic score, R = LLM relevance)

is bounded to H +/- 25 (H +/- 15 for "weak" records: unknown design or no
abstract), so the LLM refines ranking WITHIN a tier but can never invert
the evidence hierarchy: a worst on-topic systematic review stays >= 70
while a best on-topic case report stays <= 55. All LLM failures (transport
errors, malformed JSON, hallucinated citation_keys, more than 10% of keys
missing) fail open to pure heuristic scores; appraise() never raises.

Calibration note
----------------
The blend constants (0.65 heuristic / 0.35 LLM weight, the +/-25 / +/-15
adjustment bounds, and the 10% batch-coverage tolerance) are engineering
conventions consistent with the LLM-as-judge bias literature (position and
verbosity bias; Zheng et al. 2023, arXiv:2306.05685) but are not empirically
calibrated -- flagged for future benchmark calibration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from config import MAX_ABSTRACT_CHARS
from core.llm import LLMClient, LLMError
from core.schema import EvidenceRecord, StudyDesign

DEFAULT_POOL_CAP = 30

# --- deterministic rubric constants (OCEBM 2011 / SORT / DynaMed / GRADE) ---

_BASE_SCORE: dict[StudyDesign, int] = {
    # OCEBM 1a -- synthesis of randomized evidence
    StudyDesign.SYSTEMATIC_REVIEW: 95,
    StudyDesign.META_ANALYSIS: 95,
    # OCEBM 1b, SORT level 1
    StudyDesign.RCT: 85,
    # synthesis; rigor varies (AGREE II)
    StudyDesign.GUIDELINE: 75,
    # OCEBM 2b
    StudyDesign.COHORT: 65,
    # OCEBM 3b
    StudyDesign.CASE_CONTROL: 55,
    # narrative review
    StudyDesign.REVIEW: 50,
    # registry protocol, not peer-reviewed evidence
    StudyDesign.CLINICAL_TRIAL_RECORD: 40,
    # OCEBM 4
    StudyDesign.CASE_SERIES: 35,
    StudyDesign.CASE_REPORT: 25,
    # not peer-review certified (NLM / medRxiv)
    StudyDesign.PREPRINT: 20,
    StudyDesign.UNKNOWN: 15,
}

_SR_OF_NON_RANDOMIZED_BASE = 75  # OCEBM 2a: synthesis of non-randomized evidence
_CONSENSUS_PENALTY = 15          # SORT level 3: consensus / expert opinion
_EVIDENCE_BASED_BONUS = 10       # AGREE II rigor: built on systematic review
_PREPRINT_CAP = 65               # one GRADE level below peer-reviewed equivalent
                                 # (BMJ Medicine 2023, PMC9951374; NLM policy)
_MAX_QUESTION_BONUS = 10         # cap on the M5 question-type adjustment

# --- regex sub-rules (matched against lowercased title + abstract) ---

# A8: plural syntheses ("systematic reviews and meta-analyses of ...") and
# en-dash "case\u2013control" must also re-base to OCEBM 2a.
_NON_RANDOMIZED_SYNTHESIS_RE = re.compile(
    r"(?:systematic reviews?|meta-?analys(?:is|es)) of "
    r"(?:observational|cohort|case[-\u2013 ]control)"
)
_CONSENSUS_RE = re.compile(
    r"consensus (?:statement|report|guideline)|expert consensus|delphi"
    r"|position statement|expert opinion"
)
# A1: "grade" only signals GRADE methodology when a methodology term FOLLOWS
# it within one sentence (60 non-sentence-breaking chars). A bare "grade"
# (oncology: "high-grade gliomas", "grade 3 toxicity", "Gleason grade",
# "tumor grade") must NOT earn the +10 bonus. The fixed-width lookbehinds
# additionally exclude the common oncology/histology modifiers (high-, low-,
# tumor-, gleason-, histologic-, plus space variants) so phrases like
# "high-grade glioma assessment" stay plain grade words. Known residual
# edge: "grade 3 toxicity; certainty of evidence ..." inside ONE sentence
# still matches -- accepted for simplicity (rare phrasing).
_EVIDENCE_BASED_RE = re.compile(
    r"(?<!high-)(?<!low-)(?<!tumor-)(?<!gleason-)(?<!histologic-)"
    r"(?<!high )(?<!low )(?<!tumor )(?<!gleason )(?<!histologic )"
    r"\bgrade\b[^.\n]{0,60}\b(?:certainty|quality of evidence|evidence profile|framework|assessment)\b"
    r"|(?:based|informed) (?:by|on) (?:a )?systematic review"
    r"|evidence-based guideline"
)
# A3: noun forms only -- "specifically enrolled" / "nonspecific symptoms"
# are ordinary abstract prose, not diagnostic-accuracy language.
_ACCURACY_MARKER_RE = re.compile(
    r"sensitivit|specificit|diagnostic accuracy|\broc\b|predictive value"
)

# --- question-type classifier regexes (A2: word boundaries stop substring
# false positives: "because" !~ caus, "unpredictable" !~ predict,
# "American Heart Association" !~ associat) ---

_PROGNOSIS_RE = re.compile(r"\bprognos|\boutcome|\bpredict")  # \b blocks "unpredictable"
_CAUSE_RE = re.compile(r"\bcaus(?:e[sd]?|al|ation)?\b|\brisk factor|\bharm|\betiology")
_ASSOC_RE = re.compile(r"\bassociat(?:ed with|ion between)\b")

# CT.gov raw statuses that mean "protocol only, no outcome data yet" (M8).
_RECRUITING_STATUSES = {
    "RECRUITING",
    "NOT_YET_RECRUITING",
    "ENROLLING_BY_INVITATION",
}

# --- bounded LLM-as-judge blend constants ---

_LLM_BATCH_SIZE = 10          # max records per LLM call
                               # Halved from 20: with full-length abstracts
                               # (see MAX_ABSTRACT_CHARS) a 20-record batch
                               # was a ~30K-token judging task per call,
                               # deep into known LLM-as-judge position/
                               # verbosity bias territory (Zheng et al.
                               # 2023, cited above); it also shrinks the
                               # blast radius of one low-coverage batch.
_BLEND_W_HEURISTIC = 0.65     # the rubric dominates the blend
_BLEND_W_LLM = 0.35
_BLEND_BOUND = 25             # max deviation from H for solid records
_BLEND_BOUND_WEAK = 15        # tighter bound for unknown design / no abstract
_LLM_MISS_TOLERANCE = 0.10    # >10% missing keys -> heuristic-only batch

_LLM_SYSTEM = (
    "You are a clinical evidence relevance judge. Score ONLY topical relevance "
    "to the clinical question -- never study design or methodological quality. "
    "Anchor relevance: 90-100 = directly answers the question's PICO; 60-89 = "
    "same condition/intervention, partial PICO match; 30-59 = related topic "
    "only; 0-29 = unrelated. Return every input citation_key exactly once and "
    "never invent new citation_keys."
)


@dataclass
class AppraisedRecord:
    """One ranked piece of evidence: the live record, its 0-100 score, and
    an auditable rationale string explaining exactly how the score arose.

    SECURITY NOTE (A9): `rationale` is UNTRUSTED text. External abstracts are
    interpolated into the LLM judge prompt, so LLM-authored rationale (and
    any text a crafted abstract injects through it) must be treated by
    downstream renderers as plain text only -- never as executable content
    or markup (no HTML/Markdown rendering, no shell/SQL interpolation).
    """

    record: EvidenceRecord
    score: int
    rationale: str
    #: Raw, UN-blended, un-bounded LLM topical relevance (0-100), None when
    #: the LLM was unavailable/skipped for this record. `score` above is the
    #: blended, design-dominated value the rubric uses for ranking; this is
    #: the one field that answers "how on-topic is this, independent of
    #: study design" -- needed because a design-dominated score structurally
    #: cannot tell a well-supported case report from an off-topic review
    #: (see pipeline.py's _topical_score()).
    relevance: Optional[int] = None


@dataclass
class _Row:
    """Internal working row: a live record plus its computed scores."""

    record: EvidenceRecord
    heuristic: int
    base: int  # effective base score (after sub-rule overrides), for tie-breaks
    notes: list[str]
    final: int = 0
    rationale: str = ""
    relevance: Optional[int] = None  # raw LLM relevance; see AppraisedRecord.relevance


# --- small helpers -----------------------------------------------------------


def _has_abstract(record: EvidenceRecord) -> bool:
    return bool((record.abstract or "").strip())


def _short(text: str, limit: int = 48) -> str:
    """Collapse whitespace and truncate an LLM rationale for the audit trail."""
    cleaned = " ".join(text.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _recency_modifier(pub: Optional[date]) -> tuple[int, str]:
    """M1: recency modifier. Never hardcodes a year."""
    if pub is None:
        return -5, "no date (-5)"
    years_old = date.today().year - pub.year
    if years_old < 0:
        return 5, "future-dated (+5)"  # treat future dates as age 0
    if years_old <= 2:
        return 5, "recent (+5)"
    if years_old <= 5:
        return 0, ""
    if years_old <= 10:
        return -5, "aging (-5)"
    if years_old <= 20:
        return -10, "old (-10)"
    return -15, "very old (-15)"


def _classify_question(question: str) -> str:
    """
    Simple deterministic keyword classifier for the question type (M5).

    Word-boundary regexes so ordinary words containing keyword substrings
    ("because", "unpredictable", "American Heart Association") do not
    misroute therapy questions into the prognosis / etiology buckets.
    Diagnosis detection deliberately stays on plain substrings ("diagnos",
    "sensitiv", "specific", "test accuracy") because a diagnosis question
    itself is expected to use those exact clinical terms.
    """
    q = (question or "").lower()
    if _PROGNOSIS_RE.search(q):
        return "prognosis"
    if _CAUSE_RE.search(q) or _ASSOC_RE.search(q):
        return "etiology_harm"
    if "diagnos" in q or "sensitiv" in q or "specific" in q or "test accuracy" in q:
        return "diagnosis"
    return "therapy"


def _question_adjustment(
    design: StudyDesign, qtype: str, text: str
) -> tuple[int, str]:
    """
    M5: question-type adjustment. OCEBM ranks inception cohorts as the top
    evidence for prognosis/etiology questions; diagnostic-accuracy questions
    only credit cohort/case-control designs when accuracy language is present.
    Case-control diagnostic designs sit at OCEBM Level 4 (below cohort), so
    they earn half the cohort's diagnosis bonus (+5 vs +10).
    """
    if qtype in ("prognosis", "etiology_harm"):
        if design is StudyDesign.COHORT:
            return _MAX_QUESTION_BONUS, f"cohort favored for {qtype} (+10)"
        if design is StudyDesign.CASE_CONTROL:
            return 5, f"case-control favored for {qtype} (+5)"
        return 0, ""
    if qtype == "diagnosis":
        if _ACCURACY_MARKER_RE.search(text):
            if design is StudyDesign.COHORT:
                return _MAX_QUESTION_BONUS, "diagnostic accuracy evidence (+10)"
            if design is StudyDesign.CASE_CONTROL:
                # OCEBM Level 4: case-control diagnostic designs rank below
                # cohort (Level 2), so they earn half the cohort bonus.
                return 5, "diagnostic accuracy evidence (+5, OCEBM Level 4)"
    return 0, ""


def _heuristic_score(
    record: EvidenceRecord, question: str
) -> tuple[int, list[str], int]:
    """
    Deterministic heuristic score H plus audit notes plus the effective base.

    H = round(clamp(base + modifiers, 0, 100)) where base comes from the
    OCEBM/SORT/DynaMed-derived design table and modifiers are M1 (recency),
    M5 (question type), M7 (missing abstract) and M8 (recruiting protocol).
    The preprint cap (M2) is NOT applied here -- it is applied last, after
    the LLM blend, in Appraiser._finalize.
    """
    text = f"{record.title or ''} {record.abstract or ''}".lower()
    design = record.study_design
    table_base = _BASE_SCORE.get(design, _BASE_SCORE[StudyDesign.UNKNOWN])
    base = table_base
    notes = [f"{design.value} (base {table_base})"]

    # --- design sub-rules ---------------------------------------------------
    if design in (StudyDesign.SYSTEMATIC_REVIEW, StudyDesign.META_ANALYSIS):
        if _NON_RANDOMIZED_SYNTHESIS_RE.search(text):
            base = _SR_OF_NON_RANDOMIZED_BASE
            notes.append("synthesis of non-randomized evidence (base 75, OCEBM 2a)")
    if design is StudyDesign.GUIDELINE:
        if _CONSENSUS_RE.search(text):
            base -= _CONSENSUS_PENALTY
            notes.append("consensus / expert opinion (-15, SORT level 3)")
        if _EVIDENCE_BASED_RE.search(text):
            base += _EVIDENCE_BASED_BONUS
            notes.append("evidence-based methodology (+10, AGREE II)")

    # --- metadata modifiers ---------------------------------------------------
    modifiers = 0

    delta, note = _recency_modifier(record.publication_date)  # M1
    modifiers += delta
    if note:
        notes.append(note)

    delta, note = _question_adjustment(  # M5 (capped at +10 per record)
        design, _classify_question(question), text
    )
    delta = min(delta, _MAX_QUESTION_BONUS)
    modifiers += delta
    if note:
        notes.append(note)

    if not _has_abstract(record):  # M7
        modifiers -= 5
        notes.append("no abstract (-5)")

    if design is StudyDesign.CLINICAL_TRIAL_RECORD and (  # M8
        (record.trial_status or "").upper() in _RECRUITING_STATUSES
    ):
        modifiers -= 10
        notes.append("recruiting protocol, no outcome data (-10)")

    if record.is_preprint or design is StudyDesign.PREPRINT:  # M2 flag
        notes.append("preprint")

    # A5: half-up rounding (Python's round() is banker's rounding, which sends
    # an exact .5 to the nearest EVEN integer). base + modifiers is an int
    # today, so this is a no-op here -- it just pins the same half-up contract
    # the LLM blend in Appraiser._finalize relies on.
    score = int(math.floor(min(100, max(0, base + modifiers)) + 0.5))
    return score, notes, base


def _sort_key(row: _Row) -> tuple:
    """
    Deterministic total order for the final pool:
    final score desc -> effective design base desc -> publication_date desc
    (undated last) -> has-abstract (True first) -> citation_key asc.
    """
    record = row.record
    pub = record.publication_date
    # Newest date sorts first; undated records sort last.
    date_key = float("inf") if pub is None else -pub.toordinal()
    abstract_key = 0 if _has_abstract(record) else 1
    return (-row.final, -row.base, date_key, abstract_key, record.citation_key())


class Appraiser:
    """
    Ranks retrieved EvidenceRecords against a clinical question.

    Primary path is fully deterministic (evidence-hierarchy rubric). When an
    LLM endpoint is available it contributes a bounded relevance-only blend
    that can reorder records within a tier but never invert the hierarchy.
    appraise() is fail-open: it never raises to the caller.
    """

    def __init__(
        self, pool_cap: int = DEFAULT_POOL_CAP, llm: Optional[LLMClient] = None
    ) -> None:
        """
        pool_cap: maximum number of records appraise() returns. None means
        DEFAULT_POOL_CAP; zero/negative values are clamped to 0 (appraise()
        then returns []).

        llm: optional LLMClient (or anything with a compatible complete_json)
        used for the bounded relevance blend. When None, a client is
        auto-constructed here (the constructor does no network I/O); if the
        endpoint later turns out to be unreachable, appraise() automatically
        engages heuristic-only mode and probes the endpoint at most once per
        call instead of once per batch.
        """
        # A4: clamp instead of trusting the caller -- a negative pool_cap
        # would otherwise slice rows[:-n] and silently return the WORST n
        # records instead of nothing.
        self.pool_cap = DEFAULT_POOL_CAP if pool_cap is None else max(0, int(pool_cap))
        self.llm = llm or LLMClient()

    def appraise(
        self,
        records: list[EvidenceRecord],
        question: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[AppraisedRecord]:
        """
        Score, sort and truncate a pool of records. Returns AppraisedRecords
        sorted best-first, at most pool_cap of them. Retracted records are
        excluded (COPE/NLM). Empty input returns [].

        Rows are sorted by citation_key once BEFORE batching, so batch
        membership (and therefore the LLM prompts) is deterministic
        regardless of input order.

        on_progress, when given, is called as (batch_index, batch_count)
        (both 1-based/1-total) after each LLM batch resolves -- lets a
        caller stream mid-stage progress instead of only start/done.
        """
        if not records:
            return []

        rows: list[_Row] = []
        for record in records:
            if record.is_retracted:  # M3: retracted work is never evidence
                print(
                    f"[appraiser] excluding retracted record "
                    f"{record.citation_key()} "
                    f"(retraction_source={record.retraction_source}): "
                    f"retracted work is never ranked as evidence (COPE/NLM)"
                )
                continue
            score, notes, base = _heuristic_score(record, question)
            rows.append(
                _Row(record=record, heuristic=score, notes=notes, base=base)
            )

        # A7: deterministic batch membership -- sort ALL rows by citation_key
        # once BEFORE chunking. (Sorting each batch after slicing would keep
        # prompts sorted but make batch membership depend on input order.)
        rows.sort(key=lambda row: row.record.citation_key())

        # LLM pass, batches of <= _LLM_BATCH_SIZE. A6: per-call failure
        # memory -- after the first batch whose LLM call RAISED (transport
        # failure; an unreachable endpoint can block for the full client
        # timeout on every probe), skip the endpoint entirely for the
        # remaining batches. Non-transport fallbacks (malformed payload,
        # >10% missing keys) do NOT trip this memory.
        llm_down = False
        batch_count = math.ceil(len(rows) / _LLM_BATCH_SIZE)
        for batch_index, start in enumerate(range(0, len(rows), _LLM_BATCH_SIZE), start=1):
            chunk = rows[start : start + _LLM_BATCH_SIZE]
            if llm_down:
                rankings, failure_tag = None, "llm_unavailable"
            else:
                rankings, failure_tag, transport_failed = self._fetch_llm_rankings(
                    question, chunk
                )
                if transport_failed:
                    llm_down = True
                    if start + _LLM_BATCH_SIZE < len(rows):  # more batches left
                        print(
                            "[appraiser] LLM unavailable; "
                            "remaining batches scored heuristically"
                        )
            for row in chunk:
                self._finalize(row, rankings, failure_tag)

            if on_progress is not None:
                try:
                    on_progress(batch_index, batch_count)
                except Exception as exc:
                    print(f"[appraiser] on_progress callback failed ({exc}); ignoring")

        rows.sort(key=_sort_key)
        return [
            AppraisedRecord(record=row.record, score=row.final, rationale=row.rationale, relevance=row.relevance)
            for row in rows[: self.pool_cap]
        ]

    # --- LLM plumbing -------------------------------------------------------

    @staticmethod
    def _build_prompt(question: str, records: list[EvidenceRecord]) -> str:
        """Build the relevance-only prompt. `records` must already be sorted
        by citation_key (done by appraise before chunking)."""
        lines = [
            f'Clinical question: "{question}"',
            "",
            "Judge ONLY how topically relevant each record is to this question.",
            "Do NOT score study design or methodological rigor -- those are",
            "ranked separately by a deterministic evidence rubric.",
            "",
            "Relevance anchors:",
            "  90-100: directly answers the question's PICO",
            "  60-89:  same condition/intervention, partial PICO match",
            "  30-59:  related topic only",
            "  0-29:   unrelated",
            "",
            'Respond as JSON: {"rankings": [{"citation_key": "...", '
            '"relevance": <int 0-100>, "rationale": "<short>"}]}',
            "Include EVERY citation_key listed below exactly once. Do not invent",
            "new citation_keys.",
            "",
            "Records:",
        ]
        for r in records:
            lines.append(
                f"- citation_key: {r.citation_key()}\n"
                f"  title: {r.title or ''}\n"
                f"  design: {r.study_design.value}\n"
                f"  date: {r.publication_date.isoformat() if r.publication_date else 'unknown'}\n"
                f"  abstract: {(r.abstract or '')[:MAX_ABSTRACT_CHARS]}"
            )
        return "\n".join(lines)

    def _fetch_llm_rankings(
        self, question: str, chunk: list[_Row]
    ) -> tuple[Optional[dict[str, tuple[int, str]]], str, bool]:
        """
        Call the LLM for one chunk and validate the response.

        Returns (rankings, failure_tag, transport_failed):
        * rankings: {citation_key: (relevance, rationale)} or None when the
          whole batch must fall back to heuristic-only scores (call raised,
          malformed payload, or more than 10% of keys missing).
        * failure_tag: "" when rankings are usable; "llm_unavailable" when the
          call raised or the payload had an unexpected shape; and
          "llm_low_coverage" when more than 10% of the batch's citation_keys
          were missing (distinct from the per-record "llm_skipped" tag).
        * transport_failed: True only when the LLM call itself raised. Used
          by appraise() as per-call failure memory so a dead endpoint is
          probed at most once per appraise() call, not once per batch.

        Hallucinated keys are dropped; individual non-int relevances are
        treated as missing keys.
        """
        records = [row.record for row in chunk]
        prompt = self._build_prompt(question, records)
        try:
            response = self.llm.complete_json(
                prompt, system=_LLM_SYSTEM, temperature=0.0
            )
        except LLMError as exc:
            print(f"[appraiser] LLM unavailable, heuristic-only for this batch: {exc}")
            return None, "llm_unavailable", True
        except Exception as exc:  # defensive: appraise() must never raise
            print(f"[appraiser] unexpected LLM failure, heuristic-only: {exc}")
            return None, "llm_unavailable", True

        if not isinstance(response, dict) or not isinstance(
            response.get("rankings"), list
        ):
            print(
                f"[appraiser] LLM response had unexpected shape "
                f"({type(response).__name__}), heuristic-only for this batch"
            )
            return None, "llm_unavailable", False

        valid_keys = {r.citation_key() for r in records}
        rankings: dict[str, tuple[int, str]] = {}
        for entry in response["rankings"]:
            if not isinstance(entry, dict):
                continue
            key = entry.get("citation_key")
            if not isinstance(key, str):
                continue
            if key not in valid_keys:  # hallucination guard
                print(
                    f"[appraiser] dropping hallucinated citation_key {key!r} "
                    f"(not in input)"
                )
                continue
            if key in rankings:  # duplicate: first occurrence wins
                continue
            relevance = entry.get("relevance")
            if isinstance(relevance, bool) or not isinstance(relevance, int):
                print(
                    f"[appraiser] ignoring non-int relevance for {key!r}: "
                    f"{relevance!r}"
                )
                continue
            rationale = entry.get("rationale")
            if not isinstance(rationale, str):
                rationale = ""
            # clamp relevance into 0-100 before it can touch the blend
            rankings[key] = (max(0, min(100, relevance)), " ".join(rationale.split()))

        missing = len(valid_keys) - len(rankings)
        if valid_keys and missing / len(valid_keys) > _LLM_MISS_TOLERANCE:
            print(
                f"[appraiser] LLM missed {missing}/{len(valid_keys)} citation_keys "
                f"(>10%), heuristic-only for this batch"
            )
            return None, "llm_low_coverage", False
        return rankings, "", False

    def _finalize(
        self,
        row: _Row,
        rankings: Optional[dict[str, tuple[int, str]]],
        failure_tag: str = "",
    ) -> None:
        """
        Blend heuristic and LLM scores (bounded), apply the preprint cap last,
        and write the final score + auditable rationale onto the row.
        `failure_tag` records why a batch-level heuristic-only fallback
        happened ("llm_unavailable" vs "llm_low_coverage").
        """
        record = row.record
        final = row.heuristic

        if rankings is None:
            llm_note = failure_tag or "llm_unavailable"
        else:
            hit = rankings.get(record.citation_key())
            if hit is None:
                llm_note = "llm_skipped"  # heuristic-only for this record
            else:
                relevance, llm_rationale = hit
                row.relevance = relevance  # raw, un-blended -- see AppraisedRecord.relevance
                weak = (
                    record.study_design is StudyDesign.UNKNOWN
                    or not _has_abstract(record)
                )
                bound = _BLEND_BOUND_WEAK if weak else _BLEND_BOUND
                blended = _BLEND_W_HEURISTIC * row.heuristic + _BLEND_W_LLM * relevance
                windowed = min(
                    row.heuristic + bound, max(row.heuristic - bound, blended)
                )
                # A5: half-up rounding. Python's round() uses banker's
                # rounding (round-half-to-even), so an exact blend of 36.5
                # would become 36. Clinical scores are not statistical
                # aggregates where half-to-even removes bias; determinism
                # and intuition favor .5 always rounding up.
                final = int(math.floor(min(100, max(0, windowed)) + 0.5))
                llm_note = f"LLM relevance {relevance}"
                if llm_rationale:
                    llm_note += f" ({_short(llm_rationale)})"
                if windowed != blended:
                    llm_note += "; blend capped"

        # M2: preprint hard cap, applied LAST (after blending).
        if (
            record.is_preprint or record.study_design is StudyDesign.PREPRINT
        ) and final > _PREPRINT_CAP:
            final = _PREPRINT_CAP
            llm_note += "; preprint cap 65"

        row.final = int(final)
        row.rationale = "; ".join([*row.notes, llm_note])
