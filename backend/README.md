# Doctor-Patient Communication Portal — Backend

FastAPI + SQLite (SQLAlchemy) + JWT auth. See **`API_CONTRACT.md`** for the full,
locked API reference — that's the document to hand to whoever (or whatever)
builds the frontend.

Tested end-to-end while building this: Python 3.12, on the dependency
versions pinned in `requirements.txt`.

## 1. Install

```bash
cd project                       # this folder
python3 -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

> **Note on `requirements.txt`:** a few small additions beyond what you
> originally listed — `email-validator` (required for Pydantic's
> `EmailStr`, used to validate signup/login emails), a pin on
> `bcrypt==4.0.1` (`passlib` 1.7.4 crashes on `bcrypt>=4.1` with
> `AttributeError: module 'bcrypt' has no attribute '__about__'` —
> password hashing fails on every signup without it), and `groq` / `pypdf`
> / `python-dotenv` for the AI report-summary feature (see below). All are
> explained with comments in `requirements.txt`.

### Set up your Groq API key

AI report summaries call [Groq](https://console.groq.com) — get a free API
key there (no credit card needed), then:

```bash
cp .env.example .env
```

Open `.env` and replace the placeholder with your real key:

```
GROQ_API_KEY=gsk_...your real key...
```

`.env` is already in `.gitignore` — it never gets committed. The rest of
the app works fine even without this step; only `POST
/reports/{id}/ai-summary` needs it, and it fails with a clear `502` (not a
crash) if the key is missing or wrong.

## 2. Run

```bash
uvicorn app.main:app --reload
```

The API is now at `http://localhost:8000`. Interactive docs (Swagger UI) are
at **`http://localhost:8000/docs`**. A fresh `app.db` SQLite file and
`app/static/uploads/` are created automatically on first run — nothing to
set up by hand.

To use a non-default JWT secret (not required for local testing):
```bash
export SECRET_KEY="something-long-and-random"
uvicorn app.main:app --reload
```

## 3. Testing walkthrough

Two ways to test: the interactive `/docs` page (easiest, no terminal
commands to copy) or `curl` (scriptable, shown side-by-side below).

### Using /docs

1. Open `http://localhost:8000/docs`.
2. Expand `POST /auth/signup`, click **Try it out**, and submit a body like:
   ```json
   { "name": "Dr. Alice Smith", "email": "alice@example.com", "password": "doctorpass123", "role": "doctor" }
   ```
   Copy the `access_token` from the response. Repeat with a second account
   using `"role": "patient"` — copy that token too.
3. Click the green **Authorize** button (top right), paste **one** of the
   tokens into the box (just the token — no `Bearer ` prefix, Swagger adds
   that for you), and click Authorize. Every "Try it out" call now sends
   that token automatically.
4. **New: connect the two accounts before anything else works.** Authorize
   as the patient and call `POST /connections` with the doctor's email
   (`{ "email": "alice@example.com" }`) — this comes back `"pending"`.
   Re-authorize as the doctor, call `GET /connections` to see the incoming
   request, then `PATCH /connections/{id}` with `{ "status": "accepted" }`.
   Until you do this, `GET /users`, `GET /users/{id}`, and
   `POST /conversations` between this pair will all `403`/return empty —
   that's the new access-control feature working as intended, not a bug.
