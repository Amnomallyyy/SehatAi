# SehatAI — Live Database & Auth Architecture

Owner of this work: **teammate** (per conversation, 2026-08-30). This document is
the handoff — current state, target state, exact schema/SQL, and an ordered
task list. Nothing here has been applied to Supabase; it's a plan to execute
against the same Supabase project both the Node app and DietBot already share
(confirmed same project via `.env` `SUPABASE_URL` hash comparison).

---

## 1. Current state (what already exists)

**Auth**: not a real user system. `auth.js` issues a per-patient bearer token
out-of-band via `node sehatai/issuetoken.js <patientId>` (CLI, operator-run). No
signup, no login form, no password. Tokens are SHA-256 hashed in
`patient_api_tokens`. This was built specifically to close an IDOR (client
could previously pass any `patientId` in the request body) — it is NOT a login
system and was never meant to be the long-term auth story.

**Tables that exist today** (Node + DietBot, confirmed by grepping both
codebases):

| Table | Written by | Read by |
|---|---|---|
| `patients` | seed script only | Node (`Patientprofile.js`), DietBot (`retrieval.py`) |
| `medicines` | seed script only | Node, DietBot |
| `extracted_data` (labs) | seed script only | Node (`getlabvalues.js`), DietBot |
| `patient_intake_form` | seed script only | Node (`Patientprofile.js`) |
| `clinical_advice` | seed script only | DietBot (allergy text-mining), Node RAG |
| `documents` | seed script only | referenced but no real ingestion pipeline exists |
| `summaries_vectors` | — | Node (`getPatientdata.js`, RAG over AI summaries) |
| `chat_sessions` / `chat_session_state` | Node (`chatLog.js`) | Node |
| `patient_api_tokens` | Node (`auth.js`) | Node |
| `diet_session_pointers` | Node (`chatLog.js`) | Node |
| `diet_patient_preferences` | DietBot (`personalization.py`) | DietBot |
| `diet_chat_sessions` / `diet_chat_messages` | DietBot (`session_manager.py`) | DietBot |
| `diet_recommendations` | DietBot | — |
| `specialists`, `symptom_related_tests` | seed script only | Node |

**Nothing in either codebase touches Supabase Storage** — file upload is
greenfield, not a gap in an existing feature.

**No RLS policies exist anywhere.** Every table is currently reachable by
whichever Supabase key the app uses (Node uses `SUPABASE_SERVICE_KEY`, a
service-role key that bypasses RLS entirely by design — this stays true after
RLS is added, since the backend is a trusted server, not the browser).

---

## 2. Target architecture

```
Browser (login page, patient console, diet console)
   │
   │  1. signInWithPassword({ email, password })  ──────────►  Supabase Auth
   │                                                            (auth.users)
   │  2. gets back a Supabase session JWT
   │
   ▼
Node backend (sehatai/webserver.js)
   │  verifies the JWT (supabase.auth.getUser(jwt)) → auth.users.id
   │  looks up patients.auth_user_id = that id → patientId
   │  (same "server decides identity, never trust client body" principle
   │   auth.js already established — just swapping the token source)
   │
   ├─► processPatientMessage() / processDietMessage()   [unchanged]
   ├─► file upload endpoint → Supabase Storage bucket `patient-documents`
   │      → row inserted into `documents`, OCR/extraction pipeline (future
   │        work, out of scope here) populates `extracted_data`
   └─► patient intake form endpoint → upsert `patient_intake_form`
```

Everything downstream of "we know the real `patientId`" — the bot pipeline,
`Patientprofile.js`, `getlabvalues.js`, DietBot's `retrieval.py` — is
**unchanged**. This is purely: (a) replace out-of-band CLI tokens with real
login, (b) add file upload, (c) add RLS so a leaked anon key or a bug in a
future client-side Supabase call can't cross patients, (d) make the intake
form mandatory before first bot use.

---

## 3. Schema changes

