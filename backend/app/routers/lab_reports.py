"""
Architecture doc §05 -- Upload -> extraction, connected (the second
"blocking" gap).

Problem: DataFetch's OCR/extraction pipeline is a hands-on CLI
(`python main.py --file <path> --patient-id <uuid> --password <password>`)
-- that's how the 17 real documents/55 extracted_data rows already in
Supabase got there. Nothing in CareLink triggers it automatically. A
patient uploading a report through this route, before this file existed,
would have had no route to upload to at all for a *lab report* specifically
-- CareLink's existing routers/reports.py is a different, deliberately
untouched feature (a PDF shared inside one doctor-patient conversation
thread, stored in CareLink's own Report table).

Fix: save the upload to a short-lived local staging file, then run
DataFetch's own main.py exactly the way a human runs it today. DataFetch's
pipeline.py already does the real work end-to-end -- uploads to Supabase
storage, inserts the `documents` row, runs OCR, inserts `extracted_data` --
so this route does not duplicate any of that; it only triggers it.

The pipeline runs in a bounded worker pool, never on the event loop: the
original `subprocess.run()` inside this async route froze the WHOLE API
(every user, every route) for as long as OCR took -- up to three minutes.
The request waits up to REQUEST_WAIT_SECONDS for the result; past that it
answers "queued" and the extraction genuinely keeps going in the worker
(the old timeout killed the child process while telling the user it was
still running).

The --password requirement: DataFetch's authenticate_patient() gates
processing behind a bcrypt-hashed password on the `patients` row (see
create_patient.py / auth.py) -- a real, intentional consent gate, not a
login mechanic. A patient bridged in lazily via
get_or_create_sehatai_patient_id has no password of their own to give it,
and doesn't need one: this route rotates a fresh, random, throwaway
password onto that patients row immediately before each pipeline run,
uses it once, and never persists or shows it anywhere. That satisfies the
gate without pretending this is a credential the patient should know.
"""
import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import uuid as uuid_module
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import LAB_REPORT_STAGING_DIR, get_or_create_sehatai_patient_id
from ..security import get_current_user, hash_password, require_patient_role
from ..verification import run_verification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["lab-reports"])

# repo root -- backend/app/routers/lab_reports.py -> routers -> app -> backend -> root
DATAFETCH_DIR = Path(os.environ.get("DATAFETCH_DIR") or Path(__file__).resolve().parents[3])
DATAFETCH_PYTHON = os.environ.get("DATAFETCH_PYTHON", sys.executable)

# DataFetch's own auth.py/clients.py load the ROOT .env themselves (their
# load_dotenv() runs with cwd=DATAFETCH_DIR, see the subprocess call
# below) -- so most keys (SUPABASE_URL, GEMINI_API_KEY, ...) already
# arrive correctly. The one mismatch: DataFetch reads SUPABASE_KEY, but
# the root .env calls the same secret SUPABASE_SERVICE_KEY (every other
# service in this repo reads it under that name). Read it from the root
# .env file first, then fall back to this process's own environment (a
# container configured purely through env vars has no .env file at all)
# -- and hand it to the subprocess under the name DataFetch expects.
_ROOT_ENV = dotenv_values(DATAFETCH_DIR / ".env")


def _first_set(*names: str) -> str:
    for name in names:
        value = _ROOT_ENV.get(name) or os.environ.get(name)
        if value:
            return value
    return ""


DATAFETCH_SUPABASE_KEY = _first_set("SUPABASE_KEY", "SUPABASE_SERVICE_KEY")
# Same class of mismatch, found live: DataFetch's clients.py reads
# OCRSPACE_API_KEY specifically; whoever added it to the root .env named
# it OCR_API_KEY. Same fix, same reasoning.
DATAFETCH_OCR_KEY = _first_set("OCRSPACE_API_KEY", "OCR_API_KEY")

# EXTENDED (2026-09-06) to match datafetch/pipeline.py's detect_file_type
# -- GIF/TIFF/BMP are all formats OCR.space's API genuinely supports.
# HEIC deliberately excluded: OCR.space doesn't support it and this
# pipeline has no HEIC->JPEG conversion step (see detect_file_type's own
# comment on why that matters less than it sounds for "photos from a
# phone" specifically -- WhatsApp already re-encodes to JPEG).
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# How long the upload request itself waits for the result before answering
# "queued" (the frontend's button says "up to 3 min").
REQUEST_WAIT_SECONDS = float(os.environ.get("LAB_REPORT_REQUEST_WAIT_SECONDS", "170"))
# Hard ceiling for one pipeline run (OCR + Gemini batch fallback) -- only a
# genuinely hung run ever reaches it.
PIPELINE_HARD_TIMEOUT_SECONDS = float(os.environ.get("LAB_REPORT_PIPELINE_TIMEOUT_SECONDS", "900"))

# Each run is a whole Python subprocess doing OCR, so a burst of uploads
# queues here instead of forking without limit.
_EXTRACTION_POOL = ThreadPoolExecutor(
    max_workers=int(os.environ.get("LAB_REPORT_MAX_CONCURRENT", "2")),
    thread_name_prefix="lab-extract",
)