5. Work through the rest of the endpoints in `API_CONTRACT.md` in order:
   create a conversation, send a message, upload a report (the file-upload
   endpoint shows a native file picker in Swagger), create an AI summary,
   add a comment. Switch which token you've authorized with to test as the
   other user (e.g. to confirm the doctor can mark a report "reviewed" but
   the patient can't).

### Using curl

This walks through the same flow as a script. `python3 -c "..."` is used to
pull fields out of JSON responses so you don't have to copy-paste tokens by
hand — no extra tools (like `jq`) required beyond Python, which you already
have installed.

```bash
BASE=http://localhost:8000

# --- Signup: doctor and patient ---
DOCTOR_JSON=$(curl -s -X POST $BASE/auth/signup -H "Content-Type: application/json" -d '{
  "name": "Dr. Alice Smith", "email": "alice@example.com", "password": "doctorpass123", "role": "doctor"
}')
DOCTOR_TOKEN=$(echo $DOCTOR_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
DOCTOR_ID=$(echo $DOCTOR_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['user']['id'])")

PATIENT_JSON=$(curl -s -X POST $BASE/auth/signup -H "Content-Type: application/json" -d '{
  "name": "Pat Jones", "email": "pat@example.com", "password": "patientpass123", "role": "patient"
}')
PATIENT_TOKEN=$(echo $PATIENT_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
PATIENT_ID=$(echo $PATIENT_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['user']['id'])")

echo "doctor id=$DOCTOR_ID  patient id=$PATIENT_ID"

# --- Login (alternative to signup, for an existing account) ---
curl -s -X POST $BASE/auth/login -H "Content-Type: application/json" -d '{
  "email": "pat@example.com", "password": "patientpass123"
}'

# --- Connection handshake: patient requests, doctor accepts. Everything ---
# --- below (users, conversations) 403s or comes back empty until this   ---
# --- pair is connected -- that's the new access-control feature.        ---
CONN_JSON=$(curl -s -X POST $BASE/connections -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PATIENT_TOKEN" \
  -d '{"email": "alice@example.com"}')
CONN_ID=$(echo $CONN_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "connection id=$CONN_ID status=$(echo $CONN_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")"

# See it from the doctor's side (the recipient) before accepting it
curl -s "$BASE/connections?status=pending" -H "Authorization: Bearer $DOCTOR_TOKEN"

curl -s -X PATCH $BASE/connections/$CONN_ID -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DOCTOR_TOKEN" \
  -d '{"status": "accepted"}'

# Now the patient can see this doctor (and only this doctor)
curl -s "$BASE/users?role=doctor" -H "Authorization: Bearer $PATIENT_TOKEN"

# --- Create (or fetch, if it already exists) the conversation ---
CONV_JSON=$(curl -s -X POST $BASE/conversations -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PATIENT_TOKEN" \
  -d "{\"patient_id\": $PATIENT_ID, \"doctor_id\": $DOCTOR_ID}")
CONV_ID=$(echo $CONV_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "conversation id=$CONV_ID"

# --- Send a message (as the patient) ---
curl -s -X POST $BASE/conversations/$CONV_ID/messages \
  -H "Content-Type: application/json" -H "Authorization: Bearer $PATIENT_TOKEN" \
  -d '{"text": "Hello doctor, I have a question."}'

# --- Send a reply (as the doctor) ---
curl -s -X POST $BASE/conversations/$CONV_ID/messages \
  -H "Content-Type: application/json" -H "Authorization: Bearer $DOCTOR_TOKEN" \
  -d '{"text": "Of course, what'"'"'s going on?"}'

# --- Poll for messages (as the patient) ---
curl -s $BASE/conversations/$CONV_ID/messages -H "Authorization: Bearer $PATIENT_TOKEN"

# --- Poll for only NEW messages since a given id (the actual polling pattern) ---
curl -s "$BASE/conversations/$CONV_ID/messages?after_id=1" -H "Authorization: Bearer $PATIENT_TOKEN"

# --- Upload a report (needs a real file -- this makes a throwaway one) ---
# Note: this one-line fake PDF has no real text in it, so it's fine for
# testing upload/download/status but the AI summary step below needs an
# ACTUAL PDF with real text to produce anything meaningful -- swap in a
# real file's path for that part.
echo "%PDF-1.4 fake test pdf" > /tmp/test_report.pdf
REPORT_JSON=$(curl -s -X POST $BASE/conversations/$CONV_ID/reports \
  -H "Authorization: Bearer $PATIENT_TOKEN" \
  -F "file=@/tmp/test_report.pdf;type=application/pdf" \
  -F "display_name=March Bloodwork Panel")
REPORT_ID=$(echo $REPORT_JSON | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "report id=$REPORT_ID"
echo $REPORT_JSON

# --- Download the report file back (needs auth -- see API_CONTRACT.md for why) ---
curl -s $BASE/reports/$REPORT_ID/file -H "Authorization: Bearer $PATIENT_TOKEN" -o /tmp/downloaded.pdf
echo "downloaded to /tmp/downloaded.pdf"

# --- Generate an AI summary for the report (as the doctor) ---
# No request body -- the server reads the PDF itself and calls Groq.
# Needs GROQ_API_KEY set up (see step 1) AND a PDF with real text in it,
# or you'll get a 502 explaining why.
curl -s -X POST $BASE/reports/$REPORT_ID/ai-summary -H "Authorization: Bearer $DOCTOR_TOKEN"

# --- Correct one field of the summary by hand (doctor-only) ---
curl -s -X PATCH $BASE/reports/$REPORT_ID/ai-summary \
  -H "Content-Type: application/json" -H "Authorization: Bearer $DOCTOR_TOKEN" \
  -d '{"recommendation": "Repeat lipid panel in 6 weeks."}'

# --- Check the report again -- status should now be "awaiting_review" ---
curl -s $BASE/reports/$REPORT_ID -H "Authorization: Bearer $PATIENT_TOKEN"

# --- Add a comment on the report thread ---
curl -s -X POST $BASE/reports/$REPORT_ID/comments \
  -H "Content-Type: application/json" -H "Authorization: Bearer $PATIENT_TOKEN" \
  -d '{"text": "What does this mean for my diet?"}'

# --- Mark the report reviewed (doctor-only -- try this with $PATIENT_TOKEN to see the 403) ---
curl -s -X PATCH $BASE/reports/$REPORT_ID/status \
  -H "Content-Type: application/json" -H "Authorization: Bearer $DOCTOR_TOKEN" \
  -d '{"status": "reviewed"}'
```

Every response above is JSON — pipe any of them through `python3 -m json.tool`
if you want it pretty-printed.

## 4. Project layout

```
app/
  main.py            FastAPI app, CORS, router wiring, /health
  database.py        engine, SessionLocal, Base, get_db
  models.py          SQLAlchemy models (7 tables, incl. Connection)
  schemas.py         Pydantic request/response models
  security.py        password hashing, JWT issuance/verification, get_current_user
  dependencies.py    shared lookup/authorization helpers used by multiple routers
  ai.py              PDF text extraction (pypdf) + Groq call for AI report summaries
  routers/
    auth.py            /auth/signup, /auth/login
    users.py            /users/me, /users, /users/{id}
    connections.py       /connections... (the request/accept/reject handshake)
    conversations.py    /conversations...
    messages.py          /conversations/{id}/messages...
    reports.py           /conversations/{id}/reports, /reports...
    ai_summaries.py      /reports/{id}/ai-summary...
    report_comments.py   /reports/{id}/comments...
  static/uploads/     uploaded report PDFs land here (random UUID filenames)
requirements.txt
.env.example           copy to .env and fill in your real GROQ_API_KEY
API_CONTRACT.md       full API reference -- hand this to your frontend builder
test_flow.py          optional: a smoke-test script (see below)
```

`security.py` and `dependencies.py` weren't in the original stub list, but
splitting shared auth/lookup logic out of the router files avoided repeating
the same "fetch conversation, check participant" code seven times — every
router imports from these two rather than duplicating it. `ai.py` is the one
and only file that talks to Groq -- if you ever swap providers again, that's
the only file that needs to change.

## 5. Optional: automated smoke test

`test_flow.py` is the script used to verify every endpoint and authorization
rule while building this: signup/login, the connection handshake (request by
email, accept/reject, idempotent re-requests, the rejected→pending retry
path, visibility before vs. after acceptance), conversation creation incl.
get-or-create and the new connection requirement, messages incl. polling,
report upload/download incl. the auth-gated file access and `display_name`
fallback behavior, status transitions, AI summary generation/editing/history,
report comments, and a batch of expected-failure cases like wrong passwords
and cross-conversation access. It's not part of the app itself, so it's not
in `requirements.txt` — it needs `requests`, and optionally `reportlab`:

```bash
pip install requests reportlab   # reportlab is optional -- see below
uvicorn app.main:app &          # start the server first
python3 test_flow.py
```

It prints `PASS`/`FAIL` per check and exits non-zero if anything fails.

**About the AI summary checks specifically:** they call the real Groq API.
If you haven't set up `GROQ_API_KEY` yet (step 1), that section prints an
`[INFO]` line and moves on rather than failing — a `502` there is the
*correct* response when no key is configured, not a bug. If `reportlab` is
installed, the script generates a throwaway PDF with real text in it
specifically for this section, so that once your key *is* set up, the full
structured-response, edit, and regeneration-history behavior gets properly
exercised end to end. Without `reportlab`, that part is skipped with a note
rather than run against a text-less PDF (which would always 502 regardless
of your key, since there'd be nothing for Groq to summarize).
