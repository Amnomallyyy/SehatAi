"""
agents/synthesizer.py -- citation-forced draft answer generation.

Synthesizer -- generates a citation-forced draft answer from the appraised
evidence pool. Every sentence must end with one or more [S#] tags so the
Verifier can trace each claim to evidence; uncited sentences are deleted
at parse time (citation recall by construction). The exact token
INSUFFICIENT_EVIDENCE is the abstention signal.

Pipeline position: Strategist -> retrieval -> Appraiser (scores, sorts,
caps at 30) -> Synthesizer (this module) -> Verifier -> UI. The Verifier
later decomposes each kept sentence into atomic claims and checks them,
so the citation tags written here are the enforcement backbone of the
whole system.

How the forcing works
---------------------
The system prompt instructs the model to end EVERY sentence with [S#]
tags naming the evidence it used. Instructions alone are not trusted:
the parser re-checks every sentence deterministically --

* a sentence with no [S#] tag is deleted ("uncited claim");
* a sentence citing any sid absent from the evidence input is deleted
  ("citation to unknown evidence");
* surviving sentences are renumbered 0..n-1 and carry their deduplicated,
  validated citation lists.

By construction every kept sentence carries at least one valid citation
-- ALCE-style citation recall of 1.0 -- and every deletion is reported in
SynthesisResult.parse_deletions with its reason, so the UI can show what
was removed and why.

Abstention
----------
When the evidence cannot answer the question AT ALL the model must
respond with exactly INSUFFICIENT_EVIDENCE. Only that exact token
(case-sensitive, after whitespace-strip) marks abstention; the token
appearing mid-prose is ordinary text. An empty evidence pool abstains
without calling the LLM at all -- with nothing to cite, the only honest
output is abstention.

Partial answers ([GAP])
------------------------
A compound question can have some parts the evidence supports and some
it doesn't -- forcing an all-or-nothing choice between a full answer and
total abstention throws away the parts that WERE well-supported. A
sentence tagged [GAP] instead of [S#] is the model's own self-reported
statement that one specific facet isn't addressed by the evidence set.
It carries no citation, makes no claim, is never sent to the Verifier,
and is surfaced separately as SynthesisResult.unanswered_aspects rather
than as part of the answer. This is a transparency mechanism, not a
verification one -- see _parse()'s docstring for the exact rules.

"Whats coming" hint
-------------------
ClinicalTrials.gov records (retrieval/clinicaltrials.py) answer "what's
coming" rather than "what's already published": a trial still in the
field has a protocol but no outcome data. When any SELECTED item carries
an ongoing trial_status (RECRUITING / NOT_YET_RECRUITING /
ACTIVE_NOT_RECRUITING), one short trailing line is appended to the
prompt inviting -- never requiring -- a single still-cited closing
sentence about what is still being studied.

Design lineage
--------------
* VerifAI's referenced-answer generation (arXiv:2604.08549): generate an
  answer with explicit references first, verify each claim against them
  afterwards.
* ALCE (arXiv:2305.14627): citation recall / citation precision as the
  backbone evaluation for cited LLM answers; this module forces recall
  to 1 by deleting uncited sentences at parse time.
* AIS attribution framework (arXiv:2305.09352): attribution as a
  first-class property of generation, not a post-hoc retrieval step.

The LLM client is injected (duck-typed: anything with a complete()
method; the pipeline injects a FailoverLLMClient). LLM transport
failures are NOT swallowed: synthesize() prints and re-raises LLMError so
the pipeline can produce its own LLM-unavailable answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from config import MAX_ABSTRACT_CHARS, MAX_EVIDENCE_PROMPT_CHARS
from core.llm import LLMClient, LLMError

# Exact abstention token. Only this string, case-sensitive, after
# whitespace-strip, means "the evidence cannot answer the question".
ABSTENTION_TOKEN = "INSUFFICIENT_EVIDENCE"

# CT.gov overallStatus values meaning "protocol only, no outcome data
# yet" -- the whats_coming set (see retrieval/clinicaltrials.py).
_ONGOING_TRIAL_STATUSES = {
    "RECRUITING",
    "NOT_YET_RECRUITING",
    "ACTIVE_NOT_RECRUITING",
}

# [S1]-style citation tags anywhere in a sentence. A bracket group may hold
# one id ([S1]) or several, comma-separated ([S1, S2, S4]) -- confirmed
# live, 2026-08-30: the model sometimes writes multi-citations as one
# grouped bracket instead of separate [S1][S2][S4] tags, and a parser that
# only recognized the latter deleted every sentence of an otherwise
# correct, well-cited answer as "uncited". Both forms are accepted; a
# sentence citing [S1][S3, S7] yields tags ["S1", "S3", "S7"].
_CITATION_GROUP_RE = re.compile(r"\[\s*(S\d+(?:\s*,\s*S\d+)*)\s*\]")
_CITATION_ID_RE = re.compile(r"S\d+")
# Tag removal for the display text: eat the whitespace run before the tag
# so "reduced HbA1c [S1]." becomes "reduced HbA1c." and then (below) the
# bare claim text.
_TAG_STRIP_RE = re.compile(r"\s*\[\s*S\d+(?:\s*,\s*S\d+)*\s*\]")
# [GAP]-tagged sentences are the model's own "the evidence doesn't cover
# this part" statement -- not a citation, not a claim (see SynthesisResult
# .unanswered_aspects / _LLM_SYSTEM's [GAP] instruction).
_GAP_TAG_RE = re.compile(r"\[GAP\]")
_GAP_STRIP_RE = re.compile(r"\s*\[GAP\]")
# Trailing sentence terminators are presentation, not claim content.
_TRAILING_PUNCT_RE = re.compile(r"[.!?]+$")
# Sentence split: after . ! ? followed by whitespace. A trailing fragment
# without terminal punctuation survives as the final element.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_LLM_SYSTEM = (
    "You are a clinical evidence synthesizer for physicians. Answer using "
    "ONLY facts present in the evidence set. Write 6-14 sentences (this "
    "range covers cited answer sentences only, see the GAP rule below). "
    "EVERY sentence MUST end with one or more evidence tags in square "
    "brackets naming the evidence you used, e.g. 'Semaglutide reduced "
    "HbA1c by 1.5 percentage points versus placebo [S1].' When citing "
    "several sources in one sentence, either style is fine: separate "
    "brackets [S1][S2] or one bracket with a comma-separated list "
    "[S1, S2]. Do not write any sentence without a tag. Only use tags "
    "that exist in the "
    "evidence set. Use precise, hedged clinical language (no absolutes "
    "like 'always'/'never'/'cures'). If the evidence cannot answer the "
    "question AT ALL, respond with exactly: INSUFFICIENT_EVIDENCE\n\n"
    "The question may have several distinct parts. If one specific part "
    "is not addressed by the evidence set, write ONE separate, "
    "self-contained sentence describing exactly what is missing, ending "
    "with [GAP] instead of an evidence tag, placed exactly where an "
    "[S#] tag would go (before the final punctuation) -- e.g. 'The "
    "exact weight-based dosing regimen for this reversal agent is not "
    "stated in the evidence set [GAP].' Never mix [GAP] and an [S#] tag in the same "
    "sentence. Use [GAP] sparingly, only for a genuinely distinct part "
    "the evidence does not cover -- keep answering, with citations, "
    "everything else you can support. [GAP] sentences do not count "
    "toward the sentence range above and never replace a citable answer "
    "you could otherwise give."
)


@dataclass
class Sentence:
    """One kept, fully-cited sentence of the draft answer.

    SECURITY NOTE: `text` and `citations` derive from LLM output over
    untrusted abstracts; downstream renderers must treat them as plain
    text only (same policy as AppraisedRecord.rationale).

    index: 0-based position in the kept sentence sequence (contiguous --
    sentences deleted at parse time leave no gaps).
    text: sentence text WITHOUT the citation tags and trailing terminator,
    stripped.
    citations: validated sids cited by the sentence, first-occurrence
    order, deduplicated. Every sid is guaranteed to exist in the evidence
    input, so the Verifier can look each one up.
    """

    index: int
    text: str
    citations: list[str]


@dataclass
class ParseDeletion:
    """A sentence removed by the deterministic parser -- never shown as an
    answer claim.

    text: the offending sentence, citation tags INCLUDED, so the UI can
    show exactly what was removed and why.
    reason: "uncited claim" | "citation to unknown evidence".
    """

    text: str
    reason: str


@dataclass
class SynthesisResult:
    """Outcome of one synthesize() call.

    raw_text: exactly what the LLM returned (for debugging; may contain
    sentences the parser later deleted).
    sentences: kept, cited, validated sentences in answer order.
    abstained: True iff the LLM emitted exactly INSUFFICIENT_EVIDENCE (or
    the evidence pool was empty and the LLM was never called).
    parse_deletions: sentences deleted at parse time, with reasons.
    unanswered_aspects: [GAP]-tagged sentences -- the model's own
    self-reported statements that a specific facet of the question isn't
    addressed by the evidence set. These carry no citation and are NOT
    claims: they never reach the Verifier and never appear as part of
    the answer text. Always [] when abstained is True (the exact
    INSUFFICIENT_EVIDENCE token short-circuits before per-sentence
    parsing) or when the evidence pool was empty.
    """

    raw_text: str
    sentences: list[Sentence]
    abstained: bool
    parse_deletions: list[ParseDeletion]
    unanswered_aspects: list[str]


class Synthesizer:
    """Generates the citation-forced draft answer from appraised evidence.

    The prompt shows a budget-aware selection of the evidence pool (see
    _select_evidence): every item whose raw topical relevance clears
    `topical_floor` is guaranteed a place regardless of its blended,
    design-dominated relevance_score, then remaining items fill in
    best-score-first until `prompt_char_budget` is exhausted (and
    `max_evidence`, if set). This exists because case reports/series
    structurally cannot clear a high blended score (bounded blend, see
    agents/appraiser.py) even when perfectly on-topic -- a plain top-N-by-
    blended-score cut silently excludes exactly the evidence a rare-disease
    question depends on. Citation validation accepts sids from the WHOLE
    evidence input regardless of what was shown -- a citation the pipeline
    can still resolve is kept, a citation to evidence that does not exist
    is deleted. The parser is fully deterministic; the only LLM call is
    the synthesis itself.
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        max_evidence: Optional[int] = None,
        max_abstract_chars: int = MAX_ABSTRACT_CHARS,
        prompt_char_budget: int = MAX_EVIDENCE_PROMPT_CHARS,
        topical_floor: int = 60,
    ) -> None:
        """
        llm: injected LLM client (duck-typed -- anything exposing
        complete(); the pipeline injects a FailoverLLMClient). None -> a
        default LLMClient, which does no network I/O at construction.
        max_evidence: hard cap on how many evidence items the prompt may
        show. None (default) means no count cap -- prompt_char_budget is
        the real limit.
        max_abstract_chars: per-item abstract truncation length in the
        prompt.
        prompt_char_budget: total character budget for the evidence
        section of the prompt (guaranteed items are never dropped for
        this, only the "fill" pass is budget-limited).
        topical_floor: raw topical_relevance at or above which an item is
        guaranteed a place ahead of the budget/count fill pass.
        """
        self.llm = llm or LLMClient()
        # Clamp defensively (appraiser precedent): max_evidence <= 0 would
        # show the model an empty evidence set; a non-positive abstract
        # cap would truncate every abstract to a bare ellipsis.
        self.max_evidence = max(1, int(max_evidence)) if max_evidence is not None else None
        self.max_abstract_chars = max(1, int(max_abstract_chars))
        self.prompt_char_budget = max(1, int(prompt_char_budget))
        self.topical_floor = int(topical_floor)

    def synthesize(self, question: str, evidence: list[dict]) -> SynthesisResult:
        """
        Generate the citation-forced draft answer for `question` from the
        appraised evidence pool.

        * empty/whitespace question -> ValueError (nothing to answer).
        * empty evidence pool -> abstention WITHOUT calling the LLM (there
          is nothing to cite, so the only honest output is abstention).
        * LLM transport failure -> prints, then re-raises LLMError (the
          pipeline produces its own LLM-unavailable answer; this agent
          never invents one).

        The prompt shows only the top `max_evidence` items by
        relevance_score (stable sort, ties keep input order), but citation
        validation uses sids from the whole evidence input.
        """
        if not (question or "").strip():
            raise ValueError("question must be non-empty")

        if not evidence:
            print("[synthesizer] empty evidence pool; abstaining")
            return SynthesisResult(
                raw_text="", sentences=[], abstained=True, parse_deletions=[],
                unanswered_aspects=[],
            )

        selected = self._select_evidence(evidence)
        print(
            f"[synthesizer] synthesizing from {len(selected)} of "
            f"{len(evidence)} evidence items (highest relevance first)"
        )

        # Citation validation set: sids of the WHOLE input, case-sensitive.
        valid_sids = {
            str(item.get("sid")) for item in evidence if item.get("sid")
        }

        prompt = self._build_prompt(question, selected)
        try:
            raw_text = self.llm.complete(
                prompt, system=_LLM_SYSTEM, temperature=0.2
            )
        except LLMError as exc:
            print(f"[synthesizer] LLM synthesis failed: {exc}")
            raise
        if not isinstance(raw_text, str):  # defensive: complete() -> str
            raw_text = "" if raw_text is None else str(raw_text)

        result = self._parse(raw_text, valid_sids)
        if result.abstained:
            print("[synthesizer] abstained (INSUFFICIENT_EVIDENCE)")
        elif not result.sentences:
            print(
                "[synthesizer] WARNING: no sentences survived citation "
                f"parsing ({len(result.parse_deletions)} deleted)"
            )
        else:
            print(
                f"[synthesizer] kept {len(result.sentences)} cited "
                f"sentence(s); deleted {len(result.parse_deletions)} at "
                "parse time"
            )
        return result

    # --- evidence selection --------------------------------------------------

    @staticmethod
    def _blended_score(item: dict) -> int:
        return item.get("relevance_score") or 0

    @staticmethod
    def _topical(item: dict) -> Optional[int]:
        r = item.get("topical_relevance")
        return r if isinstance(r, int) and not isinstance(r, bool) else None

    def _item_chars(self, item: dict) -> int:
        """Approximate prompt cost of one evidence block: the abstract
        (capped the same way _format_item caps it) plus a small constant
        for the header/title line."""
        abstract_len = min(len(str(item.get("abstract") or "")), self.max_abstract_chars)
        return abstract_len + 200

    def _select_evidence(self, evidence: list[dict]) -> list[dict]:
        """Two-pass, budget-aware selection.

        Pass 1 (guarantee): every item whose topical_relevance >=
        topical_floor is admitted first, best-blended-score-first, and is
        NEVER dropped for budget reasons -- this is what stops an on-topic
        case report from being squeezed out by an off-topic but
        higher-blended-score review.
        Pass 2 (fill): remaining items, best-blended-score-first, added
        until prompt_char_budget is exhausted or max_evidence (if set) is
        reached.
        Output order: blended relevance_score descending overall, so the
        "best evidence first" contract callers rely on still holds.
        """
        ranked = sorted(evidence, key=lambda item: -self._blended_score(item))
        guaranteed = [
            item for item in ranked
            if (self._topical(item) or 0) >= self.topical_floor
        ]
        guaranteed_ids = {id(item) for item in guaranteed}
        rest = [item for item in ranked if id(item) not in guaranteed_ids]

        selected = list(guaranteed)
        used_chars = sum(self._item_chars(item) for item in guaranteed)

        for item in rest:
            if self.max_evidence is not None and len(selected) >= self.max_evidence:
                break
            cost = self._item_chars(item)
            if selected and used_chars + cost > self.prompt_char_budget:
                break
            used_chars += cost
            selected.append(item)

        selected.sort(key=lambda item: -self._blended_score(item))
        return selected

    # --- prompt assembly ----------------------------------------------------

    def _build_prompt(self, question: str, selected: list[dict]) -> str:
        """Assemble the user prompt: the clinical question, the evidence
        blocks (plus, when any selected trial is still ongoing, the single
        whats_coming hint line), then the answer instruction."""
        blocks = [self._format_item(item) for item in selected]
        hint = self._ongoing_hint(selected)
        if hint:
            blocks.append(hint)
        evidence_block = "\n\n".join(blocks)
        return (
            f'Clinical question: "{question}"\n\n'
            f"Evidence set:\n{evidence_block}\n\n"
            f"Answer (every sentence cited, or {ABSTENTION_TOKEN}):"
        )

    def _format_item(self, item: dict) -> str:
        """One evidence block:

            [S1] (relevance 92, rct, 2024, JAMA) Title of the study.
            Abstract: <truncated to max_abstract_chars, "…" appended if cut>

        Missing metadata degrades gracefully: absent journal/year/design
        simply drop out of the parenthetical, and a missing/blank abstract
        becomes "No abstract available.".
        """
        sid = str(item.get("sid") or "").strip()
        parts = []
        score = item.get("relevance_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            parts.append(f"relevance {score}")
        design = str(item.get("study_design") or "").strip()
        if design:
            parts.append(design)
        year = str(item.get("publication_date") or "")[:4]
        if year:
            parts.append(year)
        journal = str(item.get("journal") or "").strip()
        if journal:
            parts.append(journal)
        title = " ".join(str(item.get("title") or "").split()) or "(untitled)"
        header = (
            f"[{sid}] ({', '.join(parts)}) {title}"
            if parts
            else f"[{sid}] {title}"
        )

        abstract = str(item.get("abstract") or "").strip()
        if not abstract:
            abstract = "No abstract available."
        elif len(abstract) > self.max_abstract_chars:
            abstract = abstract[: self.max_abstract_chars].rstrip() + "…"
        return f"{header}\nAbstract: {abstract}"

    @staticmethod
    def _ongoing_hint(selected: list[dict]) -> str:
        """The single whats_coming trailing line, or "" when no selected
        trial is still in the field. Only SELECTED items are named: the
        model may only cite evidence it can actually see in the prompt."""
        descriptors = []
        for item in selected:
            status = str(item.get("trial_status") or "").strip()
            if status.upper() not in _ONGOING_TRIAL_STATUSES:
                continue
            key = str(
                item.get("citation_key") or item.get("native_id") or "unknown"
            )
            descriptors.append(f"[{item.get('sid')}] ({key}, status {status})")
        if not descriptors:
            return ""
        return (
            "Ongoing/unreported trials in the set: "
            + ", ".join(descriptors)
            + " — you may add ONE closing sentence about what is still "
            "being studied, still cited."
        )

    # --- deterministic parsing ------------------------------------------------

    @staticmethod
    def _parse(raw_text: str, valid_sids: set[str]) -> SynthesisResult:
        """
        Deterministically enforce the citation contract on the raw LLM
        text (no LLM in the loop here).

        * stripped text == INSUFFICIENT_EVIDENCE exactly -> abstention.
        * per sentence (split after . ! ? plus whitespace; a trailing
          fragment without terminal punctuation is a final sentence):
            - a [GAP] tag present -> unanswered_aspects (not a claim: no
              citation required, never sent to the Verifier). A sentence
              carrying BOTH [GAP] and an [S#] tag (malformed/mixed
              output) is also treated as a gap statement -- the prompt
              instructs the model never to mix them, so this is a
              defensive fallback, not the expected path;
            - no [S#] tag (and no [GAP]) -> ParseDeletion "uncited claim";
            - any [S#] tag not in valid_sids (sids of the whole evidence
              input, case-sensitive "S1".."Sn") -> ParseDeletion
              "citation to unknown evidence";
            - otherwise -> Sentence (tags stripped from the text,
              citations deduped in order, contiguous renumbered index).
        * INSUFFICIENT_EVIDENCE appearing mid-prose is ordinary text:
          untagged it is just another uncited claim, tagged it is kept.
          Only the exact standalone token triggers abstention.
        * Sentences empty after tag stripping (tag-only fragments) are
          skipped silently -- there is no claim to show or verify.
        """
        text = (raw_text or "").strip()
        if text == ABSTENTION_TOKEN:
            return SynthesisResult(
                raw_text=raw_text,
                sentences=[],
                abstained=True,
                parse_deletions=[],
                unanswered_aspects=[],
            )

        sentences: list[Sentence] = []
        deletions: list[ParseDeletion] = []
        unanswered_aspects: list[str] = []

        for piece in _SENTENCE_SPLIT_RE.split(text):
            sentence = piece.strip()
            if not sentence:
                continue  # empty fragment: nothing to keep or report

            if _GAP_TAG_RE.search(sentence):
                gap_text = _GAP_STRIP_RE.sub("", sentence)
                gap_text = _TAG_STRIP_RE.sub("", gap_text).strip()  # strip any stray [S#] too
                gap_text = _TRAILING_PUNCT_RE.sub("", gap_text).strip()
                if gap_text:
                    unanswered_aspects.append(gap_text)
                continue  # never a claim: no citation required, no Verifier

            tag_groups = _CITATION_GROUP_RE.findall(sentence)
            tags = [tid for group in tag_groups for tid in _CITATION_ID_RE.findall(group)]
            display = _TAG_STRIP_RE.sub("", sentence).strip()
            display = _TRAILING_PUNCT_RE.sub("", display).strip()

            if not tags:
                print(
                    f"[synthesizer] deleted uncited sentence: "
                    f"{sentence[:80]}"
                )
                deletions.append(
                    ParseDeletion(text=sentence, reason="uncited claim")
                )
                continue
            if not display:
                continue  # tag-only fragment: no claim text to keep
            unknown = [tag for tag in tags if tag not in valid_sids]
            if unknown:
                print(
                    f"[synthesizer] deleted sentence citing unknown "
                    f"evidence {unknown}: {sentence[:80]}"
                )
                deletions.append(
                    ParseDeletion(
                        text=sentence,
                        reason="citation to unknown evidence",
                    )
                )
                continue

            sentences.append(
                Sentence(
                    index=len(sentences),
                    text=display,
                    citations=list(dict.fromkeys(tags)),  # dedupe, in order
                )
            )

        return SynthesisResult(
            raw_text=raw_text,
            sentences=sentences,
            abstained=False,
            parse_deletions=deletions,
            unanswered_aspects=unanswered_aspects,
        )
