"""Keeps the offline suite hermetic.

config.py's load_dotenv() walks up from this directory and can pick up the
repo-root .env, whose GROQ/AIONLABS/GEMINI keys build_llm_clients() appends
as backup providers -- which makes client-count assertions depend on
whatever keys the developer happens to have locally. Clearing those
variables per test pins the suite to the Settings each test constructs.
"""
import pytest

from config import _BACKUP_LLM_PROVIDERS


@pytest.fixture(autouse=True)
def _no_backup_llm_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    for _name, key_env, _base_url, model_env, _default_model in _BACKUP_LLM_PROVIDERS:
        monkeypatch.delenv(key_env, raising=False)
        monkeypatch.delenv(model_env, raising=False)
