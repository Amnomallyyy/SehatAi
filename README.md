# SehatAI

**Every marker, in the same room as your doctor.**

A doctor-patient portal that turns uploaded lab reports into structured,
trend-tracked markers a doctor can actually review — plus an AI symptom/diet
assistant for patients and a verification-first clinical evidence search for
doctors. Built as five small services rather than one monolith, each with a
narrow job.

## What it does

- **Connections** — a patient and doctor mutually opt in (request/accept,
  by email) before anything is shared between them.
- **Structured lab reports** — a patient uploads a PDF/image; OCR +
  extraction turns it into individual markers (test name, value, unit,
  reference range, flag), each cross-checked by an independent verification
  pass. Doctors get a per-marker view, history/trend across reports, and can
  leave notes or mark a report reviewed.
- **Conversations** — direct messaging between a connected doctor and
  patient, with inline report/prescription attachments.
- **Reports access grants** — separate from a connection: a patient
  explicitly (and revocably) grants a doctor visibility into their full
  cross-conversation report history.
- **AI Assistant (patient-only)** — a symptom-triage and diet/nutrition
  chatbot.
- **Clinical Evidence (doctor-only)** — ask a clinical question, get an
  answer where every claim carries its own citation and unverifiable claims
  are removed before you see them, backed by a ranked, scored set of source
  papers.
- **Med Calendar** — medication reminders/scheduling for both roles.
- **Prescriptions** — doctor-issued, attached to a conversation.

## Architecture

Five independent services, each with its own Dockerfile, talking over plain
HTTP on the ports below. The browser talks to all four externally-facing
ones directly; only `sehatai-backend` → `dietbot` is service-to-service.

```
                    ┌──────────────────────┐
   browser ───────▶ │ carelink-frontend     │  :3002   Jinja2 + vanilla JS
                    │ (this repo's UI)      │          the "Industry" design system
                    └──────────┬────────────┘
                               │ REST
              ┌────────────────┼────────────────┬─────────────────┐
              ▼                ▼                ▼                 ▼
   ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ ┌───────────────┐
   │ carelink-backend│ │ sehatai-backend│ │ evidenceboard│ │    dietbot     │
   │   :8000         │ │   :3000        │ │   :8002      │ │   :8001        │
   │ FastAPI+SQLAlch.│ │ FastAPI        │ │ FastAPI      │ │ FastAPI        │
   │ Postgres        │ │ (SehatAI       │ │ Clinical     │ │ Diet/nutrition │
   │ (Supabase)      │ │  profile sync) │ │ evidence     │ │ recommender    │
   │                 │ │                │ │ search       │ │                │
   └────────┬────────┘ └────────────────┘ └──────┬───────┘ └────────┬───────┘
            │ subprocess                          │ SQLite cache     │
            ▼                                      ▼                  ▲
   ┌─────────────────┐                    (question history)          │
   │    datafetch     │  OCR + structured extraction pipeline,        │
   │  (not a service — │  shelled out to on report upload              │
   │   invoked as CLI) │                                               │
   └──────────────────┘                                                │
                               carelink-backend calls dietbot ─────────┘
```

| Service | Port | Role |
|---|---|---|
| `carelink-frontend` | 3002 | The web UI (this is what you open in a browser) |
| `carelink-backend` | 8000 | Core API — auth, connections, conversations, reports, prescriptions, med calendar |
| `sehatai-backend` | 3000 | SehatAI profile bridge (date of birth/sex sync for the AI Assistant) |
| `evidenceboard` | 8002 | Clinical Evidence search (`sehatEvidence/`) |
| `dietbot` | 8001 | Diet/nutrition recommender (`dietbot/`) |
| `datafetch` | — | Not a standing service — invoked as a subprocess on each lab-report upload to run OCR/extraction |

## Prerequisites

- Docker + Docker Compose (recommended), **or** Python 3.12 + a venv per
  service for local/non-Docker dev
- A [Supabase](https://supabase.com) project (Postgres + storage)
- API keys: Groq (report summaries), OCR.space + Gemini (lab report
  extraction), an LLM provider for Clinical Evidence, NVIDIA/Jina for
  DietBot's retrieval — see each `.env.example` below for the exact list

## Setup

Four separate env files, each copied from its own example:

```bash
cp .env.example .env                        # datafetch: OCRSPACE_API_KEY, GEMINI_API_KEY, SUPABASE_URL/KEY, ...
cp backend/.env.example backend/.env         # carelink-backend: DATABASE_URL, SECRET_KEY, GROQ_API_KEY
cp dietbot/.env.example dietbot/.env         # dietbot: SUPABASE_URL/KEY, NVIDIA_API_KEY, JINA_API_KEY, ...
cp sehatEvidence/.env.example sehatEvidence/.env   # evidenceboard: LLM_*, NCBI_*
```

Fill in real values in each, then:

```bash
docker compose up --build
```

Open **http://localhost:3002**.

### Running a single service locally (no Docker)

```bash
cd backend && python -m venv venv && venv\Scripts\activate  # (or source venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Same pattern for `frontend/`, `dietbot/`, `sehatEvidence/` — each has its own
`requirements.txt`. `datafetch/` has no server to run; it's invoked directly
by `backend/app/routers/lab_reports.py` as a subprocess.

## Project layout

```
backend/         Core API (FastAPI + SQLAlchemy + Postgres/Supabase)
  app/routers/    One file per resource (connections, reports, prescriptions, ...)
  API_CONTRACT.md Full endpoint reference
frontend/        UI (Jinja2 templates + static/js/app.js, no build step)
datafetch/       OCR + structured extraction pipeline (CLI, not a server)
dietbot/         Diet/nutrition recommender service
sehatEvidence/   Clinical evidence search service — see its own README
docs/            Architecture notes, database schema, migration SQL
```

Each service that has meaningful setup of its own documents it in its own
`README.md` — start there for service-specific detail; this file is the map.

## Design system

The frontend follows an internal design system ("Industry"): Barlow /
Barlow Condensed type, sharp corners, corner-bracket blueprint cards, a
monochrome + steel-blue palette, and no emoji or decorative icons — thin-
stroke inline SVG only.
