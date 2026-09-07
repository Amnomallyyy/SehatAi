"""
Turns a report PDF into structured fields using an LLM. Two steps, not one:

1. Pull the plain text out of the PDF ourselves (pypdf). None of the
   providers below take PDF input directly on their free tiers; there's
   no "attach a PDF" option, so we do the PDF-reading locally before ever
   calling an API.
2. Send that extracted text to an LLM and ask for a JSON object back in a
   specific shape, via response_format={"type": "json_object"} (every
   provider below supports this over its OpenAI-compatible endpoint).

Multi-provider fallback (added after the original Groq-only key turned
out to be invalid/revoked -- single point of failure with no way to
recover except a new key): tries each CONFIGURED provider in order and
fails over to the next on ANY error, same pattern as sehatai/callAi.js.
Order is roughly fastest-first based on live testing (2026-09-04):
Groq (custom LPU silicon, fastest *when its key works*) -> AionLabs
(confirmed live: ~2.5s, clean JSON) -> Gemini Flash-Lite (normally fast,
occasionally 503s under load) -> NVIDIA NIM (confirmed live: ~7s+, free
"Prototype" tier, last resort). A provider with no key configured is
skipped entirely, never attempted.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from pypdf import PdfReader

# Reads a .env file in the project root (if one exists) into the process's
# environment. Safe to call even with no .env file present -- it just does
# nothing in that case. See .env.example for every var below.
load_dotenv()

# Free-tier request budgets are limited (see console.groq.com/docs/rate-limits),
# so very long reports get truncated before being sent -- this is plenty
# of text for a typical lab report or clinical note.
MAX_REPORT_CHARS = 12000


def _make_provider(name: str, api_key_env: str, base_url: str, model_env: str,
                    default_model: str, system_prefix: Optional[str] = None,
                    min_max_tokens: Optional[int] = None) -> list[dict]:
    # <VAR>S (plural, comma-separated) takes precedence over the singular
    # <VAR> when both are set -- same convention as sehatEvidence's
    # LLM_API_KEYS/LLM_API_KEY. Lets a provider have several free-tier
    # accounts failed over across before moving on to the next provider
    # entirely, not just the next name in _PROVIDER_FACTORIES.
    keys_raw = os.environ.get(f"{api_key_env}S") or os.environ.get(api_key_env)
    if not keys_raw:
        return []
    keys = [k.strip() for k in keys_raw.split(",") if k.strip()]
    model = os.environ.get(model_env) or default_model
    providers = []
    for i, key in enumerate(keys):
        providers.append({
            "name": name if len(keys) == 1 else f"{name}#{i + 1}",
            "client": OpenAI(api_key=key, base_url=base_url),
            "model": model,
            # NVIDIA's Nemotron models don't reliably honor json_object
            # mode -- they can write visible chain-of-thought ahead of the
            # real answer regardless. "detailed thinking off" as the first
            # system line is NVIDIA's own documented way to suppress it (no
            # separate API param). Not needed for the other providers here.
            "system_prefix": system_prefix,
            # Same NVIDIA quirk: a tight token cap truncates the response
            # before the JSON ever arrives if it does ramble first.
            "min_max_tokens": min_max_tokens,
        })
    return providers


# Reuses the exact same env vars sehatai's callAi.js already reads out of
# root .env -- no new keys needed here, just copies of ones already
# provided, kept in backend/.env since that's this process's own env file.
_PROVIDER_FACTORIES = [
    lambda: _make_provider("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1",
                            "GROQ_MODEL", "openai/gpt-oss-120b"),
    lambda: _make_provider("aionlabs", "AIONLABS_API_KEY", "https://api.aionlabs.ai/v1",
                            "AIONLABS_MODEL", "aion-labs/aion-3.0-mini"),
    lambda: _make_provider("gemini", "GEMINI_API_KEY",
                            "https://generativelanguage.googleapis.com/v1beta/openai/",
                            "GEMINI_MODEL", "gemini-3.5-flash-lite"),
    lambda: _make_provider("nvidia", "NVIDIA_API_KEY_SEC", "https://integrate.api.nvidia.com/v1",
                            "NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b",
                            system_prefix="detailed thinking off", min_max_tokens=2048),
]

# Built once at import time -- provider identity/config doesn't change at
# runtime. A provider (or key) with nothing configured is silently
# skipped, never attempted (mirrors callAi.js's PROVIDER_CHAIN). Each
# factory can now expand to several entries (one per key), so this is a
# flatten, not a filter.
_PROVIDERS = [p for factory in _PROVIDER_FACTORIES for p in factory()]


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise

_PROMPT_TEMPLATE = """You are reviewing a medical report for a doctor-patient \
communication app. Below is the extracted text of the report. Respond with \
ONLY a JSON object (no text before or after it) with exactly these fields:

