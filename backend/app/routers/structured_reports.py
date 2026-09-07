"""
structured_reports.py -- read API over DataFetch's structured extraction
pipeline (documents/extracted_data), Phase 1 of the reports-section
rebuild (design doc: reports-rebuild-plan, 2026-09-05).

Two separate report/document systems exist in this repo:
  - CareLink's own `reports` table (routers/reports.py) -- a PDF shared
    inside one doctor-patient conversation thread, with a free-text AI
    summary. That router is UNTOUCHED by this file.
  - DataFetch's `documents`/`extracted_data` tables -- structured
    per-marker lab results (test_name/value/unit/normal_range/flag),
    patient-scoped (keyed on the shared `patients.id` UUID), not
    conversation-scoped. This file is the first-ever read API over it;
    until now only a one-way upload bridge existed (routers/lab_reports.py).

Query implementation is plain SQLAlchemy against the same pooled Postgres
session every other router uses, NOT an import of datafetch/history.py --
that module builds its own supabase-py client per call and reads
SUPABASE_KEY, which backend/.env's unified env doesn't define under that
name (see lab_reports.py's own doc comment on this exact mismatch). It
remains the reference implementation for the CLI/DietBot side; the
query semantics here (latest-per-test_name, ordered by document_date)
are deliberately kept identical to it and must not drift.

Auth: the client NEVER sends a raw SehatAI `patients.id` UUID -- every
route takes a CareLink `patient_id: int` and resolves it server-side via
dependencies.resolve_structured_patient, which enforces the same
connection+reports-access-grant gate GET /reports?patient_id= already
uses. Accepting a raw UUID would let any authenticated user enumerate
the shared `patients` table, which other services (SehatAI's Node
service, DietBot) also read.
"""
import os
import re
import uuid
from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import list_granted_patients_for_doctor, resolve_structured_patient
from ..marker_names import normalize_marker
from ..security import get_current_user
from ..storage import download_file

router = APIRouter(prefix="/structured", tags=["structured-reports"])

_ABNORMAL_FLAGS = {"high", "low", "abnormal", "critical"}

# Handles the real shapes seen in extracted_data.normal_range: "13.0-17.0",
# "4500 - 13500", "150,000 - 450,000", "< 0.01" / "≤ 69" / "&lt;69",
# "> 5". Anything else (e.g. "Negative: ≤69", "90/60-120/80") returns
# (None, None) on purpose -- the caller falls back to plain text, not a
# guess.
_RANGE_PAIR_RE = re.compile(r"^\s*([\d,]+\.?\d*)\s*-\s*([\d,]+\.?\d*)\s*$")
_RANGE_LT_RE = re.compile(r"^\s*[<≤]\s*([\d,]+\.?\d*)\s*$")
_RANGE_GT_RE = re.compile(r"^\s*[>≥]\s*([\d,]+\.?\d*)\s*$")


