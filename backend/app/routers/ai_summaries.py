from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..ai import generate_report_summary
from ..database import get_db
from ..dependencies import UPLOAD_DIR, ensure_report_participant, get_report_or_404
from ..security import get_current_user, require_doctor_role

router = APIRouter(tags=["ai-summaries"])


@router.post(
    "/reports/{report_id}/ai-summary",
    response_model=schemas.AISummaryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ai_summary(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Reads the report's PDF and asks Claude to analyze it -- no request
    body anymore, since there's nothing for the caller to supply. Creates a
    NEW AISummary row and repoints Report.ai_summary_id at it (older
    summaries are kept, not overwritten). Always advances the report to
    'awaiting_review', since a fresh summary means a doctor needs to look
    at it again.
    """
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)

    pdf_path = UPLOAD_DIR / report.pdf_path
    try:
        result = generate_report_summary(pdf_path)
    except Exception as exc:
        # Covers both a malformed AI response (ValueError from ai.py) and
        # any API-level failure (network, auth, rate limit) -- either way
        # the caller gets a clean error instead of a raw 500 traceback.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI summary generation failed: {exc}",
        )

    summary = models.AISummary(
        report_id=report.id,
        summary=result["summary"],
        key_findings=result["key_findings"],
        flagged_values=result["flagged_values"],
        recommendation=result["recommendation"],
    )
    db.add(summary)
    db.commit()
    db.refresh(summary)

    report.ai_summary_id = summary.id
    report.status = models.ReportStatus.awaiting_review
    db.commit()

    return summary


@router.patch("/reports/{report_id}/ai-summary", response_model=schemas.AISummaryOut)
def edit_current_ai_summary(
    report_id: int,
    payload: schemas.AISummaryEdit,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Corrects the CURRENT summary's fields in place -- does NOT create a
    new history row (unlike POST, which always does; this is a fix, not a
    regeneration). Doctor-only: this is clinical content a patient
    shouldn't be able to alter, e.g. to hide a flagged value from their own
    doctor. Only the fields included in the request body change."""
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)
    require_doctor_role(current_user)

    if report.ai_summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No AI summary exists yet to edit")

    summary = report.ai_summary
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(summary, field, value)
    db.commit()
    db.refresh(summary)
    return summary


@router.get("/reports/{report_id}/ai-summary", response_model=schemas.AISummaryOut)
def get_current_ai_summary(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)

    if report.ai_summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No AI summary has been generated for this report yet")
    return report.ai_summary


@router.get("/reports/{report_id}/ai-summary/history", response_model=List[schemas.AISummaryOut])
def get_ai_summary_history(
    report_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Every summary ever generated for this report, newest first --
    exists specifically to make good on the design intent behind AISummary
    being its own table (regeneration doesn't lose data)."""
    report = get_report_or_404(db, report_id)
    ensure_report_participant(report, current_user)

    summaries = (
        db.query(models.AISummary)
        .filter(models.AISummary.report_id == report_id)
        .order_by(models.AISummary.id.desc())
        .all()
    )
    return summaries
