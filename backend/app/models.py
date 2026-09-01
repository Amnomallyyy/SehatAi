"""
SQLAlchemy ORM models. Matches the finalized data model exactly:
User, Conversation, Message, Report, AISummary, ReportComment -- plus
Connection, added on top to gate patient/doctor visibility and conversation
creation behind a mutual opt-in (see the Connection class docstring).

A note on Report <-> AISummary:
Report.ai_summary_id points at the *current* summary, while AISummary.report_id
points at the report it was generated for (there can be many AISummary rows
per report over time, per the design notes on regeneration history). That
means the two tables logically reference each other. If AISummary.report_id
were *also* a real ForeignKey, SQLAlchemy could not compute a valid table
creation order (Report needs ai_summaries to exist first for its FK; AISummary
would need reports to exist first for its FK). Rather than reach for
SQLAlchemy's `use_alter`/`post_update` workaround for circular FKs (which is
finicky on SQLite, since SQLite has very limited ALTER TABLE support), this
model keeps AISummary.report_id as a plain indexed integer column with no DB
level constraint, and enforces that link in application code instead -- the
same pattern the spec already calls for with patient/doctor role validity.
Report.ai_summary_id remains a normal, fully-enforced ForeignKey.
"""
import enum
import uuid as uuid_module
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .database import Base


