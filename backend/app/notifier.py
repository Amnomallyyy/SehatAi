"""
Outbound email for account links (confirm email, reset password) and the
patient's emergency alert.

Configuration (env var NAMES only -- values live in .env, never in code):
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
    PUBLIC_APP_URL  base URL used to build emailed links (default http://localhost:3002)

When SMTP_HOST is not set nothing is sent ("dry run"). Callers get that
back as a status so the UI never claims a message went out when it didn't.
For links only, and only outside production, the link is written to the
server log so a developer can still finish sign-up without a mail server.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger("carelink.notifier")

SENT = "sent"
DRY_RUN = "dry_run"
FAILED = "failed"


def public_app_url() -> str:
    return os.environ.get("PUBLIC_APP_URL", "http://localhost:3002").rstrip("/")


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "").lower() == "production"


def send_email(to: str, subject: str, body: str, dev_log_text: Optional[str] = None):
    """Returns (status, error). status is sent | dry_run | failed.

    dev_log_text: shown in the server log in dry-run mode outside production
    (used for account links so sign-up still works locally). Never pass
    anything that shouldn't sit in a log file in production -- it is not
    written there."""
    if not smtp_configured():
        if dev_log_text and not _is_production():
            logger.warning("EMAIL (dry run, SMTP not configured) to %s -- %s: %s", to, subject, dev_log_text)
        else:
            logger.warning("EMAIL (dry run, SMTP not configured) to %s -- %s", to, subject)
        return DRY_RUN, None

    host = os.environ["SMTP_HOST"].strip()
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user or "no-reply@sehatai.local"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except Exception as exc:
        # Log the exception TYPE only; messages from some servers echo
        # credentials or addresses.
        logger.error("Email to %s failed: %s", to, type(exc).__name__)
        return FAILED, type(exc).__name__
    return SENT, None
