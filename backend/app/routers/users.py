import uuid
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import AVATAR_DIR, sync_sehatai_profile
from ..security import get_current_user

router = APIRouter(prefix="/users", tags=["users"])

MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB -- profile pictures, not medical documents
_AVATAR_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


# NOTE: "/me" must be declared before "/{user_id}". FastAPI/Starlette tries
# routes in declaration order, and a plain "{user_id}" path segment matches
# ANY string first -- including "me" -- before FastAPI even attempts to
# coerce it to int. Declaring "/me" second would make it unreachable (every
# request to it would 422 trying to parse "me" as an int instead).
@router.get("/me", response_model=schemas.UserOut)
def get_my_profile(current_user: models.User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=schemas.UserOut)
def update_my_profile(
    payload: schemas.ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Edit-profile endpoint for both roles. `specialization` is doctor-only
    and `date_of_birth`/`sex` are patient-only -- sending the wrong one for
    your role gets a clear 400 rather than it being silently dropped."""
    if payload.specialization is not None and current_user.role != models.UserRole.doctor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only doctors can set a specialization"
        )
    if (payload.date_of_birth is not None or payload.sex is not None) and current_user.role != models.UserRole.patient:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only patients can set date of birth / sex"
        )
    if payload.name is not None:
        current_user.name = payload.name
    if payload.specialization is not None:
        current_user.specialization = payload.specialization
    if payload.date_of_birth is not None:
        current_user.date_of_birth = payload.date_of_birth
    if payload.sex is not None:
        current_user.sex = payload.sex
    db.commit()
    db.refresh(current_user)

    # Keep SehatAI's own `patients` row in sync so the change is live for
    # the patient's very next chat message -- see dependencies.py's
    # sync_sehatai_profile doc comment. Patient-only and only worth a call
    # when one of the two synced fields actually changed.
    if current_user.role == models.UserRole.patient and (payload.date_of_birth is not None or payload.sex is not None):
        sync_sehatai_profile(current_user, db)

    return current_user


@router.post("/me/avatar", response_model=schemas.UserOut)
async def upload_my_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Profile picture upload for either role. Unlike report PDFs, avatars
    aren't sensitive medical documents, but this stays behind the same
    authenticated-download pattern as everything else in this app (see
    GET /users/{user_id}/avatar) rather than introducing a new public
    static-file precedent."""
    ext = _AVATAR_CONTENT_TYPES.get(file.content_type)
    if ext is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG, PNG, or WebP images are accepted",
        )

    contents = await file.read()
    if len(contents) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image exceeds the 2 MB upload limit")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}.{ext}"
    with open(AVATAR_DIR / stored_filename, "wb") as f:
        f.write(contents)

    current_user.avatar_path = stored_filename
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("", response_model=List[schemas.UserOut])
def list_users(
    role: models.UserRole = Query(..., description="'doctor' or 'patient' -- required, no unfiltered listing"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """No more open browsing of the full roster -- this now returns only
    users the caller has an ACCEPTED connection with (see /connections for
    how that's established). A patient asking for role=doctor gets their
    connected doctors; a doctor asking for role=patient gets their connected
    patients. The "wrong-direction" combinations (a patient asking for
    role=patient, a doctor asking for role=doctor) aren't specially
    rejected -- patient-patient and doctor-doctor connections can never
    exist (POST /connections blocks same-role pairs), so the query below
    naturally returns an empty list for them rather than needing a
    special-cased error.
    """
    if role == models.UserRole.doctor:
        # Doctors the caller (as a patient) is accepted-connected to.
        query = (
            db.query(models.User)
            .join(models.Connection, models.Connection.doctor_id == models.User.id)
            .filter(
                models.Connection.patient_id == current_user.id,
                models.Connection.status == models.ConnectionStatus.accepted,
            )
        )
    else:
        # Patients the caller (as a doctor) is accepted-connected to.
        query = (
            db.query(models.User)
            .join(models.Connection, models.Connection.patient_id == models.User.id)
            .filter(
                models.Connection.doctor_id == current_user.id,
                models.Connection.status == models.ConnectionStatus.accepted,
            )
        )
    return query.order_by(models.User.name).all()


@router.get("/{user_id}", response_model=schemas.UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user_id != current_user.id:
        if current_user.role == models.UserRole.patient:
            filters = (models.Connection.patient_id == current_user.id, models.Connection.doctor_id == user_id)
        else:
            filters = (models.Connection.patient_id == user_id, models.Connection.doctor_id == current_user.id)
        is_connected = (
            db.query(models.Connection)
            .filter(*filters, models.Connection.status == models.ConnectionStatus.accepted)
            .first()
        )
        if is_connected is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this user"
            )
    return user


@router.get("/{user_id}/avatar")
def download_user_avatar(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Same visibility rule as GET /users/{user_id}: self, or an accepted
    connection -- a stranger can't fetch someone's photo any more than their
    name or profile."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user_id != current_user.id:
        if current_user.role == models.UserRole.patient:
            filters = (models.Connection.patient_id == current_user.id, models.Connection.doctor_id == user_id)
        else:
            filters = (models.Connection.patient_id == user_id, models.Connection.doctor_id == current_user.id)
        is_connected = (
            db.query(models.Connection)
            .filter(*filters, models.Connection.status == models.ConnectionStatus.accepted)
            .first()
        )
        if is_connected is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="You are not connected to this user"
            )

    if not user.avatar_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This user has no avatar")

    file_path = AVATAR_DIR / user.avatar_path
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found on server")

    media_type = "image/" + file_path.suffix.lstrip(".").replace("jpg", "jpeg")
    return FileResponse(path=file_path, media_type=media_type)
