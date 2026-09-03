from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=schemas.Token, status_code=status.HTTP_201_CREATED)
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
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))


@router.post("/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        # Same error for "no such user" and "wrong password" on purpose --
        # distinguishing them lets an attacker enumerate registered emails.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")

    token = create_access_token(user.id)
    return schemas.Token(access_token=token, user=schemas.UserOut.model_validate(user))
