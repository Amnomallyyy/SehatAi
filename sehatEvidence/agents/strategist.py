"""
agents/strategist.py -- Query Strategist, first stage of the EvidenceBoard
pipeline.

Turns a physician's plain clinical question into database-ready search
queries for the retrieval layer. Retrieval is a FIXED, DETERMINISTIC fan-out
that is deliberately NOT agent-controlled (see retrieval/retrieve.py's
architecture note: the Verifier's frozen-pool citation check, benchmark
reproducibility and bounded API cost all depend on the evidence pool being
predictable) -- so all search breadth is created up front HERE, through query
diversity, never through extra retrieval calls.

Query COUNT is not fixed. A first LLM call proposes as many queries as it
judges the question actually needs; a second, independent LLM call critiques
that proposal for coverage (missing facets or redundant near-duplicates); if
the critic flags a problem, the proposer revises and the critic reviews
again. This proposer<->critic loop is deliberately UNBOUNDED by design (a
prior fixed-k=3 design under-covered dense, multi-part clinical questions,
which is what this replaced) -- the two models keep talking until the critic
is satisfied, rather than the pipeline imposing any round limit or query
count. Every external call in the loop still fails open: an unreachable or
malformed response at any point is treated as "accept the last good draft
as-is," never as a reason to raise.

Query contract (PubMed / Europe PMC / ClinicalTrials.gov keyword-phrase best
practice): plain MeSH-friendly phrases of 4-12 words, no boolean operators,
no [MeSH]-style field tags, no quotes, no truncation wildcards. Whatever the
LLM returns is validated defensively against that same contract; if the LLM
is unreachable at all (not merely dissatisfied), a deterministic keyword
heuristic fills in instead, so plan_queries() fail-opens and never raises.
"""

from __future__ import annotations

import re
import string
from typing import Callable, Optional

from core.llm import LLMClient

_MIN_WORDS = 4
_MAX_WORDS = 12

# Boolean operators as WHOLE words only ("and" inside "androgen" is fine).
_BOOLEAN_RE = re.compile(r"\b(?:and|or|not)\b", re.IGNORECASE)
# Any bracket risks being parsed as a database field tag, e.g. "aspirin[MeSH]".
_FIELD_TAG_RE = re.compile(r"[\[\]]")

_PROPOSER_SYSTEM_PROMPT = (
    "You write literature-search queries for PubMed, Europe PMC and "
    "ClinicalTrials.gov. You will receive a clinical question from a "
    "physician. Decide for yourself how many distinct queries are needed "
    "to cover every clinically relevant facet of the question -- there is "
    "no fixed number: a simple question may need only one or two queries; "
    "a dense, multi-part question (e.g. one asking about mechanism, "
    "treatment AND a prognostic biomarker) needs one query per distinct "
    "facet. Typical facets when they apply: (1) the core "
    "intervention-outcome question, (2) a broader condition query "
    "capturing reviews/guidelines, (3) a harms/adverse-events or "
    "population variant, (4) any other named mechanism, biomarker or "
    "sub-question the physician explicitly asked about. Each query: plain "
    "keywords, MeSH-friendly phrasing (e.g. 'semaglutide obesity "
    "cardiovascular outcomes'), NO boolean operators (AND/OR/NOT), no "
    "field tags like [MeSH], no quotes, no truncation wildcards, 4-12 "
    "words. Respond ONLY with JSON: {\"queries\": [\"...\", ...]}."
)

_CRITIC_SYSTEM_PROMPT = (
    "You are a second, independent reviewer auditing a literature-search "
    "query plan before it is run against PubMed, Europe PMC and "
    "ClinicalTrials.gov. You will receive the physician's original "
    "clinical question and the queries another AI proposed for it. Judge "
    "COVERAGE, not wording: would running exactly these queries retrieve "
    "literature sufficient to answer every distinct clinically relevant "
    "part of the question? Flag it as insufficient if a material facet of "
    "the question (a named mechanism, a named outcome, a harms angle, a "
    "distinct sub-question) has no query covering it. Also flag it as "
    "insufficient the other direction if two or more queries are "
    "near-duplicates covering the same ground -- more queries is not "
    "automatically better. Respond ONLY with JSON: {\"sufficient\": "
    "true|false, \"feedback\": \"<if false: one or two sentences on "
    "exactly what to add, drop or merge; if true: empty string>\"}."
)

# Stop words stripped before heuristic term extraction. The boolean words
# (and/or/not) are included so heuristic queries can never re-introduce them.
_STOP_WORDS = frozenset(
    "is are was were do does what how why which best for in on of to with "
    "a an the and or not should can".split()
)

