"""
core/schema.py

Unified schema every retrieval source (PubMed, Europe PMC, ClinicalTrials.gov)
normalizes into. Nothing downstream (agents, verifier, API) should ever look
at raw PubMed XML or raw ClinicalTrials JSON directly -- everything goes
through EvidenceRecord first.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class StudyDesign(str, Enum):
    """
    Rough evidence-hierarchy buckets. This is intentionally coarse --
    the Appraiser agent (not this file) does the nuanced ranking.
    Retrieval layer just makes a best-effort guess so downstream code
    always has *something* to sort/filter on, even before Appraiser runs.
    """

    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    RCT = "rct"
    COHORT = "cohort"
    CASE_CONTROL = "case_control"
    CASE_SERIES = "case_series"
    CASE_REPORT = "case_report"
    REVIEW = "review"
    GUIDELINE = "guideline"
    PREPRINT = "preprint"
    CLINICAL_TRIAL_RECORD = "clinical_trial_record"  # from ClinicalTrials.gov, not a paper
    UNKNOWN = "unknown"


class SourceDB(str, Enum):
    PUBMED = "pubmed"
    EUROPE_PMC = "europe_pmc"
    CLINICAL_TRIALS = "clinical_trials"


@dataclass
class EvidenceRecord:
    # --- identity ---
    record_id: str  # our internal id, see citation_key()
    source: SourceDB
    native_id: str  # PMID, Europe PMC id, or NCT number -- whatever the source calls it natively
    doi: Optional[str] = None

    # --- bibliographic core ---
    title: str = ""
    abstract: str = ""
    abstract_sections: dict[str, str] = field(default_factory=dict)  # NlmCategory -> text, PubMed only
    journal: Optional[str] = None
    publication_date: Optional[date] = None
    authors: list[str] = field(default_factory=list)

    # --- evidence quality signals ---
    study_design: StudyDesign = StudyDesign.UNKNOWN
    is_preprint: bool = False
    is_retracted: bool = False
    retraction_notice_id: Optional[str] = None  # points at the retraction notice record, if known
    retraction_source: Optional[str] = None  # "pubmed" | "crossref" -- which check caught it

    # --- clinical trial specific (only populated for SourceDB.CLINICAL_TRIALS) ---
    trial_status: Optional[str] = None  # RECRUITING, COMPLETED, etc -- raw enum string from CT.gov

    # --- access ---
    url: Optional[str] = None

    def citation_key(self) -> str:
        """
        Stable, source-prefixed key used everywhere a claim needs to point at
        a specific piece of evidence (Synthesizer citation_id, Verifier
        existence check, etc). Mirrors Europe PMC's {source}/{id} convention
        so cross-source dedup logic can reuse it directly.
        """
        prefix = {
            SourceDB.PUBMED: "MED",
            SourceDB.EUROPE_PMC: "EPMC",
            SourceDB.CLINICAL_TRIALS: "NCT",
        }[self.source]
        return f"{prefix}/{self.native_id}"

    def content_hash(self) -> str:
        """
        Used for cross-source deduplication. PMID is the real primary key
        for peer-reviewed literature (per architecture doc: DOI is NOT
        reliable -- missing on older records, and preprints get a different
        DOI than their published version). So dedup priority is:
            1. PMID, if this record has one (via native_id when source==PUBMED,
               or a resolved PMID cross-reference for Europe PMC records)
            2. DOI, as a fallback only
            3. normalized title, as a last resort
        This method just hashes whatever identity retrieve.py decides is
        authoritative for this record -- see retrieval/retrieve.py for the
        actual PMID > DOI > title precedence logic.
        """
        basis = self.doi or self.title.strip().lower()
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]