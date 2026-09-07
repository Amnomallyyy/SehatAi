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
thread, stored in CareLink's own SQLite-backed Report table).

Fix, deliberately the simplest of the two options named in the
architecture doc: save the upload to a short-lived local staging file,
then shell out to DataFetch's own main.py exactly the way a human runs it
today. DataFetch's pipeline.py already does the real work end-to-end --
uploads to Supabase storage, inserts the `documents` row, runs OCR,
inserts `extracted_data` -- so this route does not duplicate any of that;
it only triggers it. (The other option in the doc, a processing_queue
worker, is worth revisiting if extraction ever needs to survive a request
timeout -- not needed for a first working version.)

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
import json
import os
import secrets
import subprocess
import sys
import uuid as uuid_module
from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import LAB_REPORT_STAGING_DIR, get_or_create_sehatai_patient_id
from ..security import get_current_user, hash_password, require_patient_role
from ..verification import run_verification

router = APIRouter(prefix="/me", tags=["lab-reports"])

# repo root -- backend/app/routers/lab_reports.py -> routers -> app -> backend -> root
DATAFETCH_DIR = Path(os.environ.get("DATAFETCH_DIR") or Path(__file__).resolve().parents[3])
DATAFETCH_PYTHON = os.environ.get("DATAFETCH_PYTHON", sys.executable)

# DataFetch's own auth.py/clients.py load the ROOT .env themselves (their
# load_dotenv() runs with cwd=DATAFETCH_DIR, see the subprocess call
# below) -- so most keys (SUPABASE_URL, GEMINI_API_KEY, ...) already
# arrive correctly. The one mismatch: DataFetch reads SUPABASE_KEY, but
# the root .env calls the same secret SUPABASE_SERVICE_KEY (every other
# service in this repo reads it under that name). Read it directly from
# the root .env file here -- NOT from CareLink's own os.environ, which
# loads its own separate backend/.env and never has this value at all --
# and hand it to the subprocess under the name DataFetch expects.
_ROOT_ENV = dotenv_values(DATAFETCH_DIR / ".env")
DATAFETCH_SUPABASE_KEY = _ROOT_ENV.get("SUPABASE_KEY") or _ROOT_ENV.get("SUPABASE_SERVICE_KEY", "")
# Same class of mismatch, found live: DataFetch's clients.py reads
# OCRSPACE_API_KEY specifically; whoever added it to the root .env named
# it OCR_API_KEY. Same fix, same reasoning.
DATAFETCH_OCR_KEY = _ROOT_ENV.get("OCRSPACE_API_KEY") or _ROOT_ENV.get("OCR_API_KEY", "")

# EXTENDED (2026-09-06) to match datafetch/pipeline.py's detect_file_type
# -- GIF/TIFF/BMP are all formats OCR.space's API genuinely supports.
# HEIC deliberately excluded: OCR.space doesn't support it and this
# pipeline has no HEIC->JPEG conversion step (see detect_file_type's own
# comment on why that matters less than it sounds for "photos from a
# phone" specifically -- WhatsApp already re-encodes to JPEG).
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@router.post("/lab-reports", response_model=schemas.LabReportUploadOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_lab_report(
    background_tasks: BackgroundTasks,
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

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds the 20 MB upload limit")

    sehatai_patient_id = get_or_create_sehatai_patient_id(current_user, db)

    # Rotate a throwaway password onto this patient's SehatAI row -- see
    # module doc comment for why this is correct rather than a hack.
    raw_password = secrets.token_urlsafe(24)
    patient_row = db.query(models.Patient).filter(models.Patient.id == sehatai_patient_id).first()
    if patient_row is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="SehatAI patient record is missing")
    patient_row.password_hash = hash_password(raw_password)
    db.commit()

    LAB_REPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_path = LAB_REPORT_STAGING_DIR / f"{uuid_module.uuid4().hex}{suffix}"
    staged_path.write_bytes(contents)

    try:
        result = subprocess.run(
            [
                DATAFETCH_PYTHON,
                "datafetch/main.py",
                "--file", str(staged_path),
                "--patient-id", str(sehatai_patient_id),
                "--password", raw_password,
                "--json",
            ],
            cwd=str(DATAFETCH_DIR),
            capture_output=True,
            text=True,
            timeout=180,  # OCR + batch fallback can be slow -- see DataFetch's own README on Gemini Batch latency
            # Same fix startDietBot.js already needed for the identical
            # reason: main.py prints emoji progress markers, and Windows'
            # default console codec (cp1252) crashes on them otherwise.
            # SUPABASE_KEY -- see the module-level comment on
            # DATAFETCH_SUPABASE_KEY above for why this one has to be
            # passed explicitly rather than relying on DataFetch's own
            # load_dotenv() to find it under a name it never uses.
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "SUPABASE_KEY": DATAFETCH_SUPABASE_KEY,
                "OCRSPACE_API_KEY": DATAFETCH_OCR_KEY,
            },
        )
    except subprocess.TimeoutExpired:
        return schemas.LabReportUploadOut(
            status="queued",
            detail="Upload received; extraction is still running and will finish in the background. Check back shortly.",
        )
    finally:
        staged_path.unlink(missing_ok=True)

    if result.returncode != 0:
        # Surface DataFetch's own output rather than a generic message --
        # this is a hackathon-stage integration, a raw error is more useful
        # to whoever's debugging it than a polished one that hides the cause.
        # FOUND LIVE: main.py prints its actual result JSON (including the
        # real "error" field) to STDOUT, not stderr -- stderr is only
        # httpx/postgrest's own INFO-level request logging. Surfacing
        # stderr alone showed a misleadingly truncated, uninformative
        # message ending mid-log-line; both streams are needed to see
        # what actually happened.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Extraction pipeline failed. stdout: {result.stdout.strip()[-800:] or '(empty)'} "
                f"| stderr: {result.stderr.strip()[-300:] or '(empty)'}"
            ),
        )

    # main.py --json prints its progress log AND the result dict as JSON
    # to stdout -- TWICE, in fact (once inside process_single_file's own
    # "📊 Result:" box, again right after because of main()'s own `if
    # args.json: print(json.dumps(result))`). A greedy `\{.*\}` regex
    # across the whole of stdout would span both blocks and produce
    # invalid JSON (confirmed live -- silently swallowed by the
    # try/except below before this fix, so verification never ran).
    # json.JSONDecoder().raw_decode() from the first "{" is immune to
    # that: it stops at the end of the FIRST complete JSON value
    # regardless of what text (a second block, a separator line) follows.
    # Best-effort only -- a parse miss here just means no verification
    # runs for this upload; the upload itself already succeeded and its
    # response is unaffected.
    brace_idx = result.stdout.find("{")
    if brace_idx != -1:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(result.stdout[brace_idx:])
            document_id = parsed.get("document_id")
            if document_id:
                background_tasks.add_task(run_verification, uuid_module.UUID(document_id))
        except (json.JSONDecodeError, ValueError):
            pass

    return schemas.LabReportUploadOut(status="processed", detail=result.stdout.strip()[-1000:])
