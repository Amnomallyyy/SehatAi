"""
Password hashing, JWT issuance/verification, and the auth dependencies used
across every router.

Design note -- Bearer token instead of OAuth2PasswordBearer:
FastAPI's tutorials usually pair JWT auth with `OAuth2PasswordBearer`, which
expects the login endpoint to accept `application/x-www-form-urlencoded`
(username/password fields) so Swagger's "Authorize" button can drive the
whole login flow for you. This app uses plain `HTTPBearer` instead, and
/auth/login takes a normal JSON body. That keeps every endpoint in this API
-- including login -- consistent JSON in, JSON out, which matters more here
since a separate AI is building a fetch()-based frontend against this
contract. The tradeoff: in /docs, you paste a token into "Authorize" by hand
(via a "Try it out" call to /auth/login) rather than typing a
username/password into the padlock directly. See README.md for the exact
steps.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from . import models
from .database import get_db

# For local hackathon use a fallback dev secret is fine. For anything beyond
# your own machine, set a real SECRET_KEY env var before running uvicorn.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-3f8a9c2e1b7d")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24h -- long enough to not expire mid-demo

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

_credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """Decodes the bearer token and loads the current User row. Used as a
    dependency on nearly every route in the app."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        raise _credentials_exception

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise _credentials_exception
    return user


def require_doctor_role(user: models.User) -> None:
    """Plain helper (not a FastAPI dependency) for endpoints that are mostly
    open to either role but restrict one specific action to doctors --
    e.g. marking a report 'reviewed', or listing all patients."""
    if user.role != models.UserRole.doctor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This action requires a doctor account")


def require_patient_role(user: models.User) -> None:
    """Symmetric to require_doctor_role -- e.g. only a patient can grant/
    revoke a doctor's access to their reports history."""
    if user.role != models.UserRole.patient:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This action requires a patient account")
