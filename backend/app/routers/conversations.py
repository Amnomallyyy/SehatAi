from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import ensure_conversation_participant, get_conversation_or_404
from ..security import get_current_user

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=schemas.ConversationOut, status_code=status.HTTP_200_OK)
def create_or_get_conversation(
    payload: schemas.ConversationCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Get-or-create, not create-only: calling this twice for the same
    patient/doctor pair returns the existing conversation instead of
    spawning a duplicate. Always 200 (whether found or newly created) so the
    frontend doesn't need to branch on status code -- see API_CONTRACT.md.
    """
    if payload.patient_id == payload.doctor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="patient_id and doctor_id must refer to different users",
        )
    if current_user.id not in (payload.patient_id, payload.doctor_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only create a conversation you are a part of",
        )

    # Role validity is business logic, not something a ForeignKey can check
    # (per the spec) -- enforced here explicitly.
    patient = db.query(models.User).filter(models.User.id == payload.patient_id).first()
    if patient is None or patient.role != models.UserRole.patient:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="patient_id must refer to an existing user with role 'patient'",
        )
    doctor = db.query(models.User).filter(models.User.id == payload.doctor_id).first()
    if doctor is None or doctor.role != models.UserRole.doctor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="doctor_id must refer to an existing user with role 'doctor'",
        )

    # New gate: a Conversation can only be created between a patient and
    # doctor who've gone through the connection handshake. This is the main
    # enforcement point for "patients must have a specific doctor" -- see
    # /connections and models.Connection.
    connection = (
        db.query(models.Connection)
        .filter(
            models.Connection.patient_id == payload.patient_id,
            models.Connection.doctor_id == payload.doctor_id,
            models.Connection.status == models.ConnectionStatus.accepted,
        )
        .first()
    )
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "No accepted connection exists between this patient and doctor. "
                "Send a connection request via POST /connections and have the other party accept it first."
            ),
        )

    existing = (
        db.query(models.Conversation)
        .filter(
            models.Conversation.patient_id == payload.patient_id,
            models.Conversation.doctor_id == payload.doctor_id,
        )
        .first()
    )
    if existing is not None:
        return existing

    conversation = models.Conversation(patient_id=payload.patient_id, doctor_id=payload.doctor_id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("", response_model=List[schemas.ConversationOut])
def list_my_conversations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Newest-first, for a chat-app-style conversation sidebar."""
    conversations = (
        db.query(models.Conversation)
        .filter(
            or_(
                models.Conversation.patient_id == current_user.id,
                models.Conversation.doctor_id == current_user.id,
            )
        )
        .order_by(models.Conversation.created_at.desc())
        .all()
    )
    return conversations


@router.get("/{conversation_id}", response_model=schemas.ConversationOut)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)
    return conversation
