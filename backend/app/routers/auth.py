import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..ratelimit import check_login_attempt, limit_per_ip
from ..security import create_access_token, get_current_user, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

SIGNUPS_PER_IP_PER_HOUR = int(os.environ.get("SIGNUP_RATE_LIMIT_PER_HOUR", "20"))
LOGIN_ATTEMPTS_PER_5_MIN = int(os.environ.get("LOGIN_RATE_LIMIT_PER_5_MIN", "10"))


@router.post(
    "/signup",
    response_model=schemas.Token,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_per_ip("signup", limit=SIGNUPS_PER_IP_PER_HOUR, window_seconds=3600))],
)
def signup(payload: schemas.SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered")

    # Mirrors PATCH /users/me's identical role-conditional check
    # (routers/users.py) -- date_of_birth/sex feed the symptom-triage bot's
    # age/sex resolution and are meaningless for a doctor account. Placed
    # after the duplicate-email check (not before), and as a router check
    # rather than a schema validator, so an already-registered email still
    # gets the clearer, more specific 400 first -- see test_flow.py's
    # duplicate-email signup test, which intentionally sends no DOB/sex.
    if payload.role == models.UserRole.patient:
        if payload.date_of_birth is None or payload.sex is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Date of birth and sex are required for patient accounts",
            )
    elif payload.date_of_birth is not None or payload.sex is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only patients can set date of birth / sex"
        )

    try:
        password_hash = hash_password(payload.password)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password could not be processed")

    user = models.User(
        name=payload.name,
        email=payload.email,
        password_hash=password_hash,
        role=payload.role,
        date_of_birth=payload.date_of_birth,
        sex=payload.sex,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two signups for the same email racing past the check above --
        # the unique index catches the loser; answer it the same way.
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered")
    db.refresh(user)

    token = create_access_token(user.id)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))


@router.post("/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    # Caps password guessing against one account from one client; see
    # ratelimit.py. Counted before the bcrypt check so a blocked caller
    # doesn't even cost us a hash.
    check_login_attempt(request, payload.email, limit=LOGIN_ATTEMPTS_PER_5_MIN)
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        # Same error for "no such user" and "wrong password" on purpose --
        # distinguishing them lets an attacker enumerate registered emails.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    token = create_access_token(user.id)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
def verify_token(
    role: Optional[models.UserRole] = Query(default=None),
    current_user: models.User = Depends(get_current_user),
):
    """Subrequest target for the gateway's `auth_request` (deploy/nginx.conf):
    services with no login of their own -- the Clinical Evidence API -- are
    only reachable through the gateway with a valid CareLink token, and
    optionally only for one role. 204 = allow, 401/403 = deny."""
    if role is not None and current_user.role != role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"This requires a {role.value} account")
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-User-Id": str(current_user.id), "X-User-Role": current_user.role.value},
    )