### 3.1 `patients` — add login identifiers + link to Supabase Auth

```sql
alter table patients
  add column if not exists auth_user_id uuid unique references auth.users(id),
  add column if not exists email text unique,
  add column if not exists cnic text unique; -- Pakistani national ID, 13 digits, no dashes stored

create index if not exists patients_cnic_idx on patients (cnic);
```

`auth_user_id` is the source of truth for "who is this." `email`/`cnic` are
lookup identifiers only — see the login flow in §5 for why CNIC login still
goes through Supabase Auth rather than being a parallel auth system.

### 3.2 File uploads — new `documents` columns + Storage bucket

`documents` already exists (seeded, not yet written to by real code). Add:

```sql
alter table documents
  add column if not exists storage_path text,       -- bucket-relative path
  add column if not exists original_filename text,
  add column if not exists mime_type text,
  add column if not exists uploaded_at timestamptz not null default now(),
  add column if not exists processing_status text not null default 'pending';
  -- 'pending' | 'processing' | 'done' | 'failed' — for the future OCR/extraction step
```

Storage bucket (create via Supabase dashboard or `supabase storage`):

```sql
insert into storage.buckets (id, name, public)
values ('patient-documents', 'patient-documents', false)
on conflict (id) do nothing;
```

Keep it **private** (`public: false`) — files are accessed only via signed
URLs the backend generates after verifying the requester owns the document,
never via a public bucket URL.

### 3.3 Mandatory intake form — nothing new needed

