"""
Emergency alert: a patient asks the portal to email the loved one they
saved on the intake form. Triggered by the always-visible Emergency button
or by the triage bot's "Notify someone" action (frontend-only wiring).

The email says who, when, where (city/country) and, for the triage
trigger, the emergency category -- never the chat text or any medical
details. Every attempt is recorded in `emergency_alerts`, which also
drives the rate limit.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, notifier, schemas
from ..database import get_db
from ..security import get_current_user, require_patient_role

router = APIRouter(tags=["emergency"])

MIN_SECONDS_BETWEEN_ALERTS = 60
MAX_ALERTS_PER_HOUR = 5


def _check_rate_limit(db: Session, user_id: int) -> None:
    now = models.utc_now()
    recent = (
        db.query(models.EmergencyAlert)
        .filter(
            models.EmergencyAlert.patient_id == user_id,
            models.EmergencyAlert.created_at >= now - timedelta(hours=1),
            # A failed send must never lock someone out of trying again.
            models.EmergencyAlert.status != "failed",
        )
        .order_by(models.EmergencyAlert.created_at.desc())
        .all()
    )
    if recent and (now - recent[0].created_at).total_seconds() < MIN_SECONDS_BETWEEN_ALERTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="An alert was just sent. Please wait a minute before sending another.",
        )
    if len(recent) >= MAX_ALERTS_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many alerts in the last hour. If this is an emergency, call your local emergency number now.",
        )


@router.post("/emergency/notify", response_model=schemas.EmergencyNotifyOut)
def notify_emergency_contact(
    payload: schemas.EmergencyNotifyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    require_patient_role(current_user)
    contact = db.query(models.EmergencyContact).filter(models.EmergencyContact.user_id == current_user.id).first()
    if contact is None:
        # The frontend keys off this exact code to send the patient to the intake page.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no_contact")
    _check_rate_limit(db, current_user.id)

    now = models.utc_now()
    where = ", ".join(x for x in (current_user.city, current_user.country) if x) or "not on file"
    lines = [
        f"Hi {contact.name},",
        "",
        f"{current_user.name} pressed the emergency button in SehatAI at {now.strftime('%Y-%m-%d %H:%M')} UTC "
        "and asked us to let you know.",
        f"Their location on file: {where}.",
    ]
    if payload.trigger == "triage" and payload.category:
        lines.append(f"The assistant flagged this as: {payload.category}.")
    lines += [
        "",
        "Please check on them right now. If you can't reach them, call your local emergency number.",
        "",
        "This message contains no medical details.",
    ]
    result, error = notifier.send_email(
        contact.email, f"Urgent: {current_user.name} may need help", "\n".join(lines)
    )

    db.add(
        models.EmergencyAlert(
            patient_id=current_user.id,
            contact_email=contact.email,
            trigger=payload.trigger,
            category=payload.category,
            status=result,
            error=error,
        )
    )
    db.commit()

    if result == notifier.SENT:
        detail = f"We emailed {contact.name}. If this is an emergency, also call your local emergency number."
    elif result == notifier.DRY_RUN:
        detail = (
            "Email isn't set up on this server yet, so NO message was sent. "
            "Please call your contact or your local emergency number directly."
        )
    else:
        detail = (
            f"We couldn't send the email to {contact.name}. "
            "Please call them or your local emergency number directly."
        )
    return schemas.EmergencyNotifyOut(status=result, contact_name=contact.name, detail=detail)
