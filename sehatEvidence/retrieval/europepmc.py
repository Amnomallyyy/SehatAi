"""
retrieval/europepmc.py

Europe PMC REST API client. JSON, not XML -- much simpler than PubMed.
No API key or auth required. Rate limit: 10 req/s (500/min) per IP.

Key features vs PubMed:
  - Preprints are first-class: filter with SRC:PPR, covers 32 preprint
    servers including bioRxiv/medRxiv.
  - isOpenAccess flag tells us if full JATS XML is available (we don't
    fetch full text in this hackathon scope -- abstracts only -- but we
    still capture the flag since it's free and may matter later).
  - Abstract is a single flat string, no NlmCategory-style sectioning like
    PubMed has. abstract_sections will just contain {"UNASSIGNED": text}
    for Europe PMC records, kept consistent with PubMed's schema shape.

This module does ZERO AI -- pure retrieval + parsing, same as pubmed.py.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import requests

from core.cache import TokenBucket
from core.schema import EvidenceRecord, SourceDB, StudyDesign

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Cheap keyword heuristic only -- same approach as pubmed.py, real ranking
# happens in agents/appraiser.py later.
_STUDY_DESIGN_KEYWORDS: list[tuple[str, StudyDesign]] = [
    ("systematic review", StudyDesign.SYSTEMATIC_REVIEW),
    ("meta-analysis", StudyDesign.META_ANALYSIS),
    ("randomized controlled trial", StudyDesign.RCT),
    ("randomised controlled trial", StudyDesign.RCT),
    ("cohort", StudyDesign.COHORT),
    ("case-control", StudyDesign.CASE_CONTROL),
    ("case series", StudyDesign.CASE_SERIES),
    ("case report", StudyDesign.CASE_REPORT),
    ("guideline", StudyDesign.GUIDELINE),
    ("review", StudyDesign.REVIEW),
]


class EuropePMCClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        # No API key concept here, but still throttle -- 10 req/s ceiling,
        # stay slightly under it same as we do for NCBI.
        self._limiter = TokenBucket(rate_per_second=9)

    def _get(self, params: dict) -> dict:
        self._limiter.acquire()
        full_params = {**params, "format": "json"}
        resp = self.session.get(SEARCH_URL, params=full_params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search(
        self,
        query: str,
        page_size: int = 20,
        include_preprints: bool = True,
    ) -> list[EvidenceRecord]:
        """
        Searches Europe PMC and returns parsed EvidenceRecords.
        By default includes preprints (Europe PMC's main differentiator
        from PubMed) -- pass include_preprints=False to restrict to
        peer-reviewed only, which appends 'NOT SRC:PPR' to the query.
        """
        full_query = query if include_preprints else f"({query}) NOT SRC:PPR"
        data = self._get(
            {
                "query": full_query,
                "pageSize": page_size,
                "resultType": "core",
                # Europe PMC's MeSH/synonym query expansion is OFF by
                # default -- turning it on is the Europe-PMC-side
                # equivalent of PubMed's Automatic Term Mapping, and closes
                # the same class of gap on rare-disease vocabulary (e.g.
                # "anti-MDA5" vs "MDA5" vs "IFIH1").
                "synonym": "TRUE",
            }
        )
        results = data.get("resultList", {}).get("result", [])
        records = [self._parse_result(r) for r in results]
        return [r for r in records if r is not None]

    def search_preprints_only(self, query: str, page_size: int = 20) -> list[EvidenceRecord]:
        """Convenience: only preprints matching the query (SRC:PPR)."""
        full_query = f"({query}) AND SRC:PPR"
        data = self._get(
            {"query": full_query, "pageSize": page_size, "resultType": "core"}
        )
        results = data.get("resultList", {}).get("result", [])
        records = [self._parse_result(r) for r in results]
        return [r for r in records if r is not None]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_result(self, result: dict) -> Optional[EvidenceRecord]:
        # Europe PMC's own composite-style identity: id + source (e.g.
        # source="MED", id="12345678" or source="PPR", id="...").
        native_id = result.get("id")
        source_code = result.get("source", "")
        if not native_id:
            return None

        is_preprint = source_code == "PPR"

        title = result.get("title", "") or ""
        abstract = result.get("abstractText", "") or ""
        # Europe PMC doesn't section abstracts like PubMed's NlmCategory --
        # keep the schema shape consistent by bucketing everything as
        # UNASSIGNED, same convention as an unstructured PubMed abstract.
        abstract_sections = {"UNASSIGNED": abstract} if abstract else {}

        journal_info = result.get("journalInfo", {}) or {}
        journal = journal_info.get("journal", {}).get("title") if journal_info else None
        pub_date = self._extract_pub_date(result, journal_info)

        authors = self._extract_authors(result)
        doi = result.get("doi")
        pmid = result.get("pmid")  # cross-reference to PubMed, when available

        pub_type_list = result.get("pubTypeList", {}).get("pubType", [])
        if isinstance(pub_type_list, str):
            pub_type_list = [pub_type_list]
        study_design = self._guess_study_design(pub_type_list, title, abstract)

        is_open_access = result.get("isOpenAccess") == "Y"

        return EvidenceRecord(
            record_id=f"EPMC/{native_id}",
            source=SourceDB.EUROPE_PMC,
            native_id=native_id,
            doi=doi,
            title=title,
            abstract=abstract,
            abstract_sections=abstract_sections,
            journal=journal,
            publication_date=pub_date,
            authors=authors,
            study_design=study_design,
            is_preprint=is_preprint,
            # Europe PMC doesn't independently confirm retraction status in
            # the base search response -- retraction_check.py (Crossref +
            # native PubMed check) is the source of truth for that flag.
            # We leave it False here rather than guessing; retrieve.py
            # should overwrite this after cross-referencing.
            is_retracted=False,
            url=self._build_url(source_code, native_id, pmid),
        )

    @staticmethod
    def _extract_pub_date(result: dict, journal_info: dict) -> Optional[date]:
        # journalInfo.pubYear is a clean integer per the architecture doc --
        # far less messy than PubMed's PubDate parsing.
        pub_year = journal_info.get("yearOfPublication") or journal_info.get("pubYear")
        if pub_year:
            try:
                return date(int(pub_year), 1, 1)
            except (ValueError, TypeError):
                pass
        # Fallback: firstPublicationDate is sometimes given as YYYY-MM-DD
        first_pub = result.get("firstPublicationDate")
        if first_pub:
            try:
                parts = [int(p) for p in first_pub.split("-")]
                while len(parts) < 3:
                    parts.append(1)
                return date(*parts[:3])
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _extract_authors(result: dict) -> list[str]:
        author_list = result.get("authorList", {}).get("author", [])
        names = []
        for author in author_list:
            full_name = author.get("fullName")
            if full_name:
                names.append(full_name)
        return names

    @staticmethod
    def _guess_study_design(
        pub_types: list[str], title: str, abstract: str
    ) -> StudyDesign:
        pub_types_lower = {pt.lower() for pt in pub_types}
        if "review-article" in pub_types_lower or "systematic review" in pub_types_lower:
            return StudyDesign.SYSTEMATIC_REVIEW
        if "meta-analysis" in pub_types_lower:
            return StudyDesign.META_ANALYSIS
        if "clinical trial" in pub_types_lower or "randomized controlled trial" in pub_types_lower:
            return StudyDesign.RCT

        haystack = f"{title} {abstract}".lower()
        for keyword, design in _STUDY_DESIGN_KEYWORDS:
            if keyword in haystack:
                return design
        return StudyDesign.UNKNOWN

    @staticmethod
    def _build_url(source_code: str, native_id: str, pmid: Optional[str]) -> str:
        if pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        return f"https://europepmc.org/article/{source_code}/{native_id}"