def utc_now() -> datetime:
    """Naive UTC datetime (tzinfo stripped) used for all timestamp defaults.

    SQLite has no real timezone-aware datetime type -- it just stores
    whatever text SQLAlchemy hands it. Mixing timezone-aware and naive
    datetimes against SQLite is a common source of subtle comparison bugs
    (e.g. filtering `WHERE timestamp > :since`). Storing everything as naive
    UTC, consistently, sidesteps that entirely. Every timestamp in this app
    is UTC by convention, documented in API_CONTRACT.md.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UserRole(str, enum.Enum):
    doctor = "doctor"
    patient = "patient"


class ReportStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    awaiting_review = "awaiting_review"
    reviewed = "reviewed"


class ConnectionStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class ReportAccessStatus(str, enum.Enum):
    granted = "granted"
    revoked = "revoked"


class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(
        Enum(UserRole, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
    )
    # Doctors only, by convention (not DB-enforced -- same "business logic,
    # not a constraint" philosophy as role validity elsewhere in this app).
    # Nullable so existing/new doctor accounts aren't blocked at signup;
    # "mandatory going forward" is enforced as a frontend nudge instead.
    specialization = Column(String(100), nullable=True)
    # Random on-disk filename, same pattern as Report.pdf_path -- never the
    # client's original filename (path traversal / collision safety).
    avatar_path = Column(String(500), nullable=True)
    # BRIDGE (see architecture doc §03/§04): links this CareLink account to
    # its row in SehatAI's own `patients` table -- a DIFFERENT database
    # today (SQLite here vs Supabase Postgres there), which is why this is
    # a plain UUID column, not a real ForeignKey, until CareLink's backend
    # actually points at the same Postgres database SehatAI/DataFetch use
    # (database.py still targets local SQLite as of this commit -- that
    # switch is a deliberate, separate, confirmed-with-the-team step, not
    # done here). Null for doctors, and null for a patient who hasn't yet
    # opened the AI Assistant tab or uploaded a lab report -- both routes
    # that need it call get_or_create_sehatai_patient_id (bridge.py) to
    # fill it in lazily on first use, since there's no shared email column
    # on SehatAI's `patients` table to auto-match an existing row by.
    sehatai_patient_id = Column(UUID(as_uuid=True), nullable=True, unique=True)

    @property
    def avatar_url(self) -> "str | None":
        """Derived, like Report.pdf_url -- an authenticated download path,
        not a public static URL. Exposed as a plain property (not a Column)
        so schemas.UserOut's from_attributes=True mapping picks it up via
        plain attribute access with no extra serialization helper needed."""
        return f"/users/{self.id}/avatar" if self.avatar_path else None

    conversations_as_patient = relationship(
        "Conversation", foreign_keys="Conversation.patient_id", back_populates="patient"
    )
    conversations_as_doctor = relationship(
        "Conversation", foreign_keys="Conversation.doctor_id", back_populates="doctor"
    )
    connections_as_patient = relationship(
        "Connection", foreign_keys="Connection.patient_id", back_populates="patient"
    )
    connections_as_doctor = relationship(
        "Connection", foreign_keys="Connection.doctor_id", back_populates="doctor"
    )
    connection_requests_sent = relationship(
        "Connection", foreign_keys="Connection.requested_by_id", back_populates="requested_by"
    )
    messages_sent = relationship("Message", back_populates="sender")
    reports = relationship("Report", foreign_keys="Report.patient_id", back_populates="patient")
    report_comments = relationship("ReportComment", back_populates="sender")


class Connection(Base):
    """A patient-doctor pairing, requested by one side (by email) and
    accepted or rejected by the other. Nothing else in the app treats two
    users as related to each other until a Connection between them reaches
    'accepted' -- that's what patient/doctor visibility and conversation
    creation are gated on.

    One row per (patient_id, doctor_id) pair (enforced by a unique
    constraint below): re-requesting after a rejection flips that same row
    back to 'pending' with a new requested_by rather than inserting a
    second row, so there's never more than one relationship state between
    a given pair to reason about.
    """

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("patient_id", "doctor_id", name="uq_connection_patient_doctor"),)

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Who sent the current request -- always the *other* party's id from
    # whoever needs to accept/reject it. Must be patient_id or doctor_id.
    requested_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(
        Enum(ConnectionStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=ConnectionStatus.pending,
        index=True,
    )
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)
    responded_at = Column(DateTime, nullable=True)
    # A private label the doctor sets for this patient, for the doctor's own
    # convenience -- never shown to the patient. Lives here rather than a
    # separate table since it's 1:1 with this already-unique
    # (patient_id, doctor_id) row.
    doctor_nickname = Column(String(100), nullable=True)

    patient = relationship("User", foreign_keys=[patient_id], back_populates="connections_as_patient")
    doctor = relationship("User", foreign_keys=[doctor_id], back_populates="connections_as_doctor")
    requested_by = relationship("User", foreign_keys=[requested_by_id], back_populates="connection_requests_sent")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    patient = relationship("User", foreign_keys=[patient_id], back_populates="conversations_as_patient")
    doctor = relationship("User", foreign_keys=[doctor_id], back_populates="conversations_as_doctor")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="conversation", cascade="all, delete-orphan")
    prescriptions = relationship("Prescription", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=utc_now, nullable=False, index=True)

    conversation = relationship("Conversation", back_populates="messages")
    sender = relationship("User", back_populates="messages_sent")


class AISummary(Base):
    __tablename__ = "ai_summaries"

    id = Column(Integer, primary_key=True, index=True)
    # Intentionally not a ForeignKey -- see module docstring.
    report_id = Column(Integer, nullable=False, index=True)
    summary = Column(Text, nullable=False)
    key_findings = Column(JSON, nullable=False, default=list)
    flagged_values = Column(JSON, nullable=False, default=list)
    recommendation = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    # Redundant with conversation.patient_id by design, for fast patient-scoped
    # lookups (e.g. "all reports for patient X") without joining conversations.
    patient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # What the uploader chose to call it (e.g. "Bloodwork - March 2026").
    # Separate from pdf_path, which stays a random on-disk filename for
    # security -- this is purely a display label.
    display_name = Column(String(200), nullable=False)
    pdf_path = Column(String(500), nullable=False)
    ai_summary_id = Column(Integer, ForeignKey("ai_summaries.id"), nullable=True)
    status = Column(
        Enum(ReportStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=ReportStatus.uploaded,
    )
    timestamp = Column(DateTime, default=utc_now, nullable=False, index=True)

    conversation = relationship("Conversation", back_populates="reports")
    patient = relationship("User", foreign_keys=[patient_id], back_populates="reports")
    ai_summary = relationship("AISummary", foreign_keys=[ai_summary_id])
    comments = relationship("ReportComment", back_populates="report", cascade="all, delete-orphan")


class ReportComment(Base):
    __tablename__ = "report_comments"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=utc_now, nullable=False, index=True)

    report = relationship("Report", back_populates="comments")
    sender = relationship("User", back_populates="report_comments")


class ReportAccessGrant(Base):
    """A patient's explicit, revocable grant letting a specific connected
    doctor view the patient's full cross-conversation Reports/Prescriptions
    history (the dedicated Reports tab) -- separate from Connection (which
    only gates messaging and the always-available inline 'View Report' link
    inside a shared conversation). One row per (patient_id, doctor_id) pair,
    flipped between granted/revoked rather than deleted, mirroring
    Connection's "one row per pair" design.
    """

    __tablename__ = "report_access_grants"
    __table_args__ = (UniqueConstraint("patient_id", "doctor_id", name="uq_report_access_patient_doctor"),)

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(
        Enum(ReportAccessStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=ReportAccessStatus.granted,
        index=True,
    )
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    patient = relationship("User", foreign_keys=[patient_id])
    doctor = relationship("User", foreign_keys=[doctor_id])


class Prescription(Base):
    """Deliberately separate from Report: no AI-summary wiring, no status
    stepper, doctor-only upload. Keeping this as its own table (rather than a
    'kind' flag on Report) means the existing, contract-locked Report/
    AISummary/ReportComment code never needs a conditional for "unless this
    is a prescription" -- there is simply no AI-summary route for this model
    at all, which is the whole enforcement mechanism.
    """

    __tablename__ = "prescriptions"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    # Redundant with conversation.patient_id, mirroring Report.patient_id --
    # fast patient-scoped lookups without joining conversations.
    patient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Always the uploader -- prescriptions are doctor-only, enforced in the router.
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    display_name = Column(String(200), nullable=False)
    pdf_path = Column(String(500), nullable=False)
    timestamp = Column(DateTime, default=utc_now, nullable=False, index=True)

    conversation = relationship("Conversation", back_populates="prescriptions")
    patient = relationship("User", foreign_keys=[patient_id])
    doctor = relationship("User", foreign_keys=[doctor_id])


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scheduled_at = Column(DateTime, nullable=False, index=True)
    reason = Column(String(500), nullable=True)
    status = Column(
        Enum(AppointmentStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=AppointmentStatus.scheduled,
        index=True,
    )
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utc_now, nullable=False)

    patient = relationship("User", foreign_keys=[patient_id])
    doctor = relationship("User", foreign_keys=[doctor_id])
    created_by = relationship("User", foreign_keys=[created_by_id])


# ============================================================
# BRIDGE TABLES -- owned by SehatAI / DataFetch, not by CareLink.
#
# These map tables that already exist (with real data) in the SAME
# Postgres database CareLink is meant to join -- see architecture doc
# §02/§03/§04. CareLink's backend does NOT create, own, or migrate these
# tables; they're declared here only so SQLAlchemy can read/write rows in
# them from the two new bridge routes (routers/sehatai_bridge.py,
# routers/lab_reports.py). None of these show up in CareLink's own
# `models.py`-driven migrations.
#
# PRECONDITION: none of this can actually run until database.py's
# connection string points at that same Postgres database instead of the
# local SQLite file it uses today. That swap is a deliberate, separate
# step -- confirm with the team before doing it, since it's a teammate's
# branch and (today) their own local data. Until then, importing these
# models is harmless (SQLAlchemy just doesn't try to create/touch tables
# it doesn't own), but any query against them will fail with "no such
# table" against the current SQLite database.
# ============================================================

class Patient(Base):
    """SehatAI's own `patients` table (auth.js / seedfakedata.js). No email
    column exists on it -- see the sehatai_patient_id doc comment on User
    above for why identity is bridged lazily, not matched at signup."""
    __tablename__ = "patients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid_module.uuid4)
    name = Column(String, nullable=True)
    date_of_birth = Column(DateTime, nullable=True)
    age = Column(Integer, nullable=True)
    sex = Column(String, nullable=True)
    consented_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    # CORRECTION (found live): an earlier pass called this dead code --
    # wrong, only checked SehatAI's own JS. DataFetch's own auth.py reads
    # AND requires it (authenticate_patient's bcrypt consent gate -- see
    # architecture doc §03/§05's corrected note). Leaving it unmapped here
    # meant routers/lab_reports.py's password-rotation write
    # (`patient_row.password_hash = ...`) was silently setting a plain,
    # untracked Python attribute instead of a real column -- db.commit()
    # never issued the UPDATE, so DataFetch's pipeline correctly (if
    # confusingly) reported "no password set" on every real attempt.
    password_hash = Column(String, nullable=True)


class PatientAPIToken(Base):
    """SehatAI's bearer-token table (auth.js). Tokens are stored as a
    SHA-256 hash, never plaintext -- see hash_sehatai_token in
    routers/sehatai_bridge.py, which MUST use the identical hashing scheme
    auth.js uses, or a token minted here won't verify on SehatAI's side."""
    __tablename__ = "patient_api_tokens"

    token_hash = Column(String, primary_key=True)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=utc_now)
    revoked = Column(Boolean, default=False, nullable=False)


class Document(Base):
    """DataFetch's `documents` table -- already holds 17 real rows. A
    lab-report upload through CareLink inserts here (status='uploaded'),
    NOT into CareLink's own Report table (routers/reports.py) -- that one
    is a distinct, existing feature (PDFs shared inside a conversation
    thread) and is intentionally left untouched by this bridge."""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, index=True)
    category = Column(String, nullable=True)
    uploaded_at = Column(DateTime, default=utc_now)
    doctor_reviewed = Column(Boolean, default=False)
    file_path = Column(String, nullable=True)
    file_url = Column(String, nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    mime_type = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    file_hash = Column(String, nullable=True)
    document_date = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)
    raw_ocr = Column(Text, nullable=True)
    ocr_engine = Column(String, nullable=True)
    superseded_by = Column(Integer, nullable=True)
