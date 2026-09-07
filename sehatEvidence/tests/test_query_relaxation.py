"""
tests/test_query_relaxation.py -- offline, pure string logic, no network.

Run: python -m tests.test_query_relaxation

Fixtures use the REAL `querytranslation` strings captured live against
NCBI's esearch API on 2026-08-29 (apixaban/warfarin/CKD-stage-3b/atrial
fibrillation query) -- not synthetic examples -- so these tests prove the
mechanism against the actual payload shape that motivated it, including a
nested AND inside a parenthesized OR-alternative that a naive string split
would mishandle.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.query_relaxation import Concept, drop_order, rebuild, split_translation

# Real, live-captured translation (apixaban warfarin stroke prevention CKD
# stage 3b atrial fibrillation) -- 8 top-level concepts, including a nested
# AND at "(\"prevention\"[All Fields] AND \"control\"[All Fields])" inside
# the "prevent" OR-group, and two bare unmapped singletons ("CKD", "3b").
REAL_TRANSLATION = (
    '("apixaban"[Supplementary Concept] OR "apixaban"[All Fields] OR "apixaban s"[All Fields]) '
    'AND ("warfarin"[Supplementary Concept] OR "warfarin"[All Fields] OR "warfarin"[MeSH Terms] '
    'OR "warfarin s"[All Fields] OR "warfarinization"[All Fields] OR "warfarinized"[All Fields] '
    'OR "warfarins"[All Fields]) '
    'AND ("stroke"[MeSH Terms] OR "stroke"[All Fields] OR "strokes"[All Fields] OR "stroke s"[All Fields]) '
    'AND ("prevent"[All Fields] OR "preventability"[All Fields] OR "preventable"[All Fields] '
    'OR "preventative"[All Fields] OR "preventatively"[All Fields] OR "preventatives"[All Fields] '
    'OR "prevented"[All Fields] OR "preventing"[All Fields] OR "prevention and control"[MeSH Subheading] '
    'OR ("prevention"[All Fields] AND "control"[All Fields]) OR "prevention and control"[All Fields] '
    'OR "prevention"[All Fields] OR "prevention s"[All Fields] OR "preventions"[All Fields] '
    'OR "preventive"[All Fields] OR "preventively"[All Fields] OR "preventives"[All Fields] '
    'OR "prevents"[All Fields]) '
    'AND "CKD"[All Fields] '
    'AND ("stage"[All Fields] OR "staged"[All Fields] OR "stages"[All Fields] OR "staging"[All Fields] '
    'OR "stagings"[All Fields]) '
    'AND "3b"[All Fields] '
    'AND ("atrial fibrillation"[MeSH Terms] OR ("atrial"[All Fields] AND "fibrillation"[All Fields]) '
    'OR "atrial fibrillation"[All Fields])'
)

# Real supersession-style translation shape (agents/verifier.py's
# _fetch_supersession_reviews wraps its base query with explicit
# filter/type-tag syntax before it ever reaches esearch).
SUPERSESSION_TRANSLATION = (
    '("metformin"[MeSH Terms] OR "metformin"[All Fields]) '
    'AND ("systematic"[sb] OR "meta-analysis"[pt]) '
    'AND ("2021"[dp] : "2026"[dp])'
)


def t01_split_top_level_and():
    concepts = split_translation(REAL_TRANSLATION)
    assert len(concepts) == 8, f"expected 8 top-level concepts, got {len(concepts)}: {[c.text[:40] for c in concepts]}"
    # The renal/CKD analog here is the bare "CKD" concept -- must be its OWN
    # single concept, never merged with a neighbor.
    assert concepts[4].text == '"CKD"[All Fields]', concepts[4].text
    assert concepts[6].text == '"3b"[All Fields]', concepts[6].text
    # The nested AND inside the "prevent" OR-group must NOT have split the
    # group into two top-level concepts.
    prevent_group = concepts[3]
    assert '"prevention"[All Fields] AND "control"[All Fields]' in prevent_group.text, (
        "the nested AND inside a parenthesized OR-alternative must survive intact"
    )
    assert prevent_group.leaf_count > 1
    print("PASS 01: 8 top-level concepts split correctly; nested AND-inside-OR-group untouched")


def t02_drop_order_identifies_3b_first():
    concepts = split_translation(REAL_TRANSLATION)
    order = drop_order(concepts)
    # index 6 = "3b" (bare, unmapped, single-leaf) must be first.
    # index 4 = "CKD" (bare, unmapped, single-leaf) must be second.
    assert order[0] == 6, f"expected '3b' (index 6) dropped first, got index {order[0]}: {concepts[order[0]].text}"
    assert order[1] == 4, f"expected 'CKD' (index 4) dropped second, got index {order[1]}: {concepts[order[1]].text}"
    # index 5 = the stage/staged/... group (unmapped, multi-leaf) must rank
    # below the two bare singletons but above any MeSH-mapped concept.
    assert order.index(5) < order.index(0), "unmapped multi-leaf group must be droppable before a real MeSH concept"
    assert order.index(5) < order.index(1)
    # No MeSH-mapped concept (0, 1, 2, 3, 7 all carry a real tag) may precede
    # BOTH unmapped tiers.
    for real_idx in (0, 1, 2, 3, 7):
        assert order.index(real_idx) > order.index(6), f"concept {real_idx} ranked before the unmapped '3b' singleton"
        assert order.index(real_idx) > order.index(4), f"concept {real_idx} ranked before the unmapped 'CKD' singleton"
    print("PASS 02: drop_order is syntactic -- unmapped bare tokens rank before every MeSH-mapped concept")


def t03_pinned_filters_never_dropped():
    concepts = split_translation(SUPERSESSION_TRANSLATION)
    assert len(concepts) == 3
    pinned_indices = {i for i, c in enumerate(concepts) if c.pinned}
    assert pinned_indices == {1, 2}, f"expected the [sb]/[pt] and [dp] groups pinned, got {pinned_indices}"
    order = drop_order(concepts)
    assert 1 not in order and 2 not in order, "a pinned filter/date/type concept must never be droppable"
    assert order == [0], order  # only the metformin concept (unpinned, but mapped) is left as a last resort
    print("PASS 03: date/type/subset filter concepts are never returned by drop_order")


def t04_rebuild_roundtrip():
    concepts = split_translation(REAL_TRANSLATION)
    # Dropping nothing reproduces the original concept set, rejoined.
    assert rebuild(concepts, set()) == REAL_TRANSLATION
    # Dropping index 6 ("3b") removes exactly that group and nothing else.
    dropped = rebuild(concepts, {6})
    assert '"3b"[All Fields]' not in dropped
    assert '"CKD"[All Fields]' in dropped
    # Re-splitting the rebuilt term must yield exactly the 7 surviving
    # concepts (not 6 or 8 -- confirms rebuild's top-level joins are exact,
    # despite the nested "AND"s inside two of the surviving OR-groups).
    assert len(split_translation(dropped)) == 7, split_translation(dropped)
    print("PASS 04: rebuild() drops exactly the requested concept, nothing else")


def t05_empty_and_no_tags_are_handled():
    assert split_translation("") == []
    assert split_translation("   ") == []
    # A bare phrase with no bracket tags at all (defensive: shouldn't occur
    # in a real esearch response, but must not crash) -- unmapped is False
    # since there's nothing to judge, so it's a tier-3 (last resort) drop.
    concepts = split_translation("just plain text")
    assert len(concepts) == 1
    assert concepts[0].unmapped is False
    print("PASS 05: empty translation -> []; untagged text doesn't crash and isn't misclassified as unmapped")


def run():
    t01_split_top_level_and()
    t02_drop_order_identifies_3b_first()
    t03_pinned_filters_never_dropped()
    t04_rebuild_roundtrip()
    t05_empty_and_no_tags_are_handled()
    print("\nAll query_relaxation tests passed.")


if __name__ == "__main__":
    run()
