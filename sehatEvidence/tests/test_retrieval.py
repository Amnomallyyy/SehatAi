"""
tests/live_test_retrieve.py

Full Stage 1 end-to-end test against real APIs: fans out to PubMed,
Europe PMC, and ClinicalTrials.gov in parallel, merges, dedupes, and
cross-references retraction status via Crossref.

Usage:
    export NCBI_TOOL_NAME=evidenceboard
    export NCBI_EMAIL=your.email@example.com
    python3 tests/live_test_retrieve.py

Note: this makes noticeably more API calls than the single-source tests
(3 sources x N queries). Crossref retraction checking runs serially over
every DOI found, so expect this to take a bit -- that's the rate limiter
doing its job, not a hang.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collections import Counter

from core.schema import SourceDB
from retrieval.retrieve import gather_evidence

QUERIES = [
    "GLP-1 receptor agonist cardiovascular outcomes 2026",
    "semaglutide heart failure trial",
]


def run():
    print(f"Gathering evidence for {len(QUERIES)} queries across 3 sources...")
    print("(Crossref retraction checks run serially -- this may take a minute)\n")
    start = time.time()

    records = gather_evidence(QUERIES, per_source_limit=8)

    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s -- {len(records)} unique records after dedupe.\n")

    by_source = Counter(r.source.value for r in records)
    print("Records by source:")
    for source, count in by_source.items():
        print(f"  {source}: {count}")

    retracted = [r for r in records if r.is_retracted]
    preprints = [r for r in records if r.is_preprint]
    print(f"\nFlagged retracted: {len(retracted)}")
    print(f"Preprints: {len(preprints)}")

    print("\n--- Sample records ---")
    for r in records[:8]:
        flags = []
        if r.is_retracted:
            flags.append(f"RETRACTED({r.retraction_source})")
        if r.is_preprint:
            flags.append("PREPRINT")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"[{r.citation_key()}]{flag_str}")
        print(f"  {r.title[:95]}")
        print(f"  {r.source.value} | {r.publication_date} | {r.study_design.value}")
        if r.trial_status:
            print(f"  trial status: {r.trial_status}")
        print()

    if retracted:
        print("--- Retracted records caught ---")
        for r in retracted:
            print(f"[{r.citation_key()}] {r.title[:80]}")
            print(f"  caught by: {r.retraction_source}")


if __name__ == "__main__":
    run()