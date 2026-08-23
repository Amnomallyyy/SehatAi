"""
tests/test_europepmc_parsing.py

Offline test -- no internet required. Feeds a realistic Europe PMC JSON
response directly into the parser to prove extraction works, including
a peer-reviewed (MED) record and a preprint (PPR) record.

Run: python3 tests/test_europepmc_parsing.py   (from evidenceboard/ root)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.europepmc import EuropePMCClient
from core.schema import StudyDesign

SAMPLE_RESULT_MED = {
    "id": "38112233",
    "source": "MED",
    "pmid": "38112233",
    "doi": "10.1016/j.example.2024.001",
    "title": "A Randomized Controlled Trial of Drug Z for Condition W",
    "abstractText": "We conducted a randomized controlled trial of Drug Z in 300 patients with Condition W.",
    "journalInfo": {
        "journal": {"title": "European Journal of Fictional Medicine"},
        "yearOfPublication": 2024,
    },
    "authorList": {
        "author": [
            {"fullName": "Jane Smith"},
            {"fullName": "John Doe"},
        ]
    },
    "pubTypeList": {"pubType": ["Randomized Controlled Trial", "Journal Article"]},
    "isOpenAccess": "Y",
}

SAMPLE_RESULT_PREPRINT = {
    "id": "10.1101/2025.01.01.123456",
    "source": "PPR",
    "doi": "10.1101/2025.01.01.123456",
    "title": "Preliminary findings on Drug Z (preprint, not peer reviewed)",
    "abstractText": "This is an early, unreviewed report on Drug Z.",
    "journalInfo": {},
    "firstPublicationDate": "2025-01-15",
    "authorList": {"author": [{"fullName": "Alex Researcher"}]},
    "pubTypeList": {},
    "isOpenAccess": "Y",
}

SAMPLE_RESPONSE = {
    "resultList": {"result": [SAMPLE_RESULT_MED, SAMPLE_RESULT_PREPRINT]}
}


def run():
    client = EuropePMCClient()
    results = SAMPLE_RESPONSE["resultList"]["result"]
    records = [client._parse_result(r) for r in results]

    assert len(records) == 2

    r1 = records[0]
    print("--- Record 1 (peer-reviewed) ---")
    print("citation_key:", r1.citation_key())
    print("title:", r1.title)
    print("doi:", r1.doi)
    print("journal:", r1.journal)
    print("publication_date:", r1.publication_date)
    print("authors:", r1.authors)
    print("study_design:", r1.study_design)
    print("is_preprint:", r1.is_preprint)
    print("url:", r1.url)
    print()

    assert r1.citation_key() == "EPMC/38112233"
    assert r1.is_preprint is False
    assert r1.study_design == StudyDesign.RCT
    assert r1.authors == ["Jane Smith", "John Doe"]
    assert r1.publication_date.year == 2024
    assert r1.url == "https://pubmed.ncbi.nlm.nih.gov/38112233/", (
        "should prefer PMID-based URL when available"
    )

    r2 = records[1]
    print("--- Record 2 (preprint) ---")
    print("citation_key:", r2.citation_key())
    print("title:", r2.title)
    print("is_preprint:", r2.is_preprint)
    print("publication_date:", r2.publication_date)
    print("url:", r2.url)

    assert r2.is_preprint is True, "SRC:PPR record should be flagged as preprint"
    assert r2.publication_date.year == 2025, "should fall back to firstPublicationDate"
    assert "europepmc.org/article/PPR" in r2.url, (
        "preprint with no PMID should link to Europe PMC directly"
    )

    print("\nAll assertions passed.")


if __name__ == "__main__":
    run()