# EvidenceBoard API contract

`api/server.py` exposes the [pipeline](../pipeline.py) over HTTP using nothing but
Python's standard library (`http.server.ThreadingHTTPServer`) plus one SQLite
module ([`core/store.py`](../core/store.py)) for question history and response
caching. There are no third-party runtime dependencies on the Python side.

The frontend lives separately in [`../web/`](../web/) (Vite + React + TypeScript,
its own `npm` build). In development, run the two independently:

```
python -m api.server            # live: calls PubMed / Europe PMC / CT.gov / the LLM
python -m api.server --mock     # offline: replays demo/mock_response.json
cd web && npm run dev           # frontend dev server, proxies /api/* to :8000
```

In production, build the frontend once and this server serves it too:

```
cd web && npm run build         # -> web/dist/
cd .. && python -m api.server   # now also serves web/dist/ at GET /
```

The bind address comes from configuration (`SERVER_HOST`, `SERVER_PORT`;
defaults `127.0.0.1:8000`) — see [`.env.example`](../.env.example).

## Conventions

| Aspect | Rule |
| --- | --- |
| Encoding | UTF-8 everywhere, request and response |
| Content type | `application/json; charset=utf-8` for the API; static frontend assets get their real MIME type |
| CORS | `Access-Control-Allow-Origin: *`, methods `GET, POST, DELETE, OPTIONS`, header `Content-Type`; `OPTIONS` preflight answers `204`. This is a single-user local tool with no auth/session state, so wildcard CORS carries no CSRF-style risk on `GET`/`POST` — `DELETE /api/history` (bulk wipe) is the one endpoint worth re-scoping if this server is ever bound beyond `127.0.0.1` |
| Max request body | 64 KiB (`MAX_BODY_BYTES`) |
| HTTP version | `HTTP/1.1`; fixed-length responses carry `Content-Length`, `/api/ask/stream` uses chunked transfer encoding instead (its length isn't known up front) |
| Logging | every startup line, request and error is printed with the `[server]` prefix |

**Abstention is not an error.** When EvidenceBoard refuses to answer — a thin
evidence pool, an unavailable LLM, a verifier that deleted every claim — the
response is still `200 OK` with `abstained: true` and populated
`abstain_reasons`. A non-2xx status means the *request* or the *server* was at
fault, never that the evidence was insufficient.

**History and caching are the same log, read two ways.** Every ask (live or
mock) is recorded as one row in `core/store.py`'s `runs` table. Re-asking the
identical question (after normalizing case/punctuation/whitespace) replays
that row instead of re-running the pipeline — the cache never expires on a
timer; pass `force_refresh: true` to bypass it explicitly. `GET /api/history`
lists those same rows by time.

---

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

Runs one clinical question through the full pipeline (or replays a cached
report) and returns the whole report in one response.

- **Request** `application/json`

```json
{ "question": "Does metformin reduce all-cause mortality in type 2 diabetes?", "force_refresh": false }
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `question` | `string` | yes | non-empty after trimming; unknown extra fields are ignored |
| `force_refresh` | `boolean` | no, default `false` | `true` skips the cache and always runs the pipeline live, recording a new history row even if an older one exists for this question |

- **Response** `200` · `application/json` — the report object below, plus two
  fields describing how it was produced: `"cached": boolean` (`true` when this
  is a replayed row, `false` for a fresh run) and `"run_id": string | null`
  (the history row id — `null` only if history recording itself failed, which
  never blocks the answer; see `_record()`'s fail-open behavior).

A cache miss is **slow and synchronous** (typically 30–120 s live, often
longer on a free-tier LLM backend): it fans out to PubMed, Europe PMC and
ClinicalTrials.gov, then makes several LLM calls (planning, appraisal,
synthesis, verification, adversarial audit). A cache hit returns immediately.
The server is threaded, so concurrent questions do not block each other, but
each key rotation in the shared `FailoverLLMClient` is process-wide.

## `POST /api/ask/stream`

Same question, same cache behavior, but streams progress as NDJSON (one JSON
object per line, `Content-Type: application/x-ndjson`, chunked transfer
encoding) instead of waiting silently for the final report. This is the
endpoint the shipped frontend actually calls, so its live "Agent activity"
panel can show real per-stage progress instead of a blank wait.

- **Request** — identical body to `POST /api/ask`.
- **Response** `200` · `application/x-ndjson` — a sequence of lines, each one
  of:

**On a cache hit** — exactly two lines, nothing else:

```json
{"type": "cache_hit", "run_id": "3f9a...", "cached_at": "2026-08-28T10:21:51.893700+00:00"}
{"type": "result", "report": { /* ... */ } }
```

No stage events are ever sent for a cache hit — the point is that nothing
actually ran this time, so nothing pretends to.

**On a cache miss (or `force_refresh: true`)** — one `stage` line per real
pipeline stage boundary as it happens (see [`pipeline.py`](../pipeline.py)'s
`on_event` callback for the authoritative field list per stage), then one
`result` line:

```json
{"type": "stage", "stage": "strategist", "status": "start"}
{"type": "stage", "stage": "strategist", "status": "done", "queries": ["..."], "llm_calls": 1}
{"type": "stage", "stage": "retrieval", "status": "start"}
{"type": "stage", "stage": "retrieval", "status": "done", "pool_size": 41, "retracted": 1, "llm_calls": 0}
{"type": "stage", "stage": "appraiser", "status": "start"}
{"type": "stage", "stage": "appraiser", "status": "done", "appraised": 12, "top_score": 95, "llm_calls": 3}
{"type": "stage", "stage": "synthesizer", "status": "start"}
{"type": "stage", "stage": "synthesizer", "status": "done", "sentences": 5, "abstained": false, "llm_calls": 1}
{"type": "stage", "stage": "verifier", "status": "start"}
{"type": "stage", "stage": "verifier", "status": "done", "funnel": { /* ... */ }, "llm_calls": 6}
{"type": "stage", "stage": "red_team", "status": "start"}
{"type": "stage", "stage": "red_team", "status": "done", "flagged_claims": 1, "llm_calls": 1}
{"type": "stage", "stage": "complete", "status": "answered"}
{"type": "result", "report": { /* same shape as POST /api/ask, incl. cached/run_id */ } }
```

`llm_calls` is the real number of successful calls that stage's agent made
through the shared `FailoverLLMClient` (see `config.py`'s call counter) —
never simulated, and `0` (not `null`) for `retrieval`, which is deterministic
by design (zero AI; see `retrieval/retrieve.py`). A stage that self-abstains
skips the remaining stages and jumps straight to a `complete` line with
`"status": "abstained"` and a `reason`.

If the client disconnects mid-stream, the pipeline keeps running to
completion server-side (its result is still recorded to history) — only the
NDJSON write itself is best-effort past that point.

---

## `GET /api/history`

Lists past runs (most recent first), summaries only — no full report, so this
stays cheap regardless of history size.

- **Query params**: `limit` (default `50`, max `200`), `offset` (default `0`),
  `abstained` (`true`/`false`, omit for both)
- **Response** `200` · `application/json`

```json
{
  "runs": [
    { "id": "3f9a...", "question": "...", "abstained": false, "created_at": "2026-08-28T10:21:51Z", "source": "live" }
  ],
  "total": 1
}
```

`source` is `"live"` or `"mock"` (server started with `--mock`) — what actually
*produced* that row's answer. A cache hit never creates a new row; it replays
an existing one, so check the replayed report's own `"cached"` field for that
distinction, not `source`.

## `GET /api/history/{id}`

One past run's full report.

- `{id}` — the 32-character hex run id from `record_run()`/a history list entry
- **Response** `200` · `application/json` — `{"id", "question", "report", "created_at", "source"}`, where `report` is the same shape as `POST /api/ask`'s response
- **Errors** — `400` if `{id}` isn't a well-formed run id, `404` if it's well-formed but unknown

## `DELETE /api/history/{id}`

Deletes one run. `204 No Content` on success, `404` if unknown, `400` if
`{id}` is malformed.

## `DELETE /api/history`

Wipes all history/cache. Requires a JSON body confirming intent:

```json
{ "confirm": true }
```

- **Response** `200` · `{"deleted": <count>}`
- **Errors** — `400` without `"confirm": true` (a bare empty body is rejected, not treated as confirmation)

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
    "by_reason": { "source retracted (crossref)": 1, "unsupported by cited evidence": 1 }
  },
  "answer_text": "Metformin was associated with lower all-cause mortality [S1]. ...",
  "claims": [ /* Claim objects, see below */ ],
  "evidence": [ /* Evidence objects, see below */ ],
  "disclaimer": "EvidenceBoard is a literature search and evidence-summarization aid ...",
  "queries": ["metformin all-cause mortality type 2 diabetes", "..."],
  "synthesizer_parse_deletions": [ { "text": "...", "reason": "uncited sentence" } ],
  "cached": false,
  "run_id": "3f9a1c2b..."
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
| `disclaimer` | `string` | `config.DISCLAIMER`; must be displayed on every surface that shows an answer |
| `queries` | `string[]` | the Strategist's planned search queries, always populated whenever stage 1 ran (empty only for the top-level "LLM totally dead" net and a failed mock replay, which never reach stage 1) |
| `synthesizer_parse_deletions` | `object[]` | `{"text", "reason"}` — draft sentences the Synthesizer's own deterministic parser dropped *before* the Verifier ever saw them (uncited sentences, citations to an unknown S-id). Distinct from the verification funnel above: these never entered it at all |
| `cached` | `boolean` | present on responses from `POST /api/ask`/`POST /api/ask/stream` (not from `GET /api/history/{id}`, where it's implied): `true` if this report was replayed rather than freshly computed |
| `run_id` | `string \| null` | the history row id this run was recorded as; `null` only if recording itself failed (never blocks the answer) |

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
  "flag_details": [ { "source": "verifier", "label": "weakly supported", "note": null } ],
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
| `deletion_reason` | `string \| null` | non-null only when `status == "deleted"`. A retraction reason names its source when known: `"source retracted (crossref)"` / `"source retracted (pubmed)"`, or the generic `"source retracted (retracted)"` when the retraction check itself didn't attribute a source |
| `flags` | `string[]` | plain display strings — Verifier flags (`"weakly supported"`, `"expression of concern"`) merged with Red Team findings, formatted `"{flag}: {note}"`. Kept exactly as-is for backward compatibility |
| `flag_details` | `object[]` | parallel to `flags` (same order, same length), structured: `{"source": "verifier" \| "red_team", "label": string, "note": string \| null}`. This is how a client tells "the Verifier itself flagged this" from "the Red Team's adversarial audit flagged this" — the two never have delete authority in common: **only the Verifier deletes; the Red Team can only flag** |
| `checks.existence` | `"pass" \| "fail" \| "skipped"` | does every cited S-id resolve in the frozen pool? |
| `checks.entailment` | `"supports" \| "refutes" \| "nei" \| "skipped"` | does the cited evidence actually entail the claim? (`nei` = not enough information) |
| `checks.standing` | `"pass" \| "fail" \| "flag" \| "skipped"` | is the source still standing? `fail` = retracted, `flag` = expression of concern / superseded |
| `verdict` | `string \| null` | the entailment judge's verdict, e.g. `"SUPPORTS"`, `"REFUTES"`, `"NEI"` |
| `confidence` | `number \| null` | `0.0`–`1.0`, from the entailment judge. Below `0.70` is what earns the `"weakly supported"` flag (`agents/verifier.py`'s `min_support_confidence`) |
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
  "retraction_source": null,
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
| `study_design` | `string \| null` | e.g. `"rct"`, `"systematic_review"`, `"cohort"` — see `core/schema.py`'s `StudyDesign` enum for the full set |
| `trial_status` | `string \| null` | registry recruitment status, trials only |
| `is_preprint` | `boolean` | not peer reviewed — display a warning |
| `is_retracted` | `boolean` | retracted — display prominently; the Verifier deletes any claim citing it |
| `retraction_source` | `string \| null` | which check caught the retraction — `"pubmed"` or `"crossref"` — when `is_retracted` is true; `null` otherwise (or if retracted but the source wasn't attributed) |
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
| `400` | empty/oversized/malformed body, missing/empty/non-string `question`, a malformed run id, or `DELETE /api/history` without `{"confirm": true}` |
| `403` | a static asset path would resolve outside `web/dist/` (rejected outright, not reported as 404, so a traversal attempt can't be used to probe for file existence) |
| `404` | unknown route, unknown (but well-formed) history run id, or a request for a static file that doesn't exist |
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

Ask again — this one replays instantly from cache:

```bash
curl -s -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Does metformin reduce all-cause mortality in type 2 diabetes?"}' \
  | python -c "import json,sys; r=json.load(sys.stdin); print(r['cached'], r['run_id'])"
```

Force a fresh run instead of the cached one:

```bash
curl -s -X POST http://127.0.0.1:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Does metformin reduce all-cause mortality in type 2 diabetes?", "force_refresh": true}'
```

Stream progress live:

```bash
curl -s -N -X POST http://127.0.0.1:8000/api/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "Is tranexamic acid effective in traumatic brain injury?"}'
```

List and inspect history:

```bash
curl -s "http://127.0.0.1:8000/api/history?limit=10"
curl -s "http://127.0.0.1:8000/api/history/<run_id>"
curl -s -X DELETE "http://127.0.0.1:8000/api/history/<run_id>"
curl -s -X DELETE http://127.0.0.1:8000/api/history -H "Content-Type: application/json" -d '{"confirm": true}'
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

`python -m api.server --mock` makes **every** ask (`/api/ask` and
`/api/ask/stream` alike) skip the network entirely and replay
[`demo/mock_response.json`](../demo/mock_response.json). The response shape is
unchanged, so the UI and any client behave identically — this is the demo
fallback for a dead network or an exhausted set of API keys.

- The flag is server-wide; there is no per-request mock switch.
- The path is resolved relative to `pipeline.py`, so the working directory does
  not matter.
- A missing or unparseable mock file is reported as an abstention
  (`"mock response unavailable: ..."`) with `200 OK`, never as a crash.
- `/api/health` is unaffected and still reports the configured models and key
  count.
- Mock runs are still recorded to history, tagged `"source": "mock"` — never
  conflated with `"live"` in `GET /api/history`.
- `/api/ask/stream` in mock mode sends no `stage` lines (there is nothing
  genuine to report — see `EvidencePipeline.run()`'s docstring: "Mock mode
  never calls [on_event]"), just the `result` line (or a `cache_hit` pair, if
  a mock run for that question was already recorded).

Mock mode still requires at least one configured API key, because the pipeline
is built (not called) at startup: set `LLM_API_KEY` or `LLM_API_KEYS` as
documented in [`.env.example`](../.env.example). Without one, startup fails with
`[server] error: cannot build pipeline: ...` and exit code `1`.