`patient_intake_form` already has the right shape (`existing_conditions`,
`allergies`, `current_medications`, `family_history` — see
`Patientprofile.js`'s doc comment). What's missing is enforcement: the app
currently never checks whether a row exists before letting a patient chat.
See §6.

---

## 4. Row-Level Security (RLS)

Enable RLS on every patient-scoped table, with one consistent policy shape:
a row is visible/writable only if it belongs to the `patients` row whose
`auth_user_id` matches the current session (`auth.uid()`). The Node backend
uses the **service-role key**, which bypasses RLS — these policies are the
safety net for any future direct-from-browser Supabase call (e.g. a
Supabase Storage upload signed on the client) and for defense-in-depth if a
key is ever scoped down or exposed.

```sql
-- Helper: resolve auth.uid() to this patient's row id, once, reusably.
create or replace function auth_patient_id()
returns uuid
language sql
stable
as $$
  select id from patients where auth_user_id = auth.uid()
$$;

-- Repeat this pattern for: patients, medicines, extracted_data,
-- patient_intake_form, clinical_advice, documents, chat_sessions,
-- diet_session_pointers, diet_patient_preferences, diet_recommendations.
-- (chat_session_state / diet_chat_sessions / diet_chat_messages key off
-- session_id, not patient_id directly — join through chat_sessions /
-- diet_chat_sessions for those instead, same idea.)

alter table patients enable row level security;
create policy "patients_select_own" on patients
  for select using (auth_user_id = auth.uid());
create policy "patients_update_own" on patients
  for update using (auth_user_id = auth.uid());
-- no insert/delete policy for patients — accounts are created server-side
-- during signup (§5), never directly by the client.

alter table medicines enable row level security;
create policy "medicines_select_own" on medicines
  for select using (patient_id = auth_patient_id());

alter table extracted_data enable row level security;
create policy "extracted_data_select_own" on extracted_data
  for select using (patient_id = auth_patient_id());

alter table patient_intake_form enable row level security;
create policy "intake_select_own" on patient_intake_form
  for select using (patient_id = auth_patient_id());
create policy "intake_upsert_own" on patient_intake_form
  for all using (patient_id = auth_patient_id())
  with check (patient_id = auth_patient_id());

alter table clinical_advice enable row level security;
create policy "clinical_advice_select_own" on clinical_advice
  for select using (patient_id = auth_patient_id());

alter table documents enable row level security;
create policy "documents_all_own" on documents
  for all using (patient_id = auth_patient_id())
  with check (patient_id = auth_patient_id());

-- Storage: mirror the same ownership check on the bucket, keyed by the
-- top-level folder in storage_path being the patient's own auth_user_id
-- (i.e. upload path convention: `${auth.uid()}/${filename}`).
create policy "patient_documents_own_folder"
  on storage.objects for all
  using (bucket_id = 'patient-documents' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'patient-documents' and (storage.foldername(name))[1] = auth.uid()::text);
```

**Do not enable RLS on `patient_api_tokens`** unless it's being kept
long-term (see §7 migration note) — it's server-only, service-role-key-only
data; RLS on it adds nothing since the browser should never query it
directly regardless.

---

## 5. Login flow: email OR CNIC

Supabase Auth's password grant needs an email. CNIC login is handled by
resolving CNIC → email server-side first, then calling the same
`signInWithPassword`, so there is exactly **one** credential-verification
path (Supabase Auth's), not two auth systems to keep in sync.

```
POST /api/auth/login   { identifier: string, password: string }
  1. identifier looks like an email (contains "@")?
       → email = identifier
     else (treat as CNIC):
       → SELECT email FROM patients WHERE cnic = identifier
       → 401 if no match (don't reveal whether the CNIC exists at all —
         same generic "invalid credentials" message either way)
  2. supabase.auth.signInWithPassword({ email, password })
  3. on success, return the session (access_token + refresh_token) to the
     client; client stores it (e.g. supabase-js's own session persistence,
     not a hand-rolled bearer token anymore)
```

**Signup** (`POST /api/auth/signup`) is a separate, simpler flow:
`supabase.auth.signUp({ email, password })`, then insert the `patients` row
with `auth_user_id`, `email`, `cnic` in the same request (server-side,
service-role key — so the two writes can't end up half-done from the
client's point of view). CNIC uniqueness is enforced by the `unique`
constraint in §3.1 — surface that constraint violation as a friendly
"this CNIC is already registered" error, not a raw DB error.

**On the Node backend**, `auth.js`'s `verifyApiToken(rawToken)` gets a new
sibling (or is replaced — see §7): `verifySupabaseSession(jwt)` that calls
`supabase.auth.getUser(jwt)`, then resolves `patients.auth_user_id` the same
way `verifyApiToken` currently resolves a token hash. Same shape, same
"server decides identity" principle — just a different credential source.

---

## 6. Mandatory intake form gate

Add a check at the very top of `processPatientMessage`/`processDietMessage`
(or, cleaner, in `sehatai/webserver.js` before either is called): if
`patient_intake_form` has no row for this `patientId`, short-circuit with a
`kind: 'intake_required'` response instead of running the pipeline. The
frontend renders a form (existing_conditions, allergies,
current_medications, family_history — the exact fields
`Patientprofile.js` already reads) and `POST`s it to a new
`/api/intake` endpoint that upserts `patient_intake_form`. Once that row
exists, the gate never fires again for that patient. This is intentionally a
gate, not baked into the chat pipeline itself, so it's one small addition
rather than a change to `processMessage.js`'s existing stage numbering.

---

## 7. Migration note: token auth → real auth

Don't delete `auth.js`/`patient_api_tokens` on day one. Keep both auth paths
accepted in `authenticate()` (`sehatai/webserver.js`) during rollout — Bearer token
OR Supabase session JWT, either resolves to a `patientId` — so demo/test
flows that already have issued tokens (e.g. anything using
`sehatai/issuetoken.js`) keep working while the real login UI is being built and
tested. Retire the token path once the login flow is confirmed working
end-to-end, at whatever point makes sense for the demo timeline.

---

## 8. Client-side session persistence (separate from DB work, but related)

Not a database change, but came up in the same conversation and affects how
`sessionId` should be handled once real login exists: right now
`public/index.html` / `public/diet.html` keep `sessionId` in a plain JS
variable, so a page refresh loses it and the chat log starts empty (the
"New session" button is the only *intentional* reset path — a refresh
shouldn't be an accidental one). **Priority is `index.html` (the symptom/
triage console)** — that's the one carrying real clinical state
(accumulated symptoms, pending clarification/disambiguation questions,
emergency-acknowledgment flags) that's genuinely costly to lose on an
accidental refresh mid-triage; the diet console's state is much lower-stakes
and can follow after. Once file upload/login exist, persist `sessionId` in
`localStorage` per console (keep the two consoles' keys separate, same
reasoning as the two-page split — see the diet-console commit) alongside a
timestamp, treat it as expired after 24h (matches how sessions already
naturally go stale), and on load, if a non-expired `sessionId` is found,
re-fetch that session's message history from a new `GET
/api/chat/:sessionId/history` endpoint to rebuild the visual log instead of
starting blank. This is a small, self-contained addition — flagging it here
so whoever builds the login/upload work doesn't have to rediscover it.

---

## 9. Ordered task list

1. Run the `patients` schema migration (§3.1).
2. Create the `patient-documents` Storage bucket (§3.2) + `documents` column
   migration.
3. Write and test the RLS policies (§4) against a real logged-in session —
   verify a patient can only ever see their own rows, and that the Node
   backend (service-role key) is unaffected.
4. Build `POST /api/auth/signup` and `POST /api/auth/login` (§5).
5. Build the frontend login/signup page (replaces manually pasting a token
   from `sehatai/issuetoken.js`).
6. Add the intake-form gate (§6) + its endpoint + frontend form.
7. Build the file upload endpoint (signed upload to `patient-documents`,
   insert into `documents`) + frontend upload UI.
8. Update `sehatai/webserver.js`'s `authenticate()` to accept both token and
   Supabase-JWT auth (§7) during rollout.
9. (Optional, flagged in §8) session-persistence + history-rehydration
   endpoint.
10. Once login is confirmed working end-to-end, retire `sehatai/issuetoken.js` /
    `patient_api_tokens` path.

Everything downstream of step 4 (the actual triage/diet pipelines,
Infermedica calls, DietBot integration) needs **zero changes** — they only
ever cared about "what is the correct `patientId`," never about how it was
authenticated.

---

## 10. Known gaps / open questions (not designed yet — flagging, not blocking)

1. **Uploaded documents aren't processed into usable data.** This doc only
   gets a file into Storage + a `documents` row (`processing_status:
   'pending'`). Nothing parses a lab report/PDF into `extracted_data` or
   `clinical_advice` — that OCR/extraction pipeline is undesigned. Until it
   exists, an uploaded file is inert: the bot still can't see anything in
   it. This is the biggest gap if "the bot can retrieve data from what the
   user uploaded" is a hard requirement, not a nice-to-have.
2. **Auth calls need the anon key, not the service-role key.** `.env`
   currently only has `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`. Login/signup
   (`signInWithPassword`/`signUp`) should go through `SUPABASE_ANON_KEY` —
   add it. Service-role bypasses RLS, which is the wrong trust level for a
   browser-facing credential check.
3. **No migration path for existing seeded/demo patients** (Ayesha, Bilal,
   etc.) — they have no `auth_user_id`/`email`/`cnic`. Once RLS is on,
   they're unreachable via a real login until backfilled or recreated
   through signup.
4. **Editing the intake form after the first submission isn't covered** —
   §6 only designs the one-time gate ("no row yet → block"), not a normal
   "update my allergies" settings flow. The RLS policy already allows it
   (`intake_upsert_own` is `for all`); the endpoint/UI for it isn't
   designed.
5. **No brute-force protection on login**, no CNIC format validation
   (13-digit Pakistani format), no file type/size allowlist on upload, no
   password reset flow, no email verification, no logout/session
   invalidation handling. None of these are designed — standard
   auth/upload hardening, left to whoever builds it.
