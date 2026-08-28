# EvidenceBoard API contract

`api/server.py` exposes the [pipeline](../pipeline.py) over HTTP using nothing but
Python's standard library (`http.server.ThreadingHTTPServer`). There are no
third-party runtime dependencies, no CDN assets, and no build step: the
single-page UI is embedded in `server.py` and served inline, so the whole
product runs on an air-gapped machine.

```
python -m api.server            # live: calls PubMed / Europe PMC / CT.gov / the LLM
python -m api.server --mock     # offline: replays demo/mock_response.json
python api/server.py            # equivalent (the module fixes sys.path itself)
```

The bind address comes from configuration (`SERVER_HOST`, `SERVER_PORT`;
defaults `127.0.0.1:8000`) — see [`.env.example`](../.env.example).

## Conventions

| Aspect | Rule |
| --- | --- |
| Encoding | UTF-8 everywhere, request and response |
| Content type | `application/json; charset=utf-8` for the API, `text/html; charset=utf-8` for the UI |
| CORS | `Access-Control-Allow-Origin: *`, methods `GET, POST, OPTIONS`, header `Content-Type`; `OPTIONS` preflight answers `204` |
| Max request body | 64 KiB (`MAX_BODY_BYTES`) |
| HTTP version | `HTTP/1.1` with `Content-Length` on every response (keep-alive safe) |
| Logging | every startup line, request and error is printed with the `[server]` prefix |

**Abstention is not an error.** When EvidenceBoard refuses to answer — a thin
evidence pool, an unavailable LLM, a verifier that deleted every claim — the
response is still `200 OK` with `abstained: true` and populated
`abstain_reasons`. A non-2xx status means the *request* or the *server* was at
fault, never that the evidence was insufficient.

---

## `GET /`

Returns the embedded single-page UI (also served at `/index.html`).

- **Response** `200` · `text/html; charset=utf-8`
- Inline `<style>` and `<script>` only; system font stack; no external requests
  other than the page's own calls to `/api/health` and `/api/ask`.
- The disclaimer strip renders `config.DISCLAIMER` verbatim — the same string
  every `/api/ask` response carries.

## `GET /api/health`

Liveness plus the model/key configuration of the running process.

- **Response** `200` · `application/json`

```json
{
  "status": "ok",
  "llm_model": "meta/llama-3.3-70b-instruct",
  "sensitive_model": null,
  "keys_count": 3
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | `string` | always `"ok"` when the process answers at all |
| `llm_model` | `string` | `LLM_MODEL`: the default model used by every agent |
| `sensitive_model` | `string \| null` | `LLM_SENSITIVE_MODEL`: the heavier model used by the Verifier's entailment judge; `null` when unset (the Verifier then shares `llm_model`) |
| `keys_count` | `integer` | number of API keys the `FailoverLLMClient` can rotate through. Key **values** are never exposed. |

## `POST /api/ask`

Runs one clinical question through the full pipeline and returns the report.

- **Request** `application/json`

```json
{ "question": "Does metformin reduce all-cause mortality in type 2 diabetes?" }
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `question` | `string` | yes | non-empty after trimming; unknown extra fields are ignored |

- **Response** `200` · `application/json` — the report object below, verbatim
  from `EvidencePipeline.run()`.

This request is **slow and synchronous** (typically 30–120 s live): it fans out
to PubMed, Europe PMC and ClinicalTrials.gov, then makes several LLM calls
(planning, appraisal, synthesis, verification, adversarial audit). The server is
threaded, so concurrent questions do not block each other, but each key rotation
in the shared `FailoverLLMClient` is process-wide.

---

## Report schema

The shape is **identical** for answers and abstentions — only the values differ.

```json
{
  "question": "Does metformin reduce all-cause mortality in type 2 diabetes?",
  "abstained": false,
  "abstain_reasons": [],
  "funnel": {
    "claims_generated": 5,
    "claims_deleted": 2,
    "claims_kept": 3,
    "by_reason": { "source retracted (PubMed)": 1, "not entailed by cited evidence": 1 }
  },
  "answer_text": "Metformin was associated with lower all-cause mortality [S1]. ...",
  "claims": [ /* Claim objects, see below */ ],
  "evidence": [ /* Evidence objects, see below */ ],
  "disclaimer": "EvidenceBoard is a literature search and evidence-summarization aid ..."
}
```

