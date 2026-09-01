from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import ensure_conversation_participant, get_conversation_or_404, normalize_to_naive_utc
from ..security import get_current_user

router = APIRouter(tags=["messages"])


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=schemas.MessageOut,
    status_code=status.HTTP_201_CREATED,
)
def send_message(
    conversation_id: int,
    payload: schemas.MessageCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)

    message = models.Message(conversation_id=conversation.id, sender_id=current_user.id, text=payload.text)
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


@router.get("/conversations/{conversation_id}/messages", response_model=List[schemas.MessageOut])
def list_messages(
    conversation_id: int,
    after_id: Optional[int] = Query(default=None, description="Only messages with id greater than this (recommended for polling)"),
    since: Optional[datetime] = Query(default=None, description="Only messages with timestamp after this, ISO 8601"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Chronological (oldest first), like any chat transcript. Supports
    both after_id and since for polling -- after_id is recommended: it's a
    monotonic cursor, so it can't miss or double-return a message the way
    timestamp-based polling can if two messages land in the same instant.
    Both may be supplied together (applied as AND); neither is required."""
    conversation = get_conversation_or_404(db, conversation_id)
    ensure_conversation_participant(conversation, current_user)

    query = db.query(models.Message).filter(models.Message.conversation_id == conversation_id)
    if after_id is not None:
        query = query.filter(models.Message.id > after_id)
    since_normalized = normalize_to_naive_utc(since)
    if since_normalized is not None:
        query = query.filter(models.Message.timestamp > since_normalized)

    return query.order_by(models.Message.id.asc()).limit(limit).all()
