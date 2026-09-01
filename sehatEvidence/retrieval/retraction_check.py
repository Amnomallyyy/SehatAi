"""
retrieval/retraction_check.py

Crossref-based retraction screening.

Why this exists alongside pubmed.py's native check:
  - pubmed.py catches retractions the NLM has curated into PubMed's own
    metadata (PublicationType="Retracted Publication", CommentsCorrections
    RefType="RetractionIn"). Authoritative, but subject to indexing lag.
  - Crossref absorbed the Retraction Watch database in Sept 2023, so its
    update-to feed catches retractions publishers deposited (or that
    Retraction Watch flagged independently) that may not be in PubMed yet.
  - Records that came ONLY from Europe PMC have no native retraction signal
    at all -- Crossref is the only check they get.

Neither source alone is sufficient. retrieve.py runs both.

ZERO AI in this module.
"""

from __future__ import annotations

from typing import Optional

import requests

from core.cache import TokenBucket

CROSSREF_WORKS_URL = "https://api.crossref.org/works"


class RetractionChecker:
    def __init__(
        self,
        mailto: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        # Crossref asks for a mailto in the User-Agent for the "polite pool"
        # (faster, more reliable service). Not strictly required.
        self.mailto = mailto
        self.session = session or requests.Session()
        self._limiter = TokenBucket(rate_per_second=5)

    def _headers(self) -> dict:
        ua = "EvidenceBoard/0.1"
        if self.mailto:
            ua += f" (mailto:{self.mailto})"
        return {"User-Agent": ua}

    def check_doi(self, doi: str) -> tuple[bool, Optional[str]]:
        """
        Checks a single DOI for retraction.

        Returns (is_retracted, retraction_source_detail).
        retraction_source_detail is e.g. "crossref:publisher" or
        "crossref:retraction-watch" when retracted, else None.

        Fails OPEN (returns False) on network errors -- a Crossref outage
        should not silently mark everything as clean, but it also should not
        crash the pipeline. Callers should treat False as "no evidence of
        retraction found", not "definitely not retracted".
        """
        if not doi:
            return False, None

        self._limiter.acquire()
        try:
            resp = self.session.get(
                f"{CROSSREF_WORKS_URL}/{doi}",
                headers=self._headers(),
                timeout=20,
            )
            if resp.status_code == 404:
                return False, None  # DOI not in Crossref -- nothing to say
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            return False, None

        message = data.get("message", {})
        update_to = message.get("update-to", []) or []

        for update in update_to:
            if update.get("type") == "retraction":
                source = update.get("source", "unknown")
                return True, f"crossref:{source}"

        return False, None

    def check_many(self, dois: list[str]) -> dict[str, tuple[bool, Optional[str]]]:
        """
        Checks a batch of DOIs serially (rate-limited internally).
        Returns {doi: (is_retracted, source_detail)}.
        """
        results: dict[str, tuple[bool, Optional[str]]] = {}
        for doi in dois:
            if not doi or doi in results:
                continue
            results[doi] = self.check_doi(doi)
        return results