# Padding words that lift short heuristic templates to the 4-word floor.
# Ordered so +2 padding reads "clinical study" and +1 padding reads
# "evidence"; the tail of the chain covers dedup collisions (a query that
# already contains one of the padding words).
_PADDING_CHAIN = ("clinical", "study", "evidence", "trial", "review", "patients")


def _notify(on_round: Optional[Callable[[int, list[str]], None]], round_index: int, queries: list[str]) -> None:
    """Fire ``on_round(round_index, queries)`` if present; a broken callback
    must never break query planning (same fail-open rule as pipeline._emit
    and the Appraiser/Verifier's own on_progress callbacks)."""
    if on_round is None:
        return
    try:
        on_round(round_index, queries)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
        print(f"[strategist] on_round callback failed ({exc}); ignoring")


def _clean_and_validate_queries(raw: list) -> list[str]:
    """
    Shared query validator, used by BOTH the LLM path and the heuristic path.

    complete_json() may return ANY JSON, so every entry is checked against
    the same contract the system prompt states: a non-empty string, no
    boolean operators as whole words, no bracketed field tags, and 4-12
    whitespace-separated words. Duplicates are dropped case-insensitively,
    first occurrence wins.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        query = entry.strip()
        if not query:
            continue
        if _BOOLEAN_RE.search(query):
            continue
        if _FIELD_TAG_RE.search(query):
            continue
        if not _MIN_WORDS <= len(query.split()) <= _MAX_WORDS:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(query)
    return cleaned


def _significant_terms(question: str) -> list[str]:
    """
    Deterministic keyword extraction: lowercase, strip trailing punctuation,
    drop stop words and pure numbers, dedupe preserving first-occurrence
    order, cap at 6 terms.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for token in question.lower().split():
        word = token.rstrip(string.punctuation)
        if not word or word in _STOP_WORDS or word.isdigit():
            continue
        if word in seen:
            continue
        seen.add(word)
        terms.append(word)
    return terms[:6]


def _pad_to_minimum_words(query: str) -> str:
    """
    Pad a short heuristic template query with generic clinical words to reach
    the 4-word floor, e.g. "aspirin guideline" -> "aspirin guideline clinical
    study" and "metformin safe guideline" -> "metformin safe guideline
    evidence". Words already present in the query are skipped so padding
    never duplicates content.
    """
    words = query.split()
    if len(words) >= _MIN_WORDS:
        return query
    if _MIN_WORDS - len(words) == 1:
        # One word short: append "evidence" first (spec example), then the
        # rest of the chain in case "evidence" is already present.
        chain = ("evidence",) + tuple(w for w in _PADDING_CHAIN if w != "evidence")
    else:
        # Two or three words short: "clinical study" (+2) / "clinical study
        # evidence" (+3), then the rest of the chain.
        chain = _PADDING_CHAIN
    for pad in chain:
        if len(words) >= _MIN_WORDS:
            break
        if pad not in words:
            words.append(pad)
    return " ".join(words)


def _heuristic_queries(question: str) -> list[str]:
    """
    Deterministic fallback queries, used ONLY when the LLM is completely
    unreachable (a critic can't review a proposal that was never made).
    Emits every one of its 5 template queries that survives validation,
    built from the question's significant terms and padded to the word
    floor -- there is no count cap here beyond "how many templates exist",
    matching the no-fixed-count design of the LLM path.

    Templates cover the same ground the proposer's system prompt asks the
    LLM to cover: a synthesis query (systematic review), a trial query, a
    harms query, a guideline query, and a bare keyword phrase.
    """
    terms = _significant_terms(question)
    if not terms:
        # Nothing usable in the question at all (all stop words / numbers).
        # Hand retrieval one generic query rather than an empty plan.
        # Deliberately exempt from the 4-word floor: with zero known terms
        # there is nothing meaningful to pad with, and a 3-word generic
        # query beats no plan at all.
        return ["clinical evidence review"]

    if len(terms) >= 4:
        first = " ".join(terms[:4]) + " systematic review"
    else:
        first = " ".join(terms) + " review"
    templates = [
        first,
        " ".join(terms[:3]) + " randomized trial",
        " ".join(terms[:3]) + " adverse effects",
        " ".join(terms[:2]) + " guideline",
        " ".join(terms[:5]),
    ]
    # Pad short templates to the 4-word floor, then apply the SAME validation
    # as LLM queries (this also dedupes and drops anything still invalid).
    return _clean_and_validate_queries(
        [_pad_to_minimum_words(t) for t in templates]
    )


