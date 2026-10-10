"""
Pydantic request/response models -- this is the actual API contract.
Mirrors API_CONTRACT.md; if you change something here, update that file too.
"""
from datetime import date, datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .models import AppointmentStatus, ConnectionStatus, ReportAccessStatus, ReportStatus, UserRole

# Preset specialization options for doctors, plus a free-text "Other" escape
# hatch -- the column itself is just a plain string, so "Other" is not a
# distinct stored value, it's a UI affordance pairing this list with a
# text input.
DOCTOR_SPECIALIZATIONS = [
    "Cardiologist",
    "Dermatologist",
    "Endocrinologist",
    "Gastroenterologist",
    "General Practitioner",
    "Nephrologist",
    "Neurologist",
    "Obstetrician/Gynecologist",
    "Oncologist",
    "Ophthalmologist",
    "Orthopedist",
    "Pediatrician",
    "Psychiatrist",
    "Pulmonologist",
    "Rheumatologist",
    "Urologist",
    "Other",
]

# ---------------------------------------------------------------------------
# Users / Auth
# ---------------------------------------------------------------------------


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    role: UserRole
    specialization: Optional[str] = None
    avatar_url: Optional[str] = None
    date_of_birth: Optional[date] = None
    sex: Optional[Literal["male", "female"]] = None
    city: Optional[str] = None
    country: Optional[str] = None


def _validate_date_of_birth_value(v: Optional[date]) -> Optional[date]:
    """Shared by ProfileUpdate and SignupRequest -- not in the future, not
    implausibly old. The role-conditional "is this required/forbidden for
    you" check lives in the router (routers/users.py's PATCH /me,
    routers/auth.py's signup), not here -- this only ever validates a date
    that's actually present."""
    if v is None:
        return v
    today = datetime.now(timezone.utc).date()
    if v > today:
        raise ValueError("Date of birth can't be in the future")
    if today.year - v.year > 120:
        raise ValueError("Date of birth is not plausible")
    return v


