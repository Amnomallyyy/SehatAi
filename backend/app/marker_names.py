"""
marker_names.py -- normalizes lab test names so the same marker reported
under different labels (e.g. "Hemoglobin" vs "Haemoglobin") groups
together for history/trend/delta purposes.

Built from the real distinct test_name values in extracted_data (30
values across 60 rows, confirmed live 2026-09-05) -- the synonym map
below is deliberately just those, not a general medical-terminology
list. Display always uses the original test_name; normalize_marker()
is only ever used as a grouping/lookup key.
"""
import re

_PUNCT_RE = re.compile(r"[.,()/]")
_LEADING_JUNK_RE = re.compile(r"^[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")

_SYNONYMS = {
    "haemoglobin": "hemoglobin",
    "fasting glucose": "fasting glucose",
    "glucose fasting": "fasting glucose",
    "total wbc count": "wbc",
    "wbc count": "wbc",
    "wbcs on ps": "wbc",
    "white blood cell count": "wbc",
    "r b c count": "rbc",
    "pcv": "hematocrit",
    "pcv hct": "hematocrit",
    "reactive protein crp": "crp",
}


def normalize_marker(name: str) -> str:
    """Casefold, strip leading junk (e.g. the stray '@' in '@MCV'),
    collapse punctuation/whitespace, then apply the synonym map.
    Unknown names pass through casefolded+collapsed rather than raising --
    a marker this doesn't recognize should still work, just ungrouped."""
    if not name:
        return ""
    n = name.strip().casefold()
    n = _LEADING_JUNK_RE.sub("", n)
    n = _PUNCT_RE.sub(" ", n)
    n = _WS_RE.sub(" ", n).strip()
    return _SYNONYMS.get(n, n)
