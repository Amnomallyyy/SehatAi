# API Contract — Doctor-Patient Communication Portal

This is the full, locked contract for the backend. It's written to be sufficient
on its own — you should not need to read the backend source to build a
frontend against this.

> **Update (Aug 31, 2026): reports access grants, prescriptions, appointments,
> profiles/avatars, and doctor specialization/nicknames added.**
>
> - **Breaking:** `POST /reports/{report_id}/comments` is now **doctor-only**
>   (`403` for a patient). `GET` is unaffected — patients keep read access.
> - **Additive:** `User` gained `specialization` and `avatar_url`.
>   `Connection` gained `doctor_nickname` (only ever populated for the doctor
>   on that connection — always `null` when the caller is the patient).
> - **New:** a `ReportAccessGrant` concept gates the new dedicated,
>   cross-conversation Reports view (see [Reports Access](#reports-access))
>   — separate from `Connection` and from the always-available inline
>   per-conversation reports list, both of which are unaffected.
> - **New:** [Prescriptions](#prescriptions) — doctor-only PDF uploads, kept
>   entirely separate from Report/AISummary/ReportComment. No AI-summary
>   route exists for this model at all.
> - **New:** [Appointments](#appointments) — a Med Calendar, with a
>   stateless, computed-on-read reminder field.
> - **New:** [Profile / Avatar](#profile--avatar) endpoints.
>
> See the updated [Users](#users) / [Connections](#connections) sections,
> the new sections linked above, and design notes 13–17 at the bottom.

> **Update (Aug 30, 2026): connection ("handshake") requirement added.**
> Patients and doctors are no longer visible to each other by default, and a
> Conversation can no longer be created between just any patient/doctor pair.
> A new `/connections` flow gates both. **If a frontend was already built
> against an earlier version of this contract, these are breaking changes:**
> - `GET /users?role=doctor` / `GET /users?role=patient` now return only
>   users the caller has an **accepted** connection with, not the full
>   roster. A doctor/patient picker built around "browse everyone" needs to
>   be rebuilt around the new `/connections` flow instead (see below).
> - `GET /users/{user_id}` now `403`s unless the target is the caller
>   themself or an accepted connection — it's no longer a general-purpose
>   profile lookup.
> - `POST /conversations` now `403`s if there's no accepted connection
>   between the given `patient_id`/`doctor_id`, even if both ids are valid
>   and the caller is one of the two parties.
>
> See the new [Connections](#connections) section, the updated
> [Users](#users) / [Conversations](#conversations) sections, and design
> notes 8–11 at the bottom for the full detail.

## Overview

- **Base URL (local dev):** `http://localhost:8000`
- **Format:** JSON request/response bodies everywhere, **except**:
  - `POST /conversations/{conversation_id}/reports` — `multipart/form-data` (file upload)
  - `GET /reports/{report_id}/file` — raw PDF bytes (`application/pdf`)
- **Auth:** Bearer JWT. Get a token from `/auth/signup` or `/auth/login`, then send it on every other request:
  `Authorization: Bearer <access_token>`
- **CORS:** enabled for all origins/methods/headers. No cookies are used, so no `credentials: 'include'` is needed on `fetch()` calls.
- **Timestamps:** ISO 8601 strings, always UTC, **no offset suffix** (e.g. `"2026-08-28T10:33:00.408876"`). Treat every timestamp field in this API as UTC regardless of the missing suffix.
- **IDs:** integers.
- **Errors:** see [Error shapes](#error-shapes) below — there are two different shapes depending on the failure type.

## Auth flow

1. `POST /auth/signup` (new account) or `POST /auth/login` (existing account) → response includes `access_token` and the `user` object.
2. Store the token (e.g. `localStorage`).
3. Send `Authorization: Bearer <access_token>` on every subsequent request.
4. Tokens expire after 24 hours. On a `401`, send the user back to login.

Every endpoint below requires auth **except** `/health`, `/auth/signup`, and `/auth/login`.

## Error shapes

- **Business-logic errors** (auth failure, not found, forbidden, bad input caught by the route itself) — HTTP 400/401/403/404, body:
  ```json
  { "detail": "Human-readable message" }
  ```
- **Request validation errors** (missing/malformed JSON fields — FastAPI/Pydantic's built-in validation) — HTTP 422, body is a **list**, not a string:
  ```json
  { "detail": [ { "type": "missing", "loc": ["body", "email"], "msg": "Field required", "input": {...} } ] }
  ```
  If you're showing errors to the user, check whether `detail` is a string or an array and handle both.

## Core object shapes

These are the exact JSON shapes referenced by name in the endpoint list below.

**User**
```json
{
  "id": 1, "name": "Dr. Alice Smith", "email": "alice@example.com", "role": "doctor",
  "specialization": "Cardiologist", "avatar_url": "/users/1/avatar"
}
```
`role` is always `"doctor"` or `"patient"`. `specialization` is doctor-only (always `null` for a patient), nullable (existing accounts start with no specialization), and a plain string — see [Profile / Avatar](#profile--avatar) for the preset list. `avatar_url` is `null` until a photo is uploaded; when present it's an authenticated download path (`/users/{id}/avatar`), same pattern as `Report.pdf_url` — **not** a public static URL.

**Token** (response of signup/login)
```json
{ "access_token": "eyJ...", "token_type": "bearer", "user": User }
```

**Connection**
```json
{
  "id": 1, "patient_id": 3, "doctor_id": 1, "requested_by_id": 3,
  "status": "pending",
  "created_at": "2026-08-30T14:25:38.779694",
  "responded_at": null,
  "doctor_nickname": null,
  "patient": User, "doctor": User
}
```
`status` is `"pending"`, `"accepted"`, or `"rejected"`. `requested_by_id` is whichever of `patient_id`/`doctor_id` sent the request — compare it to your own id to know whether you're waiting on the other party or they're waiting on you. `responded_at` is `null` until the recipient accepts or rejects. `doctor_nickname` is a private label the doctor sets for this patient (see `PATCH /connections/{id}/nickname` below) — it is **only ever populated when you're the doctor on this connection**; the server nulls it out in every response sent to the patient side, since it's a note about them they're not meant to see.

**Conversation**
```json
{
  "id": 1, "patient_id": 2, "doctor_id": 1,
  "created_at": "2026-08-28T10:33:00.408876",
  "patient": User, "doctor": User
}
```
`patient`/`doctor` are the full nested User objects (not just IDs) so you can render names without an extra round trip.

**Message**
```json
{ "id": 1, "conversation_id": 1, "sender_id": 2, "text": "Hello doctor", "timestamp": "2026-08-28T10:33:00.434926" }
```
Note: `sender_id` is **not** expanded into a nested User. Match it against the conversation's `patient_id`/`doctor_id` (which you already have from `GET /conversations`) to know who sent it and render their name/avatar. This keeps polling payloads small — there are only ever two possible senders per conversation anyway.

**AISummary**
```json
{
  "id": 2, "report_id": 1,
  "summary": "LDL cholesterol is mildly elevated; other values are within normal range.",
  "key_findings": ["LDL Cholesterol: 145 mg/dL (high)", "Fasting Glucose: 92 mg/dL (normal)"],
  "flagged_values": ["LDL Cholesterol: 145 mg/dL"],
  "recommendation": "Discuss dietary changes and consider a follow-up lipid panel.",
  "created_at": "2026-08-28T10:33:00.521678"
}
```
Generated automatically (see `POST /reports/{id}/ai-summary` below) — the client never writes this text directly, only edits it afterward.

**Report**
```json
{
  "id": 1, "conversation_id": 1, "patient_id": 2,
  "display_name": "Bloodwork - March 2026",
  "pdf_url": "/reports/1/file",
  "status": "awaiting_review",
  "timestamp": "2026-08-28T10:33:00.462141",
  "ai_summary": AISummary | null
}
```
`status` is one of `"uploaded"`, `"processing"`, `"awaiting_review"`, `"reviewed"`. `display_name` is whatever the uploader chose to call it (see the upload endpoint) — purely a label, unrelated to how the file is actually stored.

⚠️ **`pdf_url` is not a plain static file URL.** It's a path on *this* API (`/reports/{id}/file`) that requires the same `Authorization` header as everything else. A plain `<a href={pdf_url}>` or `<img src={pdf_url}>` will get a 401, because browsers don't attach custom headers to normal navigations/`src` loads. See [Downloading a report file](#downloading-a-report-file) below for the exact pattern to use. This is intentional — report PDFs are medical documents, so they're behind the same auth as everything else rather than a guessable-but-public static URL.

**ReportComment**
```json
{ "id": 1, "report_id": 1, "sender_id": 2, "text": "What does this mean for my diet?", "timestamp": "2026-08-28T10:33:00.544670" }
```
Same shape as Message, same "no nested sender" rule — this is the "thread" view on a report, separate from the main chat.

**ReportAccessGrant**
```json
{
  "id": 1, "patient_id": 3, "doctor_id": 1,
  "status": "granted",
  "created_at": "2026-08-31T10:00:00.000000",
  "updated_at": "2026-08-31T10:00:00.000000",
  "patient": User, "doctor": User
}
```
`status` is `"granted"` or `"revoked"`. One row per `(patient_id, doctor_id)` pair, same "flip status, don't delete" idiom as `Connection`.

**Prescription**
```json
{
  "id": 1, "conversation_id": 1, "patient_id": 3, "doctor_id": 1,
  "display_name": "Amoxicillin - Aug 2026",
  "pdf_url": "/prescriptions/1/file",
  "timestamp": "2026-08-31T10:00:00.000000"
}
```
Deliberately has no `status` and no `ai_summary` field — neither concept applies to prescriptions. `doctor_id` is always the uploader (prescriptions are doctor-only). `pdf_url` follows the same authenticated-download pattern as `Report.pdf_url`.

**Appointment**
```json
{
  "id": 1, "patient_id": 3, "doctor_id": 1,
  "scheduled_at": "2026-09-05T10:00:00.000000",
  "reason": "Follow-up checkup",
  "status": "scheduled",
  "created_by_id": 3,
  "created_at": "2026-08-31T10:00:00.000000",
  "active_reminder": "3d",
  "patient": User, "doctor": User
}
```
`status` is `"scheduled"` or `"cancelled"`. `active_reminder` is computed fresh on every request from `scheduled_at` vs. the current time — it is **not** stored, and there is no dismissal/read-tracking state. It's `"3d"`, `"1d"`, `"2h"`, or `null`; only the single closest threshold is reported (e.g. an appointment 90 minutes out reports `"2h"`, not all three). It's always `null` once `status` is `"cancelled"` or the appointment time has passed.

---

## Endpoints

### Auth

#### `POST /auth/signup`
Create an account and log in immediately.
- Auth: none
- Body: `{ "name": string, "email": string, "password": string (8-72 chars), "role": "doctor" | "patient" }`
- `201` → `Token`
- `400` if email is already registered
- Email is lowercased/trimmed automatically before storage and comparison.

#### `POST /auth/login`
- Auth: none
- Body: `{ "email": string, "password": string }`
- `200` → `Token`
- `401` if email/password don't match (same message either way, so you can't tell which one was wrong)

### Users

#### `GET /users/me`
- `200` → `User` (the caller's own profile)

#### `GET /users?role=doctor` or `GET /users?role=patient`
List users by role. `role` is **required** — there's no unfiltered "list everyone".
- Returns only users the caller has an **accepted** connection with (see [Connections](#connections) below for how one is formed) — not the full roster of that role.
- `role=doctor`: your connected doctors. `role=patient`: your connected patients. There's no role-based restriction on who can *call* this anymore (any authenticated user can query either value) — the connection filter does all the work, and naturally returns `[]` for combinations that can't apply to you (e.g. a doctor has no "connected doctors").
- `200` → `User[]`, sorted by name. Empty list, not an error, if you have no accepted connections of that role.

#### `GET /users/{user_id}`
- `200` → `User` if `user_id` is your own id, **or** you have an accepted connection with that user.
- `403` if neither of those is true (even if the user exists).
- `404` if no such user exists at all.

### Profile / Avatar

#### `PATCH /users/me`
Edit your own profile.
- Body: any subset of `{ "name": string, "specialization": string }` — only included fields change.
- `specialization` is doctor-only — `400` if a patient sends it. Not validated against the preset list server-side (a patient sending garbage would 400 anyway; a doctor's client is expected to pair the preset dropdown with a free-text "Other" field, but the column itself just stores whatever string is sent).
- `200` → `User`

Preset specialization options (`DOCTOR_SPECIALIZATIONS`, exposed for the frontend to build a dropdown from): `Cardiologist`, `Dermatologist`, `Endocrinologist`, `Gastroenterologist`, `General Practitioner`, `Nephrologist`, `Neurologist`, `Obstetrician/Gynecologist`, `Oncologist`, `Ophthalmologist`, `Orthopedist`, `Pediatrician`, `Psychiatrist`, `Pulmonologist`, `Rheumatologist`, `Urologist`, `Other`. `"Other"` is a UI affordance pairing this list with a free-text field — the stored value is just whatever string is sent, preset or custom.

#### `POST /users/me/avatar`
Upload a profile photo. **`multipart/form-data`**, like report uploads.
- Form field: `file` — JPEG, PNG, or WebP, max 2 MB.
- Stored as-uploaded (no resizing) with a random on-disk filename, same security rationale as report PDFs.
- `200` → `User` (with the new `avatar_url` populated)
- `400` if the content-type isn't one of the three accepted image types, or the file exceeds 2 MB.

#### `GET /users/{user_id}/avatar`
Streams the photo (`Content-Type: image/jpeg|png|webp`). **Requires the `Authorization` header**, same fetch+blob pattern as report files (see [Downloading a report file](#downloading-a-report-file)).
- Same visibility rule as `GET /users/{user_id}`: self, or an accepted connection. `403` otherwise.
- `404` if the user has no avatar uploaded, or doesn't exist.

### Connections

The handshake: one side requests a connection to the other **by email**, the recipient accepts or rejects it, and only an **accepted** connection unlocks visibility (`GET /users`) and conversation creation. There's no "browse all doctors/patients" endpoint anymore — you're expected to already know the email of the specific person you want to connect with (e.g. your doctor gave it to you at your visit), the same way you'd add any contact by email.

No email is actually sent anywhere — a pending request just becomes visible to the recipient via `GET /connections`. If you want real email notifications, that's a separate integration this backend doesn't include.

#### `POST /connections`
Send (or effectively re-send) a connection request.
- Body: `{ "email": string }` — the **other** party's email, not yours.
- Always `200` (mirrors `POST /conversations`'s get-or-create style) → `Connection`:
  - No existing row for this pair → a new one is created with `status: "pending"` and `requested_by_id` set to you.
  - Existing row with status `"pending"` or `"accepted"` → returned as-is, unchanged. Calling this twice doesn't spam a second request.
  - Existing row with status `"rejected"` → flipped back to `"pending"` with `requested_by_id` set to you (the same row is reused, not a new one — there's only ever one Connection row per patient/doctor pair).
- `400` if the email is your own, or belongs to a user of the **same** role as you (patient↔patient and doctor↔doctor connections aren't a thing).
- `404` if no user has that email at all.

#### `GET /connections`
- Query param `status` (optional): filter to `"pending"`, `"accepted"`, or `"rejected"`. Omit it to get everything.
- `200` → `Connection[]`, **newest first** — every connection you're part of, either direction, any status.
- Build your UI by filtering this client-side: **incoming requests** = `status === "pending" && requested_by_id !== myId`; **outgoing requests** = `status === "pending" && requested_by_id === myId`; **connected** = `status === "accepted"`.

#### `PATCH /connections/{connection_id}`
Accept or reject a pending request. Only the **recipient** can call this — not the person who sent the request.
- Body: `{ "status": "accepted" | "rejected" }`
- `403` if you're not part of this connection, or if you *are* the one who sent the request (`requested_by_id === your id`) — you can't accept your own request.
- `400` if the connection isn't currently `"pending"` (already decided).
- `200` → `Connection`
- Note: there's no separate "disconnect" endpoint for an already-`"accepted"` connection in this version — this only handles responding to a pending request.

#### `PATCH /connections/{connection_id}/nickname`
Set (or clear) a private label for a connected patient — for the doctor's own convenience only, never shown to the patient.
- Body: `{ "doctor_nickname": string | null }`
- **Doctor-only**, and only the doctor on this specific connection — `403` otherwise.
- `400` if the connection isn't `"accepted"` yet.
- `200` → `Connection`

### Conversations

#### `POST /conversations`
Get-or-create. If a conversation between this exact `patient_id`/`doctor_id` pair already exists, it's returned as-is instead of creating a duplicate — always check `id` in the response rather than assuming a new one was made.
- Body: `{ "patient_id": int, "doctor_id": int }`
- The caller must be one of the two parties (`current_user.id` must equal `patient_id` or `doctor_id`) — `403` otherwise.
- `patient_id` must reference an existing user with `role: "patient"`, and `doctor_id` a user with `role: "doctor"` — `400` otherwise (this is the role-validity check the data model can't express via a foreign key).
- **New:** an **accepted** `Connection` must already exist between `patient_id` and `doctor_id` — `403` otherwise, with a message pointing at `POST /connections`. Send and accept a connection request first.
- `200` (not 201 — same response whether it was found or newly created) → `Conversation`

#### `GET /conversations`
- `200` → `Conversation[]`, **newest first** (`created_at` descending) — the caller's own conversations only, as either patient or doctor.

#### `GET /conversations/{conversation_id}`
- `200` → `Conversation`
- `403` if the caller isn't a participant, `404` if it doesn't exist

### Messages

#### `POST /conversations/{conversation_id}/messages`
- Body: `{ "text": string (1-10000 chars) }`
- `sender_id` is always the caller — not part of the request body.
- `201` → `Message`
- `403` if not a participant

#### `GET /conversations/{conversation_id}/messages`
The polling endpoint. **Chronological order (oldest first)**, like any chat transcript.

Query params (all optional):
| param | type | meaning |
|---|---|---|
| `after_id` | int | only messages with `id` greater than this |
| `since` | ISO 8601 datetime | only messages with `timestamp` after this |
| `limit` | int, default 200, max 1000 | cap on results |

**Use `after_id` for polling, not `since`.** `after_id` is a monotonic cursor — poll with `after_id = <id of the last message you already have>`. `since` is provided too since the spec mentions both, but timestamp-based polling has a real edge case: if two messages land in the same instant, a `since` set to "the last message's own timestamp" can skip one of them. `since` accepts both naive and `Z`/offset-suffixed datetimes (e.g. JavaScript's `date.toISOString()` output works fine) — the server normalizes either form before comparing.

Recommended polling pattern:
```js
let lastId = 0; // or the highest id you already have
setInterval(async () => {
  const res = await fetch(`${API_BASE}/conversations/${convId}/messages?after_id=${lastId}`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  const newMessages = await res.json();
  if (newMessages.length) {
    lastId = newMessages[newMessages.length - 1].id;
    // append newMessages to your UI
  }
}, 3000);
```

### Reports

Reports appear inline in the conversation, so they're listed both per-conversation and per-patient.

#### `POST /conversations/{conversation_id}/reports`
Upload a PDF. **`multipart/form-data`, not JSON** — the one exception to "everything is JSON" in this API, because it's a binary file.
- Form field: `file` (must be a PDF — checked by content-type or `.pdf` extension; max 20 MB)
- Form field: `display_name` (optional string, ≤200 chars) — what to call this report. If omitted or blank, it falls back to the uploaded file's own filename (minus the `.pdf`), or `"Untitled Report"` if that's not available either.
- Either participant (patient or doctor) may upload.
- `patient_id` is derived from the conversation server-side — it is **not** something you send.
- `201` → `Report` (status `"uploaded"`, `ai_summary: null`)
- `400` if the file isn't a PDF or exceeds the size limit

Example with `fetch`:
```js
const formData = new FormData();
formData.append("file", fileInput.files[0]);
formData.append("display_name", "Bloodwork - March 2026"); // optional
const res = await fetch(`${API_BASE}/conversations/${convId}/reports`, {
  method: "POST",
  headers: { Authorization: `Bearer ${token}` }, // do NOT set Content-Type -- let the browser set the multipart boundary
  body: formData,
});
```

#### `GET /conversations/{conversation_id}/reports`
- `200` → `Report[]`, chronological (oldest first), for inline placement in the chat.

#### `GET /reports?patient_id={id}`
All reports for one patient across every conversation (e.g. a "patient history" view). `patient_id` is **required**.
- Patients may only query their own `patient_id` (`403` otherwise).
- Doctors may only query a patient they have an **accepted connection** with (`403` otherwise) — same rule as `GET /users`, so a doctor can't pull a stranger's reports just by knowing/guessing their id.
- `200` → `Report[]`, **newest first**.

#### `GET /reports/{report_id}`
- `200` → `Report`
- `403`/`404` as usual

#### `GET /reports/{report_id}/file`
Streams the actual PDF (`Content-Type: application/pdf`). **Requires the `Authorization` header** — see the warning under `pdf_url` above.

#### Downloading a report file
```js
const res = await fetch(`${API_BASE}${report.pdf_url}`, {
  headers: { Authorization: `Bearer ${token}` }
});
const blob = await res.blob();
const objectUrl = URL.createObjectURL(blob);
// Use objectUrl in <iframe src={objectUrl}> to preview inline,
// or <a href={objectUrl} download="report.pdf"> to let the user save it.
// Call URL.revokeObjectURL(objectUrl) when you're done with it to free memory.
```

#### `PATCH /reports/{report_id}/status`
- Body: `{ "status": "uploaded" | "processing" | "awaiting_review" | "reviewed" }`
- Status only moves **forward** through that exact sequence — `400` if you try to move backward or set the same status again.
- Setting `"reviewed"` is **doctor-only** (`403` for a patient). Every other forward move just requires being a participant.
- `200` → `Report`

### AI Summaries

Backed by a real model call (Groq): the server extracts the uploaded PDF's text and asks an LLM to turn it into the four structured fields below. The client never writes the summary text directly — only edits it afterward.

#### `POST /reports/{report_id}/ai-summary`
Reads the report's PDF and generates a fresh summary. **No request body.**
- Creates a **new** AISummary (does not overwrite previous ones — that's the whole point of AISummary being its own table) and repoints the report at it.
- Side effects: `report.ai_summary_id` now points at this new summary; `report.status` is set to `"awaiting_review"` regardless of what it was before (a new summary always needs a fresh review).
- `201` → `AISummary`
- `502` if generation fails — e.g. the API key isn't configured, the PDF has no extractable text (a scanned image with no text layer), or the model's response couldn't be parsed. `detail` has a human-readable reason.

#### `PATCH /reports/{report_id}/ai-summary`
Correct one or more fields of the **current** summary by hand — e.g. the model misread a number. This edits in place; it does **not** create a new history entry (regeneration via `POST` above is what does that).
- Body: any subset of `{ "summary": string, "key_findings": string[], "flagged_values": string[], "recommendation": string }` — only included fields change.
- **Doctor-only** (`403` for a patient) — this is clinical content a patient shouldn't be able to alter.
- `404` if no summary exists yet to edit.
- `200` → `AISummary`

#### `GET /reports/{report_id}/ai-summary`
The current summary only.
- `200` → `AISummary`
- `404` if none has been generated yet

#### `GET /reports/{report_id}/ai-summary/history`
Every summary ever generated for this report, **newest first**. Edits made via `PATCH` are reflected in place on the relevant entry — they don't add new entries here.
- `200` → `AISummary[]` (empty array if none yet)

### Report Comments

The Slack-thread-style view on a single report — separate from the main chat, same shape as Message, same polling params.

#### `POST /reports/{report_id}/comments`
**Doctor-only** (`403` for a patient) — this is the clinical-discussion thread on a report; patients keep read access below.
- Body: `{ "text": string (1-10000 chars) }`
- `201` → `ReportComment`

#### `GET /reports/{report_id}/comments`
Either participant, unaffected by the doctor-only restriction above.
- Query params: `after_id`, `since`, `limit` — identical semantics to the messages endpoint above.
- `200` → `ReportComment[]`, chronological (oldest first).

### Reports Access

A patient's explicit, revocable grant letting a specific connected doctor view their full cross-conversation Reports/Prescriptions history — the dedicated Reports tab. This is separate from `Connection` (which only gates messaging and the always-available inline per-conversation reports/prescriptions list) and separate from a second handshake — a `Connection` already requires mutual opt-in for the relationship to exist, so granting/revoking here is a simple one-sided flag, not another request/accept flow. There's no "doctor requests access" endpoint; a doctor has to ask the patient out-of-band, the same spirit as `Connection`s being initiated by already knowing the other party's email.

#### `POST /reports-access/grant`
Get-or-create + flip-to-granted, mirroring `POST /connections`' idempotent style.
- Body: `{ "doctor_id": int }`
- **Patient-only.** Requires an accepted `Connection` with that doctor — `403` otherwise.
- `400` if `doctor_id` doesn't refer to an existing doctor.
- `200` → `ReportAccessGrant`

#### `POST /reports-access/{grant_id}/revoke`
- **Patient-only**, and only the patient who owns the grant — `403` otherwise.
- `404` if no such grant.
- `200` → `ReportAccessGrant` (with `status: "revoked"`)

#### `GET /reports-access`
- Query param `status` (optional): `"granted"` or `"revoked"`.
- `200` → `ReportAccessGrant[]`, newest-first (by `updated_at`) — a patient sees their own grants (to any doctor); a doctor sees grants naming them (from any patient).

### Prescriptions

Doctor-only PDF uploads, deliberately kept separate from Report/AISummary/ReportComment. **There is no AI-summary endpoint for prescriptions at all** — that's the entire enforcement mechanism for "no AI summary is ever generated for a prescription." Prescriptions appear inline in the shared conversation thread (like reports) and in the same cross-conversation history view, gated by the same `ReportAccessGrant` used for reports.

#### `POST /conversations/{conversation_id}/prescriptions`
**Doctor-only** (`403` for a patient), unlike report uploads which either participant may do.
- Form fields: `file` (PDF, max 20 MB), `display_name` (optional, same fallback behavior as report uploads).
- `patient_id`/`doctor_id` are derived server-side (patient from the conversation, doctor is always the caller).
- `201` → `Prescription`

#### `GET /conversations/{conversation_id}/prescriptions`
- Either participant. `200` → `Prescription[]`, chronological (oldest first), for inline placement in the chat.

#### `GET /prescriptions?patient_id={id}`
Same authorization shape as `GET /reports?patient_id=`: patients may only query themselves; doctors need **both** an accepted `Connection` **and** a granted `ReportAccessGrant`.
- `200` → `Prescription[]`, newest-first.

#### `GET /prescriptions/{prescription_id}`
- `200` → `Prescription`; `403`/`404` as usual (participant-only).

#### `GET /prescriptions/{prescription_id}/file`
Streams the PDF. Same authenticated pattern as `GET /reports/{report_id}/file`.

### Appointments

A Med Calendar shared between a connected patient/doctor pair. Either participant may create, edit, or cancel — mirroring the "either participant" convention already used for report uploads. Reminders are computed statelessly on every read; there is no background scheduler and no dismissal/read-tracking table.

#### `POST /appointments`
- Body: `{ "patient_id": int, "doctor_id": int, "scheduled_at": datetime, "reason": string? }`
- Caller must be one of the two parties — `403` otherwise. `patient_id`/`doctor_id` must reference existing users of the matching role — `400` otherwise.
- Requires an accepted `Connection` between the two — `403` otherwise.
- `201` → `Appointment` (`created_by_id` is the caller)

#### `GET /appointments`
- Query param `scope` (optional, default `"upcoming"`): `"upcoming"`, `"past"`, or `"all"`.
- `200` → `Appointment[]`, the caller's own appointments (as patient or doctor), each including a computed `active_reminder`.

#### `PATCH /appointments/{appointment_id}`
- Either participant. Body: any subset of `{ "scheduled_at": datetime, "reason": string, "status": "cancelled" }` — `status` accepts only `"cancelled"`; there's no un-cancel.
- `200` → `Appointment`

#### `GET /appointments/{appointment_id}`
- Participant-only. `200` → `Appointment`; `403`/`404` as usual.

### Health

#### `GET /health`
- Auth: none
- `200` → `{ "status": "ok" }`

---

## Design notes & assumptions

The spec was explicit about the data model but left some behavior undefined. Here's every place this contract made a call, so you know what to expect:

1. **Conversation creation is get-or-create**, always `200`. Calling it twice with the same pair doesn't create duplicates.
2. **`GET /users?role=X` and `GET /reports?patient_id=`** are now both gated on an accepted `Connection` (see notes 9–12) rather than the old "any doctor sees any patient" simplification — that open-roster approach is what this whole update replaced.
3. **Report file access is authenticated**, not a public static URL, because these are medical documents. This is the one place the frontend needs slightly more than a plain link (see the fetch+blob pattern above).
4. **Report status only moves forward**, and only a doctor can set `"reviewed"`. The spec defines the sequence but not who drives it or whether steps can be skipped — this assumes skipping is fine, reversing isn't.
5. **AI summary generation can be triggered by either participant, but editing the result is doctor-only.** Generating calls a real model (Groq) against the report's extracted text; editing afterward corrects clinical content, which a patient shouldn't be able to alter.
6. **The uploader chooses a display name at upload time** (`display_name`, with a same-request fallback to the file's own name). This is separate from `pdf_path`/the on-disk filename, which stays a random, unguessable name for security — `display_name` is purely a label.
7. **PDF text extraction only reads a real text layer** (via `pypdf`) — a scanned report (a photograph or image saved as a PDF, with no underlying text) will fail AI summary generation with a `502` rather than silently producing a blank or wrong summary. There's no OCR step in this version.
8. **All timestamps are naive UTC** (no offset suffix). This was a deliberate simplification to avoid SQLite timezone-comparison edge cases — see `models.py` for details.
9. **Connections are requested by email, not by browsing.** There's deliberately no "list every doctor/patient in the system" endpoint anymore — you already need to know who you're trying to reach. This is different from `/auth/login`'s enumeration-resistant design (same error for wrong-email vs wrong-password): `POST /connections` gives a clear `404` for an unknown email, since it requires being authenticated already (not useful for anonymous account probing the way a login endpoint is) and a vague error would just make a real feature harder to use. Worth revisiting if this ever needs to resist a logged-in user probing for arbitrary registered emails.
10. **A patient can have more than one doctor** (and vice versa) — there's no uniqueness rule beyond one `Connection` row per specific pair. "Patients must have a specific doctor" was read as "not just anyone," not "exactly one."
11. **No disconnect / revoke endpoint.** Once a connection is `"accepted"`, nothing in this version can move it back — `PATCH /connections/{id}` only acts on a `"pending"` row. A rejected request *can* be retried (flips back to `"pending"`), which is different from unwinding an existing acceptance. If you need "remove this doctor," that's a new endpoint, flagged here rather than guessed at.
12. **No email/notification is sent** when a request comes in — the recipient finds out by calling `GET /connections` (poll it the same way you'd poll messages, or just check it on page load). Wiring up real notifications would mean a new external dependency this backend doesn't have.
13. **`ReportAccessGrant` is a one-sided flag, not a second handshake.** A `Connection` already requires mutual opt-in for the relationship to exist at all; requiring the doctor to also *request* reports access (rather than the patient unilaterally granting/revoking it) would be ceremony without a clear benefit, and there's no precedent elsewhere in this contract for a two-step grant of something already gated by an existing mutual relationship.
14. **Prescriptions are a wholly separate table/router from Report**, not a `kind` discriminator column. `Report` carries AI-summary wiring and a status stepper that must never apply to prescriptions; keeping them separate means neither concept needs a conditional for "unless this is a prescription," and there's simply no route that could generate an AI summary for one.
15. **Appointment reminders are computed on every read, not stored.** `active_reminder` is a pure function of `scheduled_at` vs. the current time (see `dependencies.compute_active_reminder`) — there's no scheduler, no push/email notification, and no dismissal-tracking table. This was a deliberate scope decision: this backend has no background job runner, and a badge that's simply "currently true or not" based on time math needed no new infrastructure.
16. **Doctor specialization is nullable, not required at signup.** `SignupRequest` is shared across both roles and is itself part of the locked contract — adding a required field there would break existing patient signups too. "Mandatory going forward" is a frontend-enforced nudge (a doctor with `specialization: null` sees a "complete your profile" prompt), not a server-side constraint.
17. **Avatars stay behind an authenticated download endpoint** (`GET /users/{id}/avatar`), not a public static file mount, even though photos are lower-stakes than medical PDFs. This keeps the app's file-serving story consistent (everything so far is "authenticated `FileResponse`") and reuses the exact same visibility rule as `GET /users/{user_id}` — a stranger can't fetch someone's photo any more than their name.
