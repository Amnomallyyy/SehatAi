"""
retrieval/query_relaxation.py -- pure string logic over PubMed's own
esearch `querytranslation` field, used to recover yield when NCBI's
Automatic Term Mapping (ATM) collapses a multi-concept query.

ZERO network, ZERO AI, ZERO clinical vocabulary. PubMed's esearch response
already segments a plain-keyword query into top-level `AND`-joined concept
groups (one group per input phrase/word, each an OR-list of every way ATM
mapped it -- MeSH term, supplementary concept, free-text fallback, etc.).
When ATM can't map a token to anything, that group degenerates to a single
bare `"<token>"[All Fields]` alternative -- syntactically distinguishable
from a real mapped concept without knowing anything about what the token
means. That syntactic signal, not a hardcoded list of "bad" clinical
tokens, is what this module drops first.

Confirmed against a real captured translation (2026-08-29, apixaban/CKD
query) containing a nested `AND` *inside* a parenthesized OR-alternative:
    ("prevent"[All Fields] OR ... OR ("prevention"[All Fields] AND
    "control"[All Fields]) OR ...)
A naive split on the literal substring " AND " would incorrectly treat
that nested AND as a top-level concept boundary. split_translation() only
splits at paren depth 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Tags that mark a filter/date/type restriction PubMed added on top of the
# concept mapping itself (publication date, publication type, subset,
# journal, language). A concept carrying one of these is never a candidate
# for relaxation -- dropping it would silently widen a deliberate filter
# (e.g. the Verifier's supersession date/type restriction) rather than
# recovering topical yield.
_PINNED_TAGS = {"dp", "pt", "sb", "ta", "la", "filter"}

_TAG_RE = re.compile(r"\[([^\]]+)\]")
_AND_TOKEN = " AND "
_OR_TOKEN = " OR "


@dataclass(frozen=True)
class Concept:
    text: str  # verbatim top-level group, e.g. '"3b"[All Fields]'
    pinned: bool
    unmapped: bool  # every bracket tag in this group is exactly [All Fields]
    leaf_count: int  # number of OR'd alternatives; 1 == no synonym expansion


def _paren_depth_split(s: str, token: str) -> list[str]:
    """Split `s` on `token` only where paren depth is 0 at the split point."""
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(s)
    tlen = len(token)
    while i < n:
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and s[i : i + tlen] == token:
            parts.append(s[start:i])
            i += tlen
            start = i
            continue
        i += 1
    parts.append(s[start:])
    return parts


def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        # Only strip if the outer parens actually wrap the whole string
        # (not e.g. "(a) AND (b)" collapsed to one concept by a caller bug).
        depth = 0
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    return s
        return s[1:-1]
    return s


def _make_concept(text: str) -> Concept:
    text = text.strip()
    inner = _strip_outer_parens(text)
    leaves = _paren_depth_split(inner, _OR_TOKEN)
    tags = _TAG_RE.findall(text)
    pinned = any(tag.strip().lower() in _PINNED_TAGS for tag in tags)
    unmapped = bool(tags) and all(tag.strip() == "All Fields" for tag in tags)
    return Concept(text=text, pinned=pinned, unmapped=unmapped, leaf_count=len(leaves))


def split_translation(query_translation: str) -> list[Concept]:
    """Split an esearch `querytranslation` string into its top-level
    AND-joined concepts, depth-aware (never splits inside an OR group or
    inside a nested parenthesized sub-clause)."""
    if not query_translation or not query_translation.strip():
        return []
    groups = _paren_depth_split(query_translation.strip(), _AND_TOKEN)
    return [_make_concept(g) for g in groups if g.strip()]


def drop_order(concepts: list[Concept]) -> list[int]:
    """Indices of droppable (non-pinned) concepts, most-droppable first.

    Tiers, each internally broken rightmost-first (later concepts tend to
    be qualifiers appended to an otherwise-complete phrase):
        1. unmapped, single-leaf (a bare unrecognized token -- the "3b" case)
        2. unmapped, multi-leaf (ATM found only word-stem variants, no
           real concept -- ATM still gave up, just verbosely)
        3. everything else (a real mapped concept) -- last resort only
    Pinned concepts (date/type/subset filters) never appear here.
    """
    tier1, tier2, tier3 = [], [], []
    for i, c in enumerate(concepts):
        if c.pinned:
            continue
        if c.unmapped and c.leaf_count == 1:
            tier1.append(i)
        elif c.unmapped:
            tier2.append(i)
        else:
            tier3.append(i)
    return list(reversed(tier1)) + list(reversed(tier2)) + list(reversed(tier3))


def rebuild(concepts: list[Concept], drop: set[int]) -> str:
    """Rejoin the surviving concepts' verbatim (already-ATM-translated)
    text with ' AND ' -- resubmitting already-translated syntax so the
    retry isn't re-mapped ambiguously by a second pass of ATM."""
    kept = [c.text for i, c in enumerate(concepts) if i not in drop]
    return _AND_TOKEN.join(kept)
