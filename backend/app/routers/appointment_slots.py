"""
Doctor availability and patient booking.

A doctor publishes time slots; a CONNECTED patient books an open one, which
creates a normal `appointments` row and marks the slot booked (so it
disappears for everyone else). Patients only ever see open, future slots of
doctors they are connected to. Cancelling an appointment re-opens its slot
(see routers/appointments.py).
"""
from datetime import timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import has_accepted_connection
from ..security import get_current_user, require_doctor_role, require_patient_role
from .appointments import _serialize_appointment

router = APIRouter(prefix="/appointment-slots", tags=["appointment-slots"])

MAX_WINDOW_HOURS = 16
MAX_SLOTS_PER_REQUEST = 64


def _slot_out(slot: models.AppointmentSlot, *, for_doctor: bool) -> schemas.SlotOut:
    patient_name = None
    if for_doctor and slot.appointment is not None and slot.appointment.patient is not None:
        patient_name = slot.appointment.patient.name
    return schemas.SlotOut(
        id=slot.id,
        doctor_id=slot.doctor_id,
        doctor_name=slot.doctor.name if slot.doctor else None,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        status=slot.status,
        patient_name=patient_name,
        appointment_id=slot.appointment_id if for_doctor else None,
    )


@router.post("", response_model=List[schemas.SlotOut], status_code=status.HTTP_201_CREATED)
def create_slots(
    payload: schemas.SlotCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Cuts the window [start, end) into back-to-back slots of slot_minutes.
    Slots that would overlap one the doctor already published are skipped."""
    require_doctor_role(current_user)
    now = models.utc_now()
    if payload.end <= payload.start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The end time must be after the start time")
    if payload.start <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Availability must be in the future")
    if payload.end - payload.start > timedelta(hours=MAX_WINDOW_HOURS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Add at most {MAX_WINDOW_HOURS} hours of availability at a time"
        )

    step = timedelta(minutes=payload.slot_minutes)
    windows = []
    cursor = payload.start
    while cursor + step <= payload.end:
        windows.append((cursor, cursor + step))
        cursor += step
    if not windows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That window is shorter than one slot")
    if len(windows) > MAX_SLOTS_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"That makes more than {MAX_SLOTS_PER_REQUEST} slots; use a shorter window"
        )

    existing = (
        db.query(models.AppointmentSlot)
        .filter(
            models.AppointmentSlot.doctor_id == current_user.id,
            models.AppointmentSlot.starts_at < payload.end,
            models.AppointmentSlot.ends_at > payload.start,
        )
        .all()
    )
    created = []
    for starts, ends in windows:
        if any(e.starts_at < ends and e.ends_at > starts for e in existing):
            continue
        slot = models.AppointmentSlot(doctor_id=current_user.id, starts_at=starts, ends_at=ends, status="open")
        db.add(slot)
        created.append(slot)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You already have availability covering that whole window"
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Some of those slots already exist")
    for slot in created:
        db.refresh(slot)
    return [_slot_out(s, for_doctor=True) for s in created]


@router.get("/mine", response_model=List[schemas.SlotOut])
def list_my_slots(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """The doctor's own upcoming availability, open and booked."""
    require_doctor_role(current_user)
    cutoff = models.utc_now() - timedelta(hours=12)
    slots = (
        db.query(models.AppointmentSlot)
        .filter(models.AppointmentSlot.doctor_id == current_user.id, models.AppointmentSlot.starts_at >= cutoff)
        .order_by(models.AppointmentSlot.starts_at)
        .all()
    )
    return [_slot_out(s, for_doctor=True) for s in slots]


@router.delete("/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_slot(slot_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    require_doctor_role(current_user)
    slot = db.query(models.AppointmentSlot).filter(models.AppointmentSlot.id == slot_id).first()
    if slot is None or slot.doctor_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found")
    if slot.status != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A patient has booked this slot. Cancel the appointment from the calendar first.",
        )
    db.delete(slot)
    db.commit()


@router.get("", response_model=List[schemas.SlotOut])
def list_open_slots(
    doctor_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """A patient's view of ONE connected doctor's open, future slots."""
    require_patient_role(current_user)
    if not has_accepted_connection(db, patient_id=current_user.id, doctor_id=doctor_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this doctor")
    slots = (
        db.query(models.AppointmentSlot)
        .filter(
            models.AppointmentSlot.doctor_id == doctor_id,
            models.AppointmentSlot.status == "open",
            models.AppointmentSlot.starts_at > models.utc_now(),
        )
        .order_by(models.AppointmentSlot.starts_at)
        .all()
    )
    return [_slot_out(s, for_doctor=False) for s in slots]


@router.post("/{slot_id}/book", response_model=schemas.AppointmentOut, status_code=status.HTTP_201_CREATED)
def book_slot(
    slot_id: int,
    payload: schemas.SlotBook,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    require_patient_role(current_user)
    # Row lock: two patients clicking the same slot are serialised, and the
    # loser sees it already booked.
    slot = (
        db.query(models.AppointmentSlot)
        .filter(models.AppointmentSlot.id == slot_id)
        .with_for_update()
        .first()
    )
    # Unconnected patients get the same "not found" as a missing slot, so
    # slot ids of other doctors can't be probed.
    if slot is None or not has_accepted_connection(db, patient_id=current_user.id, doctor_id=slot.doctor_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slot not found")
    if slot.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Sorry, that slot was just booked by someone else")
    if slot.starts_at <= models.utc_now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That time has already passed")

    appointment = models.Appointment(
        patient_id=current_user.id,
        doctor_id=slot.doctor_id,
        scheduled_at=slot.starts_at,
        reason=(payload.reason or "").strip() or None,
        status=models.AppointmentStatus.scheduled,
        created_by_id=current_user.id,
    )
    db.add(appointment)
    db.flush()
    slot.status = "booked"
    slot.appointment_id = appointment.id
    db.commit()
    db.refresh(appointment)
    return _serialize_appointment(appointment)
