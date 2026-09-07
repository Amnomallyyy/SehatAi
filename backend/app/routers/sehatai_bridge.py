"""
Architecture doc §04 -- Auth bridge (the first of the two "blocking" gaps).

Problem: SehatAI's own webserver.js was deliberately built with no login
system -- patient_api_tokens are minted out-of-band by an operator running
issueToken.js on the command line (see SehatAI's auth.js doc comment).
That's fine for a standalone tool with a handful of test patients; it's not
sufficient once a real, already-logged-in CareLink patient needs their own
token the instant they open the AI Assistant tab.

Fix: this endpoint, not SehatAI's, mints the token. A CareLink patient is
already authenticated (their own JWT, via get_current_user) -- this reuses
that trust rather than exposing SehatAI's raw token-issuance table write to
an untrusted browser directly.

Token format has to be BYTE-FOR-BYTE compatible with what SehatAI's own
auth.js writes and reads, since SehatAI's webserver.js is the thing that
verifies it on every /api/chat call:
  - raw token:  32 random bytes, base64url-encoded, no padding
  - stored key: SHA-256 hex digest of the raw token's UTF-8 bytes
See hash_sehatai_token below -- it has to match auth.js's hashToken()
exactly or every token minted here will fail verification on SehatAI's side.

Revoke-and-reissue, not cache-and-return: tokens are stored as a hash only
(same principle as a password hash -- even a full DB read never exposes a
usable credential), so there is no "existing valid token" to hand back on a
second call. Calling this endpoint again simply revokes whatever this
patient had and mints a fresh one. The frontend should call it once per
session and hold the raw token in memory only -- never persist it.
"""
import hashlib
import secrets

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..dependencies import get_or_create_sehatai_patient_id
from ..security import get_current_user, require_patient_role

router = APIRouter(prefix="/me", tags=["sehatai-bridge"])

TOKEN_BYTES = 32  # matches SehatAI's auth.js TOKEN_BYTES exactly


def hash_sehatai_token(raw_token: str) -> str:
    """MUST match SehatAI's auth.js hashToken() byte-for-byte -- see the
    module doc comment above."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@router.post("/sehatai-token", response_model=schemas.SehatAITokenOut, status_code=status.HTTP_201_CREATED)
def issue_sehatai_token(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    require_patient_role(current_user)

    sehatai_patient_id = get_or_create_sehatai_patient_id(current_user, db)

    # Revoke any tokens this patient already has -- there's no way to
    # recover a previously-issued raw token to reuse it (see module doc
    # comment), so an old, still-technically-valid one left un-revoked
    # would just be a second live credential nobody's tracking client-side.
    db.query(models.PatientAPIToken).filter(
        models.PatientAPIToken.patient_id == sehatai_patient_id,
        models.PatientAPIToken.revoked == False,  # noqa: E712 -- SQLAlchemy needs `== False`, not `is False`
    ).update({"revoked": True})

    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    token_row = models.PatientAPIToken(
        token_hash=hash_sehatai_token(raw_token),
        patient_id=sehatai_patient_id,
    )
    db.add(token_row)
    db.commit()

    return schemas.SehatAITokenOut(token=raw_token, patient_id=str(sehatai_patient_id))
