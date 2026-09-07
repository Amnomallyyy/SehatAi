"""
The connection ("handshake") flow: one side requests a connection to the
other by email, the recipient accepts or rejects it, and everything else in
the app -- visibility in /users, and creating a Conversation -- is gated on
that connection reaching 'accepted'. See models.Connection's docstring for
the data-model reasoning.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import get_connection_or_404
from ..security import get_current_user, require_doctor_role

router = APIRouter(prefix="/connections", tags=["connections"])


def _serialize_connection(connection: models.Connection, current_user: models.User) -> schemas.ConnectionOut:
    """doctor_nickname is a private label the doctor sets for a patient --
    never shown to the patient side of the connection, so it's nulled out
    here before returning to anyone but that doctor."""
    out = schemas.ConnectionOut.model_validate(connection)
    if current_user.id != connection.doctor_id:
        out = out.model_copy(update={"doctor_nickname": None})
    return out


@router.post("", response_model=schemas.ConnectionOut, status_code=status.HTTP_200_OK)
def request_connection(
    payload: schemas.ConnectionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Send a connection request by email -- not by user_id, since the whole
    point is you can't browse/enumerate the other role to find an id. You're
    expected to already know the email (e.g. your actual doctor gave it to
    you), the same way you'd add a contact by email elsewhere.

    Always 200, mirroring POST /conversations' get-or-create convention:
    - No existing row -> a new 'pending' connection is created.
    - Existing 'pending' or 'accepted' row -> returned as-is (idempotent;
      calling this twice doesn't spam a new request).
    - Existing 'rejected' row -> flipped back to 'pending' with you as the
      new requester, so a declined request can be retried (e.g. after a
      typo) without leaving a second, confusing row behind.
    """
    target = db.query(models.User).filter(models.User.email == payload.email).first()
    if target is None:
        # Deliberately explicit (unlike /auth/login's same-message-either-way
        # pattern): this endpoint requires you to already be authenticated,
        # so it isn't useful for account enumeration the way a login
        # endpoint is, and a clear "no such user" is genuinely more useful
        # here since you're trying to add someone you believe has an
        # account. Flagged in API_CONTRACT.md as a deliberate tradeoff.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No user found with that email")
    if target.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't connect with yourself")
    if target.role == current_user.role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You can only connect with a user of the opposite role",
        )

    if current_user.role == models.UserRole.patient:
        patient_id, doctor_id = current_user.id, target.id
    else:
        patient_id, doctor_id = target.id, current_user.id

    existing = (
        db.query(models.Connection)
        .filter(models.Connection.patient_id == patient_id, models.Connection.doctor_id == doctor_id)
        .first()
    )
    if existing is not None:
        if existing.status == models.ConnectionStatus.rejected:
            existing.status = models.ConnectionStatus.pending
            existing.requested_by_id = current_user.id
            existing.responded_at = None
            db.commit()
            db.refresh(existing)
        return _serialize_connection(existing, current_user)

    connection = models.Connection(
        patient_id=patient_id,
        doctor_id=doctor_id,
        requested_by_id=current_user.id,
        status=models.ConnectionStatus.pending,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return _serialize_connection(connection, current_user)


@router.get("", response_model=List[schemas.ConnectionOut])
def list_my_connections(
    status_filter: Optional[models.ConnectionStatus] = Query(
        None, alias="status", description="Optional: 'pending', 'accepted', or 'rejected'"
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Every connection the caller is part of, in either direction and any
    status -- newest first. The frontend buckets these into "incoming
    requests" (status=pending, requested_by_id != me), "outgoing requests"
    (status=pending, requested_by_id == me), and "connected" (status=
    accepted) itself rather than this endpoint exposing three separate
    routes for what's really just one list plus client-side filtering.
    """
    query = db.query(models.Connection).filter(
        or_(models.Connection.patient_id == current_user.id, models.Connection.doctor_id == current_user.id)
    )
    if status_filter is not None:
        query = query.filter(models.Connection.status == status_filter)
    connections = query.order_by(models.Connection.created_at.desc()).all()
    return [_serialize_connection(c, current_user) for c in connections]


@router.patch("/{connection_id}", response_model=schemas.ConnectionOut)
def respond_to_connection(
    connection_id: int,
    payload: schemas.ConnectionRespond,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Accept or reject a pending request. Only the recipient can act on
    it -- whoever sent it (requested_by_id) is waiting, not deciding.
    Disconnecting an already-accepted connection isn't in scope here (that's
    a different action from responding to a request); this only moves a
    'pending' row to 'accepted' or 'rejected'.
    """
    connection = get_connection_or_404(db, connection_id)
    if current_user.id not in (connection.patient_id, connection.doctor_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this connection")
    if current_user.id == connection.requested_by_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can't accept or reject your own request"
        )
    if connection.status != models.ConnectionStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This connection is already '{connection.status.value}', not pending",
        )

    connection.status = models.ConnectionStatus(payload.status)
    connection.responded_at = models.utc_now()
    db.commit()
    db.refresh(connection)
    return _serialize_connection(connection, current_user)


@router.patch("/{connection_id}/nickname", response_model=schemas.ConnectionOut)
def set_patient_nickname(
    connection_id: int,
    payload: schemas.ConnectionNicknameUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """A private label the doctor sets for a connected patient, for the
    doctor's own convenience -- never visible to the patient. Doctor-only,
    and only on the doctor's own side of an accepted connection."""
    connection = get_connection_or_404(db, connection_id)
    require_doctor_role(current_user)
    if current_user.id != connection.doctor_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not the doctor on this connection")
    if connection.status != models.ConnectionStatus.accepted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This connection is not accepted yet")

    connection.doctor_nickname = payload.doctor_nickname
    db.commit()
    db.refresh(connection)
    return _serialize_connection(connection, current_user)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Two distinct real-world actions share this one DELETE, split by the
    row's current status:
      - 'pending': the SENDER withdraws a request they no longer want
        outstanding (e.g. a typo'd email) -- the recipient should use
        PATCH .../respond (accept/reject) instead, not this.
      - 'accepted': EITHER party ends the relationship. Any
        reports-access grant between the same pair is revoked at the
        same time -- otherwise a later re-connection would silently
        reinstate report sharing the patient never re-approved, since
        the grant row is keyed on (patient_id, doctor_id), not on this
        connection's id.
    A 'rejected' row may also be deleted by either party, for the same
    reason request_connection() is willing to flip one back to
    'pending' -- it's just cleanup, nothing sensitive hinges on it.

    Deleting (not soft-marking 'disconnected') is deliberate: it lets a
    fresh POST /connections after this start a clean new 'pending' row,
    exactly the same path as two people who were never connected --
    there is no distinct "we used to be connected" state anywhere else
    in the app to preserve.
    """
    connection = get_connection_or_404(db, connection_id)
    if current_user.id not in (connection.patient_id, connection.doctor_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this connection")

    if connection.status == models.ConnectionStatus.pending and current_user.id != connection.requested_by_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the sender can cancel a pending request -- accept or reject it instead",
        )

    if connection.status == models.ConnectionStatus.accepted:
        db.query(models.ReportAccessGrant).filter(
            models.ReportAccessGrant.patient_id == connection.patient_id,
            models.ReportAccessGrant.doctor_id == connection.doctor_id,
        ).update({"status": models.ReportAccessStatus.revoked})

    db.delete(connection)
    db.commit()
