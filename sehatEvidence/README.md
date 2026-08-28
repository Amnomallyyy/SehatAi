# EvidenceBoard

**`FREE`** · **`NO CREDENTIALS`** · **`VERIFICATION-FIRST`** · **`NOT A MEDICAL DEVICE`**

A free, verification-first clinical evidence assistant. Ask a clinical question, get a
short answer where **every sentence carries its own citation** — and where every claim
that could not be verified has already been **deleted** before you see it.

## Why this exists

The best-known clinical evidence assistants are gated. OpenEvidence is free only to
verified US physicians behind an NPI check; UpToDate is a paid subscription. Clinicians
outside those gates — trainees, non-US physicians, researchers, public-health workers —
are left with raw PubMed.

EvidenceBoard is open to anyone with an API key from a free tier. But "open" is not the
differentiator. The differentiator is that **it deletes its own output**.

Every claim passes a **three-check verification bundle** — EXISTENCE, ENTAILMENT,
STANDING. Claims that fail any check are removed, and the removal is reported as a
headline, not buried:

```
14 claims generated → 6 deleted → 8 shown
```

That funnel is the product. A tool that tells you what it threw away is more useful than
one that sounds confident.

**Positioning, honestly:** this is a literature search and evidence-summarization aid.
It is not a medical device, it does not give medical advice, and it does not replace
clinical judgment. Every claim links to its primary source so you can check it yourself.

## Honest comparison

| Feature | EvidenceBoard | OpenEvidence | UpToDate |
|---|---|---|---|
| Cost | Free | Free (US NPI required) | Subscription (~$500/yr) |
| Access | Open (no credentials) | US physicians only (NPI gate) | Institutional/individual |
| Verification | 3-check bundle per claim | Unknown (proprietary) | Expert editorial review |
| Citation style | Per-sentence forced `[S#]` | Inline references | Section-level |
| Claim deletion | Automated (REFUTES / NEI) | N/A | Manual editorial |
| Evidence sources | PubMed, Europe PMC, ClinicalTrials.gov | Company-reported | Proprietary database |
| EU availability | Yes | Reported withdrawal (2026) | Yes |
| Model | `nvidia/nemotron-3-super-120b-a12b` | Proprietary | N/A (human-written) |

