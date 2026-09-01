"""
tests/test_clinicaltrials_parsing.py

Offline test -- no internet required. Feeds a realistic ClinicalTrials.gov
v2 study object directly into the parser.

Run: python3 tests/test_clinicaltrials_parsing.py   (from evidenceboard/ root)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.clinicaltrials import ClinicalTrialsClient
from core.schema import StudyDesign

SAMPLE_STUDY = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT05555555",
            "briefTitle": "A Study of Drug Z in Adults With Condition W",
            "officialTitle": "A Phase 3 Randomized Study of Drug Z in Adults With Condition W",
            "organization": {"fullName": "Fictional Pharma Inc.", "class": "INDUSTRY"},
        },
        "statusModule": {
            "overallStatus": "ACTIVE_NOT_RECRUITING",
            "startDateStruct": {"date": "2024-03", "type": "ACTUAL"},
            "completionDateStruct": {"date": "2026-09-15", "type": "ESTIMATED"},
        },
        "descriptionModule": {
            "briefSummary": "This study evaluates the safety and efficacy of Drug Z in adults with Condition W.",
        },
    }
}

# Edge case: minimal study, no completion date, no org
SAMPLE_STUDY_MINIMAL = {
    "protocolSection": {
        "identificationModule": {
            "nctId": "NCT06666666",
            "briefTitle": "Pilot Study of Intervention Q",
        },
        "statusModule": {
            "overallStatus": "RECRUITING",
            "startDateStruct": {"date": "2026-01"},
        },
        "descriptionModule": {},
    }
}


def run():
    client = ClinicalTrialsClient()

    r1 = client._parse_study(SAMPLE_STUDY)
    print("--- Record 1 ---")
    print("citation_key:", r1.citation_key())
    print("title:", r1.title)
    print("trial_status:", r1.trial_status)
    print("publication_date (=completion date):", r1.publication_date)
    print("journal (=sponsor org):", r1.journal)
    print("study_design:", r1.study_design)
    print("url:", r1.url)
    print()

    assert r1.citation_key() == "NCT/NCT05555555"
    assert r1.trial_status == "ACTIVE_NOT_RECRUITING"
    assert r1.publication_date == __import__("datetime").date(2026, 9, 15), (
        "should prefer completion date over start date"
    )
    assert r1.journal == "Fictional Pharma Inc."
    assert r1.study_design == StudyDesign.CLINICAL_TRIAL_RECORD
    assert r1.doi is None
    assert r1.authors == []
    assert r1.url == "https://clinicaltrials.gov/study/NCT05555555"

    r2 = client._parse_study(SAMPLE_STUDY_MINIMAL)
    print("--- Record 2 (minimal) ---")
    print("citation_key:", r2.citation_key())
    print("trial_status:", r2.trial_status)
    print("publication_date (=start date fallback):", r2.publication_date)
    print("journal (=sponsor org, missing):", r2.journal)

    assert r2.trial_status == "RECRUITING"
    assert r2.publication_date == __import__("datetime").date(2026, 1, 1), (
        "should fall back to start date when completion date is missing"
    )
    assert r2.journal is None

    print("\nAll assertions passed.")


if __name__ == "__main__":
    run()