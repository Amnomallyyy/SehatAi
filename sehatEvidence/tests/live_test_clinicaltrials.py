"""
tests/live_test_clinicaltrials.py

Run this on a machine with real internet access to confirm the client
talks to the real ClinicalTrials.gov v2 API correctly.

Usage:
    python3 tests/live_test_clinicaltrials.py

No env vars or API key needed -- CT.gov's API is fully public.
Rate limit is 50 req/min, so this script deliberately stays to 1-2 pages.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.clinicaltrials import ClinicalTrialsClient, RELEVANT_STATUSES_FOR_PIPELINE


def run():
    client = ClinicalTrialsClient()

    print("Searching ClinicalTrials.gov for 'GLP-1 agonist cardiovascular'...")
    records = client.search(
        "GLP-1 agonist cardiovascular",
        page_size=5,
        max_pages=1,
        statuses=list(RELEVANT_STATUSES_FOR_PIPELINE),
    )
    print(f"Got {len(records)} records.\n")
    for r in records:
        print(f"[{r.citation_key()}] {r.title}")
        print(f"  status: {r.trial_status} | est. completion/start: {r.publication_date}")
        print(f"  sponsor: {r.journal}")
        print(f"  url: {r.url}")
        print()


if __name__ == "__main__":
    run()