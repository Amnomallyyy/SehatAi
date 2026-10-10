"""
Shared, non-auth helper functions used by multiple routers: fetching a
Conversation/Report or 404ing, checking participant-hood, and normalizing
client-supplied timestamps for the polling endpoints.
"""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from . import models

UPLOAD_DIR = Path(__file__).resolve().parent / "static" / "uploads"
AVATAR_DIR = Path(__file__).resolve().parent / "static" / "avatars"
LAB_REPORT_STAGING_DIR = Path(__file__).resolve().parent / "static" / "lab_report_staging"


def get_or_create_sehatai_patient_id(user: models.User, db: Session) -> uuid.UUID:
    """Fills in User.sehatai_patient_id the first time a patient needs it
    (opening the AI Assistant tab, or uploading a lab report) -- see the
    doc comment on that column in models.py for why this is lazy rather
    than matched at signup: SehatAI's `patients` table has no email column
    to match an existing row against, so the only correct move is to
    create a fresh one. Idempotent -- a second call for the same user just
    returns the id already stored.

    Requires the caller to already be running against the shared Postgres
    database (see models.py's BRIDGE TABLES doc comment) -- this will
    raise a normal SQLAlchemy "no such table" error against CareLink's
    current SQLite database, on purpose, rather than silently no-op.
    """
    if user.role != models.UserRole.patient:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only a patient account has a SehatAI identity")

    if user.sehatai_patient_id is not None:
        return user.sehatai_patient_id

    # consented_at: FOUND LIVE -- DataFetch's own pipeline (pipeline.py's
    # "Step 6: Checking patient consent") refuses to process ANY document
    # for a patient row where this is null, entirely separate from the
    # password/auth gate. A patient reaching this code path is already
    # authenticated as themselves via CareLink and about to upload their
    # own file through their own session -- that action IS the consent;
    # there's no separate consent UI/flow for this bridge to collect.
    #
    # date_of_birth/sex: seeded from whatever's already on the CareLink
    # User row (null if the patient hasn't filled in their Profile page
    # yet) -- see sync_sehatai_profile below for the other half of this
    # (a LATER profile edit, after the bridge already exists).
    patient = models.Patient(
        name=user.name,
        date_of_birth=user.date_of_birth,
        sex=user.sex,
        consented_at=datetime.now(timezone.utc),
    )
    db.add(patient)
    db.flush()  # assigns patient.id without committing yet

    user.sehatai_patient_id = patient.id
    db.commit()
    db.refresh(user)
    return user.sehatai_patient_id


def sync_sehatai_profile(user: "models.User", db: Session) -> None:
    """Pushes User.date_of_birth/sex onto SehatAI's own `patients` row --
    called from routers/users.py's PATCH /users/me right after a patient
    edits their profile, so the symptom-triage bot's age/sex resolution
    (Patientprofile.js, read fresh every turn) is live for that patient's
    very next chat message with zero extra plumbing (see architecture doc:
    "Age/sex are patient-scoped ... fetched fresh every turn").

    No-ops if this patient hasn't opened the AI Assistant tab yet
    (sehatai_patient_id still null) -- get_or_create_sehatai_patient_id
    above already seeds the Patient row from the User's CURRENT
    date_of_birth/sex the first time it runs, so there's nothing stale
    to fix here; this only matters for a patient who bridged BEFORE
    filling in DOB/sex, or who edits it again afterwards.
    """
    if user.sehatai_patient_id is None:
        return

    patient = db.query(models.Patient).filter(models.Patient.id == user.sehatai_patient_id).first()
    if patient is None:
        return

    patient.date_of_birth = user.date_of_birth
    patient.sex = user.sex
    db.commit()


def looks_like_pdf_bytes(contents: bytes) -> bool:
    """Real PDFs start with "%PDF-" (a few writers prepend whitespace or a
    BOM, hence the small search window). The client-supplied content type
    and filename are just labels -- this is what actually stops arbitrary
    files from being stored and later served back as application/pdf."""
    return b"%PDF-" in contents[:1024]


# Magic numbers for the avatar formats accepted in routers/users.py.
_IMAGE_SIGNATURES = {
    "jpg": b"\xff\xd8\xff",
    "png": b"\x89PNG\r\n\x1a\n",
}


def looks_like_image_bytes(contents: bytes, ext: str) -> bool:
    if ext == "webp":
        return contents[:4] == b"RIFF" and contents[8:12] == b"WEBP"
    signature = _IMAGE_SIGNATURES.get(ext)
    return signature is not None and contents.startswith(signature)


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


def ensure_report_viewer(db: Session, report: models.Report, user: models.User) -> None:
    """Read-only access to a conversation report: either participant, or
    any doctor with an accepted connection to the report's patient (a
    connected doctor sees all of the patient's reports, not only the ones
    shared in their own conversation)."""
    if user.id in (report.conversation.patient_id, report.conversation.doctor_id):
        return
    if user.role == models.UserRole.doctor and has_accepted_connection(db, report.patient_id, user.id):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this patient")


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


def resolve_structured_patient(
    target_user_id: int, current_user: models.User, db: Session, allow_missing: bool = False
) -> Optional[uuid.UUID]:
    """Resolves a CareLink `patient_id: int` (never a raw SehatAI UUID --
    see routers/structured_reports.py's module docstring for why the
    client must never send one directly) to the shared `patients.id` UUID
    that DataFetch's extracted_data/documents tables key on, for a GET
    request specifically.

    Deliberately does NOT call get_or_create_sehatai_patient_id: that
    function WRITES a new Patient row as a side effect, which is correct
    for an action that's about to create data (opening the AI Assistant,
    uploading a lab report) but wrong for a plain read -- a patient who's
    never touched either of those features should see "no data yet", not
    silently get an empty Patient row created just by loading the Reports
    page.

    Patient: may only resolve themselves. Doctor: an accepted connection is
    enough -- a connected doctor sees ALL of the patient's lab documents
    (uploaded on the patient's own side or in chat). The separate reports-
    access grant is no longer required here.

    allow_missing=True returns None (instead of 404) for a patient who has
    never uploaded anything, so list endpoints can answer "no data yet"
    with an empty list.
    """
    if current_user.role == models.UserRole.patient:
        if current_user.id != target_user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own lab data")
        target_user = current_user
    else:
        target_user = db.query(models.User).filter(models.User.id == target_user_id).first()
        if target_user is None or target_user.role != models.UserRole.patient:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
        if not has_accepted_connection(db, patient_id=target_user_id, doctor_id=current_user.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this patient")

    if target_user.sehatai_patient_id is None:
        if allow_missing:
            return None
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No structured lab data yet")
    return target_user.sehatai_patient_id


def list_granted_patients_for_doctor(db: Session, doctor_id: int) -> list:
    """Patients with an accepted connection to this doctor -- the same gate
    resolve_structured_patient checks per-patient, applied as one query so
    the doctor's cross-patient notification feed (GET /structured/notifications)
    doesn't need a round trip per connected patient."""
    accepted_patient_ids = {
        c.patient_id
        for c in db.query(models.Connection).filter(
            models.Connection.doctor_id == doctor_id,
            models.Connection.status == models.ConnectionStatus.accepted,
        )
    }
    if not accepted_patient_ids:
        return []
    return db.query(models.User).filter(models.User.id.in_(accepted_patient_ids)).all()


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