class ProfileUpdate(BaseModel):
    """PATCH /users/me -- every field optional, only what's sent changes."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    specialization: Optional[str] = Field(default=None, max_length=100)
    # Patient-only (see routers/users.py) -- feeds the SehatAI symptom-triage
    # bot's age/sex resolution (dependencies.sync_sehatai_profile). Binary
    # only, matching Infermedica's actual API constraint (sex is one of its
    # required /triage evidence fields).
    date_of_birth: Optional[date] = Field(default=None, description="Not in the future, not implausibly old")
    sex: Optional[Literal["male", "female"]] = None
    city: Optional[str] = Field(default=None, min_length=1, max_length=100)
    country: Optional[str] = Field(default=None, min_length=1, max_length=100)

    @field_validator("city", "country")
    @classmethod
    def _strip_location(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v

    @field_validator("date_of_birth")
    @classmethod
    def _validate_date_of_birth(cls, v: Optional[date]) -> Optional[date]:
        return _validate_date_of_birth_value(v)


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72, description="8-72 characters (bcrypt's hard limit is 72 bytes)")
    role: UserRole
    # Patient-only. Stays Optional here (schema-level backward compatibility,
    # same "shared contract" convention as the rest of this class) --
    # required-if-patient / forbidden-if-doctor is enforced in
    # routers/auth.py's signup(), mirroring PATCH /users/me's identical
    # role-conditional check on these same two fields.
    date_of_birth: Optional[date] = Field(default=None, description="Patient-only; not in the future, not implausibly old")
    sex: Optional[Literal["male", "female"]] = None
    # Typed location, required for both roles.
    city: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=1, max_length=100)

    @field_validator("city", "country")
    @classmethod
    def _strip_location(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Location can't be blank")
        return v

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("date_of_birth")
    @classmethod
    def _validate_date_of_birth(cls, v: Optional[date]) -> Optional[date]:
        return _validate_date_of_birth_value(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class SignupResponse(BaseModel):
    """Sign-up no longer logs the user in: the email must be confirmed first."""
    status: Literal["confirmation_sent"] = "confirmation_sent"
    email: EmailStr


class StatusMessage(BaseModel):
    """Plain {"message": ...} reply for the account-link endpoints."""
    message: str


class EmailOnlyRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class ConfirmEmailRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=8, max_length=72, description="8-72 characters (bcrypt limit)")


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class ConnectionRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class ConnectionRespond(BaseModel):
    status: Literal["accepted", "rejected"]


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    doctor_id: int
    requested_by_id: int
    status: ConnectionStatus
    created_at: datetime
    responded_at: Optional[datetime] = None
    # Only ever populated for the doctor who set it -- the router nulls this
    # out before returning to the patient side of the connection, since it's
    # a private label, not something the patient should see about themself.
    doctor_nickname: Optional[str] = None
    patient: UserOut
    doctor: UserOut


class ConnectionNicknameUpdate(BaseModel):
    doctor_nickname: Optional[str] = Field(default=None, max_length=100)


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    patient_id: int
    doctor_id: int


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    doctor_id: int
    created_at: datetime
    patient: UserOut
    doctor: UserOut


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    sender_id: int
    text: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# AI Summaries
# ---------------------------------------------------------------------------


class AISummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    report_id: int
    summary: str
    key_findings: List[str]
    flagged_values: List[str]
    recommendation: str
    created_at: datetime


class AISummaryEdit(BaseModel):
    """Every field optional -- PATCH only changes what you send."""

    summary: Optional[str] = None
    key_findings: Optional[List[str]] = None
    flagged_values: Optional[List[str]] = None
    recommendation: Optional[str] = None


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class ReportStatusUpdate(BaseModel):
    status: ReportStatus


class ReportOut(BaseModel):
    # Deliberately NOT `from_attributes=True`. pdf_url is derived (it's an
    # authenticated download endpoint, not the raw stored path -- see
    # API_CONTRACT.md), so every route builds this explicitly via
    # reports.py's `_serialize_report()` helper rather than auto-mapping
    # straight from the ORM object. Leaving from_attributes off is a
    # deliberate guardrail against someone bypassing that helper later.
    id: int
    conversation_id: int
    patient_id: int
    display_name: str
    pdf_url: str
    status: ReportStatus
    timestamp: datetime
    ai_summary: Optional[AISummaryOut] = None


# ---------------------------------------------------------------------------
# Report Comments
# ---------------------------------------------------------------------------


class ReportCommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class ReportCommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    report_id: int
    sender_id: int
    text: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# Reports Access Grants
# ---------------------------------------------------------------------------


class ReportAccessGrantCreate(BaseModel):
    doctor_id: int


class ReportAccessGrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    doctor_id: int
    status: ReportAccessStatus
    created_at: datetime
    updated_at: datetime
    patient: UserOut
    doctor: UserOut


# ---------------------------------------------------------------------------
# Prescriptions
# ---------------------------------------------------------------------------


class PrescriptionOut(BaseModel):
    # Deliberately NOT from_attributes=True, same reasoning as ReportOut --
    # pdf_url is derived (an authenticated download endpoint), so every
    # route builds this via prescriptions.py's _serialize_prescription().
    id: int
    conversation_id: int
    patient_id: int
    doctor_id: int
    display_name: str
    pdf_url: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------


def _to_naive_utc(v: Optional[datetime]) -> Optional[datetime]:
    """Stored timestamps are naive UTC (models.utc_now). An offset-carrying
    value ("...+05:00", "...Z") must be converted, not just stripped:
    Postgres casts a timestamptz literal into a `timestamp` column by
    DROPPING the offset, which would silently shift a 10:00 PKT appointment
    to 10:00 UTC -- five hours late -- and skew every reminder window."""
    if v is not None and v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class AppointmentCreate(BaseModel):
    patient_id: int
    doctor_id: int
    scheduled_at: datetime
    reason: Optional[str] = Field(default=None, max_length=500)

    @field_validator("scheduled_at")
    @classmethod
    def _normalize_scheduled_at(cls, v: datetime) -> datetime:
        return _to_naive_utc(v)


class AppointmentUpdate(BaseModel):
    """Every field optional. status only ever accepts 'cancelled' here --
    there's no un-cancel or any other transition exposed."""

    scheduled_at: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal["cancelled"]] = None

    @field_validator("scheduled_at")
    @classmethod
    def _normalize_scheduled_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _to_naive_utc(v)


class AppointmentOut(BaseModel):
    # Deliberately NOT from_attributes=True -- active_reminder is computed
    # at request time from scheduled_at vs. now, same "derived field" pattern
    # as ReportOut.pdf_url. Built via appointments.py's _serialize_appointment().
    id: int
    patient_id: int
    doctor_id: int
    scheduled_at: datetime
    reason: Optional[str] = None
    status: AppointmentStatus
    created_by_id: int
    created_at: datetime
    active_reminder: Optional[Literal["3d", "1d", "2h"]] = None
    patient: UserOut
    doctor: UserOut


class SehatAITokenOut(BaseModel):
    """See routers/sehatai_bridge.py -- a fresh SehatAI bearer token, shown
    once. The frontend sends it as `Authorization: Bearer <token>` on every
    call to SehatAI's own webserver.js, never back to CareLink's backend."""
    token: str
    patient_id: str