### Top level

| Field | Type | Notes |
| --- | --- | --- |
| `question` | `string` | echoed back as received (trimmed) |
| `abstained` | `boolean` | `true` when no answer is given |
| `abstain_reasons` | `string[]` | empty unless `abstained`. Known values: `"fewer than 2 records retrieved"`, `"fewer than 2 high-relevance records"`, `"LLM unavailable during synthesis"`, `"synthesizer judged evidence insufficient"`, `"LLM unavailable"`, plus the Verifier's own reasons |
| `funnel` | `object` | verification funnel, see below |
| `answer_text` | `string` | the kept + flagged claim texts joined with spaces; `""` when abstained. Every sentence carries `[S#]` citation markers |
| `claims` | `Claim[]` | **all** claims in generation order, including deleted ones; empty when abstained |
| `evidence` | `Evidence[]` | the appraised pool, best-first, `S1` … `Sn`. Populated even on abstention whenever the run got that far — that is what makes an abstention auditable |
| `disclaimer` | `string` | `config.DISCLAIMER`; must be displayed on every surface |

### `funnel`

| Field | Type | Notes |
| --- | --- | --- |
| `claims_generated` | `integer` | atomic claims decomposed from the draft |
| `claims_deleted` | `integer` | claims removed by verification |
| `claims_kept` | `integer` | claims shown (status `kept` or `flagged`) |
| `by_reason` | `object<string, integer>` | deletion reason → count; `{}` when nothing was deleted |

All four fields are present and zeroed on abstention.

### `Claim`

