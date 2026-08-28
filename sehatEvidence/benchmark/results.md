# EvidenceBoard benchmark results

- Questions: 2 (from `benchmark/questions.json`)
- Arms run: Full Pipeline
- Bare-arm temperature: 0.1 (fixed for reproducibility)
- Mode: **offline sample (fixtures, no network)**
- These numbers describe the FIXTURES, not the real world; offline mode exists to smoke-test the harness.

## Metrics

| Metric | Bare LLM | Naive RAG | Full Pipeline |
|--------|----------|-----------|---------------|
| citation_resolution_rate | not run | not run | 1.00 |
| entailment_pass_rate | not run | not run | 1.00 |
| retraction_catch_rate | not run | not run | 1.00 |
| claims_deleted_per_answer | not run | not run | 1.00 |
| abstention_rate_no_evidence | not run | not run | n/a |
| abstention_rate_evidence | not run | not run | 0.00 |
| avg_latency_seconds | not run | not run | 0.00 |
| expected_outcome_match_rate | not run | not run | 1.00 |
| errors | not run | not run | 0 |

### How each metric is measured

- **citation_resolution_rate** -- of every identifier cited by the answer the arm actually showed, the fraction that resolves against PubMed esummary / ClinicalTrials.gov. Identifiers that could not be checked (transport failure, non-registry key) are excluded, never counted as fabrications.
- **entailment_pass_rate** -- of the claims the entailment judge ruled on, the fraction where the cited evidence SUPPORTS the claim (rather than REFUTES / NOT_ENOUGH_INFO).
- **retraction_catch_rate** -- of the seeded retracted records, the fraction kept out of the shown answer (excluded by the Appraiser per COPE/NLM policy, or deleted by the Verifier's standing check). Seeds are injected into the **full-pipeline arm only**, so this reads n/a for the other two arms.
- **claims_deleted_per_answer** -- mean claims deleted per question. For the bare and naive arms these deletions are *measured, not applied*: their answers still contain the claims, which is exactly the finding.
- **abstention_rate_no_evidence** -- abstention rate on the 5 `no_evidence` questions. **Higher is better** (abstaining is the correct answer).
- **abstention_rate_evidence** -- abstention rate on the evidence-rich categories. **Lower is better** (over-abstention is a failure mode too).
- **avg_latency_seconds** -- mean wall-clock seconds per question for the arm's own work. The measurement-only verification pass is excluded from the bare and naive timings.
- **expected_outcome_match_rate** -- fraction of questions where answer-vs-abstain matched `expected` in questions.json.
- **errors** -- questions that raised (LLMError or transport). Excluded from every rate above.

> The bare and naive arms are passed through a **measurement-only** Verifier: it counts what would have been deleted and removes nothing. Only the full pipeline actually deletes claims.

## Seeded failures

Injected into the retrieval pool of the **full-pipeline arm only** (the bare and naive arms never see them, so their `retraction_catch_rate` is n/a by construction):

- `MED/99999999` -- fabricated PMID, on-topic and RCT-shaped, tests the existence check.
- `MED/21177010` -- Wakefield's retracted MMR/autism paper, stubbed by the offline fixture as retracted.

| Seed | Injected | Cited by synthesis | Caught |
|------|----------|--------------------|--------|
| fabricated PMID | 2 | 2 | 2 |
| retracted paper | 2 | 0 | 2 |

The retracted seed was kept out of the ranked pool by the Appraiser on 2 of 2 injections.

## Per-question results -- Full Pipeline

| id | category | expected | actual | correct? | latency (s) | notes |
|----|----------|----------|--------|----------|-------------|-------|
| 1 | therapy | answer | answer | yes | 0.00 | fabricated PMID cited and deleted; retracted seed excluded before synthesis |
| 2 | therapy | answer | answer | yes | 0.00 | fabricated PMID cited and deleted; retracted seed excluded before synthesis |
