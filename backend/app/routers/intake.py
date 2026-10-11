"""
Patient health intake, laid out like a paper medical intake form: a table
each for allergies, existing conditions and family history, plus
the top health concerns (ranked), an emergency contact and (for accounts
that predate sign-up location) city/country.

Rows are stored in `intake_entries` (one generic table, see IntakeEntry).
The triage and diet bots keep reading the simple lists in SehatAI's
`patient_intake_form`; saving the form refreshes those lists from the row
names, so the bots need no changes. Medicines are NOT part of this form: they
live on the Medicines page (doctor-prescribed, patient taking / not taking),
and `patient_intake_form.current_medications` is left untouched. The form counts as "completed" once the
intake row, an emergency contact and a location all exist; the frontend
holds patients on this page until then.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import get_or_create_sehatai_patient_id, set_user_location
from ..security import get_current_user, require_patient_role

router = APIRouter(tags=["intake"])

# section -> (schemas row class, names of detail1..detail3 on that row)
SECTIONS = {
    "allergy": (schemas.AllergyRow, ("reaction", "severity")),
    "condition": (schemas.ConditionRow, ("since", "status")),
    "family": (schemas.FamilyRow, ("relative",)),
}


def _row_from_entry(section: str, e: models.IntakeEntry):
    cls, fields = SECTIONS[section]
    values = {"name": e.name}
    for field, col in zip(fields, (e.detail1, e.detail2, e.detail3)):
        values[field] = col
    return cls(**values)


def _entries_for(db: Session, user_id: int) -> Dict[str, List[models.IntakeEntry]]:
    out: Dict[str, List[models.IntakeEntry]] = {k: [] for k in SECTIONS}
    rows = (
        db.query(models.IntakeEntry)
        .filter(models.IntakeEntry.user_id == user_id)
        .order_by(models.IntakeEntry.section, models.IntakeEntry.position, models.IntakeEntry.id)
        .all()
    )
    for r in rows:
        if r.section in out:
            out[r.section].append(r)
    return out


def _build_out(db: Session, user: models.User) -> schemas.IntakeOut:
    intake: Optional[models.PatientIntake] = None
    if user.sehatai_patient_id is not None:
        intake = db.query(models.PatientIntake).filter(models.PatientIntake.patient_id == user.sehatai_patient_id).first()
    contact = db.query(models.EmergencyContact).filter(models.EmergencyContact.user_id == user.id).first()
    profile = db.query(models.IntakeProfile).filter(models.IntakeProfile.user_id == user.id).first()
    entries = _entries_for(db, user.id)

    missing = []
    if intake is None:
        missing.append("health_history")
    if contact is None:
        missing.append("emergency_contact")
    if user.location is None:
        missing.append("location")

    rows = {s: [_row_from_entry(s, e) for e in entries[s]] for s in SECTIONS}
    # Someone who saved the older simple-list intake has names but no rows
    # yet: show those names as rows so nothing they entered disappears.
    if intake is not None and not any(entries.values()):
        legacy = {
            "allergy": intake.allergies,
            "condition": intake.existing_conditions,
            "family": intake.family_history,
        }
        for section, names in legacy.items():
            cls, _ = SECTIONS[section]
            rows[section] = [cls(name=n) for n in (names or []) if n and n.strip()]

    return schemas.IntakeOut(
        completed=not missing,
        missing=missing,
        allergies=rows["allergy"],
        conditions=rows["condition"],
        family_history=rows["family"],
        concerns=list(profile.concerns or []) if profile else [],
        concern_began=profile.concern_began if profile else None,
        emergency_contact=(
            schemas.EmergencyContactIO(
                name=contact.name, relationship=contact.relationship_label, email=contact.email, phone=contact.phone
            )
            if contact
            else None
        ),
        city=user.city,
        country=user.country,
    )


def _names(rows) -> List[str]:
    """The simple list the bots read: just the names, de-duplicated, capped."""
    return schemas._clean_list([r.name for r in rows])


@router.get("/me/intake", response_model=schemas.IntakeOut)
def get_my_intake(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """A plain read: doesn't create a SehatAI patient row for someone who has
    never saved anything."""
    require_patient_role(current_user)
    return _build_out(db, current_user)


@router.put("/me/intake", response_model=schemas.IntakeOut)
def save_my_intake(
    payload: schemas.IntakeUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    require_patient_role(current_user)

    # Location is only collected here for accounts that don't have one yet.
    set_user_location(db, current_user, payload.city, payload.country)

    patient_uuid = get_or_create_sehatai_patient_id(current_user, db)

    # 1. The tables: replace this patient's rows with what was submitted.
    db.query(models.IntakeEntry).filter(
        models.IntakeEntry.user_id == current_user.id, models.IntakeEntry.section.in_(list(SECTIONS))
    ).delete(synchronize_session=False)
    submitted = {
        "allergy": payload.allergies,
        "condition": payload.conditions,
        "family": payload.family_history,
    }
    for section, rows in submitted.items():
        _, fields = SECTIONS[section]
        for position, row in enumerate(rows):
            details = [getattr(row, f) for f in fields] + [None, None, None]
            db.add(
                models.IntakeEntry(
                    user_id=current_user.id,
                    section=section,
                    position=position,
                    name=row.name,
                    detail1=details[0],
                    detail2=details[1],
                    detail3=details[2],
                )
            )

    # 2. The simple lists the bots read, kept in sync from the names.
    intake = db.query(models.PatientIntake).filter(models.PatientIntake.patient_id == patient_uuid).first()
    if intake is None:
        intake = models.PatientIntake(patient_id=patient_uuid)
        db.add(intake)
    intake.allergies = _names(payload.allergies)
    intake.existing_conditions = _names(payload.conditions)
    intake.family_history = _names(payload.family_history)

    # 3. Top concerns + when the main one began.
    profile = db.query(models.IntakeProfile).filter(models.IntakeProfile.user_id == current_user.id).first()
    if profile is None:
        profile = models.IntakeProfile(user_id=current_user.id)
        db.add(profile)
    profile.concerns = payload.concerns
    profile.concern_began = payload.concern_began

    # 4. Emergency contact.
    contact = db.query(models.EmergencyContact).filter(models.EmergencyContact.user_id == current_user.id).first()
    ec = payload.emergency_contact
    if contact is None:
        contact = models.EmergencyContact(user_id=current_user.id)
        db.add(contact)
    contact.name = ec.name
    contact.relationship_label = ec.relationship
    contact.email = ec.email
    contact.phone = ec.phone

    db.commit()
    db.refresh(current_user)
    return _build_out(db, current_user)
