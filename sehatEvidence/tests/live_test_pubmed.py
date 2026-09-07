"""
tests/live_test_pubmed.py

NOT part of the offline test suite. Run this ONCE on a machine with real
internet access to confirm the client actually talks to NCBI correctly --
the sandbox this was built in cannot reach eutils.ncbi.nlm.nih.gov.

Usage:
    export NCBI_TOOL_NAME=evidenceboard
    export NCBI_EMAIL=you@example.com
    export NCBI_API_KEY=your_key_here   # optional but recommended
    python tests/live_test_pubmed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from retrieval.pubmed import PubMedClient


def run():
    client = PubMedClient()
    print("Searching PubMed for 'GLP-1 agonist cardiovascular outcomes'...")
    records = client.search_and_fetch(
        "GLP-1 agonist cardiovascular outcomes", retmax=5
    )
    print(f"Got {len(records)} records.\n")
    for r in records:
        print(f"[{r.citation_key()}] {r.title}")
        print(f"  journal: {r.journal} | date: {r.publication_date} | design: {r.study_design}")
        print(f"  doi: {r.doi} | retracted: {r.is_retracted}")
        print(f"  abstract sections: {list(r.abstract_sections.keys())}")
        print()


if __name__ == "__main__":
    run()