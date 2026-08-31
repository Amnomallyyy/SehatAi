"""
Shared, non-auth helper functions used by multiple routers: fetching a
Conversation/Report or 404ing, checking participant-hood, and normalizing
client-supplied timestamps for the polling endpoints.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from . import models

UPLOAD_DIR = Path(__file__).resolve().parent / "static" / "uploads"
AVATAR_DIR = Path(__file__).resolve().parent / "static" / "avatars"


def get_conversation_or_404(db: Session, conversation_id: int) -> models.Conversation:
    conversation = db.query(models.Conversation).filter(models.Conversation.id == conversation_id).first()
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conversation


def ensure_conversation_participant(conversation: models.Conversation, user: models.User) -> None:
    if user.id not in (conversation.patient_id, conversation.doctor_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a participant in this conversation",
        )


def get_connection_or_404(db: Session, connection_id: int) -> models.Connection:
    connection = db.query(models.Connection).filter(models.Connection.id == connection_id).first()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return connection


def get_report_or_404(db: Session, report_id: int) -> models.Report:
    report = db.query(models.Report).filter(models.Report.id == report_id).first()
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report


def ensure_report_participant(report: models.Report, user: models.User) -> None:
    """A report belongs to a conversation; participant-hood is inherited
    from that conversation (same two people)."""
    ensure_conversation_participant(report.conversation, user)


def normalize_to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """`since` query params may arrive naive or timezone-aware -- e.g.
    JavaScript's `date.toISOString()` always appends "Z". Stored timestamps
    are naive UTC (see models.utc_now), so any aware input is converted to
    UTC and stripped of tzinfo before it's used in a filter. Without this,
    a `since` value with a "Z"/offset suffix would silently fail to match
    correctly against stored rows for polling clients written in JS.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def has_accepted_connection(db: Session, patient_id: int, doctor_id: int) -> bool:
    """The connection-authorization check repeated inline across users.py/
    reports.py/conversations.py, promoted here for new call sites
    (appointments, report-access grants) so it isn't inlined a fifth time."""
    return (
        db.query(models.Connection)
        .filter(
            models.Connection.patient_id == patient_id,
            models.Connection.doctor_id == doctor_id,
            models.Connection.status == models.ConnectionStatus.accepted,
        )
        .first()
        is not None
    )


def has_reports_access_grant(db: Session, patient_id: int, doctor_id: int) -> bool:
    return (
        db.query(models.ReportAccessGrant)
        .filter(
            models.ReportAccessGrant.patient_id == patient_id,
            models.ReportAccessGrant.doctor_id == doctor_id,
            models.ReportAccessGrant.status == models.ReportAccessStatus.granted,
        )
        .first()
        is not None
    )


def compute_active_reminder(scheduled_at: datetime) -> Optional[str]:
    """Pure function of "now" vs. an appointment's scheduled_at -- no stored
    dismissal/read state. Returns the closest matching threshold (a "2h"-out
    appointment is also technically within the 1d/3d windows, but only the
    most urgent one is reported)."""
    now = models.utc_now()
    delta = scheduled_at - now
    if delta.total_seconds() <= 0:
        return None
    if delta <= timedelta(hours=2):
        return "2h"
    if delta <= timedelta(days=1):
        return "1d"
    if delta <= timedelta(days=3):
        return "3d"
    return None
