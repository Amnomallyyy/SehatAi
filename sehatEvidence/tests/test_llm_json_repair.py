"""Regression tests for LLMClient._repair_json -- the near-JSON shapes the
live Nemotron verifier actually returned (see core/llm.py complete_json).
Runs under pytest or as a plain script: python -m tests.test_llm_json_repair
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm import LLMClient


def _repaired(text):
    out = LLMClient._repair_json(text)
    assert out is not None, text
    return json.loads(out)


def test_bare_value_on_its_own_line():
    raw = '{\n  "verdict": "SUPPORTS",\n  "confidence": 0.95,\n  "reason": The evidence says "x" plainly.\n}'
    assert _repaired(raw) == {"verdict": "SUPPORTS", "confidence": 0.95, "reason": 'The evidence says "x" plainly.'}


def test_bare_value_inline_last_field():
    raw = '{"verdict": "SUPPORTS", "confidence": 0.95, "evidence_quote": "35 RCTs", "reason": The evidence directly states X.}'
    assert _repaired(raw)["reason"] == "The evidence directly states X."


def test_bare_value_inline_middle_field_keeps_following_keys():
    raw = '{"verdict": "SUPPORTS", "reason": It says X, notably, "confidence": 0.9}'
    assert _repaired(raw) == {"verdict": "SUPPORTS", "reason": "It says X, notably", "confidence": 0.9}


def test_valid_values_are_never_requoted():
    raw = '{"a": -1.5, "b": [1, 2,], "c": {"d": false}, "e": null,}'
    assert _repaired(raw) == {"a": -1.5, "b": [1, 2], "c": {"d": False}, "e": None}


def test_prose_around_json_is_trimmed():
    assert _repaired('Sure! Here it is: {"ok": true} Hope that helps.') == {"ok": True}


def test_nothing_json_shaped_returns_none():
    assert LLMClient._repair_json("no json here") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("All JSON-repair tests passed.")
