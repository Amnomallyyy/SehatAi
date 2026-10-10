"""
Privacy notice version + the anonymised demographic statistics helpers.

PRIVACY_NOTICE_VERSION must match the version the frontend shows at sign-up
(frontend/static/js/app.js, PRIVACY_NOTICE_VERSION). Whenever the notice text
changes, bump it in BOTH places: sign-up then refuses a browser still showing
the old text, so a recorded consent always refers to the text the person saw.
"""
from datetime import date
from typing import Optional

PRIVACY_NOTICE_VERSION = "2026-10-v1"


def age_band(dob: Optional[date], today: Optional[date] = None) -> Optional[str]:
    """Coarse age group for the statistics table (never the date of birth)."""
    if dob is None:
        return None
    today = today or date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < 18:
        return "0-17"
    if age < 30:
        return "18-29"
    if age < 45:
        return "30-44"
    if age < 60:
        return "45-59"
    return "60+"


def clean_country(country: str) -> str:
    """'  united   states ' -> 'United States', so the same country always groups together."""
    return " ".join((country or "").split()).title()[:100]
