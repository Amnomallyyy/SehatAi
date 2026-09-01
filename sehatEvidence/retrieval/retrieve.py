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
Breadth still comes from the Strategist producing as many queries as the
question needs up front (no fixed count -- see agents/strategist.py), which
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

# This module is a standalone entry point (see docstring above) and may be
# imported without config.py ever running, so the clients here would miss
# NCBI_API_KEY / NCBI_EMAIL from .env otherwise -- load it explicitly.
from dotenv import load_dotenv

load_dotenv()


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
        collect_traces: bool = False,
    ):
        """
        Takes one query string or a list of them, fans out to all sources
        in parallel, merges, dedupes, and cross-references retraction
        status.

        Returns a single clean list of EvidenceRecords. When
        collect_traces=True (opt-in; default behavior/signature for
        existing callers is unchanged), returns (records, traces) instead,
        where traces is a list of pubmed.RelaxationTrace -- one per PubMed
        query that needed automatic-term-mapping recovery (queries that
        didn't need it produce no trace at all).
        """
        if isinstance(queries, str):
            queries = [queries]

        raw_records, traces = self._fan_out(
            queries, per_source_limit, include_trials, include_preprints
        )
        merged = self._dedupe(raw_records)
        if self.check_retractions:
            # Order matters: close the Europe-PMC PMID gap FIRST, so records
            # caught by PubMed's native check (100% in our own testing) don't
            # get redundantly re-checked against Crossref (25% in our own
            # testing) afterward.
            merged = self._close_europmc_retraction_gap(merged)
            merged = self._cross_reference_retractions(merged)
        if collect_traces:
            return merged, traces
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
    ):
        tasks = []
        for query in queries:
            tasks.append(("pubmed", query))
            tasks.append(("europepmc", query))
            if include_trials:
                tasks.append(("clinicaltrials", query))

        records: list[EvidenceRecord] = []
        traces = []
        # Worker count raised from 3: with the Strategist's proposer<->critic
        # loop now free to produce more than 3-5 queries, serialization
        # ACROSS queries -- not just across sources -- became the
        # bottleneck. Each source's own TokenBucket still serializes calls
        # to that source, so this cannot cause a 429 storm; it just lets
        # more than one query's three sources overlap at once.
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
                    result_records, trace = future.result()
                    records.extend(result_records)
                    if trace is not None:
                        traces.append(trace)
                except Exception as exc:
                    # One source failing must not kill the whole retrieval --
                    # partial evidence is better than none, and the answer
                    # will simply be grounded in fewer sources.
                    print(f"[retrieve] {source} failed for query {query!r}: {exc}")
        return records, traces

    def _fetch_one(
        self,
        source: str,
        query: str,
        limit: int,
        include_preprints: bool,
    ):
        if source == "pubmed":
            records, trace = self.pubmed.search_and_fetch_traced(query, retmax=limit)
            return records, (trace if trace.dropped else None)
        if source == "europepmc":
            records = self.europepmc.search(
                query, page_size=limit, include_preprints=include_preprints
            )
            return records, None
        if source == "clinicaltrials":
            records = self.clinicaltrials.search_relaxed(
                query,
                page_size=limit,
                max_pages=1,
                statuses=list(RELEVANT_STATUSES_FOR_PIPELINE),
            )
            return records, None
        return [], None

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
    # Step 3a: close the Europe-PMC-only retraction coverage gap
    # ------------------------------------------------------------------

    def _close_europmc_retraction_gap(
        self, records: list[EvidenceRecord]
    ) -> list[EvidenceRecord]:
        """
        Why this exists: our own seeded retraction test found PubMed's
        native curation catches 4/4 known retractions vs Crossref's 1/4.
        A record that came from Europe PMC (not matched to an equivalent
        PubMed result during dedupe -- e.g. the PubMed query didn't surface
        it, but Europe PMC's did) never gets PubMed's native check at all,
        because it was parsed from Europe PMC's JSON, which doesn't carry
        PublicationTypeList/CommentsCorrectionsList.

        Fix: for any Europe PMC record whose native_id IS a resolvable PMID
        (source=="MED" style records; preprints with PPR ids are skipped,
        they have no PMID yet), batch-efetch it through PubMedClient and
        copy over the native retraction verdict if positive.

        This runs BEFORE the Crossref cross-reference step, so records
        caught here don't get redundantly (and less reliably) re-checked.
        """
        candidates = [
            r
            for r in records
            if r.source == SourceDB.EUROPE_PMC
            and not r.is_retracted
            and r.native_id.isdigit()  # PPR ids aren't numeric PMIDs
        ]
        if not candidates:
            return records

        pmids = [r.native_id for r in candidates]
        try:
            pubmed_versions = self.pubmed.efetch(pmids)
        except Exception as exc:
            # Same fail-open philosophy as the rest of retrieve.py: a
            # failed cross-check should not crash the pipeline or silently
            # mark anything as retracted -- it just means this record only
            # gets the Crossref check that follows, same as before this fix.
            print(f"[retrieve] Europe-PMC PMID retraction cross-check failed: {exc}")
            return records

        by_pmid = {pr.native_id: pr for pr in pubmed_versions}
        for record in candidates:
            match = by_pmid.get(record.native_id)
            if match is not None and match.is_retracted:
                record.is_retracted = True
                record.retraction_notice_id = match.retraction_notice_id
                record.retraction_source = "pubmed"

        return records

    # ------------------------------------------------------------------
    # Step 3b: retraction cross-reference (Crossref)
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
    collect_traces: bool = False,
):
    global _default_gatherer
    if _default_gatherer is None:
        _default_gatherer = EvidenceGatherer()
    return _default_gatherer.gather_evidence(
        queries,
        per_source_limit=per_source_limit,
        include_trials=include_trials,
        include_preprints=include_preprints,
        collect_traces=collect_traces,
    )