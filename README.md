# SehatAI

**Every marker, in the same room as your doctor.**

A doctor-patient portal that turns uploaded lab reports into structured,
trend-tracked markers a doctor can actually review — plus an AI
symptom/diet assistant for patients and a verification-first clinical
evidence search for doctors.

## What it does

| | Patient portal | Doctor portal |
|---|---|---|
| **Connections** | Request a doctor by email; nothing is shared until they accept | Accept/decline, private nicknames, disconnect |
| **Conversations** | Secure messaging with inline report/prescription attachments | Same, plus doctor-only clinical comment threads |
| **Lab reports** | Upload a PDF/photo → OCR → per-marker values, ranges, flags, trends | Per-marker review, history across reports, independent AI re-check of every extraction, notes, "mark reviewed" |
| **AI report summaries** | Plain-language summary of any shared PDF | Edit or regenerate; every regeneration is kept in the history |
| **Reports access** | A connected doctor sees all of your lab reports, including ones you upload yourself | Full report history of every connected patient, not only what was shared in a chat |
| **AI Assistant** | Symptom triage → which specialist to see; diet advice grounded in the patient's own labs and medicines | — |
| **Clinical Evidence** | — | Ask a clinical question; every claim carries a citation and unverifiable claims are deleted before you see them |
| **Sign-up & account** | City/country, privacy agreement (required), email confirmation, forgot-password | Same |
| **Health intake** | Printed-form style page, required after login: allergies, existing conditions and family history as tables, ranked top concerns, emergency contact | — |
| **Medicines** | See what your doctors prescribed; mark each one taking / not taking | Prescribe with notes and an optional file; a medicine turns inactive after its end date |
| **Med Calendar** | Book one of your connected doctor's open time slots; appointments with reminders | Publish availability and see who booked; appointments with reminders |
| **Emergency** | An always-visible Emergency button (and the assistant's "Notify someone") emails your emergency contact after you confirm | — |

## Architecture

```
                         browser
                            │  one origin, one port
                    ┌───────▼────────┐
                    │    gateway     │  nginx: routing, gzip, security headers,
                    │   :8080 → :80  │  rate limits, doctor-only auth for /evidence
                    └──┬────┬────┬───┴──────────┐
              /        │/api│    │/sehatai      │/evidence
   ┌──────────▼──┐ ┌───▼────▼───┐ ┌▼───────────┐ ┌▼──────────────┐
   │ frontend    │ │ core API   │ │ AI assist. │ │ evidenceboard │
   │ Jinja + JS  │ │ FastAPI    │ │ Node       │ │ Python stdlib │
   │ :3002       │ │ :8000      │ │ :3000      │ │ :8002, SQLite │
   └─────────────┘ └─┬────────┬─┘ └─────┬──────┘ └───────────────┘
                     │        │ runs    │ internal token
                     │   ┌────▼─────┐ ┌─▼─────────┐
                     │   │datafetch │ │ dietbot   │  internal only
                     │   │OCR + LLM │ │ FastAPI   │  (never published)
                     │   └────┬─────┘ └─┬─────────┘
                     ▼        ▼         ▼
            ┌──────────────────────────────────────┐
            │ Supabase: Postgres + pgvector + Storage │
            └──────────────────────────────────────┘
```

| Service | Internal port | Role |
|---|---|---|
| `gateway` | 80 (host `8080`) | The only published port. Routes `/`, `/api`, `/sehatai`, `/evidence` |
| `carelink-frontend` | 3002 | Web UI for both portals |
| `carelink-backend` | 8000 | Core API: auth, connections, messaging, reports, prescriptions, appointments, structured labs; runs DataFetch OCR in a worker pool |
| `sehatai-backend` | 3000 | AI symptom/diet assistant (multi-provider LLM failover, safety gates, grounding verifier) |
| `evidenceboard` | 8002 | Clinical Evidence pipeline (PubMed, Europe PMC, ClinicalTrials.gov) |
| `dietbot` | 8001 | Diet recommender, reachable only from `sehatai-backend` |

## Quick start (Docker)

**1. Database (Supabase, one time).** Create a project at
[supabase.com](https://supabase.com), open **SQL Editor**, paste
[`supabase/schema.sql`](supabase/schema.sql) and run it. It creates every
shared table, the RPC functions, the private `medical-documents` storage
bucket, and enables row-level security. It is safe to re-run.

**2. Configure.** One file for the whole stack:

```bash
cp .env.example .env
```

Fill in `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`/`SUPABASE_KEY`,
`DATABASE_URL` (Supabase → Settings → Database → Session pooler URI;
URL-encode special characters in the password, e.g. `@` → `%40`), a random
`SECRET_KEY`, `DIETBOT_INTERNAL_TOKEN`, and at least one AI provider key.

**Email (needed for sign-up links, password reset and emergency alerts).**
Add `SMTP_HOST`, `SMTP_PORT` (587 by default), `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_FROM` and `PUBLIC_APP_URL` (the address people open, used in emailed
links). With Gmail, use an
[app password](https://myaccount.google.com/apppasswords), not your normal
one. Without `SMTP_HOST` nothing is sent: the API says so honestly, and
outside production the confirm / reset link is written to the backend log
so you can still sign in while developing. In production SMTP is required,
because new accounts must confirm their email before logging in.

The portal's own tables (locations, email confirmation, tokens, emergency
contacts, appointment slots, intake rows, consent, anonymised statistics)
are created automatically when the core API starts; the same DDL is in
[`supabase/schema.sql`](supabase/schema.sql). See
[`docs/portal-accounts-and-alerts.md`](docs/portal-accounts-and-alerts.md).

**3. Run.**

```bash
docker compose up --build -d
```

Open **http://localhost:8080**. All services report healthy in
`docker compose ps` within about 30 seconds.

**4. (Optional) demo data.** `npm ci && npm run seed:data` seeds two
example patients with labs, medicines and intake forms.

## Local development (no Docker)

Each service runs on its own port and the frontend talks to them directly
(the defaults in `frontend/frontend_main.py`):

```bash
# core API
cd backend && python -m venv venv && venv/Scripts/activate   # or source venv/bin/activate
pip install -r requirements.txt -r ../datafetch/requirements.txt
uvicorn app.main:app --reload --port 8000

# web UI
cd frontend && pip install -r requirements.txt && uvicorn frontend_main:app --reload --port 3002

# AI assistant
npm ci && node sehatai/webserver.js

# Clinical Evidence / DietBot
cd sehatEvidence && pip install -r requirements.txt && python api/server.py
cd dietbot && pip install -r requirements.txt && uvicorn api:app --port 8001
```

All of them read the same root `.env`.

## Tests

| Suite | Command |
|---|---|
| Core API end-to-end (auth → connections → chat → reports → AI summary → comments) | `cd backend && python test_flow.py` (API running on an empty database; `SEHAT_API_BASE=http://localhost:8080/api` to go through the gateway) |
| AI assistant safety regressions (deterministic) | `node sehatai/testEdgeCases.js` (`--live` adds the real-LLM battery) |
| Clinical Evidence (offline) | `cd sehatEvidence && python -m pytest` and `python -m tests.test_<name>` |

CI (`.github/workflows/ci.yml`) runs all of the above plus a full
`docker compose build` on every push and pull request.

## Security model

- **Auth:** JWT bearer tokens (24 h). Production refuses to start with a
  missing or weak `SECRET_KEY`. Login and signup are rate-limited per IP,
  both in the API and at the gateway.
- **Authorization:** nothing is shared until a patient-doctor connection is
  accepted; once it is, that doctor sees the patient's lab reports. Every
  file download is authenticated; there is no public file URL.
- **Accounts:** sign-up needs an explicit privacy agreement (enforced by the
  API; the notice version and time are recorded) and a confirmed email before
  the first login. Emailed confirm / reset links are single-use, expire (24 h /
  1 h), and only their hash is stored. Forgot-password never reveals whether an
  email is registered.
- **Privacy:** sign-up also writes one anonymised statistics row (age group,
  sex, role, country, month) with no user id, name, email or city.
- **Emergency alerts** email only the person's name, the time and their
  city/country (plus the category the assistant flagged), never chat text or
  medical details; they are confirmed first, rate-limited and logged.
- **Clinical Evidence** has no login of its own, so the gateway only
  forwards requests carrying a valid **doctor** token.
- **DietBot** is never published, and requires a shared internal token.
- **Uploads** are checked by content (PDF/image signatures), not by the
  client's label, and are size-capped before being buffered.
- **Database:** RLS is enabled on every shared table; only server-side
  service credentials can read data.
- **Containers** run as non-root users with health checks.

## Project layout

```
gateway/         nginx config: the single public entry point
backend/         core API (FastAPI + SQLAlchemy)   API_CONTRACT.md = endpoint reference
frontend/        web UI (Jinja2 template + static/js/app.js, no build step)
sehatai/         AI symptom/diet assistant (Node)
datafetch/       OCR + structured-extraction pipeline (run by the core API)
dietbot/         diet recommender service
sehatEvidence/   Clinical Evidence pipeline (see its own README)
supabase/        schema.sql: the full shared database schema
docs/            architecture notes
```

## Demo tips

- **Pre-warm Clinical Evidence.** A fresh question runs the full pipeline
  live (search, grading, synthesis, claim-by-claim verification) and takes
  several minutes on free-tier models. Answers are cached by question in the
  `evidenceboard-db` volume, so ask each demo question once beforehand; on
  stage it replays instantly, with a "cached" marker.
- **Speed knobs** (`.env`): `LLM_CONCURRENCY` (parallel LLM calls, default
  4), `POOL_CAP` (evidence records kept after grading, default 50; 25 roughly
  halves synthesis and verification), `LLM_ENABLE_THINKING` (default off for
  every stage except claim verification).
- **Seed believable data** with `npm run seed:data` and create one doctor
  and one patient account, connected and with report access granted, before
  you present.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| AI Assistant says "temporarily unavailable"; lab uploads fail | Supabase unreachable: check `SUPABASE_URL` resolves and the project isn't paused |
| Core API exits on start with a `SECRET_KEY` error | Set a random `SECRET_KEY` of at least 32 characters in `.env` |
| A provider shows `401` in logs | That provider's API key is invalid; the others fail over automatically |
| Clinical Evidence returns 401/403 | Sign in as a doctor; the gateway only admits doctor tokens |
| No confirmation / reset email arrives | `SMTP_HOST` isn't set (or the login is wrong). Outside production the link is in the core API's log; with Gmail check you used an app password |
| Emergency button says "no message was sent" | Same cause: email isn't configured, so nothing was emailed. Call your contact directly and set the `SMTP_*` values |
| Sign-up says the privacy notice was updated | The page is out of date. Refresh it (Ctrl+F5); if you changed the wording, bump `PRIVACY_NOTICE_VERSION` in both `backend/app/privacy.py` and `frontend/static/js/app.js` |
| Diet assistant says "trouble reaching the diet assistant" on Windows | DietBot prints emoji and the console isn't UTF-8. Start it with `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` |

## Design system

The frontend follows an internal design system ("Industry"): Barlow /
Barlow Condensed type, sharp corners, corner-bracket blueprint cards, a
monochrome + steel-blue palette, and no emoji or decorative icons — thin-
stroke inline SVG only.
