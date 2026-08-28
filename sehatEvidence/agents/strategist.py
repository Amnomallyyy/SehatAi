"""
agents/strategist.py -- Query Strategist, first stage of the EvidenceBoard
pipeline.

Turns a physician's plain clinical question into 3-5 database-ready search
queries for the retrieval layer. Retrieval is a FIXED, DETERMINISTIC fan-out
that is deliberately NOT agent-controlled (see retrieval/retrieve.py's
architecture note: the Verifier's frozen-pool citation check, benchmark
reproducibility and bounded API cost all depend on the evidence pool being
predictable) -- so all search breadth is created up front HERE, through query
diversity, never through extra retrieval calls.

Query contract (PubMed / Europe PMC / ClinicalTrials.gov keyword-phrase best
practice): plain MeSH-friendly phrases of 4-12 words, no boolean operators,
no [MeSH]-style field tags, no quotes, no truncation wildcards. Whatever the
LLM returns is validated defensively against that same contract and any
shortfall is topped up from a deterministic keyword heuristic, so
plan_queries() fail-opens and never raises.
"""

from __future__ import annotations

import re
import string
from typing import Optional

from core.llm import LLMClient

DEFAULT_K = 3

_MIN_WORDS = 4
_MAX_WORDS = 12

# Boolean operators as WHOLE words only ("and" inside "androgen" is fine).
_BOOLEAN_RE = re.compile(r"\b(?:and|or|not)\b", re.IGNORECASE)
# Any bracket risks being parsed as a database field tag, e.g. "aspirin[MeSH]".
_FIELD_TAG_RE = re.compile(r"[\[\]]")

_SYSTEM_PROMPT = (
    "You write literature-search queries for PubMed, Europe PMC and "
    "ClinicalTrials.gov. You will receive a clinical question from a "
    "physician. Produce database-ready queries: condition + "
    "intervention/exposure terms, plain keywords, MeSH-friendly phrasing "
    "(e.g. 'semaglutide obesity cardiovascular outcomes'). NO boolean "
    "operators (AND/OR/NOT), no field tags like [MeSH], no quotes, no "
    "truncation wildcards, 4-12 words each. Cover: (1) the core "
    "intervention-outcome query, (2) a broader condition query capturing "
    "reviews/guidelines, (3) a harms/adverse-events or population variant. "
    "Respond ONLY with JSON."
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


def _heuristic_queries(question: str, k: int) -> list[str]:
    """
    Deterministic fallback queries, used when the LLM is unreachable or
    returned fewer than k usable queries. Emits up to k template queries
    built from the question's significant terms, each padded to the word
    floor and run through the SAME shared validator as LLM queries.

    Templates cover the same ground the system prompt asks the LLM to cover:
    a synthesis query (systematic review), a trial query, a harms query, a
    guideline query, and a bare keyword phrase.
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
    )[:k]


class Strategist:
    """
    First pipeline stage: clinical question in, database-ready queries out.

    Makes exactly ONE LLM call per plan; the response is validated
    defensively and any shortfall is topped up from the deterministic
    heuristic. plan_queries() fail-opens: it never raises, and returns
    between 0 queries (blank question) and min(5, max(1, k)) queries.
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

    def plan_queries(self, question: str, k: int = DEFAULT_K) -> list[str]:
        """
        Turn a plain clinical question into up to k (clamped to [1, 5])
        database-ready queries.

        Behavior: blank/whitespace question -> [] with no LLM call; otherwise
        exactly one LLM call whose response is validated against the query
        contract; if fewer than k usable queries survive, deterministic
        heuristic fillers top the plan up to k -- the LLM is never re-asked.
        """
        question = (question or "").strip()
        if not question:
            return []
        k = max(1, min(5, int(k)))

        prompt = (
            f'Clinical question: "{question}"\n\n'
            f'Return a JSON object: "queries": ["...", "..."] '
            f"with exactly {k} queries (max 5)."
        )
        try:
            response = self.llm.complete_json(
                prompt, system=_SYSTEM_PROMPT, temperature=0.1
            )
        except Exception as exc:  # LLMError and anything else: fail open
            print(
                f"[strategist] LLM query planning failed ({exc}); "
                f"using heuristic queries"
            )
            return _heuristic_queries(question, k)

        if not isinstance(response, dict) or not isinstance(
            response.get("queries"), list
        ):
            print(
                f"[strategist] LLM response had unexpected shape "
                f"({type(response).__name__}); using heuristic queries"
            )
            return _heuristic_queries(question, k)

        valid = _clean_and_validate_queries(response["queries"])
        if len(valid) >= k:
            return valid[:k]

        # Fewer than k usable queries: top up deterministically. NEVER call
        # the LLM again -- one call per plan, by design.
        seen = {q.lower() for q in valid}
        for filler in _heuristic_queries(question, k):
            if len(valid) >= k:
                break
            if filler.lower() not in seen:
                seen.add(filler.lower())
                valid.append(filler)
        return valid[:k]
