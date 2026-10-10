"""
Medicines: a connected doctor writes medicines for a patient (with an
optional prescription file). The patient sees them and can mark each one
"taking" / "not taking"; the doctor has no stop button -- a medicine simply
becomes inactive once its last date passes.

Rows go into SehatAI's existing `medicines` table (so the triage bot's
patient profile sees doctor-prescribed medicines too), with the
prescribing doctor + attachment recorded in the CareLink-owned
`medicine_prescriptions` side table. Medicines DataFetch extracted from a
lab report have no side-table row and are shown as "from lab report".
"""
import uuid
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import (
    UPLOAD_DIR,
    get_or_create_sehatai_patient_id,
    has_accepted_connection,
    looks_like_image_bytes,
    looks_like_pdf_bytes,
)
from ..security import get_current_user, require_doctor_role, require_patient_role

router = APIRouter(tags=["medicines"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # same cap as prescriptions/reports
MEDICINE_UPLOAD_DIR = UPLOAD_DIR / "medicines"


def expire_finished_medicines(db: Session) -> int:
    """Marks medicines whose end date has passed as inactive. Nothing else
    does this, and the triage/diet bots only look at `active`, so without it
    a finished course would look current to them forever. Idempotent."""
    changed = (
        db.query(models.Medicine)
        .filter(models.Medicine.active.is_(True), models.Medicine.end_date.isnot(None), models.Medicine.end_date < date.today())
        .update({models.Medicine.active: False}, synchronize_session=False)
    )
    db.commit()
    return changed


def _status(medicine: models.Medicine) -> str:
    if medicine.end_date is not None and medicine.end_date < date.today():
        return "ended"
    return "active" if medicine.active else "paused"


def _serialize(medicine: models.Medicine) -> schemas.MedicineOut:
    rx = medicine.prescription
    return schemas.MedicineOut(
        id=str(medicine.id),
        name=medicine.name,
        dosage=medicine.dosage,
        start_date=medicine.start_date,
        end_date=medicine.end_date,
        active=bool(medicine.active) and _status(medicine) != "ended",
        status=_status(medicine),
        notes=rx.notes if rx else None,
        source="doctor" if rx else "lab_report",
        doctor_id=rx.doctor_id if rx else None,
        doctor_name=rx.doctor.name if rx and rx.doctor else None,
        has_attachment=bool(rx and rx.attachment_path),
        attachment_name=rx.attachment_name if rx else None,
        recorded_at=medicine.recorded_at,
        updated_at=rx.updated_at if rx else medicine.recorded_at,
    )


def _list_for(db: Session, patient_uuid: Optional[uuid.UUID]) -> List[schemas.MedicineOut]:
    if patient_uuid is None:
        return []
    rows = (
        db.query(models.Medicine)
        .filter(models.Medicine.patient_id == patient_uuid)
        .order_by(models.Medicine.active.desc(), models.Medicine.recorded_at.desc())
        .all()
    )
    return [_serialize(m) for m in rows]


def _connected_patient_or_403(db: Session, patient_user_id: int, doctor: models.User) -> models.User:
    require_doctor_role(doctor)
    patient = db.query(models.User).filter(models.User.id == patient_user_id).first()
    if patient is None or patient.role != models.UserRole.patient:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    if not has_accepted_connection(db, patient_id=patient.id, doctor_id=doctor.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this patient")
    return patient


def _patient_user_for(db: Session, medicine: models.Medicine) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.sehatai_patient_id == medicine.patient_id).first()


def _medicine_or_404(db: Session, medicine_id: str) -> models.Medicine:
    try:
        med_uuid = uuid.UUID(medicine_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    medicine = db.query(models.Medicine).filter(models.Medicine.id == med_uuid).first()
    if medicine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    return medicine


def _clean(value: Optional[str], limit: int) -> Optional[str]:
    value = (value or "").strip()
    return value[:limit] or None


def _check_dates(start: Optional[date], end: Optional[date]) -> None:
    if start and end and end < start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="End date can't be before the start date")


async def _save_attachment(file: UploadFile):
    """Accepts PDF/JPEG/PNG, checked by content (magic bytes), not by the
    client-supplied name or type. Returns (stored_path, original_name, mime)."""
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds the 20 MB upload limit")
    if looks_like_pdf_bytes(contents):
        ext, mime = "pdf", "application/pdf"
    elif looks_like_image_bytes(contents, "jpg"):
        ext, mime = "jpg", "image/jpeg"
    elif looks_like_image_bytes(contents, "png"):
        ext, mime = "png", "image/png"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF, JPG or PNG files are accepted")

    MEDICINE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored = f"medicines/{uuid.uuid4().hex}.{ext}"
    with open(UPLOAD_DIR / stored, "wb") as f:
        f.write(contents)
    original = (file.filename or "").strip() or f"prescription.{ext}"
    return stored, original[:255], mime


@router.get("/medicines/me", response_model=List[schemas.MedicineOut])
def list_my_medicines(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """Patient, read-only. A patient who has never used SehatAI has no
    `patients` row yet -- that's just an empty list, not an error."""
    require_patient_role(current_user)
    expire_finished_medicines(db)
    return _list_for(db, current_user.sehatai_patient_id)


@router.get("/medicines/patients/{patient_user_id}", response_model=List[schemas.MedicineOut])
def list_patient_medicines(
    patient_user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    patient = _connected_patient_or_403(db, patient_user_id, current_user)
    expire_finished_medicines(db)
    return _list_for(db, patient.sehatai_patient_id)


@router.post(
    "/medicines/patients/{patient_user_id}",
    response_model=schemas.MedicineOut,
    status_code=status.HTTP_201_CREATED,
)
async def prescribe_medicine(
    patient_user_id: int,
    name: str = Form(...),
    dosage: Optional[str] = Form(None),
    start_date: Optional[date] = Form(None),
    end_date: Optional[date] = Form(None),
    notes: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    patient = _connected_patient_or_403(db, patient_user_id, current_user)
    clean_name = _clean(name, 200)
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Medicine name is required")
    start_date = start_date or date.today()
    _check_dates(start_date, end_date)

    attachment = (None, None, None)
    if file is not None and file.filename:
        attachment = await _save_attachment(file)

    patient_uuid = get_or_create_sehatai_patient_id(patient, db)
    medicine = models.Medicine(
        patient_id=patient_uuid,
        document_id=None,
        name=clean_name,
        dosage=_clean(dosage, 200),
        start_date=start_date,
        end_date=end_date,
        active=end_date is None or end_date >= date.today(),
    )
    db.add(medicine)
    db.flush()
    db.add(
        models.MedicinePrescription(
            medicine_id=medicine.id,
            doctor_id=current_user.id,
            notes=_clean(notes, 1000),
            attachment_path=attachment[0],
            attachment_name=attachment[1],
            attachment_mime=attachment[2],
        )
    )
    db.commit()
    db.refresh(medicine)
    return _serialize(medicine)


@router.patch("/medicines/{medicine_id}", response_model=schemas.MedicineOut)
async def update_medicine(
    medicine_id: str,
    name: Optional[str] = Form(None),
    dosage: Optional[str] = Form(None),
    start_date: Optional[date] = Form(None),
    end_date: Optional[date] = Form(None),
    notes: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Only the prescribing doctor, and only while still connected. Fields
    left out are unchanged. There is no stop: set an end date instead and the
    medicine turns inactive after it. The patient's own taking / not-taking
    choice (`active`) is only touched when the END DATE actually changes."""
    require_doctor_role(current_user)
    medicine = _medicine_or_404(db, medicine_id)
    rx = medicine.prescription
    if rx is None or rx.doctor_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the prescribing doctor can change this medicine"
        )
    patient = _patient_user_for(db, medicine)
    if patient is None or not has_accepted_connection(db, patient_id=patient.id, doctor_id=current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are no longer connected to this patient")

    if name is not None:
        clean_name = _clean(name, 200)
        if not clean_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Medicine name is required")
        medicine.name = clean_name
    if dosage is not None:
        medicine.dosage = _clean(dosage, 200)
    if start_date is not None:
        medicine.start_date = start_date
    if end_date is not None and end_date != medicine.end_date:
        was_expired = medicine.end_date is not None and medicine.end_date < date.today()
        medicine.end_date = end_date
        if end_date < date.today():
            medicine.active = False
        elif was_expired:
            # The course was extended past today; it's current again.
            medicine.active = True
    if notes is not None:
        rx.notes = _clean(notes, 1000)
    _check_dates(medicine.start_date, medicine.end_date)

    if file is not None and file.filename:
        rx.attachment_path, rx.attachment_name, rx.attachment_mime = await _save_attachment(file)
    rx.updated_at = models.utc_now()
    db.commit()
    db.refresh(medicine)
    return _serialize(medicine)


@router.patch("/medicines/{medicine_id}/taking", response_model=schemas.MedicineOut)
def set_medicine_taking(
    medicine_id: str,
    payload: schemas.MedicineTakingUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The patient says whether they are taking this medicine. Stored in
    `active`, which is what the triage/diet bots already read. Works for
    medicines from lab reports too. A finished course can't be switched on."""
    require_patient_role(current_user)
    medicine = _medicine_or_404(db, medicine_id)
    if current_user.sehatai_patient_id is None or current_user.sehatai_patient_id != medicine.patient_id:
        # Same as "not found" -- don't confirm someone else's medicine exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    if payload.taking and medicine.end_date is not None and medicine.end_date < date.today():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This course has ended, so it can't be switched back on")
    medicine.active = payload.taking
    db.commit()
    db.refresh(medicine)
    return _serialize(medicine)


@router.get("/medicines/{medicine_id}/attachment")
def download_medicine_attachment(
    medicine_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    medicine = _medicine_or_404(db, medicine_id)
    if current_user.role == models.UserRole.patient:
        allowed = current_user.sehatai_patient_id == medicine.patient_id
    else:
        patient = _patient_user_for(db, medicine)
        allowed = patient is not None and has_accepted_connection(db, patient_id=patient.id, doctor_id=current_user.id)
    if not allowed:
        # Same as "not found" -- don't confirm someone else's medicine exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    rx = medicine.prescription
    if rx is None or not rx.attachment_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No file attached to this medicine")
    file_path = UPLOAD_DIR / rx.attachment_path
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found on server")
    return FileResponse(
        path=file_path, media_type=rx.attachment_mime or "application/octet-stream", filename=rx.attachment_name
    )
