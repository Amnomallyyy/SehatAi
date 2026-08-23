"""
retrieval/pubmed.py

NCBI E-utilities client for PubMed. Two-step flow:
  1. esearch: query string -> list of PMIDs
  2. efetch:  list of PMIDs -> full XML records -> parsed EvidenceRecord list

This module does ZERO AI -- it's pure retrieval + parsing. Study-design
guessing here is a cheap keyword heuristic only; real appraisal is
agents/appraiser.py's job later in the pipeline.

Requires NCBI_TOOL_NAME and NCBI_EMAIL to be set (see usage guidelines --
every request must self-identify or NCBI may silently blacklist the IP).
NCBI_API_KEY is optional but raises the rate limit from 3req/s to 10req/s.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import date
from typing import Optional

import requests

from core.cache import TokenBucket, ncbi_rate_limiter
from core.schema import EvidenceRecord, SourceDB, StudyDesign

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# NlmCategory values PubMed uses to tag structured abstract sections.
# See efetch XML: Article/Abstract/AbstractText[@NlmCategory=...]
NLM_ABSTRACT_CATEGORIES = {
    "BACKGROUND",
    "OBJECTIVE",
    "METHODS",
    "RESULTS",
    "CONCLUSIONS",
    "UNASSIGNED",
}

# Cheap keyword heuristic only -- real ranking happens in agents/appraiser.py.
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


class PubMedClient:
    def __init__(
        self,
        tool_name: Optional[str] = None,
        email: Optional[str] = None,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.tool_name = tool_name or os.environ.get("NCBI_TOOL_NAME")
        self.email = email or os.environ.get("NCBI_EMAIL")
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")

        if not self.tool_name or not self.email:
            raise ValueError(
                "NCBI requires 'tool' and 'email' on every E-utilities request. "
                "Set NCBI_TOOL_NAME and NCBI_EMAIL env vars (or pass them in directly) "
                "before making any calls -- unidentified traffic risks an IP block."
            )

        self.session = session or requests.Session()
        self._limiter: TokenBucket = ncbi_rate_limiter(has_api_key=bool(self.api_key))

    def _base_params(self) -> dict:
        params = {"tool": self.tool_name, "email": self.email}
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _get(self, url: str, params: dict) -> requests.Response:
        self._limiter.acquire()
        full_params = {**self._base_params(), **params}
        resp = self.session.get(url, params=full_params, timeout=30)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Step 1: esearch
    # ------------------------------------------------------------------

    def esearch(self, query: str, retmax: int = 20) -> list[str]:
        """Returns a list of PMIDs matching the query."""
        resp = self._get(
            ESEARCH_URL,
            {"db": "pubmed", "term": query, "retmax": retmax, "retmode": "json"},
        )
        data = resp.json()
        return data.get("esearchresult", {}).get("idlist", [])

    # ------------------------------------------------------------------
    # Step 2: efetch + parse
    # ------------------------------------------------------------------

    def efetch(self, pmids: list[str]) -> list[EvidenceRecord]:
        """Fetches full records for a list of PMIDs and parses them."""
        if not pmids:
            return []
        resp = self._get(
            EFETCH_URL,
            {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        )
        return self._parse_efetch_xml(resp.text)

    def search_and_fetch(self, query: str, retmax: int = 20) -> list[EvidenceRecord]:
        """Convenience: esearch then efetch in one call."""
        pmids = self.esearch(query, retmax=retmax)
        return self.efetch(pmids)

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    def _parse_efetch_xml(self, xml_text: str) -> list[EvidenceRecord]:
        root = ET.fromstring(xml_text)
        records: list[EvidenceRecord] = []
        for article_elem in root.findall(".//PubmedArticle"):
            record = self._parse_single_article(article_elem)
            if record is not None:
                records.append(record)
        return records

    def _parse_single_article(self, article_elem: ET.Element) -> Optional[EvidenceRecord]:
        medline = article_elem.find("MedlineCitation")
        if medline is None:
            return None

        pmid_elem = medline.find("PMID")
        if pmid_elem is None or not pmid_elem.text:
            return None
        pmid = pmid_elem.text.strip()

        article = medline.find("Article")
        if article is None:
            return None

        title = self._extract_title(article)
        abstract_text, abstract_sections = self._extract_abstract(article)
        journal = self._extract_journal(article)
        pub_date = self._extract_pub_date(article)
        authors = self._extract_authors(article)
        doi = self._extract_doi(article, article_elem)
        pub_types = self._extract_publication_types(article)
        study_design = self._guess_study_design(pub_types, title, abstract_text)
        is_preprint = "Preprint" in pub_types
        is_retracted, retraction_notice_id = self._check_native_retraction(
            medline, pub_types
        )

        return EvidenceRecord(
            record_id=f"MED/{pmid}",
            source=SourceDB.PUBMED,
            native_id=pmid,
            doi=doi,
            title=title,
            abstract=abstract_text,
            abstract_sections=abstract_sections,
            journal=journal,
            publication_date=pub_date,
            authors=authors,
            study_design=study_design,
            is_preprint=is_preprint,
            is_retracted=is_retracted,
            retraction_notice_id=retraction_notice_id,
            retraction_source="pubmed" if is_retracted else None,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        )

    @staticmethod
    def _extract_title(article: ET.Element) -> str:
        title_elem = article.find("ArticleTitle")
        if title_elem is None or not title_elem.text:
            return ""
        return "".join(title_elem.itertext()).strip()

    @staticmethod
    def _extract_abstract(article: ET.Element) -> tuple[str, dict[str, str]]:
        """
        Returns (flat_abstract_text, sections_by_nlm_category).
        Handles both structured abstracts (multiple AbstractText nodes with
        Label/NlmCategory attributes) and plain unstructured ones (single
        node, no attributes).
        """
        abstract_elem = article.find("Abstract")
        if abstract_elem is None:
            return "", {}

        parts: list[str] = []
        sections: dict[str, str] = {}

        for text_elem in abstract_elem.findall("AbstractText"):
            text = "".join(text_elem.itertext()).strip()
            if not text:
                continue
            label = text_elem.get("Label")
            category = text_elem.get("NlmCategory", "UNASSIGNED")

            # For display: prefix with the publisher's own label if present
            if label:
                parts.append(f"{label}: {text}")
            else:
                parts.append(text)

            # For programmatic sectioning: key by NlmCategory
            if category in sections:
                sections[category] += " " + text
            else:
                sections[category] = text

        return " ".join(parts), sections

    @staticmethod
    def _extract_journal(article: ET.Element) -> Optional[str]:
        journal_elem = article.find("Journal")
        if journal_elem is None:
            return None
        title_elem = journal_elem.find("Title")
        if title_elem is not None and title_elem.text:
            return title_elem.text.strip()
        iso_elem = journal_elem.find("ISOAbbreviation")
        if iso_elem is not None and iso_elem.text:
            return iso_elem.text.strip()
        return None

    @staticmethod
    def _extract_pub_date(article: ET.Element) -> Optional[date]:
        """
        PubDate is notoriously irregular: sometimes Year/Month/Day nodes,
        sometimes a single MedDate string like '2023 Jan-Feb'. We do a
        best-effort parse and fall back to Jan 1 for missing month/day,
        or None if even the year is unparseable.
        """
        pub_date_elem = article.find("Journal/JournalIssue/PubDate")
        if pub_date_elem is None:
            return None

        year_elem = pub_date_elem.find("Year")
        if year_elem is not None and year_elem.text:
            try:
                year = int(year_elem.text.strip())
            except ValueError:
                return None

            month = 1
            month_elem = pub_date_elem.find("Month")
            if month_elem is not None and month_elem.text:
                month = _parse_month(month_elem.text.strip()) or 1

            day = 1
            day_elem = pub_date_elem.find("Day")
            if day_elem is not None and day_elem.text:
                try:
                    day = int(day_elem.text.strip())
                except ValueError:
                    day = 1

            try:
                return date(year, month, day)
            except ValueError:
                return date(year, 1, 1)

        # Fallback: MedDate free-text like "2023 Jan-Feb" -- just grab the year
        med_date_elem = pub_date_elem.find("MedDate")
        if med_date_elem is not None and med_date_elem.text:
            for token in med_date_elem.text.strip().split():
                if token.isdigit() and len(token) == 4:
                    return date(int(token), 1, 1)

        return None

    @staticmethod
    def _extract_authors(article: ET.Element) -> list[str]:
        authors: list[str] = []
        author_list = article.find("AuthorList")
        if author_list is None:
            return authors
        for author_elem in author_list.findall("Author"):
            last = author_elem.find("LastName")
            fore = author_elem.find("ForeName")
            if last is not None and last.text:
                name = last.text.strip()
                if fore is not None and fore.text:
                    name = f"{fore.text.strip()} {name}"
                authors.append(name)
            else:
                # collective/corporate author
                collective = author_elem.find("CollectiveName")
                if collective is not None and collective.text:
                    authors.append(collective.text.strip())
        return authors

    @staticmethod
    def _extract_doi(article: ET.Element, article_elem: ET.Element) -> Optional[str]:
        # Primary location
        for eloc in article.findall("ELocationID"):
            if eloc.get("EIdType") == "doi" and eloc.text:
                return eloc.text.strip()
        # Redundant fallback location in PubmedData
        pubmed_data = article_elem.find("PubmedData")
        if pubmed_data is not None:
            for article_id in pubmed_data.findall("ArticleIdList/ArticleId"):
                if article_id.get("IdType") == "doi" and article_id.text:
                    return article_id.text.strip()
        return None

    @staticmethod
    def _extract_publication_types(article: ET.Element) -> set[str]:
        pub_types = set()
        pub_type_list = article.find("PublicationTypeList")
        if pub_type_list is None:
            return pub_types
        for pt in pub_type_list.findall("PublicationType"):
            if pt.text:
                pub_types.add(pt.text.strip())
        return pub_types

    @staticmethod
    def _guess_study_design(
        pub_types: set[str], title: str, abstract: str
    ) -> StudyDesign:
        # Prefer PubMed's own PublicationType tags when they map cleanly
        pub_types_lower = {pt.lower() for pt in pub_types}
        if "systematic review" in pub_types_lower:
            return StudyDesign.SYSTEMATIC_REVIEW
        if "meta-analysis" in pub_types_lower:
            return StudyDesign.META_ANALYSIS
        if "randomized controlled trial" in pub_types_lower:
            return StudyDesign.RCT
        if "practice guideline" in pub_types_lower or "guideline" in pub_types_lower:
            return StudyDesign.GUIDELINE
        if "preprint" in pub_types_lower:
            return StudyDesign.PREPRINT

        # Fallback: cheap keyword scan over title+abstract
        haystack = f"{title} {abstract}".lower()
        for keyword, design in _STUDY_DESIGN_KEYWORDS:
            if keyword in haystack:
                return design
        return StudyDesign.UNKNOWN

    @staticmethod
    def _check_native_retraction(
        medline: ET.Element, pub_types: set[str]
    ) -> tuple[bool, Optional[str]]:
        """
        Two independent signals PubMed provides for retraction, per the
        efetch schema:
          1. PublicationTypeList contains "Retracted Publication"
          2. CommentsCorrectionsList has a CommentsCorrections with
             RefType="RetractionIn", which also gives us the PMID of the
             retraction notice itself.
        Either signal alone is sufficient to flag is_retracted=True.
        """
        is_retracted = "Retracted Publication" in pub_types
        retraction_notice_pmid: Optional[str] = None

        comments_list = medline.find("CommentsCorrectionsList")
        if comments_list is not None:
            for cc in comments_list.findall("CommentsCorrections"):
                if cc.get("RefType") == "RetractionIn":
                    is_retracted = True
                    pmid_elem = cc.find("PMID")
                    if pmid_elem is not None and pmid_elem.text:
                        retraction_notice_pmid = pmid_elem.text.strip()

        return is_retracted, retraction_notice_pmid


_MONTH_MAP = {
    m.lower(): i
    for i, m in enumerate(
        [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ],
        start=1,
    )
}


def _parse_month(month_str: str) -> Optional[int]:
    if month_str.isdigit():
        m = int(month_str)
        return m if 1 <= m <= 12 else None
    key = month_str[:3].lower()
    return _MONTH_MAP.get(key)