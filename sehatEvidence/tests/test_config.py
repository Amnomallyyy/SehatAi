"""Offline test for config.py -- no network required.

Run from the project root (sehatEvidence/):
    python -m tests.test_config
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    DISCLAIMER,
    FailoverLLMClient,
    Settings,
    build_llm_clients,
    get_settings,
)
from core.llm import LLMError

# Every environment variable Settings.from_env() reads. Tests scrub these
# and restore them afterwards, so results hold no matter what the
# developer's real .env (or shell) happens to set.
_ENV_VARS = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEYS",
    "LLM_API_KEY",
    "NCBI_TOOL_NAME",
    "NCBI_EMAIL",
    "NCBI_API_KEY",
    "POOL_CAP",
    "ENABLE_SUPERSESSION",
    "ENABLE_CITATION_REPAIR",
    "LLM_TIMEOUT",
    "LLM_MAX_TOKENS",
    "SERVER_HOST",
    "SERVER_PORT",
)


class _EnvPatch:
    """Temporarily replace the EvidenceBoard env vars (try/finally safe).

    All listed vars are removed first (so the developer's real .env cannot
    leak into the test), then ``values`` are applied. The previous state is
    restored on exit -- the process environment is never permanently
    mutated, even when the body raises.
    """

    def __init__(self, **values):
        self._values = values
        self._saved = {}

    def __enter__(self):
        for var in _ENV_VARS:
            self._saved[var] = os.environ.get(var)
            os.environ.pop(var, None)
        for var, value in self._values.items():
            os.environ[var] = value
        return self

    def __exit__(self, *exc_info):
        for var, value in self._saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        return False


# --- fake LLM clients (no network, no real LLMClient) ---------------------------


class FakeFail:
    """Simulates a dead key: every call raises LLMError; counts calls."""

    def __init__(self, message="boom"):
        self.message = message
        self.calls = 0

    def complete(self, prompt, system=None, temperature=0.2):
        self.calls += 1
        raise LLMError(self.message)

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        raise LLMError(self.message)


class FakeOk:
    """Simulates a healthy key; counts calls."""

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, system=None, temperature=0.2):
        self.calls += 1
        return "ok"

    def complete_json(self, prompt, system=None, temperature=0.1):
        self.calls += 1
        return {"ok": True}


class FakeBroken:
    """Raises a non-LLMError (programming error) -- must propagate."""

    def complete(self, prompt, system=None, temperature=0.2):
        raise TypeError("programming error")

    def complete_json(self, prompt, system=None, temperature=0.1):
        raise TypeError("programming error")


# --- test cases -------------------------------------------------------------------


def t01_defaults():
    with _EnvPatch():  # scrubbed environment -> documented defaults
        s = Settings.from_env()
    assert s.llm_base_url == "https://integrate.api.nvidia.com/v1", s.llm_base_url
    assert s.llm_model == "meta/llama-3.3-70b-instruct", s.llm_model
    assert s.llm_api_keys == [], f"no keys configured -> []: {s.llm_api_keys!r}"
    assert s.has_llm_keys is False, "has_llm_keys must be False without keys"
    assert s.pool_cap == 50, f"POOL_CAP default must be 50: {s.pool_cap}"
    assert s.enable_supersession is True, s.enable_supersession
    assert s.enable_citation_repair is True, s.enable_citation_repair
    assert s.llm_timeout == 300, f"LLM_TIMEOUT default must be 300: {s.llm_timeout}"
    assert s.llm_max_tokens == 16000, f"LLM_MAX_TOKENS default must be 16000: {s.llm_max_tokens}"
    assert s.server_host == "127.0.0.1", s.server_host
    assert s.server_port == 8000, f"SERVER_PORT default must be 8000: {s.server_port}"
    assert s.ncbi_tool_name is None and s.ncbi_email is None and s.ncbi_api_key is None
    print("PASS 01: from_env() defaults (NIM URL, llama-3.3-70b, no keys, 50/300/16000/True, 127.0.0.1:8000)")


def t02_key_parsing():
    with _EnvPatch(LLM_API_KEYS="k1, k2,, k3"):  # note the junk empty entries
        s = Settings.from_env()
    assert s.llm_api_keys == ["k1", "k2", "k3"], (
        f"empty entries must be dropped after trimming: {s.llm_api_keys!r}"
    )

    with _EnvPatch(LLM_API_KEY="single"):
        s = Settings.from_env()
    assert s.llm_api_keys == ["single"], f"single key: {s.llm_api_keys!r}"
    assert s.has_llm_keys is True

    with _EnvPatch(LLM_API_KEY="single", LLM_API_KEYS="multi1,multi2"):
        s = Settings.from_env()
    assert s.llm_api_keys == ["multi1", "multi2"], (
        f"LLM_API_KEYS must take precedence over LLM_API_KEY: {s.llm_api_keys!r}"
    )

    with _EnvPatch(LLM_API_KEYS=" , ,"):  # only junk entries -> no keys at all
        s = Settings.from_env()
    assert s.llm_api_keys == [] and s.has_llm_keys is False, s.llm_api_keys

    with _EnvPatch(LLM_API_KEYS=" , ,", LLM_API_KEY="fallback"):
        s = Settings.from_env()
    assert s.llm_api_keys == ["fallback"], (
        f"all-junk LLM_API_KEYS must fall back to LLM_API_KEY: {s.llm_api_keys!r}"
    )
    print("PASS 02: key parsing -- junk dropped, single key, LLM_API_KEYS precedence, junk fallback")


def t03_build_llm_clients():
    settings = Settings(
        llm_base_url="https://integrate.api.nvidia.com/v1/",
        llm_model="meta/llama-3.3-70b-instruct",
        llm_api_keys=["nvapi-fake-1", "nvapi-fake-2"],
        llm_timeout=42,
        llm_max_tokens=2048,
    )
    clients = build_llm_clients(settings)
    assert len(clients) == 2, f"expected one client per key, got {len(clients)}"
    for i, client in enumerate(clients, start=1):
        assert client.base_url == settings.llm_base_url.rstrip("/"), (
            f"base_url must be shared (trailing slash normalized): {client.base_url}"
        )
        assert client.model == settings.llm_model, client.model
        assert client.api_key == f"nvapi-fake-{i}", "each client must carry its own key"
        assert client.timeout == 42, client.timeout
        assert client.max_tokens == 2048, client.max_tokens
    assert build_llm_clients(Settings(llm_api_keys=[])) == [], (
        "empty keys must yield []"
    )
    print("PASS 03: build_llm_clients -- one client per key, shared url/model/timeout/max_tokens; [] when keyless")


def t04_failover_rotation():
    # [fail, ok]: the first call rotates off the dead key and succeeds.
    fail, ok = FakeFail("boom"), FakeOk()
    fo = FailoverLLMClient(clients=[fail, ok])
    assert fo.client_count == 2 and fo.active_index == 0, "must start on client 0"
    result = fo.complete_json("question")
    assert result == {"ok": True}, result
    assert fail.calls == 1, f"dead key probed exactly once, got {fail.calls}"
    assert ok.calls == 1, ok.calls
    assert fo.active_index == 1, f"rotated onto the working key: {fo.active_index}"

    # stickiness: the second call goes straight to the working key.
    result2 = fo.complete_json("question 2")
    assert result2 == {"ok": True}, result2
    assert fail.calls == 1, f"rotation must be sticky (fail re-probed {fail.calls}x)"
    assert ok.calls == 2, ok.calls
    assert fo.active_index == 1, "active client must stay on the working key"

    # [fail, fail]: every key tried once, then one summarizing LLMError.
    fail1, fail2 = FakeFail("boom-1"), FakeFail("boom-2")
    fo_all = FailoverLLMClient(clients=[fail1, fail2])
    try:
        fo_all.complete_json("question")
    except LLMError as exc:
        assert "All 2 LLM keys failed" in str(exc), str(exc)
        assert "boom-2" in str(exc), f"last error must be reported: {exc}"
    else:
        raise AssertionError("all-keys-dead must raise LLMError")
    assert fail1.calls == 1 and fail2.calls == 1, "each key tried exactly once"

    # complete() rotates identically.
    fail3, ok3 = FakeFail("boom"), FakeOk()
    fo_c = FailoverLLMClient(clients=[fail3, ok3])
    assert fo_c.complete("question") == "ok"
    assert fail3.calls == 1 and ok3.calls == 1, "complete() must rotate like complete_json()"
    assert fo_c.active_index == 1
    assert fo_c.complete("question 2") == "ok"
    assert fail3.calls == 1, "complete() rotation must be sticky too"

    # constructor: no clients / no keys -> ValueError.
    try:
        FailoverLLMClient(clients=[])
    except ValueError as exc:
        assert "at least one" in str(exc), str(exc)
    else:
        raise AssertionError("empty clients list must raise ValueError")
    try:
        FailoverLLMClient(settings=Settings(llm_api_keys=[]))
    except ValueError as exc:
        assert "at least one" in str(exc), str(exc)
    else:
        raise AssertionError("no clients and no keys must raise ValueError")

    # constructor: the settings path builds real (offline) clients from keys.
    fo_built = FailoverLLMClient(
        settings=Settings(
            llm_api_keys=["nvapi-fake-1", "nvapi-fake-2"],
            llm_base_url="https://integrate.api.nvidia.com/v1",
            llm_model="meta/llama-3.3-70b-instruct",
        )
    )
    assert fo_built.client_count == 2 and fo_built.active_index == 0

    # non-LLMError propagates without rotation.
    broken, ok4 = FakeBroken(), FakeOk()
    fo_t = FailoverLLMClient(clients=[broken, ok4])
    try:
        fo_t.complete("question")
    except TypeError:
        pass  # expected: programming errors are not key failures
    else:
        raise AssertionError("TypeError must propagate untouched")
    assert ok4.calls == 0, "no rotation on non-LLMError"
    assert fo_t.active_index == 0, "active client unchanged on non-LLMError"

    print("PASS 04: failover -- rotation, stickiness, all-fail summary, ValueError, no-rotate on TypeError")


def t05_disclaimer():
    assert isinstance(DISCLAIMER, str) and DISCLAIMER.strip(), (
        "DISCLAIMER must be non-empty text"
    )
    assert "not a medical device" in DISCLAIMER, DISCLAIMER
    print("PASS 05: DISCLAIMER is non-empty text and states 'not a medical device'")


def t06_settings_cache():
    first = get_settings()
    second = get_settings()
    assert first is second, "get_settings() must return the cached singleton"
    # direct construction stays independent of the cache
    ad_hoc = Settings(pool_cap=7)
    assert ad_hoc is not first and ad_hoc.pool_cap == 7
    print("PASS 06: get_settings() caches one instance; direct Settings() stays independent")


def t07_robust_parsing():
    # junk int values fall back to the documented defaults
    with _EnvPatch(POOL_CAP="junk", LLM_TIMEOUT="junk", SERVER_PORT="junk"):
        s = Settings.from_env()
    assert s.pool_cap == 50, f"junk POOL_CAP must fall back to 50: {s.pool_cap}"
    assert s.llm_timeout == 300, s.llm_timeout
    assert s.server_port == 8000, s.server_port

    # boolean flag words, case-insensitive
    for raw, expected in (("FALSE", False), ("no", False), ("0", False),
                          ("YES", True), ("1", True), ("On", True)):
        with _EnvPatch(ENABLE_SUPERSESSION=raw):
            got = Settings.from_env().enable_supersession
        assert got is expected, f"ENABLE_SUPERSESSION={raw!r}: expected {expected}, got {got}"

    # well-formed values are honored, surrounding whitespace trimmed
    with _EnvPatch(POOL_CAP="12", SERVER_HOST="0.0.0.0", SERVER_PORT="9000",
                   LLM_TIMEOUT="120", LLM_BASE_URL=" http://localhost:11434/v1 "):
        s = Settings.from_env()
    assert s.pool_cap == 12 and s.server_host == "0.0.0.0", s
    assert s.server_port == 9000 and s.llm_timeout == 120, s
    assert s.llm_base_url == "http://localhost:11434/v1", (
        f"whitespace must be trimmed: {s.llm_base_url!r}"
    )
    print("PASS 07: junk ints fall back; bool words honored; values trimmed")


def run():
    t01_defaults()
    t02_key_parsing()
    t03_build_llm_clients()
    t04_failover_rotation()
    t05_disclaimer()
    t06_settings_cache()
    t07_robust_parsing()
    print("\nAll config tests passed.")


if __name__ == "__main__":
    run()