class LabReportUploadOut(BaseModel):
    """See routers/lab_reports.py. `status` mirrors DataFetch's own
    documents.status column -- 'queued' means the extraction pipeline was
    kicked off but hasn't necessarily finished yet. document_id is
    DataFetch's documents.id (a UUID string), set once extraction is done."""
    document_id: Optional[str] = None
    status: str
    detail: Optional[str] = None
    # Set when status == "queued": poll GET /me/lab-reports/jobs/{job_id}.
    job_id: Optional[str] = None


# ── Structured lab data (routers/structured_reports.py) ──────────────────
# Read API over DataFetch's extracted_data/documents tables -- see that
# router's module docstring for the full design (Phase 1 of the reports
# rebuild). Kept in a separate block since these mirror a different data
# source than everything above (CareLink's own `reports` table).

class MarkerOut(BaseModel):
    test_name: str  # original, for display
    normalized_name: str  # marker_names.normalize_marker() output, for grouping/history
    value: str
    value_numeric: Optional[float] = None
    unit: Optional[str] = None
    normal_range: Optional[str] = None
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None
    flag: Optional[str] = None
    operator: Optional[str] = None
    delta_value: Optional[float] = None
    delta_since: Optional[date] = None
    is_abnormal: bool = False
    needs_review: bool = False
    confidence: Optional[float] = None


class ExtractionAuditOut(BaseModel):
    markers_found: int
    high_confidence: int
    needs_review: int
    verification_status: str = "not_run"  # not_run | running | complete | failed | no_source
    verified_at: Optional[datetime] = None
    model: Optional[str] = None
    error: Optional[str] = None


class StructuredDocumentSummaryOut(BaseModel):
    document_id: str
    category: Optional[str] = None
    document_date: Optional[date] = None
    uploaded_at: Optional[datetime] = None
    status: Optional[str] = None
    original_filename: Optional[str] = None
    marker_count: int = 0
    abnormal_count: int = 0
    spark: List[float] = []
    linked_report_id: Optional[int] = None
    has_source_file: bool = False
    doctor_reviewed: bool = False
    retracted: bool = False
    mime_type: Optional[str] = None
    ai_summary: Optional[str] = None


class StructuredDocumentDetailOut(StructuredDocumentSummaryOut):
    markers: List[MarkerOut] = []
    audit: ExtractionAuditOut
    default_trend_marker: Optional[str] = None


class MarkerHistoryPointOut(BaseModel):
    document_id: str
    document_date: Optional[date] = None
    value: str
    value_numeric: Optional[float] = None
    unit: Optional[str] = None
    flag: Optional[str] = None


class DocumentNoteIn(BaseModel):
    content: str = ""  # empty/whitespace means "delete my note" -- see PUT /structured/documents/{id}/notes
    retracted: bool = False  # requires non-empty content -- see upsert_document_note's validation


class DocumentNoteOut(BaseModel):
    id: int
    document_id: str
    doctor_id: int
    doctor_name: str
    content: str
    retracted: bool
    created_at: datetime
    updated_at: datetime


class VerificationFindingOut(BaseModel):
    normalized_marker_name: str
    primary_value: Optional[str] = None
    primary_unit: Optional[str] = None
    verified_value: Optional[str] = None
    verified_unit: Optional[str] = None
    agrees: bool


class VerificationOut(BaseModel):
    status: str  # running | complete | failed | no_source
    model_used: Optional[str] = None
    agreement_count: int = 0
    disagreement_count: int = 0
    error: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    findings: List[VerificationFindingOut] = []


class DoctorNotificationOut(BaseModel):
    """One row per unreviewed lab document, across every patient who has
    both accepted this doctor's connection AND granted reports access --
    the doctor-side upload notification (structured_reports.py's
    GET /structured/notifications)."""
    document_id: str
    patient_id: int  # CareLink user id, not the shared patients.id UUID
    patient_name: str
    category: Optional[str] = None
    document_date: Optional[date] = None
    uploaded_at: Optional[datetime] = None


class UnifiedReportItemOut(BaseModel):
    kind: str  # "report" | "document" | "linked"
    report_id: Optional[int] = None
    document_id: Optional[str] = None
    title: str
    subtitle: Optional[str] = None
    date: Optional[datetime] = None
    category: Optional[str] = None
    status: Optional[str] = None
    marker_count: int = 0
    abnormal_count: int = 0
    spark: List[float] = []


class MedicineOut(BaseModel):
    id: str
    name: str
    dosage: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    active: bool = True
    # Computed: "active" | "paused" (patient says they are not taking it) |
    # "ended" (end date has passed).
    status: Literal["active", "paused", "ended"] = "active"
    notes: Optional[str] = None
    source: Literal["doctor", "lab_report"] = "doctor"
    doctor_id: Optional[int] = None
    doctor_name: Optional[str] = None
    has_attachment: bool = False
    attachment_name: Optional[str] = None
    recorded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MedicineTakingUpdate(BaseModel):
    taking: bool


