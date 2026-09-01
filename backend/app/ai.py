"""
Turns a report PDF into structured fields using Groq. Two steps, not one:

1. Pull the plain text out of the PDF ourselves (pypdf). Groq's chat
   models -- including everything on the free tier -- take plain text
   input only; there's no "attach a PDF" option like some other providers
   have, so we do the PDF-reading locally before ever calling the API.
2. Send that extracted text to Groq and ask for a JSON object back in a
   specific shape (Groq's JSON mode -- response_format={"type":
   "json_object"} -- guarantees syntactically valid JSON, so no fence-
   stripping workaround is needed here).
"""
import json
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from pypdf import PdfReader

# Reads a .env file in the project root (if one exists) into the process's
# environment. Safe to call even with no .env file present -- it just does
# nothing in that case. Put GROQ_API_KEY=... in that file; see .env.example.
load_dotenv()

# NOTE: Groq(), unlike some other AI SDKs, checks for the API key
# immediately when constructed and raises right away if it's missing --
# it does NOT wait until you actually make a call. Building the client at
# import time would therefore crash the entire app on startup if
# GROQ_API_KEY isn't set yet, not just this one feature. Instead we build
# it lazily, on first real use, so the rest of the app works fine even
# before you've set up your key -- you'll only see an error when you
# actually try to generate a summary.
_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq()
    return _client

MODEL = "openai/gpt-oss-120b"  # model available on Groq's free tier

# Free-tier request budgets are limited (see console.groq.com/docs/rate-limits),
# so very long reports get truncated before being sent -- this is plenty
# of text for a typical lab report or clinical note.
MAX_REPORT_CHARS = 12000

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
    """Extracts the PDF's text, then asks Groq to turn it into structured
    fields. Returns a dict with keys: summary (str), key_findings
    (list[str]), flagged_values (list[str]), recommendation (str).

    Raises ValueError if no text could be extracted, or if Groq's reply
    can't be parsed into that shape -- the caller (ai_summaries.py) turns
    either case into a clean HTTP error rather than saving bad data.
    """
    report_text = extract_pdf_text(pdf_path)
    if not report_text:
        raise ValueError(
            "No text could be extracted from this PDF. It may be a scanned "
            "image with no text layer, which this doesn't OCR."
        )

    if len(report_text) > MAX_REPORT_CHARS:
        report_text = report_text[:MAX_REPORT_CHARS] + "\n\n[...truncated...]"

    completion = _get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": _PROMPT_TEMPLATE.format(report_text=report_text)}],
        response_format={"type": "json_object"},
        max_completion_tokens=1024,
    )

    raw_text = completion.choices[0].message.content

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Groq did not return valid JSON: {raw_text[:500]}") from exc

    return {
        "summary": str(data.get("summary", "")),
        "key_findings": [str(item) for item in data.get("key_findings", [])],
        "flagged_values": [str(item) for item in data.get("flagged_values", [])],
        "recommendation": str(data.get("recommendation", "")),
    }
