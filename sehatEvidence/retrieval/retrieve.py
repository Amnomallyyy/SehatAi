"""
retrieval/retrieve.py

STAGE 1 single entry point. Everything downstream (Appraiser, Synthesizer,
Red Team, Verifier) calls gather_evidence() and NOTHING ELSE from this
package -- they never touch pubmed.py / europepmc.py / clinicaltrials.py
directly.

Design decision (deliberate, defensible): retrieval is a FIXED, DETERMINISTIC
fan-out, not an agent-callable tool. The agents do not decide when or what to
retrieve. Reasons:
  - The Verifier's existence check is "does this citation resolve to a record
    in the retrieved pool?" That only works if the pool is frozen before
    synthesis begins.
  - The benchmark needs reproducibility: same question -> same evidence pool
    -> comparable numbers across runs and across baselines.
  - Bounded API cost per question, which matters on a 3 req/s PubMed limit
    and a live demo.
Breadth still comes from the Strategist producing 3-5 queries up front, which
we fan out here in parallel.

ZERO AI in this module.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from core.schema import EvidenceRecord, SourceDB
from retrieval.clinicaltrials import ClinicalTrialsClient, RELEVANT_STATUSES_FOR_PIPELINE
from retrieval.europepmc import EuropePMCClient
from retrieval.pubmed import PubMedClient
from retrieval.retraction_check import RetractionChecker


class EvidenceGatherer:
    def __init__(
        self,
        pubmed: Optional[PubMedClient] = None,
        europepmc: Optional[EuropePMCClient] = None,
        clinicaltrials: Optional[ClinicalTrialsClient] = None,
        retraction_checker: Optional[RetractionChecker] = None,
        check_retractions: bool = True,
    ):
        self.pubmed = pubmed or PubMedClient()
        self.europepmc = europepmc or EuropePMCClient()
        self.clinicaltrials = clinicaltrials or ClinicalTrialsClient()
        self.retraction_checker = retraction_checker or RetractionChecker()
        self.check_retractions = check_retractions

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def gather_evidence(
        self,
        queries: list[str] | str,
        per_source_limit: int = 15,
        include_trials: bool = True,
        include_preprints: bool = True,
    ) -> list[EvidenceRecord]:
        """
        Takes one query string or a list of them (Strategist normally
        produces 3-5), fans out to all sources in parallel, merges,
        dedupes, and cross-references retraction status.

        Returns a single clean list of EvidenceRecords.
        """
        if isinstance(queries, str):
            queries = [queries]

        raw_records = self._fan_out(
            queries, per_source_limit, include_trials, include_preprints
        )
        merged = self._dedupe(raw_records)
        if self.check_retractions:
            merged = self._cross_reference_retractions(merged)
        return merged

    # ------------------------------------------------------------------
    # Step 1: parallel fan-out
    # ------------------------------------------------------------------

    def _fan_out(
        self,
        queries: list[str],
        per_source_limit: int,
        include_trials: bool,
        include_preprints: bool,
    ) -> list[EvidenceRecord]:
        tasks = []
        for query in queries:
            tasks.append(("pubmed", query))
            tasks.append(("europepmc", query))
            if include_trials:
                tasks.append(("clinicaltrials", query))

        records: list[EvidenceRecord] = []
        # Modest worker count -- the per-client TokenBuckets do the real
        # rate limiting, threads just overlap the network waits.
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(
                    self._fetch_one,
                    source,
                    query,
                    per_source_limit,
                    include_preprints,
                ): (source, query)
                for source, query in tasks
            }
            for future in as_completed(futures):
                source, query = futures[future]
                try:
                    records.extend(future.result())
                except Exception as exc:
                    # One source failing must not kill the whole retrieval --
                    # partial evidence is better than none, and the answer
                    # will simply be grounded in fewer sources.
                    print(f"[retrieve] {source} failed for query {query!r}: {exc}")
        return records

    def _fetch_one(
        self,
        source: str,
        query: str,
        limit: int,
        include_preprints: bool,
    ) -> list[EvidenceRecord]:
        if source == "pubmed":
            return self.pubmed.search_and_fetch(query, retmax=limit)
        if source == "europepmc":
            return self.europepmc.search(
                query, page_size=limit, include_preprints=include_preprints
            )
        if source == "clinicaltrials":
            return self.clinicaltrials.search(
                query,
                page_size=limit,
                max_pages=1,
                statuses=list(RELEVANT_STATUSES_FOR_PIPELINE),
            )
        return []

    # ------------------------------------------------------------------
    # Step 2: dedupe
    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
        """
        The same study routinely appears in BOTH PubMed and Europe PMC.
        Dedup priority, per the architecture research:
            1. PMID  -- most reliable key for peer-reviewed literature
            2. DOI   -- fallback only (missing on older records; preprints
                        get a different DOI than their published version)
            3. normalized title -- last resort

        When two records collide, PubMed wins over Europe PMC (NLM-curated
        metadata, and it's the only source with a native retraction signal).
        Clinical trial records never collide with papers -- different ID space.
        """
        by_key: dict[str, EvidenceRecord] = {}

        # Process PubMed first so it naturally claims keys before Europe PMC
        # tries to; ordering makes the "PubMed wins" rule fall out for free.
        ordered = sorted(
            records, key=lambda r: 0 if r.source == SourceDB.PUBMED else 1
        )

        for record in ordered:
            key = EvidenceGatherer._dedupe_key(record)
            if key not in by_key:
                by_key[key] = record
            else:
                # Collision: keep the incumbent (PubMed-preferred by ordering),
                # but salvage a DOI if the incumbent lacks one.
                incumbent = by_key[key]
                if not incumbent.doi and record.doi:
                    incumbent.doi = record.doi

        return list(by_key.values())

    @staticmethod
    def _dedupe_key(record: EvidenceRecord) -> str:
        # Clinical trials live in their own ID space -- never merge with papers
        if record.source == SourceDB.CLINICAL_TRIALS:
            return f"nct:{record.native_id}"

        # PubMed records ARE a PMID
        if record.source == SourceDB.PUBMED:
            return f"pmid:{record.native_id}"

        # Europe PMC MED-sourced records carry the PMID as their native id;
        # PPR (preprint) ids are not PMIDs, so fall through to DOI/title.
        if record.source == SourceDB.EUROPE_PMC:
            if record.native_id.isdigit():
                return f"pmid:{record.native_id}"

        if record.doi:
            return f"doi:{record.doi.strip().lower()}"

        return f"title:{record.title.strip().lower()}"

    # ------------------------------------------------------------------
    # Step 3: retraction cross-reference
    # ------------------------------------------------------------------

    def _cross_reference_retractions(
        self, records: list[EvidenceRecord]
    ) -> list[EvidenceRecord]:
        """
        pubmed.py already flagged anything NLM has curated as retracted.
        This adds the Crossref/Retraction Watch layer, which is the ONLY
        retraction signal available for records that came from Europe PMC.

        Records already flagged by PubMed are skipped (no need to re-check).
        Trial registrations are skipped (retraction isn't applicable).
        """
        to_check = [
            r
            for r in records
            if r.doi
            and not r.is_retracted
            and r.source != SourceDB.CLINICAL_TRIALS
        ]
        if not to_check:
            return records

        results = self.retraction_checker.check_many([r.doi for r in to_check])

        for record in to_check:
            is_retracted, source_detail = results.get(record.doi, (False, None))
            if is_retracted:
                record.is_retracted = True
                record.retraction_source = source_detail

        return records


# Module-level convenience so callers can just do:
#     from retrieval.retrieve import gather_evidence
_default_gatherer: Optional[EvidenceGatherer] = None


def gather_evidence(
    queries: list[str] | str,
    per_source_limit: int = 15,
    include_trials: bool = True,
    include_preprints: bool = True,
) -> list[EvidenceRecord]:
    global _default_gatherer
    if _default_gatherer is None:
        _default_gatherer = EvidenceGatherer()
    return _default_gatherer.gather_evidence(
        queries,
        per_source_limit=per_source_limit,
        include_trials=include_trials,
        include_preprints=include_preprints,
    )