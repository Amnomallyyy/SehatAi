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


class AppointmentCreate(BaseModel):
    patient_id: int
    doctor_id: int
    scheduled_at: datetime
    reason: Optional[str] = Field(default=None, max_length=500)


class AppointmentUpdate(BaseModel):
    """Every field optional. status only ever accepts 'cancelled' here --
    there's no un-cancel or any other transition exposed."""

    scheduled_at: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal["cancelled"]] = None


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
    kicked off but hasn't necessarily finished yet."""
    document_id: Optional[int] = None
    status: str
    detail: Optional[str] = None