def _parse_normal_range(text: Optional[str]):
    """Returns (low, high), either possibly None. Never raises -- a range
    string this can't parse just means the UI shows raw text instead of a
    bar, not a 500."""
    if not text:
        return (None, None)
    t = text.strip()
    m = _RANGE_PAIR_RE.match(t)
    if m:
        try:
            return (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
        except ValueError:
            return (None, None)
    m = _RANGE_LT_RE.match(t)
    if m:
        try:
            return (None, float(m.group(1).replace(",", "")))
        except ValueError:
            return (None, None)
    m = _RANGE_GT_RE.match(t)
    if m:
        try:
            return (float(m.group(1).replace(",", "")), None)
        except ValueError:
            return (None, None)
    return (None, None)


def _is_abnormal(flag: Optional[str]) -> bool:
    return bool(flag) and flag.strip().casefold() in _ABNORMAL_FLAGS


def _document_or_404(db: Session, document_id: str) -> models.Document:
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    doc = db.query(models.Document).filter(models.Document.id == doc_uuid).first()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


def _ensure_document_owned_by(doc: models.Document, resolved_patient_uuid: uuid.UUID) -> None:
    if doc.patient_id != resolved_patient_uuid:
        # Same response as "not found" -- a 403 here would confirm the
        # document id exists and just belongs to someone else, which is
        # itself information leakage for medical records.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


def _latest_previous_by_marker(
    db: Session, patient_uuid: uuid.UUID, before_date: Optional[date]
) -> Dict[str, models.ExtractedData]:
    """One query, reduced in Python: for this patient, the most recent
    extracted_data row per normalized marker name, strictly before
    before_date. Used for the marker table's delta-vs-previous column.
    Empty dict (not an error) when before_date is None -- a document with
    no date has no well-defined "previous"."""
    if before_date is None:
        return {}
    rows = (
        db.query(models.ExtractedData)
        .filter(
            models.ExtractedData.patient_id == patient_uuid,
            models.ExtractedData.document_date < before_date,
        )
        .all()
    )
    latest: Dict[str, models.ExtractedData] = {}
    for row in rows:
        key = normalize_marker(row.test_name)
        existing = latest.get(key)
        if existing is None or (row.document_date or date.min) > (existing.document_date or date.min):
            latest[key] = row
    return latest


def _latest_verification(db: Session, document_id: uuid.UUID) -> Optional[models.ExtractionVerification]:
    return (
        db.query(models.ExtractionVerification)
        .filter(models.ExtractionVerification.document_id == document_id)
        .order_by(models.ExtractionVerification.started_at.desc())
        .first()
    )


def _build_marker_out(
    row: models.ExtractedData,
    previous_by_marker: Dict[str, models.ExtractedData],
    findings_by_marker: Optional[Dict[str, models.ExtractionVerificationFinding]] = None,
) -> schemas.MarkerOut:
    normalized = normalize_marker(row.test_name)
    ref_low, ref_high = _parse_normal_range(row.normal_range)
    value_numeric = float(row.value_numeric) if row.value_numeric is not None else None

    delta_value = None
    delta_since = None
    prev = previous_by_marker.get(normalized)
    if prev is not None and prev.value_numeric is not None and value_numeric is not None:
        delta_value = value_numeric - float(prev.value_numeric)
        delta_since = prev.document_date

    finding = (findings_by_marker or {}).get(normalized)

    return schemas.MarkerOut(
        test_name=row.test_name,
        normalized_name=normalized,
        value=row.value,
        value_numeric=value_numeric,
        unit=row.unit,
        normal_range=row.normal_range,
        ref_low=ref_low,
        ref_high=ref_high,
        flag=row.flag,
        operator=row.operator,
        delta_value=delta_value,
        delta_since=delta_since,
        is_abnormal=_is_abnormal(row.flag),
        needs_review=(finding is not None and not finding.agrees),
        confidence=float(row.ocr_confidence) if row.ocr_confidence is not None else None,
    )


def _document_summary(
    db: Session,
    doc: models.Document,
    markers: Optional[List[models.ExtractedData]] = None,
    retracted: Optional[bool] = None,
) -> schemas.StructuredDocumentSummaryOut:
    if markers is None:
        markers = db.query(models.ExtractedData).filter(models.ExtractedData.document_id == doc.id).all()
    if retracted is None:
        retracted = (
            db.query(models.DocumentNote)
            .filter(models.DocumentNote.document_id == doc.id, models.DocumentNote.retracted.is_(True))
            .first()
            is not None
        )
    abnormal_count = sum(1 for m in markers if _is_abnormal(m.flag))
    numeric_vals = [float(m.value_numeric) for m in markers if m.value_numeric is not None]
    spark: List[float] = []
    if numeric_vals:
        lo, hi = min(numeric_vals), max(numeric_vals)
        span = (hi - lo) or 1.0
        spark = [round((v - lo) / span, 4) for v in numeric_vals]
    return schemas.StructuredDocumentSummaryOut(
        document_id=str(doc.id),
        category=doc.category,
        document_date=doc.document_date,
        uploaded_at=doc.uploaded_at,
        status=doc.status,
        original_filename=doc.original_filename,
        marker_count=len(markers),
        abnormal_count=abnormal_count,
        spark=spark,
        linked_report_id=None,  # reports.sehatai_document_id doesn't exist yet -- Phase 3
        has_source_file=bool(doc.file_path),
        doctor_reviewed=bool(doc.doctor_reviewed),
        retracted=retracted,
    )


@router.get("/documents", response_model=List[schemas.StructuredDocumentSummaryOut])
def list_structured_documents(
    patient_id: int = Query(..., description="CareLink user id (patient)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    docs = (
        db.query(models.Document)
        .filter(models.Document.patient_id == patient_uuid)
        .order_by(models.Document.document_date.desc().nullslast(), models.Document.uploaded_at.desc())
        .all()
    )
    if not docs:
        return []
    doc_ids = [d.id for d in docs]
    all_markers = (
        db.query(models.ExtractedData)
        .filter(models.ExtractedData.document_id.in_(doc_ids))
        .all()
    )
    markers_by_doc: Dict[uuid.UUID, List[models.ExtractedData]] = {}
    for m in all_markers:
        markers_by_doc.setdefault(m.document_id, []).append(m)
    retracted_doc_ids = {
        n.document_id
        for n in db.query(models.DocumentNote.document_id)
        .filter(models.DocumentNote.document_id.in_(doc_ids), models.DocumentNote.retracted.is_(True))
        .all()
    }
    return [
        _document_summary(db, d, markers_by_doc.get(d.id, []), retracted=d.id in retracted_doc_ids)
        for d in docs
    ]


@router.get("/documents/{document_id}", response_model=schemas.StructuredDocumentDetailOut)
def get_structured_document(
    document_id: str,
    patient_id: int = Query(..., description="CareLink user id (patient) -- required so a doctor's access can be checked"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)

    marker_rows = (
        db.query(models.ExtractedData)
        .filter(models.ExtractedData.document_id == doc.id)
        .order_by(models.ExtractedData.test_name)
        .all()
    )
    previous_by_marker = _latest_previous_by_marker(db, patient_uuid, doc.document_date)

    verification = _latest_verification(db, doc.id)
    findings_by_marker: Dict[str, models.ExtractionVerificationFinding] = {}
    if verification is not None and verification.status == "complete":
        findings_by_marker = {f.normalized_marker_name: f for f in verification.findings}

    markers = [_build_marker_out(row, previous_by_marker, findings_by_marker) for row in marker_rows]

    high_confidence = sum(1 for m in markers if m.confidence is not None and m.confidence >= 0.95)
    audit = schemas.ExtractionAuditOut(
        markers_found=len(markers),
        high_confidence=high_confidence,
        needs_review=sum(1 for m in markers if m.needs_review),
        verification_status=(verification.status if verification is not None else ("no_source" if not doc.file_path else "not_run")),
        verified_at=verification.completed_at if verification is not None else None,
        model=verification.model_used if verification is not None else None,
        error=verification.error if verification is not None else None,
    )

    summary = _document_summary(db, doc, marker_rows)
    # Prefer an abnormal marker as the default trend focus -- more likely
    # to be the thing a patient/doctor actually wants to see the trace of.
    default_trend_marker = next((m.normalized_name for m in markers if m.is_abnormal), None) or (
        markers[0].normalized_name if markers else None
    )

    return schemas.StructuredDocumentDetailOut(
        **summary.model_dump(),
        markers=markers,
        audit=audit,
        default_trend_marker=default_trend_marker,
    )


@router.get("/documents/{document_id}/verification", response_model=Optional[schemas.VerificationOut])
def get_document_verification(
    document_id: str,
    patient_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Detail behind the Extraction Audit panel's summary numbers -- per
    marker, what the independent second model read vs. what's stored.
    Returns null (not 404) when no verification has run yet for this
    document, e.g. it predates Phase 2, or the background task hasn't
    finished -- the frontend already handles a null/pending audit state."""
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)

    verification = _latest_verification(db, doc.id)
    if verification is None:
        return None
    return schemas.VerificationOut(
        status=verification.status,
        model_used=verification.model_used,
        agreement_count=verification.agreement_count,
        disagreement_count=verification.disagreement_count,
        error=verification.error,
        started_at=verification.started_at,
        completed_at=verification.completed_at,
        findings=[
            schemas.VerificationFindingOut(
                normalized_marker_name=f.normalized_marker_name,
                primary_value=f.primary_value,
                primary_unit=f.primary_unit,
                verified_value=f.verified_value,
                verified_unit=f.verified_unit,
                agrees=f.agrees,
            )
            for f in verification.findings
        ],
    )


@router.get("/documents/{document_id}/markers/{test_name}/history", response_model=List[schemas.MarkerHistoryPointOut])
def get_marker_history(
    document_id: str,
    test_name: str,
    patient_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """History for one NORMALIZED marker name across every document this
    patient has, ascending by date -- not just this one document. The
    document_id in the path is used only to authorize the request (must
    be a document this patient owns); the returned series spans all of
    their documents, which is what makes it a trend."""
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)

    target = normalize_marker(test_name)
    rows = (
        db.query(models.ExtractedData)
        .filter(models.ExtractedData.patient_id == patient_uuid)
        .all()
    )
    matching = [r for r in rows if normalize_marker(r.test_name) == target]
    matching.sort(key=lambda r: r.document_date or date.min)

    return [
        schemas.MarkerHistoryPointOut(
            document_id=str(r.document_id),
            document_date=r.document_date,
            value=r.value,
            value_numeric=float(r.value_numeric) if r.value_numeric is not None else None,
            unit=r.unit,
            flag=r.flag,
        )
        for r in matching
    ]


@router.get("/documents/{document_id}/file")
def download_structured_document_file(
    document_id: str,
    patient_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Streams the source PDF/image bytes -- deliberately authenticated,
    same reasoning as routers/reports.py's download_report_file: these are
    medical documents, so no public/static URL, no bare <a href>/<img src>
    from the frontend (browsers don't attach custom headers to plain
    navigations -- the frontend must fetch() + blob this, matching the
    existing downloadPdf() pattern for CareLink reports).

    Reads via a service-role Supabase client rather than returning
    documents.file_url directly to the browser: that URL is shaped like a
    public Storage URL but the bucket is (correctly) private, so it 400s
    for anyone who isn't already holding the service key -- see
    _get_storage_client's own doc comment for why proxying here, after
    this route's own auth check, is the right fix rather than making the
    bucket public.
    """
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)

    if not doc.file_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No source file stored for this document")

    try:
        file_bytes = download_file(doc.file_url)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not fetch file from storage: {exc}")

    filename = os.path.basename(doc.file_url.split("?")[0])
    media_type = doc.mime_type or ("application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream")
    return Response(
        content=file_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/notifications", response_model=List[schemas.DoctorNotificationOut])
def list_doctor_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Doctor-side upload notification: every unreviewed lab document
    belonging to a patient who has both accepted this doctor's connection
    AND explicitly granted reports access (the same double gate every other
    structured-reports route enforces per-patient -- see
    resolve_structured_patient). Feeds the Reports nav badge.

    Returns an empty list for a patient caller rather than 403ing -- this
    is a doctor-only feed the frontend simply never renders a badge from
    for a patient session, not a resource a patient could leak anything by
    calling.
    """
    if current_user.role != models.UserRole.doctor:
        return []
    patients = list_granted_patients_for_doctor(db, current_user.id)
    patients_by_sehatai_id = {p.sehatai_patient_id: p for p in patients if p.sehatai_patient_id is not None}
    if not patients_by_sehatai_id:
        return []
    docs = (
        db.query(models.Document)
        .filter(
            models.Document.patient_id.in_(patients_by_sehatai_id.keys()),
            (models.Document.doctor_reviewed.is_(False)) | (models.Document.doctor_reviewed.is_(None)),
        )
        .order_by(models.Document.uploaded_at.desc())
        .all()
    )
    return [
        schemas.DoctorNotificationOut(
            document_id=str(d.id),
            patient_id=patients_by_sehatai_id[d.patient_id].id,
            patient_name=patients_by_sehatai_id[d.patient_id].name,
            category=d.category,
            document_date=d.document_date,
            uploaded_at=d.uploaded_at,
        )
        for d in docs
    ]


@router.post("/documents/{document_id}/review", response_model=schemas.StructuredDocumentSummaryOut)
def mark_structured_document_reviewed(
    document_id: str,
    patient_id: int = Query(..., description="CareLink user id (patient)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Doctor-only, mirroring reports.py's existing 'Mark reviewed' action
    on CareLink's own Report model (also doctor-only there) -- a patient
    doesn't review their own labs. Idempotent: re-marking an already-
    reviewed document just returns the same state, no error."""
    if current_user.role != models.UserRole.doctor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a doctor can mark a document reviewed")
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)

    doc.doctor_reviewed = True
    db.commit()
    db.refresh(doc)
    return _document_summary(db, doc)


def _note_out(note: models.DocumentNote) -> schemas.DocumentNoteOut:
    return schemas.DocumentNoteOut(
        id=note.id,
        document_id=str(note.document_id),
        doctor_id=note.doctor_id,
        doctor_name=note.doctor.name,
        content=note.content,
        retracted=note.retracted,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


@router.get("/documents/{document_id}/notes", response_model=List[schemas.DocumentNoteOut])
def list_document_notes(
    document_id: str,
    patient_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Visible to anyone already authorized to view the document itself
    (patient or a doctor with a reports-access grant) -- same gate as
    every other structured-reports read route, not a doctor-only view.
    A patient seeing their doctor's note on a report is the point."""
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)
    notes = (
        db.query(models.DocumentNote)
        .filter(models.DocumentNote.document_id == doc.id)
        .order_by(models.DocumentNote.updated_at.desc())
        .all()
    )
    return [_note_out(n) for n in notes]


@router.put("/documents/{document_id}/notes", response_model=Optional[schemas.DocumentNoteOut])
def upsert_document_note(
    document_id: str,
    payload: schemas.DocumentNoteIn,
    patient_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Doctor-only, one row per (document, doctor) -- see DocumentNote's
    doc comment. Submitting blank/whitespace content deletes the doctor's
    existing note instead of storing an empty one, which is how a doctor
    clears a note they no longer want attached (this field is optional by
    design, not just optional to fill in the first time).

    Retracting (payload.retracted=True) requires non-empty content -- a
    retraction with no explanation of what's actually correct isn't
    useful to whoever reads it next (the patient, or another doctor)."""
    if current_user.role != models.UserRole.doctor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a doctor can add a note")
    patient_uuid = resolve_structured_patient(patient_id, current_user, db)
    doc = _document_or_404(db, document_id)
    _ensure_document_owned_by(doc, patient_uuid)

    content = (payload.content or "").strip()
    if payload.retracted and not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A retraction needs a note explaining what's correct")
    note = (
        db.query(models.DocumentNote)
        .filter(models.DocumentNote.document_id == doc.id, models.DocumentNote.doctor_id == current_user.id)
        .first()
    )
    if not content:
        if note:
            db.delete(note)
            db.commit()
        return None
    if note:
        note.content = content
        note.retracted = payload.retracted
    else:
        note = models.DocumentNote(document_id=doc.id, doctor_id=current_user.id, content=content, retracted=payload.retracted)
        db.add(note)
    db.commit()
    db.refresh(note)
    return _note_out(note)