```json
{
  "claim_id": "s0-c1",
  "text": "Metformin was associated with lower all-cause mortality [S1].",
  "status": "kept",
  "deletion_reason": null,
  "flags": ["weakly supported"],
  "checks": { "existence": "pass", "entailment": "supports", "standing": "pass" },
  "verdict": "SUPPORTS",
  "confidence": 0.86,
  "evidence_quote": "Metformin use was associated with a 21% lower risk of all-cause mortality.",
  "citations": [
    {
      "sid": "S1",
      "citation_key": "UKPDS 1998",
      "title": "Effect of intensive blood-glucose control with metformin",
      "url": "https://pubmed.ncbi.nlm.nih.gov/9742976/"
    }
  ]
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `claim_id` | `string` | `"s{sentence}-c{claim}"`, stable within one report |
| `text` | `string` | the atomic claim, wording copied from the draft sentence |
| `status` | `"kept" \| "flagged" \| "deleted"` | `flagged` is shown but caveated; `deleted` never appears in `answer_text` |
| `deletion_reason` | `string \| null` | non-null only when `status == "deleted"` |
| `flags` | `string[]` | Verifier flags (`"weakly supported"`, `"expression of concern"`) merged with Red Team findings, formatted `"{flag}: {note}"` |
| `checks.existence` | `"pass" \| "fail" \| "skipped"` | does every cited S-id resolve in the frozen pool? |
| `checks.entailment` | `"supports" \| "refutes" \| "nei" \| "skipped"` | does the cited evidence actually entail the claim? (`nei` = not enough information) |
| `checks.standing` | `"pass" \| "fail" \| "flag" \| "skipped"` | is the source still standing? `fail` = retracted, `flag` = expression of concern / superseded |
| `verdict` | `string \| null` | the entailment judge's verdict, e.g. `"SUPPORTS"`, `"REFUTES"`, `"NEI"` |
| `confidence` | `number \| null` | `0.0`–`1.0`, from the entailment judge |
| `evidence_quote` | `string \| null` | the span of the source that supports the claim |
| `citations` | `object[]` | resolved citations: `sid`, `citation_key`, `title`, `url` (`url` may be `null`) |

A `checks` value of `"skipped"` means the check was not the deciding one — for
example, standing is skipped when entailment already deleted the claim.

### `Evidence`

```json
{
  "sid": "S1",
  "citation_key": "UKPDS 1998",
  "source": "pubmed",
  "native_id": "9742976",
  "doi": null,
  "url": "https://pubmed.ncbi.nlm.nih.gov/9742976/",
  "title": "Effect of intensive blood-glucose control with metformin",
  "journal": "Lancet",
  "publication_date": "1998-09-12",
  "study_design": "RCT",
  "trial_status": null,
  "is_preprint": false,
  "is_retracted": false,
  "relevance_score": 92,
  "rationale": "Same population and intervention as the question; ...",
  "abstract": "..."
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `sid` | `string` | `"S1"` … `"Sn"`, minted by the pipeline over the ranked pool; `S1` is the highest-ranked record. This is the only citation namespace |
| `citation_key` | `string` | short human label (author/year or registry id) |
| `source` | `string` | `"pubmed"`, `"europepmc"` or `"clinicaltrials"` |
| `native_id` | `string \| null` | PMID / Europe PMC id / NCT number |
| `doi` | `string \| null` | bare DOI, no `https://doi.org/` prefix |
| `url` | `string \| null` | canonical link; clients should fall back to the DOI, then to a registry URL rebuilt from `native_id` |
| `title`, `journal`, `publication_date` | `string \| null` | as reported by the source; dates are not normalised to one format |
| `study_design` | `string \| null` | e.g. `"RCT"`, `"Systematic review"`, `"Cohort"` |
| `trial_status` | `string \| null` | registry recruitment status, trials only |
| `is_preprint` | `boolean` | not peer reviewed — display a warning |
| `is_retracted` | `boolean` | retracted — display prominently; the Verifier deletes any claim citing it |
| `relevance_score` | `integer` | Appraiser score `0`–`100`; `>= 60` counts as high relevance (the abstention threshold) |
| `rationale` | `string \| null` | why the Appraiser scored it that way |
| `abstract` | `string \| null` | the text the entailment judge was shown; may be long |

---

## Error responses

Errors always carry a single `error` field and never a partial report.

```json
{ "error": "field 'question' is required and must be a non-empty string" }
```

| Status | When |
| --- | --- |
| `400` | empty body, invalid `Content-Length`, body over 64 KiB, body that is not UTF-8 JSON, body that is not a JSON object, or a missing / empty / non-string `question` |
| `404` | any path other than `/`, `/index.html`, `/api/health`, `/api/ask` (and `/api/ask` reached with `GET` rather than `POST`) |
| `500` | the pipeline raised, returned a non-dict, or produced a report that is not JSON-serializable |
| `503` | the server started without a usable pipeline |

`EvidencePipeline.run()` is designed never to raise — it converts a dead LLM
into an abstention — so a `500` indicates a genuine defect and is accompanied by
a `[server] error: ...` line plus a traceback on the server's stdout.

---

## Examples

Health:

```bash
curl -s http://127.0.0.1:8000/api/health
```

Ask a question (live pipeline, expect to wait):

```bash
curl -s -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Does metformin reduce all-cause mortality in type 2 diabetes?"}'
```

Just the funnel and whether it abstained:

```bash
curl -s -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Is tranexamic acid effective in traumatic brain injury?"}' \
  | python -c "import json,sys; r=json.load(sys.stdin); print(r['abstained'], r['funnel'])"
```

A rejected request:

```bash
curl -s -i -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" -d '{"question": "   "}'
# HTTP/1.1 400 Bad Request
# {"error": "field 'question' is required and must be a non-empty string"}
```

PowerShell equivalent:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/ask -Method Post `
  -ContentType 'application/json' `
  -Body '{"question": "Does metformin reduce all-cause mortality in type 2 diabetes?"}'
```

---

## Mock mode

`python -m api.server --mock` makes **every** `/api/ask` call skip the network
entirely and replay [`demo/mock_response.json`](../demo/mock_response.json). The
response shape is unchanged, so the UI and any client behave identically — this
is the demo fallback for a dead network or an exhausted set of API keys.

- The flag is server-wide; there is no per-request mock switch.
- The path is resolved relative to `pipeline.py`, so the working directory does
  not matter.
- A missing or unparseable mock file is reported as an abstention
  (`"mock response unavailable: ..."`) with `200 OK`, never as a crash.
- `/api/health` is unaffected and still reports the configured models and key
  count.

Mock mode still requires at least one configured API key, because the pipeline
is built (not called) at startup: set `LLM_API_KEY` or `LLM_API_KEYS` as
documented in [`.env.example`](../.env.example). Without one, startup fails with
`[server] error: cannot build pipeline: ...` and exit code `1`.
