"""Parallel appraisal must rank exactly like sequential appraisal.

Appraiser(max_concurrency>1) runs its per-batch LLM calls concurrently
(production sets it from LLM_CONCURRENCY); this pins that the parallel path
produces the same records, scores and order as the original sequential
loop, and that progress reaches batch_count. Offline: the fake LLM scores
each record by a deterministic function of its key.
Runs under pytest or as a script: python -m tests.test_concurrency
"""
import re
import sys
import threading
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.appraiser import Appraiser
from core.schema import EvidenceRecord, SourceDB, StudyDesign

_QUESTION = "Is drug X more effective than placebo for condition Y?"


class KeyedLLM:
    """Returns a relevance for every citation key in the prompt, derived
    from the key itself -- same answer no matter which thread asks."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0

    def complete_json(self, prompt, system=None, temperature=0.1):
        with self.lock:
            self.calls += 1
        keys = re.findall(r"citation_key: (\S+)", prompt)
        return {
            "rankings": [
                {"citation_key": k, "relevance": (sum(map(ord, k)) % 90) + 5, "rationale": "fake"}
                for k in keys
            ]
        }


def _records(n=37):
    return [
        EvidenceRecord(
            record_id=f"rec-pubmed-{i}",
            source=SourceDB.PUBMED,
            native_id=str(1000 + i),
            title=f"Study {i} of drug X",
            abstract="We enrolled 500 patients and followed them for two years.",
            publication_date=date(date.today().year - (i % 8), 6, 1),
            study_design=[StudyDesign.RCT, StudyDesign.COHORT, StudyDesign.SYSTEMATIC_REVIEW][i % 3],
        )
        for i in range(n)
    ]


def _summary(out):
    return [(ar.record.citation_key(), ar.score) for ar in out]


def test_parallel_matches_sequential():
    records = _records()
    sequential = Appraiser(pool_cap=50, llm=KeyedLLM(), max_concurrency=1).appraise(records, _QUESTION)
    progress = []
    llm = KeyedLLM()
    parallel = Appraiser(pool_cap=50, llm=llm, max_concurrency=4).appraise(
        records, _QUESTION, on_progress=lambda i, n: progress.append((i, n))
    )
    assert _summary(parallel) == _summary(sequential)
    # ...and the fake model's relevance really was blended in (otherwise
    # both paths would trivially agree on heuristic-only scores).
    assert all("llm_low_coverage" not in ar.rationale for ar in parallel)
    assert llm.calls == 4  # 37 records / 10 per batch
    assert progress[-1] == (4, 4)


if __name__ == "__main__":
    test_parallel_matches_sequential()
    print("All concurrency tests passed.")
