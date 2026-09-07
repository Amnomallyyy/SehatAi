"""
Phase 2 of the reports-review plan: an independent second model re-reads a
lab document's ORIGINAL source file and compares what it finds against the
primary extraction already stored in extracted_data -- a real check on
OCR/structuring accuracy, replacing the needs_review=False /
verification_status="not_run" placeholders structured_reports.py carried
since Phase 1 (see that file's _build_marker_out doc comment, pre-Phase-2).

Runs as a FastAPI BackgroundTask (see routers/lab_reports.py), scheduled
AFTER the upload's own HTTP response is already on its way -- never
inline with the upload request, since a second vision-model round trip
would add real latency on top of an already-slow synchronous OCR
subprocess (see lab_reports.py's own doc comment on the 180s timeout).

Model choice: if the primary OCR engine for this document was already
Gemini vision (datafetch/pipeline.py's camera-photo fallback path), asking
Gemini again wouldn't be a genuine second opinion -- go straight to
NVIDIA NIM's vision model instead. Otherwise, call Gemini vision first
(cheapest to reach, already proven working elsewhere in this repo via
datafetch/clients.py's GeminiClient.extract_text_sync), falling back to
NVIDIA NIM vision only if that call raises -- the same provider-fallback
shape already used in ai.py / sehatEvidence/config.py, just applied to a
vision call instead of a text one.
"""
import base64
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF -- see _rasterize_pdf_first_page's doc comment
import requests

from . import models
from .database import SessionLocal
from .marker_names import normalize_marker
from .storage import download_file

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-3.5-flash"
_NVIDIA_VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"  # CONFIRMED LIVE 2026-09-05:
# the 90b variant timed out on every call (checked directly -- not a
# fluke, and not an auth/network problem, since the 11b model and the
# unrelated nemotron text model both respond in ~1s on the same key/base
# URL). Smaller model, but it's read-and-transcribe work, not reasoning --
# well within what an 11B vision model handles.
_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

_EXTRACTION_PROMPT = (
    "You are independently reviewing a medical lab document. List every lab "
    "test result you can read in this file, one JSON object per test. "
    "Return ONLY a JSON array -- no markdown, no commentary, no wrapping "
    "object. Each object must have EXACTLY these four keys:\n"
    '  "test_name": the name of the test\n'
    '  "value": ONLY the measured result exactly as printed -- do NOT '
    "include the reference range, units, or flags in this field\n"
    '  "value_numeric": the value as a plain JSON number if it is numeric, '
    "otherwise null\n"
    '  "unit": the unit of measurement, or null if none\n\n'
    'Example: {"test_name": "Creatinine", "value": "1.2", '
    '"value_numeric": 1.2, "unit": "mg/dL"}\n'
    'Do NOT write something like "1.2 Range 0.6-1.3" in the value field -- '
    "the reference range is a separate thing this task does not ask for."
)


def run_verification(document_id) -> None:
    """Entry point for the BackgroundTask. Owns its own DB session -- the
    request-scoped session that scheduled this has already been closed by
    the time this runs (it fires after the HTTP response is sent)."""
    db = SessionLocal()
    try:
        doc = db.query(models.Document).filter(models.Document.id == document_id).first()
        if doc is None:
            return
        if not doc.file_url:
            db.add(models.ExtractionVerification(
                document_id=doc.id, status="no_source", completed_at=models.utc_now(),
            ))
            db.commit()
            return

        verification = models.ExtractionVerification(document_id=doc.id, status="running")
        db.add(verification)
        db.commit()
        db.refresh(verification)

        try:
            file_bytes = download_file(doc.file_url)
            mime_type = _mime_type_for(doc)
            model_used, verified_items = _verify_with_fallback(file_bytes, mime_type, doc.ocr_engine or "")

            verified_by_marker: Dict[str, Dict] = {}
            for item in verified_items:
                name = item.get("test_name")
                if name:
                    verified_by_marker[normalize_marker(name)] = item

            primary_rows = db.query(models.ExtractedData).filter(models.ExtractedData.document_id == doc.id).all()
            agreement_count = 0
            disagreement_count = 0
            for row in primary_rows:
                key = normalize_marker(row.test_name)
                match = verified_by_marker.get(key)
                verified_value = match.get("value") if match else None
                verified_unit = match.get("unit") if match else None
                agrees = match is not None and _values_agree(
                    row.value_numeric, match.get("value_numeric"), row.value or "", verified_value or "",
                )
                if agrees:
                    agreement_count += 1
                else:
                    disagreement_count += 1
                db.add(models.ExtractionVerificationFinding(
                    verification_id=verification.id,
                    normalized_marker_name=key,
                    primary_value=row.value,
                    primary_unit=row.unit,
                    verified_value=verified_value,
                    verified_unit=verified_unit,
                    agrees=agrees,
                ))

            verification.status = "complete"
            verification.model_used = model_used
            verification.agreement_count = agreement_count
            verification.disagreement_count = disagreement_count
            verification.completed_at = models.utc_now()
            db.commit()
        except Exception as exc:
            db.rollback()
            verification.status = "failed"
            verification.error = str(exc)[:2000]
            verification.completed_at = models.utc_now()
            db.commit()
            logger.error(f"Verification failed for document {document_id}: {exc}")
    except Exception as exc:
        logger.error(f"Verification pass crashed for document {document_id}: {exc}")
    finally:
        db.close()


