"""EvidenceBoard configuration.

Loads ``.env`` when present (it is never required -- environment variables
may equally come from the shell), exposes a typed :class:`Settings`
snapshot built from the environment, and provides :class:`FailoverLLMClient`
for multi-key NVIDIA NIM rotation (e.g. one key per free NIM account).

``.env`` is untracked local configuration -- it must never be committed.
The tracked ``.env.example`` template documents every supported variable
with safe placeholder values: copy it to ``.env`` and fill in your own
keys. Shell-exported variables take precedence over ``.env`` values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

# python-dotenv is optional. When installed, load_dotenv() pulls .env from
# the working directory into os.environ (a no-op if the file is missing).
# When it is absent, environment variables simply come from the shell.
# Importing this module must never fail either way.
try:
    from dotenv import load_dotenv

    load_dotenv()  # no-op if .env missing
except ImportError:
    pass  # python-dotenv is optional; env vars may come from the shell

from core.llm import LLMClient, LLMError

__all__ = [
    "DISCLAIMER",
    "FailoverLLMClient",
    "Settings",
    "build_llm_clients",
    "get_settings",
]

#: Single source of truth for the medical disclaimer. Every user-facing
#: surface (API responses, UI) must carry this exact text.
DISCLAIMER = ("EvidenceBoard is a literature search and evidence-summarization aid for "
              "healthcare professionals. It is not a medical device and does not provide "
              "medical advice, diagnosis, or treatment recommendations. Every claim must be "
              "verified against the primary source (links provided) before clinical use. "
              "Do not enter patient-identifiable information. Automated verification checks "
              "can err; the treating clinician remains responsible for clinical decisions.")

#: Case-insensitive words accepted as "true" for boolean env flags.
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


# --- env parsing helpers -------------------------------------------------------


def _env_str(name: str, default: str) -> str:
    """Read a string env var with a default.

    Missing, empty, or whitespace-only values fall back to ``default``;
    surviving values are stripped of surrounding whitespace.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_opt(name: str) -> Optional[str]:
    """Read an optional passthrough env var (absent/empty -> None)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _env_int(name: str, default: int) -> int:
    """Read an int env var; junk or missing values fall back to ``default``."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env flag; accepts 1/true/yes/on (case-insensitive)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE_WORDS


def _parse_api_keys(raw_list: Optional[str], raw_single: Optional[str]) -> "list[str]":
    """Parse LLM_API_KEYS (comma-separated) first, then LLM_API_KEY.

    Entries are trimmed and empty entries are dropped; ``LLM_API_KEYS``
    takes precedence over ``LLM_API_KEY`` when both are set. Key values
    are secrets -- this module never logs or exposes them.
    """
    if raw_list:
        keys = [k.strip() for k in raw_list.split(",") if k.strip()]
        if keys:
            return keys
    if raw_single and raw_single.strip():
        return [raw_single.strip()]
    return []


# --- Settings ------------------------------------------------------------------