def _parse_pipeline_result(stdout: str) -> Optional[dict]:
    """main.py --json prints the result dict LAST, after a progress log that
    can itself contain braces -- so decode from the last line that opens a
    JSON object, falling back to the first '{' anywhere."""
    for start in (stdout.rfind("\n{"), stdout.find("{")):
        if start == -1:
            continue
        try:
            parsed, _ = json.JSONDecoder().raw_decode(stdout[start:].lstrip())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _run_extraction(cmd: List[str], env: dict, staged_path: Path) -> subprocess.CompletedProcess:
    """Worker-thread body: run DataFetch's CLI to completion, always remove
    the staged upload afterwards, then run the independent verification
    pass for a newly stored document (still off the event loop)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(DATAFETCH_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PIPELINE_HARD_TIMEOUT_SECONDS,
            env=env,
        )
    finally:
        staged_path.unlink(missing_ok=True)

    if result.returncode != 0:
        logger.warning(
            "DataFetch pipeline exited %s: stdout=%s stderr=%s",
            result.returncode, result.stdout[-1500:], result.stderr[-1500:],
        )
        return result

    parsed = _parse_pipeline_result(result.stdout) or {}
    document_id = parsed.get("document_id")
    if document_id and parsed.get("status") == "stored":
        try:
            run_verification(uuid_module.UUID(str(document_id)))
        except Exception:  # a review aid -- never fail the upload over it
            logger.exception("Verification pass failed for document %s", document_id)
    return result


def _log_background_failure(fut: Future) -> None:
    exc = fut.exception()
    if exc is not None and not isinstance(exc, subprocess.TimeoutExpired):
        logger.error("Lab report extraction crashed: %r", exc)


@router.post("/lab-reports", response_model=schemas.LabReportUploadOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_lab_report(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Patient-only, on purpose -- see the architecture doc's permission
    model (§06): a patient may upload a lab report; only a doctor may add
    medicines or clinical advice. This route only ever creates a
    `documents` row (via DataFetch's own pipeline) -- never `medicines` or
    `clinical_advice`."""
    require_patient_role(current_user)

    original_name = file.filename or ""
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{suffix or 'unknown'}' -- expected one of {sorted(ALLOWED_EXTENSIONS)}",
        )

    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds the 20 MB upload limit")
    if not contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty")

    sehatai_patient_id = get_or_create_sehatai_patient_id(current_user, db)

    # One-time, throwaway password for DataFetch's consent gate -- see the
    # module doc comment. Never returned, logged, or reused.
    raw_password = secrets.token_urlsafe(24)
    patient_row = db.query(models.Patient).filter(models.Patient.id == sehatai_patient_id).first()
    if patient_row is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SehatAI patient record is missing")
    patient_row.password_hash = hash_password(raw_password)
    db.commit()

    LAB_REPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_path = LAB_REPORT_STAGING_DIR / f"{uuid_module.uuid4().hex}{suffix}"
    staged_path.write_bytes(contents)

    cmd = [
        DATAFETCH_PYTHON,
        "datafetch/main.py",
        "--file", str(staged_path),
        "--patient-id", str(sehatai_patient_id),
        "--password", raw_password,
        "--json",
    ]
    # PYTHONIOENCODING/PYTHONUTF8: main.py prints emoji progress markers,
    # and Windows' default console codec (cp1252) crashes on them
    # otherwise. SUPABASE_KEY/OCRSPACE_API_KEY: see the module-level
    # comments above -- only overridden when a value was actually found,
    # so an empty string can never clobber a key the container already
    # has in its own environment.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if DATAFETCH_SUPABASE_KEY:
        env["SUPABASE_KEY"] = DATAFETCH_SUPABASE_KEY
    if DATAFETCH_OCR_KEY:
        env["OCRSPACE_API_KEY"] = DATAFETCH_OCR_KEY

    future = _EXTRACTION_POOL.submit(_run_extraction, cmd, env, staged_path)
    future.add_done_callback(_log_background_failure)

    try:
        # shield(): the request giving up waiting must not cancel the run.
        result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=REQUEST_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return schemas.LabReportUploadOut(
            status="queued",
            detail="Upload received; extraction is still running and will finish in the background. Check back shortly.",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Extraction took too long and was stopped. Please try again with a clearer or smaller file.",
        )

    parsed = _parse_pipeline_result(result.stdout) or {}
    if result.returncode != 0:
        # DataFetch reports user-meaningful failures in the result dict's
        # `error` field -- surface that, never raw stdout/stderr (internal
        # paths, stack traces); those are logged server-side above.
        reason = parsed.get("error") or parsed.get("message") or "the extraction pipeline could not process this file"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Extraction failed: {reason}")

    document_id = str(parsed["document_id"]) if parsed.get("document_id") else None
    if parsed.get("status") == "duplicate":
        return schemas.LabReportUploadOut(
            status="processed", document_id=document_id,
            detail="This report was already uploaded -- showing the existing results.",
        )
    return schemas.LabReportUploadOut(
        status="processed", document_id=document_id,
        detail="Extraction complete -- markers are ready to review.",
    )
