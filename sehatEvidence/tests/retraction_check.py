"""
tests/test_europmc_retraction_gap.py

Offline test -- no internet. Proves _close_europmc_retraction_gap()
correctly propagates PubMed's native retraction verdict onto a
Europe-PMC-sourced record that has a resolvable PMID, using a mocked
PubMedClient so this doesn't depend on network access.

Run: python3 tests/test_europmc_retraction_gap.py   (from evidenceboard/ root)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

os.environ.setdefault("NCBI_TOOL_NAME", "evidenceboard_test")
os.environ.setdefault("NCBI_EMAIL", "test@example.com")

from core.schema import EvidenceRecord, SourceDB
from retrieval.retrieve import EvidenceGatherer


class FakePubMedClient:
    """Stands in for PubMedClient.efetch without touching the network."""

    def __init__(self, canned_results: dict[str, EvidenceRecord]):
        self.canned_results = canned_results
        self.calls: list[list[str]] = []

    def efetch(self, pmids: list[str]) -> list[EvidenceRecord]:
        self.calls.append(pmids)
        return [self.canned_results[p] for p in pmids if p in self.canned_results]


def make_epmc(native_id, is_retracted=False, title=""):
    return EvidenceRecord(
        record_id=f"EPMC/{native_id}",
        source=SourceDB.EUROPE_PMC,
        native_id=native_id,
        title=title,
        is_retracted=is_retracted,
    )


def make_pubmed(native_id, is_retracted, notice_id=None):
    return EvidenceRecord(
        record_id=f"MED/{native_id}",
        source=SourceDB.PUBMED,
        native_id=native_id,
        title="",
        is_retracted=is_retracted,
        retraction_notice_id=notice_id,
        retraction_source="pubmed" if is_retracted else None,
    )


def run():
    # --- Case 1: Europe-PMC-only record with a resolvable PMID that IS
    # actually retracted in PubMed's curation -- should get flagged. ---
    epmc_records = [
        make_epmc("24476887", title="STAP cells (Europe PMC copy, no PubMed match)"),
        make_epmc("99999999", title="Some unrelated clean paper"),
    ]
    fake_client = FakePubMedClient(
        {
            "24476887": make_pubmed("24476887", is_retracted=True, notice_id="24990753"),
            "99999999": make_pubmed("99999999", is_retracted=False),
        }
    )

    gatherer = EvidenceGatherer(pubmed=fake_client, check_retractions=False)
    # bypass __init__'s other clients since we're only testing this one method
    result = gatherer._close_europmc_retraction_gap(epmc_records)

    retracted_record = next(r for r in result if r.native_id == "24476887")
    clean_record = next(r for r in result if r.native_id == "99999999")

    assert retracted_record.is_retracted is True, "gap fix should catch this"
    assert retracted_record.retraction_notice_id == "24990753"
    assert retracted_record.retraction_source == "pubmed"
    assert clean_record.is_retracted is False, "clean record should stay clean"
    print("Case 1 PASS: Europe-PMC-only retracted record correctly flagged via PMID cross-check")

    # --- Case 2: preprint (PPR id, non-numeric) must be skipped entirely --
    # there's no PMID to resolve, so efetch should never even be called for it.
    fake_client_2 = FakePubMedClient({})
    gatherer2 = EvidenceGatherer(pubmed=fake_client_2, check_retractions=False)
    preprint_records = [make_epmc("PPR123456", title="A preprint")]
    result2 = gatherer2._close_europmc_retraction_gap(preprint_records)

    assert result2[0].is_retracted is False
    assert fake_client_2.calls == [], "should never call efetch for non-numeric ids"
    print("Case 2 PASS: non-numeric PPR ids are skipped, no wasted API call")

    # --- Case 3: already-flagged records should not be re-checked ---
    fake_client_3 = FakePubMedClient({})
    gatherer3 = EvidenceGatherer(pubmed=fake_client_3, check_retractions=False)
    already_flagged = [make_epmc("11111111", is_retracted=True, title="Already known bad")]
    result3 = gatherer3._close_europmc_retraction_gap(already_flagged)

    assert fake_client_3.calls == [], "should skip records already flagged retracted"
    print("Case 3 PASS: already-flagged records are not redundantly re-checked")

    print("\nAll gap-fix assertions passed.")


if __name__ == "__main__":
    run()