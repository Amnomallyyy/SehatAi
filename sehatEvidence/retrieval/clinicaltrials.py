"""
retrieval/clinicaltrials.py

ClinicalTrials.gov API v2 client. JSON, no auth required.
Rate limit: 50 req/min per IP.

Important quirks vs PubMed/Europe PMC:
  - Pagination is cursor-based and MUST be serial -- the nextPageToken for
    page N only appears in the response for page N-1. Cannot parallelize.
  - A single study record can run to 16,000 lines of JSON if unfiltered,
    so we always pass `fields` to prune the payload to what we actually need.
  - This is the one source that answers "what's coming" rather than "what's
    already published" -- trial_status is the key field the Synthesizer's
    whats_coming section will filter on.

This module does ZERO AI -- pure retrieval + parsing, same as the other two.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import requests

from core.cache import TokenBucket
from core.schema import EvidenceRecord, SourceDB, StudyDesign

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"

# Fields we actually need -- keeps the payload small per NCBI/CT.gov's own
# recommendation (a full unfiltered record can be huge).
FIELDS = [
    "NCTId",
    "BriefTitle",
    "OfficialTitle",
    "OverallStatus",
    "Condition",
    "InterventionName",
    "OrgFullName",
    "OrgClass",
    "StartDate",
    "CompletionDate",
    "BriefSummary",
    "EligibilityCriteria",
]

# Status values relevant to "what's coming" framing -- see architecture doc.
# Kept here as a reference set; callers can filter with these.
RELEVANT_STATUSES_FOR_PIPELINE = {
    "NOT_YET_RECRUITING",
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "COMPLETED",
}


class ClinicalTrialsClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        # 50 req/min = ~0.83 req/s. Stay comfortably under it.
        self._limiter = TokenBucket(rate_per_second=0.7)

    def _get(self, params: dict) -> dict:
        self._limiter.acquire()
        resp = self.session.get(STUDIES_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search(
        self,
        query: str,
        page_size: int = 20,
        max_pages: int = 1,
        statuses: Optional[list[str]] = None,
    ) -> list[EvidenceRecord]:
        """
        Searches ClinicalTrials.gov. Pagination is serial by necessity
        (cursor-based -- see module docstring), so max_pages controls how
        many sequential requests we're willing to make. Default 1 page is
        deliberately conservative given the 50 req/min ceiling.

        statuses: optional list of overallStatus values to filter to
        (e.g. RELEVANT_STATUSES_FOR_PIPELINE). None = no filter.
        """
        records: list[EvidenceRecord] = []
        page_token: Optional[str] = None
        pages_fetched = 0

        while pages_fetched < max_pages:
            params = {
                "query.term": query,
                "pageSize": page_size,
                "fields": ",".join(FIELDS),
                "format": "json",
            }
            if statuses:
                # CT.gov v2 supports filter.overallStatus as a pipe-separated list
                params["filter.overallStatus"] = "|".join(statuses)
            if page_token:
                params["pageToken"] = page_token

            data = self._get(params)
            studies = data.get("studies", [])
            for study in studies:
                record = self._parse_study(study)
                if record is not None:
                    records.append(record)

            pages_fetched += 1
            page_token = data.get("nextPageToken")
            if not page_token:
                break  # no more pages available

        return records

    def search_relaxed(
        self,
        query: str,
        page_size: int = 20,
        max_pages: int = 1,
        statuses: Optional[list[str]] = None,
        yield_floor: Optional[int] = None,
        min_words: int = 3,
        max_rounds: int = 6,
    ) -> list[EvidenceRecord]:
        """search() with automatic recovery from the SAME class of
        collapse found in PubMed's Automatic Term Mapping: ClinicalTrials.
        gov's `query.term` (Essie syntax) also implicitly ANDs every term,
        so a specific multi-word query can return 0 hits while a shorter
        prefix of the exact same query returns plenty (confirmed live,
        2026-08-30: "andexanet alfa dosing regimen apixaban reversal FDA
        approved" -> 0 hits; "andexanet alfa dosing" -> 9).

        Unlike PubMed, ClinicalTrials.gov exposes no query-translation to
        target which term is unmapped, so relaxation here is a simpler,
        general strategy: progressively drop TRAILING words (Strategist
        queries front-load the core drug/condition and append more
        specific qualifiers, so the tail is where over-specification
        tends to live) and keep whichever version yields the most
        records, merging in newly-found studies rather than replacing.
        `yield_floor` defaults to page_size for the same reason as
        pubmed.py's esearch_relaxed: "fewer than requested" is the
        collapse signature.
        """
        floor = page_size if yield_floor is None else yield_floor
        best = self.search(query, page_size=page_size, max_pages=max_pages, statuses=statuses)
        if len(best) >= floor:
            return best

        words = query.split()
        for _round in range(max_rounds):
            if len(words) <= min_words:
                break
            words = words[:-1]
            candidate = " ".join(words)
            relaxed = self.search(candidate, page_size=page_size, max_pages=max_pages, statuses=statuses)
            if len(relaxed) > len(best):
                seen_ids = {r.native_id for r in best}
                best = best + [r for r in relaxed if r.native_id not in seen_ids]
            if len(best) >= floor:
                break
        return best

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_study(self, study: dict) -> Optional[EvidenceRecord]:
        protocol = study.get("protocolSection", {})
        id_module = protocol.get("identificationModule", {})
        status_module = protocol.get("statusModule", {})
        desc_module = protocol.get("descriptionModule", {})
        org = id_module.get("organization", {})

        nct_id = id_module.get("nctId")
        if not nct_id:
            return None

        title = id_module.get("briefTitle") or id_module.get("officialTitle") or ""
        summary = desc_module.get("briefSummary", "") or ""
        status = status_module.get("overallStatus")

        start_date = self._parse_ct_date(status_module.get("startDateStruct", {}))
        completion_date = self._parse_ct_date(status_module.get("completionDateStruct", {}))
        # Prefer completion date for publication_date-equivalent sorting --
        # it's the most informative single date for "when will/did this
        # produce results", which is what a clinical evidence tool cares about.
        best_date = completion_date or start_date

        org_name = org.get("fullName")

        return EvidenceRecord(
            record_id=f"NCT/{nct_id}",
            source=SourceDB.CLINICAL_TRIALS,
            native_id=nct_id,
            doi=None,  # trials don't have DOIs
            title=title,
            abstract=summary,
            abstract_sections={"UNASSIGNED": summary} if summary else {},
            journal=org_name,  # repurpose "journal" field for sponsor org -- closest analog
            publication_date=best_date,
            authors=[],  # trials don't have "authors" in the paper sense
            study_design=StudyDesign.CLINICAL_TRIAL_RECORD,
            is_preprint=False,
            is_retracted=False,  # not applicable to trial registrations
            trial_status=status,
            url=f"https://clinicaltrials.gov/study/{nct_id}",
        )

    @staticmethod
    def _parse_ct_date(date_struct: dict) -> Optional[date]:
        """
        CT.gov v2 date fields look like {"date": "2025-06", "type": "ACTUAL"}
        or {"date": "2025-06-15", ...}. Handles both YYYY-MM and YYYY-MM-DD.
        """
        raw = date_struct.get("date")
        if not raw:
            return None
        parts = raw.split("-")
        try:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            return date(year, month, day)
        except (ValueError, IndexError):
            return None