class Strategist:
    """
    First pipeline stage: clinical question in, database-ready queries out.

    Query count is decided by the models, not the pipeline: a proposer call
    drafts as many queries as it judges the question needs, then a critic
    call reviews that draft for coverage. If the critic finds a gap or
    redundancy, the proposer revises and the critic reviews again -- this
    loop is UNBOUNDED by design (see the module docstring). plan_queries()
    still fail-opens and never raises: an unreachable proposer degrades to
    the deterministic heuristic, and an unreachable critic (at any point in
    the loop) means the last proposed draft is accepted as-is.
    """

    def __init__(self, llm: Optional[LLMClient] = None):
        """
        llm: LLMClient or anything with a compatible complete_json() (the
        pipeline injects its FailoverLLMClient this way). When None, a plain
        LLMClient is constructed here -- its constructor does no network I/O;
        if the endpoint is later unreachable, plan_queries() automatically
        degrades to the heuristic.
        """
        self.llm = llm or LLMClient()

    def _propose(self, prompt: str) -> Optional[list[str]]:
        """One LLM call in the proposer role (initial draft or a revision
        after critic feedback -- same role either way). Returns validated
        queries, or None if the call failed or the response was unusable."""
        try:
            response = self.llm.complete_json(
                prompt, system=_PROPOSER_SYSTEM_PROMPT, temperature=0.1
            )
        except Exception as exc:  # LLMError and anything else: fail open
            print(f"[strategist] query proposal failed ({exc})")
            return None
        if not isinstance(response, dict) or not isinstance(
            response.get("queries"), list
        ):
            print(
                f"[strategist] proposal response had unexpected shape "
                f"({type(response).__name__})"
            )
            return None
        valid = _clean_and_validate_queries(response["queries"])
        return valid or None

    def _critique(self, question: str, queries: list[str]) -> Optional[dict]:
        """One LLM call in the critic role. Returns {"sufficient": bool,
        "feedback": str}, or None if the call failed or the response was
        unusable -- callers treat None as "no further review possible,
        accept the draft as-is", never as a reason to retry the critic."""
        prompt = (
            f'Clinical question: "{question}"\n\n'
            "Proposed queries:\n" + "\n".join(f"- {q}" for q in queries)
        )
        try:
            response = self.llm.complete_json(
                prompt, system=_CRITIC_SYSTEM_PROMPT, temperature=0.1
            )
        except Exception as exc:
            print(f"[strategist] critic unavailable ({exc}); accepting proposal as-is")
            return None
        if not isinstance(response, dict) or not isinstance(
            response.get("sufficient"), bool
        ):
            print(
                f"[strategist] critic response had unexpected shape "
                f"({type(response).__name__}); accepting proposal as-is"
            )
            return None
        return {
            "sufficient": response["sufficient"],
            "feedback": str(response.get("feedback") or ""),
        }

    def plan_queries(
        self,
        question: str,
        on_round: Optional[Callable[[int, list[str]], None]] = None,
    ) -> list[str]:
        """
        Turn a plain clinical question into database-ready queries, with
        the model(s) -- not this function -- deciding how many.

        Behavior: blank/whitespace question -> [] with no LLM call.
        Otherwise: one proposer call drafts the queries; if the proposer is
        unreachable or returns nothing usable, the deterministic heuristic
        fills in (no critique -- there is nothing to critique without a
        reachable model) and that heuristic list is returned. Otherwise the
        critic reviews the draft; the proposer<->critic loop continues
        until the critic reports the draft sufficient, the critic itself
        becomes unavailable (draft accepted as-is), or a revision comes
        back empty/unreachable (the last known-good draft is kept).

        on_round, when given, is called as (round_index, queries) (1-based)
        after each proposer draft resolves -- BEFORE that draft is sent to
        the critic. The loop is still unbounded by design (see the module
        docstring); this exists only so a caller (pipeline.py's on_event)
        can stream real mid-stage progress instead of the UI sitting on
        "start" for however many rounds a dense question needs -- the same
        gap the Appraiser/Verifier's own on_progress hooks close for their
        loops. A broken callback never breaks query planning.
        """
        question = (question or "").strip()
        if not question:
            return []

        queries = self._propose(f'Clinical question: "{question}"')
        if queries is None:
            print("[strategist] proposer unusable; using heuristic queries")
            return _heuristic_queries(question)

        round_index = 1
        _notify(on_round, round_index, queries)

        while True:
            verdict = self._critique(question, queries)
            if verdict is None or verdict["sufficient"]:
                return queries

            print(f"[strategist] critic flagged the draft: {verdict['feedback']}")
            revised = self._propose(
                f'Clinical question: "{question}"\n\n'
                "Your previous queries:\n"
                + "\n".join(f"- {q}" for q in queries)
                + f"\n\nA reviewer found this insufficient: {verdict['feedback']}\n"
                "Propose a revised, complete query list that addresses this feedback."
            )
            if revised is None:
                print("[strategist] revision unavailable; keeping the last accepted queries")
                return queries
            queries = revised
            round_index += 1
            _notify(on_round, round_index, queries)
