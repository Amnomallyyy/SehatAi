import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import (
    UPLOAD_DIR,
    ensure_conversation_participant,
    ensure_report_participant,
    get_conversation_or_404,
    get_report_or_404,
    has_reports_access_grant,
)
from ..security import get_current_user, require_doctor_role

router = APIRouter(tags=["reports"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB safety cap, not in the spec but cheap insurance

# The only forward direction status is allowed to move in.
_STATUS_ORDER = [
    models.ReportStatus.uploaded,
    models.ReportStatus.processing,
    models.ReportStatus.awaiting_review,
    models.ReportStatus.reviewed,
]


def _serialize_report(report: models.Report) -> schemas.ReportOut:
    """The one place a Report becomes JSON. See schemas.ReportOut for why
    this can't just be automatic ORM->Pydantic mapping."""
    ai_summary_out = schemas.AISummaryOut.model_validate(report.ai_summary) if report.ai_summary else None
    return schemas.ReportOut(
        id=report.id,
        conversation_id=report.conversation_id,
        patient_id=report.patient_id,
        display_name=report.display_name,
        pdf_url=f"/reports/{report.id}/file",
        status=report.status,
        timestamp=report.timestamp,
        ai_summary=ai_summary_out,
    )


@router.post(
    "/conversations/{conversation_id}/reports",
    response_model=schemas.ReportOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_report(
    conversation_id: int,
    file: UploadFile = File(...),
    display_name: Optional[str] = Form(
        None, description="Optional label for this report, e.g. 'Bloodwork - March 2026'. Defaults to the uploaded file's name."
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Either participant may upload (the spec doesn't say only the patient
    can), multipart/form-data with a `file` field and an optional
    `display_name` field. patient_id is always derived from the
    conversation server-side, never taken from the client, so it can't be
    spoofed to point at the wrong patient."""
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)

    looks_like_pdf = file.content_type == "application/pdf" or (file.filename or "").lower().endswith(".pdf")
    if not looks_like_pdf:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are accepted")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds the 20 MB upload limit")

    # Falls back to the uploaded file's own name if the uploader didn't
    # type one in -- better than a blank label, and closes the old gap
    # where the original filename went nowhere at all.
    chosen_name = (display_name or "").strip()
    if not chosen_name:
        original = (file.filename or "").rsplit(".", 1)[0].strip()
        chosen_name = original or "Untitled Report"
    chosen_name = chosen_name[:200]  # matches the display_name column's length

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Randomly named ON DISK regardless of chosen_name -- never trust a
    # client-supplied filename as a path component (path traversal), and
    # it doubles as an unguessable handle. chosen_name is purely a display
    # label stored separately; it has no effect on where the file lives.
    stored_filename = f"{uuid.uuid4().hex}.pdf"
    with open(UPLOAD_DIR / stored_filename, "wb") as f:
        f.write(contents)

    report = models.Report(
        conversation_id=conversation.id,
        patient_id=conversation.patient_id,
        display_name=chosen_name,
        pdf_path=stored_filename,
        status=models.ReportStatus.uploaded,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return _serialize_report(report)


@router.get("/conversations/{conversation_id}/reports", response_model=List[schemas.ReportOut])
def list_reports_for_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Chronological, so reports interleave correctly with the chat they
    appear inline in."""
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)

    reports = (
        db.query(models.Report)
        .filter(models.Report.conversation_id == conversation_id)
        .order_by(models.Report.id.asc())
        .all()
    )
    return [_serialize_report(r) for r in reports]


@router.get("/reports", response_model=List[schemas.ReportOut])
def list_reports_for_patient(
    patient_id: int = Query(..., description="Required -- all reports for this patient, across every conversation"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Uses the redundant patient_id column for a single-table lookup
    instead of joining through conversations. Patients may only query
    themselves. Doctors may only query a patient they have an accepted
    Connection with AND who has separately granted them access to their
    reports history (see /reports-access) -- this dedicated cross-
    conversation view is gated more tightly than the always-available
    inline per-conversation reports list, which an accepted Connection
    alone is sufficient for."""
    if current_user.role == models.UserRole.patient:
        if current_user.id != patient_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Patients may only list their own reports"
            )
    else:
        connected = (
            db.query(models.Connection)
            .filter(
                models.Connection.patient_id == patient_id,
                models.Connection.doctor_id == current_user.id,
                models.Connection.status == models.ConnectionStatus.accepted,
            )
            .first()
        )
        if connected is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this patient")
        if not has_reports_access_grant(db, patient_id, current_user.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This patient has not granted you access to their reports history",
            )

    reports = (
        db.query(models.Report)
        .filter(models.Report.patient_id == patient_id)
        .order_by(models.Report.id.desc())
        .all()
    )
    return [_serialize_report(r) for r in reports]


@router.get("/reports/{report_id}", response_model=schemas.ReportOut)
def get_report(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)
    return _serialize_report(report)


@router.get("/reports/{report_id}/file")
def download_report_file(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Streams the actual PDF bytes. This is deliberately an authenticated
    endpoint rather than a public static file mount -- these are medical
    documents. That means plain `<a href>`/`<img src>` won't work from the
    frontend (browsers don't attach custom headers to navigations); the
    contract doc shows the fetch+blob pattern needed instead."""
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)

    file_path = UPLOAD_DIR / report.pdf_path
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found on server")

    return FileResponse(path=file_path, media_type="application/pdf", filename=f"report-{report.id}.pdf")


@router.patch("/reports/{report_id}/status", response_model=schemas.ReportOut)
def update_report_status(
    report_id: int,
    payload: schemas.ReportStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Status only moves forward through uploaded -> processing ->
    awaiting_review -> reviewed (the spec defines that sequence but not who
    drives it or whether steps can be skipped -- this assumes skipping is
    fine but reversing isn't). Marking 'reviewed' is doctor-only; every
    other transition just requires being a participant."""
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)

    current_index = _STATUS_ORDER.index(report.status)
    new_index = _STATUS_ORDER.index(payload.status)
    if new_index <= current_index:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"status can only move forward through {[s.value for s in _STATUS_ORDER]}; "
                f"report is currently '{report.status.value}'"
            ),
        )

    if payload.status == models.ReportStatus.reviewed:
        require_doctor_role(current_user)

    report.status = payload.status
    db.commit()
    db.refresh(report)
    return _serialize_report(report)
