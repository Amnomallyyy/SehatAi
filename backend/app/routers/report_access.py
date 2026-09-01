"""
Reports-access grants: a patient's explicit, revocable permission letting a
specific connected doctor view the patient's full cross-conversation Reports/
Prescriptions history (the dedicated Reports tab). This is deliberately a
simple grant/revoke flag, not a second handshake like Connection -- a
Connection already requires mutual opt-in for the relationship to exist at
all, so layering a second request/accept flow on top of that would be
unneeded ceremony. There's no "doctor requests access" endpoint; a doctor
has to ask the patient out-of-band, the same spirit as Connections being
initiated by already knowing the other party's email.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import has_accepted_connection
from ..security import get_current_user, require_patient_role

router = APIRouter(prefix="/reports-access", tags=["reports-access"])


@router.post("/grant", response_model=schemas.ReportAccessGrantOut, status_code=status.HTTP_200_OK)
def grant_reports_access(
    payload: schemas.ReportAccessGrantCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Patient-only. Get-or-create + flip-to-granted, mirroring
    POST /connections' idempotent get-or-create style: calling this twice
    for the same doctor doesn't create a duplicate row."""
    require_patient_role(current_user)

    doctor = db.query(models.User).filter(models.User.id == payload.doctor_id).first()
    if doctor is None or doctor.role != models.UserRole.doctor:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="doctor_id must refer to an existing doctor")

    if not has_accepted_connection(db, current_user.id, payload.doctor_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must have an accepted connection with this doctor before granting reports access",
        )

    grant = (
        db.query(models.ReportAccessGrant)
        .filter(
            models.ReportAccessGrant.patient_id == current_user.id,
            models.ReportAccessGrant.doctor_id == payload.doctor_id,
        )
        .first()
    )
    if grant is None:
        grant = models.ReportAccessGrant(
            patient_id=current_user.id,
            doctor_id=payload.doctor_id,
            status=models.ReportAccessStatus.granted,
        )
        db.add(grant)
    else:
        grant.status = models.ReportAccessStatus.granted
    db.commit()
    db.refresh(grant)
    return grant


@router.post("/{grant_id}/revoke", response_model=schemas.ReportAccessGrantOut)
def revoke_reports_access(
    grant_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Patient-only, and only the patient who owns the grant."""
    require_patient_role(current_user)

    grant = db.query(models.ReportAccessGrant).filter(models.ReportAccessGrant.id == grant_id).first()
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    if grant.patient_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This is not your grant to revoke")

    grant.status = models.ReportAccessStatus.revoked
    db.commit()
    db.refresh(grant)
    return grant


@router.get("", response_model=List[schemas.ReportAccessGrantOut])
def list_reports_access(
    status_filter: Optional[models.ReportAccessStatus] = Query(
        None, alias="status", description="Optional: 'granted' or 'revoked'"
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Either side: a patient sees their own grants (to any doctor); a
    doctor sees grants naming them (from any patient)."""
    query = db.query(models.ReportAccessGrant).filter(
        or_(
            models.ReportAccessGrant.patient_id == current_user.id,
            models.ReportAccessGrant.doctor_id == current_user.id,
        )
    )
    if status_filter is not None:
        query = query.filter(models.ReportAccessGrant.status == status_filter)
    return query.order_by(models.ReportAccessGrant.updated_at.desc()).all()