Two rows are deliberately hedged. OpenEvidence's source coverage and usage figures are
**company-reported** and not independently verifiable, and its EU exit is a **reported
withdrawal (2026)** from press coverage rather than something we have confirmed. We do
not claim more certainty than we have — the same standard we apply to our own answers.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Strategist │────▶│   Retrieval  │────▶│   Appraiser  │
│ (query plan)│     │ PubMed/EPMC/ │     │ OCEBM/GRADE  │
│             │     │ ClinTrials   │     │ scoring      │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                 │
                    ┌──────────────┐     ┌───────▼───────┐
                    │   Red Team   │◀────│  Synthesizer  │
                    │  (flag-only) │     │ forced [S#]   │
                    └──────┬───────┘     └───────┬───────┘
                           │                     │
                           │             ┌───────▼───────┐
                           └────────────▶│   Verifier    │
                                         │ 3-check gate  │
                                         │ EXIST/ENTAIL/ │
                                         │ STANDING      │
                                         └───────────────┘
```

| Stage | Responsibility |
|---|---|
| **Strategist** | Turns a clinical question into structured PICO + per-source query plans |
| **Retrieval** | Fans out to PubMed E-utilities, Europe PMC, ClinicalTrials.gov API v2; dedupes into one pool |
| **Appraiser** | Scores each record: OCEBM level, GRADE-style certainty, relevance; caps the pool |
| **Synthesizer** | Drafts the answer with a forced `[S#]` citation on every sentence |
| **Red Team** | Adversarial critique — **flags only**, never edits or deletes |
| **Verifier** | The gate. Runs the three checks and deletes what fails |

The Red Team flags; only the Verifier deletes. Keeping critique and deletion authority
separate means a hostile critic cannot silently rewrite the answer.

## The three-check bundle

**1. EXISTENCE — does the source actually exist?**
Every cited source is re-resolved against its registry rather than trusted from the
model's output: PubMed `esummary` for PMIDs, `doi.org` content negotiation for DOIs, and
the ClinicalTrials.gov API v2 for NCT numbers. This is the anti-hallucination floor — a
fabricated citation cannot survive a live registry lookup.

**2. ENTAILMENT — does the evidence support the claim?**
An LLM NLI judge (SciFact-style) compares each claim against the text of the source it
cites and returns one of `SUPPORTS`, `REFUTES`, `NOT_ENOUGH_INFO`. A real citation
attached to a claim it does not actually support is the more common failure mode, and it
is the one this check exists to catch.

**3. STANDING — is the source still good?**
Is it retracted? Under an expression of concern? Superseded by a newer systematic review
or meta-analysis? A paper can exist and support a claim and still be the wrong thing to
cite today.

**Claims failing ANY check are deleted before display.** The generated → deleted → shown
funnel is reported as a headline output, per-claim verdicts included.

## Quickstart

```powershell
# Clone and enter
cd sehatEvidence

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env: add your NVIDIA NIM API key(s) and NCBI credentials

# Run the server
python -m api.server

# Run in demo/mock mode (no API keys needed for display testing)
python -m api.server --mock

# Open in browser
# http://127.0.0.1:8000
```

`--mock` replays `demo/mock_response.json` for every `/api/ask` call, so the UI can be
demonstrated with no network and no keys.

## Environment configuration

Copy `.env.example` to `.env` and fill it in. Shell-exported variables take precedence
over `.env`, so exporting them directly also works. **Never commit `.env`.**

### PubMed / NCBI E-utilities

| Variable | Required | Notes |
|---|---|---|
| `NCBI_TOOL_NAME` | Yes | Identifies your app to NCBI, e.g. `evidenceboard` |
| `NCBI_EMAIL` | Yes | Contact address NCBI can reach you at |
| `NCBI_API_KEY` | No | Raises the PubMed rate limit from 3 to 10 req/sec |

### LLM backend

| Variable | Required | Notes |
|---|---|---|
| `LLM_BASE_URL` | Yes | OpenAI-compatible endpoint. Default `https://integrate.api.nvidia.com/v1`; local Ollama fallback `http://localhost:11434/v1` |
| `LLM_API_KEY` | Yes* | Single key — the simple setup |
| `LLM_API_KEYS` | No | Comma-separated keys for automatic failover across several free accounts. Takes precedence over `LLM_API_KEY` |
| `LLM_MODEL` | Yes | Model name exactly as the endpoint expects, e.g. `nvidia/nemotron-3-super-120b-a12b` |
| `LLM_SENSITIVE_MODEL` | No | Heavier model used by the Verifier for entailment judgment; other agents keep `LLM_MODEL` |
| `LLM_TIMEOUT` | No | Request timeout in seconds (default `60`) |

\* One of `LLM_API_KEY` or `LLM_API_KEYS` must be set, or the pipeline will not build.

### Pipeline and server

| Variable | Notes |
|---|---|
| `POOL_CAP` | Maximum records kept in the evidence pool after appraisal (default `30`) |
| `ENABLE_SUPERSESSION` | Enable the supersession check in STANDING (default `true`) |
| `SERVER_HOST` | Bind address (default `127.0.0.1`, local-only) |
| `SERVER_PORT` | Bind port (default `8000`) |

**Keys:** the NVIDIA NIM free tier is sufficient to run this. Generate keys at
<https://build.nvidia.com>. Because the free tier is rate-limited, `LLM_API_KEYS`
supports multi-key failover — on a `429` the client rotates to the next key.

## Benchmark

```powershell
# Run full benchmark (all 30 questions, all 3 arms)
python -m benchmark.run_benchmark

# Quick test (first 3 questions, full pipeline only)
python -m benchmark.run_benchmark --max 3 --arm full

# Offline sample (no network)
python -m benchmark.run_benchmark --offline-sample
```

Questions live in `benchmark/questions.json`; results are written to
`benchmark/results.md`.

## Limitations

These are real and specific. Read them before trusting any output.

- **LLM judge accuracy is unvalidated.** Entailment judgments sit in roughly the
  GPT-4-zero-shot band — *estimated*, not measured. We have run no formal validation on
  HealthVer or SciFact with this model. The in-domain → out-of-domain drop reported in
  the literature (≈0.88 → ≈0.48) should be assumed to apply here too.
- **Supersession is a heuristic.** The "superseded by a newer SR/MA" check is a PubMed
  query pattern, not a causal or citation analysis. False positives are expected.
- **Abstracts only.** Full text is not retrieved. Findings that live in methods or
  results sections are invisible to the pipeline.
- **Verifier OOD behaviour is uncharacterized.** Rare diseases, veterinary medicine, and
  pediatric subspecialties fall outside anything we have examined.
- **English only.** Non-English publications are excluded, with the geographic and
  topical bias that implies.
- **Rate limits.** The free NIM tier throttles; high-volume use will hit `429`s. Key
  rotation mitigates this but does not remove it.
- **Not a medical device.** It cannot replace clinical judgment. Every claim links to its
  primary source precisely because you are expected to check it.

## Disclaimer

> EvidenceBoard is a literature search and evidence-summarization aid for healthcare
> professionals. It is not a medical device and does not provide medical advice,
> diagnosis, or treatment recommendations. Every claim must be verified against the
> primary source (links provided) before clinical use. Do not enter patient-identifiable
> information. Automated verification checks can err; the treating clinician remains
> responsible for clinical decisions.

This text is defined once in `config.py` as `DISCLAIMER` and carried by every
user-facing surface — API responses and UI alike.

## References

**Evidence appraisal frameworks**

- OCEBM Levels of Evidence (2011) — <https://www.cebm.ox.ac.uk/resources/levels-of-evidence>
- GRADE Working Group — <https://www.gradeworkinggroup.org/>

**Verification and attribution research**

- SciFact — Wadden et al., 2020 — [arXiv:2004.14974](https://arxiv.org/abs/2004.14974)
- SAFE — Wei et al., 2024 — [arXiv:2403.18802](https://arxiv.org/abs/2403.18802)
- VerifAI — 2024 — [arXiv:2604.08549](https://arxiv.org/abs/2604.08549)
- ALCE — Gao et al., 2023 — [arXiv:2305.14627](https://arxiv.org/abs/2305.14627)

**Data sources**

- NCBI PubMed E-utilities
- Europe PMC REST API
- ClinicalTrials.gov API v2

## Demo

`demo/backup_recording.mp4` is a placeholder for a future screen recording of the tool in
action. `demo/mock_response.json` backs `--mock` mode and `demo/seed_queries.json` holds
sample questions for a live walkthrough.