# ---------------------------------------------------------------------------
# Patient intake + emergency contact
# ---------------------------------------------------------------------------

MAX_INTAKE_ITEMS = 40
MAX_INTAKE_ITEM_LEN = 100


def _clean_list(values: List[str]) -> List[str]:
    seen, out = set(), []
    for v in values or []:
        v = (v or "").strip()[:MAX_INTAKE_ITEM_LEN]
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out[:MAX_INTAKE_ITEMS]


class EmergencyContactIO(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=50)

    @field_validator("name", "relationship")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field can't be blank")
        return v

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("phone")
    @classmethod
    def _strip_phone(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


def _row_text(v: Optional[str], limit: int) -> Optional[str]:
    v = (v or "").strip()[:limit]
    return v or None


class _IntakeRowBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, v):
        v = (v or "").strip() if isinstance(v, str) else v
        if not v:
            raise ValueError("Each row needs a name")
        return v[:200]


class AllergyRow(_IntakeRowBase):
    reaction: Optional[str] = None
    severity: Optional[Literal["mild", "moderate", "severe"]] = None

    @field_validator("reaction", mode="before")
    @classmethod
    def _clean_reaction(cls, v):
        return _row_text(v, 100)

    @field_validator("severity", mode="before")
    @classmethod
    def _blank_severity(cls, v):
        return v or None


class ConditionRow(_IntakeRowBase):
    since: Optional[str] = None
    status: Optional[Literal["ongoing", "managed", "resolved"]] = None

    @field_validator("since", mode="before")
    @classmethod
    def _clean_since(cls, v):
        return _row_text(v, 100)

    @field_validator("status", mode="before")
    @classmethod
    def _blank_status(cls, v):
        return v or None


class FamilyRow(_IntakeRowBase):
    relative: Optional[str] = None

    @field_validator("relative", mode="before")
    @classmethod
    def _clean_relative(cls, v):
        return _row_text(v, 100)


class IntakeUpdate(BaseModel):
    allergies: List[AllergyRow] = Field(default_factory=list, max_length=MAX_INTAKE_ITEMS)
    conditions: List[ConditionRow] = Field(default_factory=list, max_length=MAX_INTAKE_ITEMS)
    family_history: List[FamilyRow] = Field(default_factory=list, max_length=MAX_INTAKE_ITEMS)
    # Top health concerns in order of importance (up to 5) + when the main one began.
    concerns: List[str] = Field(default_factory=list, max_length=5)
    concern_began: Optional[str] = Field(default=None, max_length=100)
    emergency_contact: EmergencyContactIO
    # Only needed when the account has no location yet (accounts created
    # before location was collected at sign-up).
    city: Optional[str] = Field(default=None, max_length=100)
    country: Optional[str] = Field(default=None, max_length=100)

    @field_validator("concerns")
    @classmethod
    def _clean_concerns(cls, v: List[str]) -> List[str]:
        return [c.strip()[:200] for c in v if c and c.strip()][:5]

    @field_validator("concern_began", mode="before")
    @classmethod
    def _clean_began(cls, v):
        return _row_text(v, 100)


class IntakeOut(BaseModel):
    completed: bool
    missing: List[str] = []
    allergies: List[AllergyRow] = []
    conditions: List[ConditionRow] = []
    family_history: List[FamilyRow] = []
    concerns: List[str] = []
    concern_began: Optional[str] = None
    emergency_contact: Optional[EmergencyContactIO] = None
    city: Optional[str] = None
    country: Optional[str] = None


class EmergencyNotifyRequest(BaseModel):
    trigger: Literal["button", "triage"] = "button"
    category: Optional[str] = Field(default=None, max_length=100)


class EmergencyNotifyOut(BaseModel):
    status: Literal["sent", "dry_run", "failed"]
    contact_name: str
    detail: str


# ---------------------------------------------------------------------------
# Appointment slots (doctor availability)
# ---------------------------------------------------------------------------


class SlotCreate(BaseModel):
    """Generates back-to-back slots between `start` and `end` (UTC or offset
    datetimes -- normalised to naive UTC like appointments)."""
    start: datetime
    end: datetime
    slot_minutes: Literal[15, 20, 30, 45, 60] = 30

    @field_validator("start", "end")
    @classmethod
    def _to_naive_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class SlotBook(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class SlotOut(BaseModel):
    id: int
    doctor_id: int
    doctor_name: Optional[str] = None
    starts_at: datetime
    ends_at: datetime
    status: Literal["open", "booked"]
    # Doctor's own view only: who booked it.
    patient_name: Optional[str] = None
    appointment_id: Optional[int] = None
