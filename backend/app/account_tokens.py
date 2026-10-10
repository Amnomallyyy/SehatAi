"""
One-time emailed tokens for confirming an email address and resetting a
password.

Only the SHA-256 of a token is stored (`auth_tokens.token_hash`), so a
database leak can't be replayed. Tokens are single use, expire, and issuing
a new one for the same purpose invalidates the older ones.
"""
import hashlib
import secrets
from datetime import timedelta
from typing import Optional
from urllib.parse import quote

from sqlalchemy.orm import Session

from . import models, notifier

CONFIRM_EMAIL = "confirm_email"
RESET_PASSWORD = "reset_password"

_TTL = {
    CONFIRM_EMAIL: timedelta(hours=24),
    RESET_PASSWORD: timedelta(hours=1),
}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(db: Session, user: models.User, purpose: str) -> str:
    """Creates a fresh token (invalidating this user's older unused ones for
    the same purpose) and returns the RAW value -- the only time it exists
    outside the emailed link. Caller commits."""
    now = models.utc_now()
    for old in (
        db.query(models.AuthToken)
        .filter(
            models.AuthToken.user_id == user.id,
            models.AuthToken.purpose == purpose,
            models.AuthToken.used_at.is_(None),
        )
        .all()
    ):
        old.used_at = now
    raw = secrets.token_urlsafe(32)
    db.add(
        models.AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash(raw),
            expires_at=now + _TTL[purpose],
        )
    )
    return raw


def consume_token(db: Session, raw: str, purpose: str) -> Optional[models.User]:
    """Returns the user a valid, unused, unexpired token belongs to and marks
    it used; None for anything else (the caller gives one generic error so a
    guesser learns nothing). Caller commits."""
    row = (
        db.query(models.AuthToken)
        .filter(models.AuthToken.token_hash == _hash(raw), models.AuthToken.purpose == purpose)
        .first()
    )
    now = models.utc_now()
    if row is None or row.used_at is not None or row.expires_at < now:
        return None
    user = db.query(models.User).filter(models.User.id == row.user_id).first()
    if user is None:
        return None
    row.used_at = now
    return user


def send_confirmation_email(user: models.User, raw_token: str):
    link = f"{notifier.public_app_url()}/?confirm={quote(raw_token)}"
    body = (
        f"Hi {user.name},\n\n"
        "Welcome to SehatAI. Please confirm your email address to finish creating your account:\n\n"
        f"{link}\n\n"
        "This link works once and expires in 24 hours. If you didn't create an account, you can ignore this email."
    )
    return notifier.send_email(user.email, "Confirm your SehatAI email", body, dev_log_text=link)


def send_reset_email(user: models.User, raw_token: str):
    link = f"{notifier.public_app_url()}/?reset={quote(raw_token)}"
    body = (
        f"Hi {user.name},\n\n"
        "We received a request to reset your SehatAI password. Choose a new one here:\n\n"
        f"{link}\n\n"
        "This link works once and expires in 1 hour. If you didn't ask for this, you can ignore this email "
        "and your password will stay as it is."
    )
    return notifier.send_email(user.email, "Reset your SehatAI password", body, dev_log_text=link)
