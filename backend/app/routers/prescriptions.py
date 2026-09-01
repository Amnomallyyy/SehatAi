"""
Prescriptions: doctor-only PDF uploads, deliberately kept separate from
Report/AISummary/ReportComment. There is no AI-summary route anywhere in
this file or wired to this model -- that's the entire enforcement mechanism
for "no AI summary is ever generated for a prescription."
"""
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
    get_conversation_or_404,
    has_accepted_connection,
    has_reports_access_grant,
)
from ..security import get_current_user, require_doctor_role

router = APIRouter(tags=["prescriptions"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # same cap as report uploads


def _serialize_prescription(prescription: models.Prescription) -> schemas.PrescriptionOut:
    return schemas.PrescriptionOut(
        id=prescription.id,
        conversation_id=prescription.conversation_id,
        patient_id=prescription.patient_id,
        doctor_id=prescription.doctor_id,
        display_name=prescription.display_name,
        pdf_url=f"/prescriptions/{prescription.id}/file",
        timestamp=prescription.timestamp,
    )


def _get_prescription_or_404(db: Session, prescription_id: int) -> models.Prescription:
    prescription = db.query(models.Prescription).filter(models.Prescription.id == prescription_id).first()
    if prescription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prescription not found")
    return prescription


@router.post(
    "/conversations/{conversation_id}/prescriptions",
    response_model=schemas.PrescriptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_prescription(
    conversation_id: int,
    file: UploadFile = File(...),
    display_name: Optional[str] = Form(None, description="Optional label, e.g. 'Amoxicillin - Aug 2026'"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Doctor-only, unlike report uploads which either participant may do."""
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)
    require_doctor_role(current_user)

    looks_like_pdf = file.content_type == "application/pdf" or (file.filename or "").lower().endswith(".pdf")
    if not looks_like_pdf:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are accepted")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds the 20 MB upload limit")

    chosen_name = (display_name or "").strip()
    if not chosen_name:
        original = (file.filename or "").rsplit(".", 1)[0].strip()
        chosen_name = original or "Untitled Prescription"
    chosen_name = chosen_name[:200]

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}.pdf"
    with open(UPLOAD_DIR / stored_filename, "wb") as f:
        f.write(contents)

    prescription = models.Prescription(
        conversation_id=conversation.id,
        patient_id=conversation.patient_id,
        doctor_id=current_user.id,
        display_name=chosen_name,
        pdf_path=stored_filename,
    )
    db.add(prescription)
    db.commit()
    db.refresh(prescription)
    return _serialize_prescription(prescription)


@router.get("/conversations/{conversation_id}/prescriptions", response_model=List[schemas.PrescriptionOut])
def list_prescriptions_for_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Chronological, for inline placement in the shared chat thread --
    either participant may read."""
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)

    prescriptions = (
        db.query(models.Prescription)
        .filter(models.Prescription.conversation_id == conversation_id)
        .order_by(models.Prescription.id.asc())
        .all()
    )
    return [_serialize_prescription(p) for p in prescriptions]


@router.get("/prescriptions", response_model=List[schemas.PrescriptionOut])
def list_prescriptions_for_patient(
    patient_id: int = Query(..., description="Required -- all prescriptions for this patient, across every conversation"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Same authorization shape as GET /reports?patient_id= -- patients may
    only query themselves; doctors need both an accepted Connection and a
    granted ReportAccessGrant (the same grant used for the Reports tab)."""
    if current_user.role == models.UserRole.patient:
        if current_user.id != patient_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Patients may only list their own prescriptions"
            )
    else:
        if not has_accepted_connection(db, patient_id, current_user.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this patient")
        if not has_reports_access_grant(db, patient_id, current_user.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This patient has not granted you access to their reports history",
            )

    prescriptions = (
        db.query(models.Prescription)
        .filter(models.Prescription.patient_id == patient_id)
        .order_by(models.Prescription.id.desc())
        .all()
    )
    return [_serialize_prescription(p) for p in prescriptions]


@router.get("/prescriptions/{prescription_id}", response_model=schemas.PrescriptionOut)
def get_prescription(
    prescription_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    prescription = _get_prescription_or_404(db, prescription_id)
    ensure_conversation_participant(prescription.conversation, current_user)
    return _serialize_prescription(prescription)


@router.get("/prescriptions/{prescription_id}/file")
def download_prescription_file(
    prescription_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    prescription = _get_prescription_or_404(db, prescription_id)
    ensure_conversation_participant(prescription.conversation, current_user)

    file_path = UPLOAD_DIR / prescription.pdf_path
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found on server")

    return FileResponse(
        path=file_path, media_type="application/pdf", filename=f"prescription-{prescription.id}.pdf"
    )