{{
  "summary": "a 1-3 sentence plain-language overview of the report",
  "key_findings": ["short bullet points of the report's main results"],
  "flagged_values": ["specific results outside a normal/reference range -- empty array if none"],
  "recommendation": "a short suggested next step, or an empty string if not applicable"
}}

If the text doesn't look like a medical report, or you can't extract \
meaningful data, still return this exact JSON shape -- put your \
best-effort explanation in "summary" instead of leaving fields out.

--- REPORT TEXT ---
{report_text}
"""


def extract_pdf_text(pdf_path: Path) -> str:
    """Pulls all text out of the PDF, page by page, and joins it.

    Known limitation: this only reads a PDF's actual text layer. A
    *scanned* report (a photo/image of a page saved as PDF, with no text
    layer) will extract as empty or near-empty -- pypdf doesn't do OCR.
    generate_report_summary() below turns that into a clear error rather
    than silently sending Groq an empty report.
    """
    reader = PdfReader(str(pdf_path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def generate_report_summary(pdf_path: Path) -> dict:
    """Extracts the PDF's text, then asks an LLM to turn it into structured
    fields. Returns a dict with keys: summary (str), key_findings
    (list[str]), flagged_values (list[str]), recommendation (str).

    Tries each configured provider in _PROVIDERS in order, failing over to
    the next on any error (auth failure, rate limit, timeout, unparseable
    output) -- same fail-over-not-fail-fast pattern as callAi.js. Raises
    ValueError if no text could be extracted, or if every provider failed --
    the caller (ai_summaries.py) turns either case into a clean HTTP error
    rather than saving bad data.
    """
    report_text = extract_pdf_text(pdf_path)
    if not report_text:
        raise ValueError(
            "No text could be extracted from this PDF. It may be a scanned "
            "image with no text layer, which this doesn't OCR."
        )

    if len(report_text) > MAX_REPORT_CHARS:
        report_text = report_text[:MAX_REPORT_CHARS] + "\n\n[...truncated...]"

    if not _PROVIDERS:
        raise ValueError(
            "No AI provider is configured -- set GROQ_API_KEY and/or "
            "AIONLABS_API_KEY/GEMINI_API_KEY/NVIDIA_API_KEY_SEC in backend/.env."
        )

    prompt = _PROMPT_TEMPLATE.format(report_text=report_text)
    failures = []
    for provider in _PROVIDERS:
        messages = [{"role": "user", "content": prompt}]
        if provider["system_prefix"]:
            messages.insert(0, {"role": "system", "content": provider["system_prefix"]})
        max_tokens = 1024
        if provider["min_max_tokens"]:
            max_tokens = max(max_tokens, provider["min_max_tokens"])

        try:
            completion = provider["client"].chat.completions.create(
                model=provider["model"],
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
            )
            raw_text = completion.choices[0].message.content
            if not raw_text:
                raise ValueError("empty response content")
            data = _extract_json(raw_text)
        except Exception as exc:
            failures.append(f"{provider['name']}: {exc}")
            continue

        return {
            "summary": str(data.get("summary", "")),
            "key_findings": [str(item) for item in data.get("key_findings", [])],
            "flagged_values": [str(item) for item in data.get("flagged_values", [])],
            "recommendation": str(data.get("recommendation", "")),
        }

    raise ValueError(f"AI summary failed on every configured provider -- {' | '.join(failures)}")