def _verify_with_fallback(file_bytes: bytes, mime_type: str, primary_engine: str) -> Tuple[str, List[Dict]]:
    primary_was_gemini = primary_engine.startswith("gemini")
    if not primary_was_gemini:
        try:
            return "gemini_vision", _call_gemini_vision(file_bytes, mime_type)
        except Exception as exc:
            logger.warning(f"Verifier: Gemini vision failed, falling back to NVIDIA vision: {exc}")
    return "nvidia_vision", _call_nvidia_vision(file_bytes, mime_type)


def _call_gemini_vision(file_bytes: bytes, mime_type: str) -> List[Dict]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    payload = {
        "contents": [{
            "parts": [
                {"text": _EXTRACTION_PROMPT},
                {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode("utf-8")}},
            ]
        }]
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"
    resp = requests.post(url, json=payload, params={"key": api_key}, timeout=60)
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_json_array(text)


def _rasterize_pdf_first_page(pdf_bytes: bytes) -> bytes:
    """CONFIRMED LIVE 2026-09-05: the NVIDIA NIM vision model rejects a PDF
    passed as image_url data outright ("cannot identify image file") --
    unlike Gemini's inline_data, which accepts application/pdf natively,
    this endpoint only understands actual raster images. Render the first
    page to PNG so NVIDIA can be used as a verifier for PDF documents too
    -- most lab reports are one page; a multi-page PDF's later pages
    simply aren't checked by this pass, which is a real but acceptable
    gap for a review aid rather than the extraction step itself."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        page = pdf[0]
        pix = page.get_pixmap(dpi=150)
        return pix.tobytes("png")


def _call_nvidia_vision(file_bytes: bytes, mime_type: str) -> List[Dict]:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY not set")
    if mime_type == "application/pdf":
        file_bytes = _rasterize_pdf_first_page(file_bytes)
        mime_type = "image/png"
    data_url = f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode('utf-8')}"
    payload = {
        "model": _NVIDIA_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _EXTRACTION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(f"{_NVIDIA_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return _parse_json_array(text)


def _parse_json_array(text: str) -> List[Dict]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise RuntimeError(f"Verifier response was not a JSON array: {text[:200]}")


def _mime_type_for(doc: models.Document) -> str:
    if doc.mime_type:
        return doc.mime_type
    if (doc.file_path or "").lower().endswith(".pdf"):
        return "application/pdf"
    return "image/jpeg"


_LEADING_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _extract_leading_number(text: str) -> Optional[float]:
    """Fallback for when a vision model doesn't strictly follow the
    value_numeric instruction and leaves it null but still puts a plain
    number at the start of `value` (seen live from the smaller NVIDIA
    vision model, e.g. value="1.2" with value_numeric missing, or even
    "1.2 Range 0.6-1.3" despite the prompt asking it not to) -- pulling
    the leading number back out here is more robust than only trusting
    the model followed the schema instruction perfectly."""
    match = _LEADING_NUMBER_RE.match(text.strip())
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def _values_agree(primary_numeric, verified_numeric, primary_text: str, verified_text: str) -> bool:
    """Numeric marker values are compared with a 5% tolerance (OCR/reading
    noise on the last decimal shouldn't read as a disagreement); anything
    non-numeric falls back to a case-insensitive exact text match."""
    if verified_numeric is None:
        verified_numeric = _extract_leading_number(verified_text)
    if primary_numeric is not None and verified_numeric is not None:
        try:
            p, v = float(primary_numeric), float(verified_numeric)
        except (TypeError, ValueError):
            return primary_text.strip().casefold() == verified_text.strip().casefold()
        return abs(p - v) <= 0.05 * max(abs(p), 1.0)
    return primary_text.strip().casefold() == verified_text.strip().casefold()