@dataclass
class Settings:
    """Typed configuration snapshot built from environment variables.

    Construct directly (``Settings(pool_cap=10)``) in tests or when
    overriding values; use :meth:`from_env` / :func:`get_settings` for
    the environment-driven path.
    """

    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "meta/llama-3.3-70b-instruct"
    llm_sensitive_model: Optional[str] = None  # heavier model for entailment/NLI
    llm_api_keys: list[str] = field(default_factory=list)
    ncbi_tool_name: Optional[str] = None
    ncbi_email: Optional[str] = None
    ncbi_api_key: Optional[str] = None
    pool_cap: int = 30
    enable_supersession: bool = True
    llm_timeout: int = 60
    server_host: str = "127.0.0.1"
    server_port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        """Build a Settings snapshot from the current environment."""
        return cls(
            llm_base_url=_env_str("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            llm_model=_env_str("LLM_MODEL", "meta/llama-3.3-70b-instruct"),
            llm_sensitive_model=_env_opt("LLM_SENSITIVE_MODEL"),
            llm_api_keys=_parse_api_keys(
                os.getenv("LLM_API_KEYS"), os.getenv("LLM_API_KEY")
            ),
            ncbi_tool_name=_env_opt("NCBI_TOOL_NAME"),
            ncbi_email=_env_opt("NCBI_EMAIL"),
            ncbi_api_key=_env_opt("NCBI_API_KEY"),
            pool_cap=_env_int("POOL_CAP", 30),
            enable_supersession=_env_bool("ENABLE_SUPERSESSION", True),
            llm_timeout=_env_int("LLM_TIMEOUT", 60),
            server_host=_env_str("SERVER_HOST", "127.0.0.1"),
            server_port=_env_int("SERVER_PORT", 8000),
        )

    @property
    def has_llm_keys(self) -> bool:
        """True when at least one LLM API key is configured."""
        return bool(self.llm_api_keys)


#: Module-level singleton cache for get_settings(); built once, never rebuilt.
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return the process-wide Settings singleton (built on first call).

    Tests that need isolated values should construct ``Settings(...)``
    directly rather than mutating this cache.
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def build_llm_clients(settings: Settings, model_override: Optional[str] = None) -> "list[LLMClient]":
    """Build one LLMClient per configured API key.

    All clients share the settings' base_url/model/timeout. Returns an
    empty list when no keys are configured (the caller decides how to
    handle that). LLMClient construction performs no network I/O.

    When ``model_override`` is given, that model is used instead of
    ``settings.llm_model`` (e.g. for the sensitive/heavier model).
    """
    model = model_override or settings.llm_model
    return [
        LLMClient(
            base_url=settings.llm_base_url,
            api_key=key,
            model=model,
            time_out=settings.llm_timeout,
        )
        for key in settings.llm_api_keys
    ]


# --- failover wrapper ------------------------------------------------------------


class FailoverLLMClient:
    """Failover wrapper around multiple LLM clients (one per API key).

    Typical use: several free NVIDIA NIM accounts -> one key each -> this
    wrapper rotates to the next key whenever the active one fails with
    :class:`LLMError`. Rotation is STICKY: after a success, the active
    client stays selected for subsequent calls and only changes again on
    the next failure. Non-LLMError exceptions (programming errors)
    propagate immediately without any rotation.

    Wrapped objects only need ``complete``/``complete_json`` -- any
    duck-typed client works (tests inject fakes).
    """

    def __init__(
        self,
        clients: Optional[list] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        """Wrap ``clients``; when None, build them from ``settings``.

        When ``settings`` is also None, the global :func:`get_settings`
        snapshot is used. Raises ValueError when no client/key is
        available at all.
        """
        if clients is None:
            clients = build_llm_clients(
                settings if settings is not None else get_settings()
            )
        if not clients:
            raise ValueError("FailoverLLMClient requires at least one LLM client/key")
        self._clients: list = list(clients)
        self._active: int = 0  # index of the currently selected client
        self._rotations: int = 0  # total rotations, for diagnostics
        self._calls: int = 0  # successful complete()/complete_json() calls
        self._call_failures: int = 0  # failed attempts across all keys

    # --- diagnostics ----------------------------------------------------------

    @property
    def active_index(self) -> int:
        """Index (0-based) of the currently selected client."""
        return self._active

    @property
    def client_count(self) -> int:
        """Number of wrapped clients (== number of API keys)."""
        return len(self._clients)

    @property
    def calls(self) -> int:
        """Total successful LLM calls made through this client so far.

        The single source of truth for "is this agent actually calling the
        model": every agent shares one FailoverLLMClient (see
        build_default_pipeline), so pipeline.py snapshots this counter
        before/after each stage to report real per-agent LLM usage --
        never inferred, never simulated.
        """
        return self._calls

    @property
    def call_failures(self) -> int:
        """Total failed attempts (across all key rotations) so far."""
        return self._call_failures

    # --- LLMClient-compatible surface -------------------------------------------

    def complete(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.2
    ) -> str:
        """complete() with automatic key failover; see class docstring."""
        return self._call("complete", prompt, system=system, temperature=temperature)

    def complete_json(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.1
    ) -> Any:
        """complete_json() with automatic key failover; see class docstring."""
        return self._call(
            "complete_json", prompt, system=system, temperature=temperature
        )

    # --- internals ---------------------------------------------------------------

    def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke ``method_name`` on the active client, rotating on LLMError.

        Each wrapped client is tried at most once (``len(clients)``
        attempts in total). Raises LLMError naming the last failure when
        all keys fail. Non-LLMError exceptions propagate untouched.
        """
        n = len(self._clients)
        exc: Optional[LLMError] = None
        for _attempt in range(n):
            client = self._clients[self._active]
            try:
                result = getattr(client, method_name)(*args, **kwargs)
            except LLMError as failure:
                exc = failure
                self._call_failures += 1
                print(
                    f"[config] LLM key {self._active + 1}/{n} failed "
                    f"({exc}); rotating..."
                )
                self._rotations += 1
                self._active = (self._active + 1) % n
                continue
            self._calls += 1
            return result
        raise LLMError(f"All {n} LLM keys failed; last error: {exc}")
