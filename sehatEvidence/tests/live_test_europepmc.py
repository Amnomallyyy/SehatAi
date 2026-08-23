"""
tests/live_test_europepmc.py

Run this on a machine with real internet access to confirm the client
talks to the real Europe PMC API correctly.

Usage:
    python3 tests/live_test_europepmc.py

No env vars or API key needed -- Europe PMC's API is fully public.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.europepmc import EuropePMCClient


def run():
    client = EuropePMCClient()

    print("Searching Europe PMC (including preprints) for 'GLP-1 agonist cardiovascular outcomes'...")
    records = client.search("GLP-1 agonist cardiovascular outcomes", page_size=5)
    print(f"Got {len(records)} records.\n")
    for r in records:
        print(f"[{r.citation_key()}] {r.title}")
        print(f"  journal: {r.journal} | date: {r.publication_date} | design: {r.study_design}")
        print(f"  doi: {r.doi} | is_preprint: {r.is_preprint}")
        print(f"  url: {r.url}")
        print()

    print("\nSearching preprints only (SRC:PPR) for 'GLP-1 obesity'...")
    preprints = client.search_preprints_only("GLP-1 obesity", page_size=3)
    print(f"Got {len(preprints)} preprint records.\n")
    for r in preprints:
        print(f"[{r.citation_key()}] {r.title} (is_preprint={r.is_preprint})")


if __name__ == "__main__":
    run()