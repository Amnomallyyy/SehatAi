"""
Med Calendar: appointments between a connected patient/doctor pair.

Patients book from the doctor's published availability (routers/
appointment_slots.py) rather than creating free-form appointments; a doctor
may still create one directly. Either participant may cancel, and
cancelling re-opens the booked slot. Reminders are computed
statelessly at request time (see dependencies.compute_active_reminder); there
is no background scheduler and no dismissal/read-tracking table.
"""
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import compute_active_reminder, has_accepted_connection
from ..security import get_current_user

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _serialize_appointment(appointment: models.Appointment) -> schemas.AppointmentOut:
    return schemas.AppointmentOut(
        id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_id=appointment.doctor_id,
        scheduled_at=appointment.scheduled_at,
        reason=appointment.reason,
        status=appointment.status,
        created_by_id=appointment.created_by_id,
        created_at=appointment.created_at,
        active_reminder=compute_active_reminder(appointment.scheduled_at)
        if appointment.status == models.AppointmentStatus.scheduled
        else None,
        patient=appointment.patient,
        doctor=appointment.doctor,
    )


def _get_appointment_or_404(db: Session, appointment_id: int) -> models.Appointment:
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    return appointment


def _ensure_appointment_participant(appointment: models.Appointment, user: models.User) -> None:
    if user.id not in (appointment.patient_id, appointment.doctor_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not a participant in this appointment")


@router.post("", response_model=schemas.AppointmentOut, status_code=status.HTTP_201_CREATED)
def create_appointment(
    payload: schemas.AppointmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if current_user.id not in (payload.patient_id, payload.doctor_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only create an appointment you are a part of"
        )
    if current_user.role == models.UserRole.patient:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please book one of your doctor's open time slots instead.",
        )

    patient = db.query(models.User).filter(models.User.id == payload.patient_id).first()
    if patient is None or patient.role != models.UserRole.patient:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="patient_id must refer to an existing patient")
    doctor = db.query(models.User).filter(models.User.id == payload.doctor_id).first()
    if doctor is None or doctor.role != models.UserRole.doctor:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="doctor_id must refer to an existing doctor")

    if not has_accepted_connection(db, payload.patient_id, payload.doctor_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No accepted connection exists between this patient and doctor",
        )

    appointment = models.Appointment(
        patient_id=payload.patient_id,
        doctor_id=payload.doctor_id,
        scheduled_at=payload.scheduled_at,
        reason=payload.reason,
        created_by_id=current_user.id,
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    return _serialize_appointment(appointment)


@router.get("", response_model=List[schemas.AppointmentOut])
def list_my_appointments(
    scope: Literal["upcoming", "past", "all"] = Query(default="upcoming"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    query = db.query(models.Appointment).filter(
        or_(models.Appointment.patient_id == current_user.id, models.Appointment.doctor_id == current_user.id)
    )
    now = models.utc_now()
    if scope == "upcoming":
        query = query.filter(models.Appointment.scheduled_at >= now, models.Appointment.status == models.AppointmentStatus.scheduled)
        query = query.order_by(models.Appointment.scheduled_at.asc())
    elif scope == "past":
        query = query.filter(
            or_(models.Appointment.scheduled_at < now, models.Appointment.status == models.AppointmentStatus.cancelled)
        )
        query = query.order_by(models.Appointment.scheduled_at.desc())
    else:
        query = query.order_by(models.Appointment.scheduled_at.desc())

    return [_serialize_appointment(a) for a in query.all()]


@router.get("/{appointment_id}", response_model=schemas.AppointmentOut)
def get_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    appointment = _get_appointment_or_404(db, appointment_id)
    _ensure_appointment_participant(appointment, current_user)
    return _serialize_appointment(appointment)


@router.patch("/{appointment_id}", response_model=schemas.AppointmentOut)
def update_appointment(
    appointment_id: int,
    payload: schemas.AppointmentUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Either participant may reschedule, edit the reason, or cancel.
    'cancelled' is the only status value this accepts -- there's no
    un-cancel."""
    appointment = _get_appointment_or_404(db, appointment_id)
    _ensure_appointment_participant(appointment, current_user)

    if payload.scheduled_at is not None:
        if current_user.role == models.UserRole.patient:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="To change the time, cancel this appointment and book another open slot.",
            )
        appointment.scheduled_at = payload.scheduled_at
    if payload.reason is not None:
        appointment.reason = payload.reason
    if payload.status is not None:
        appointment.status = models.AppointmentStatus.cancelled

    # A slot tied to this appointment is released when the appointment is
    # cancelled or moved (a moved appointment no longer matches the slot).
    if payload.status is not None or payload.scheduled_at is not None:
        slot = (
            db.query(models.AppointmentSlot)
            .filter(models.AppointmentSlot.appointment_id == appointment.id)
            .first()
        )
        if slot is not None:
            slot.appointment_id = None
            # Past slots stay booked-looking history; future ones can be booked again.
            slot.status = "open" if slot.starts_at > models.utc_now() else "booked"

    db.commit()
    db.refresh(appointment)
    return _serialize_appointment(appointment)
