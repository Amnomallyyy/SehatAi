from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import ensure_report_participant, get_report_or_404, normalize_to_naive_utc
from ..security import get_current_user, require_doctor_role

router = APIRouter(tags=["report-comments"])


@router.post(
    "/reports/{report_id}/comments",
    response_model=schemas.ReportCommentOut,
    status_code=status.HTTP_201_CREATED,
)
def add_report_comment(
    report_id: int,
    payload: schemas.ReportCommentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Doctor-only: this is the clinical-discussion thread on a report, and
    only the doctor may post into it now (patients keep read access via the
    GET route below, unaffected)."""
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)
    require_doctor_role(current_user)

    comment = models.ReportComment(report_id=report.id, sender_id=current_user.id, text=payload.text)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


@router.get("/reports/{report_id}/comments", response_model=List[schemas.ReportCommentOut])
def list_report_comments(
    report_id: int,
    after_id: Optional[int] = Query(default=None, description="Only comments with id greater than this (recommended for polling)"),
    since: Optional[datetime] = Query(default=None, description="Only comments with timestamp after this, ISO 8601"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Deliberately mirrors GET /conversations/{id}/messages -- same
    after_id/since/limit polling params, same ascending order -- since
    ReportComment mirrors Message's shape by design."""
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)

    query = db.query(models.ReportComment).filter(models.ReportComment.report_id == report_id)
    if after_id is not None:
        query = query.filter(models.ReportComment.id > after_id)
    since_normalized = normalize_to_naive_utc(since)
    if since_normalized is not None:
        query = query.filter(models.ReportComment.timestamp > since_normalized)

    return query.order_by(models.ReportComment.id.asc()).limit(limit).all()
