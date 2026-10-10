# Portal accounts, scheduling and emergency alerts

What was added to the CareLink portal (FastAPI `backend/`, vanilla-JS `frontend/`), and what you need to configure.

## New tables (all CareLink-owned; nothing existing is altered)
Created automatically by the backend on start (`create_all`, with row-level security enabled). The same DDL is in
`supabase/schema.sql` if you would rather run it in the Supabase SQL editor.

| Table | Purpose |
|---|---|
| `user_locations` | typed city / country, collected at sign-up (patients and doctors) |
| `unconfirmed_users` | a row means "email not confirmed yet". Accounts created before this existed have no row, so they count as confirmed |
| `auth_tokens` | one-time emailed tokens (confirm email, reset password). Only the SHA-256 hash is stored; confirm = 24 h, reset = 1 h |
| `emergency_contacts` | the loved one a patient wants alerted |
| `emergency_alerts` | audit trail and rate limit for alerts (never stores chat text) |
| `appointment_slots` | doctor availability; booking creates a normal `appointments` row |
| `intake_entries` | one row per line of the intake form's tables (allergies, existing conditions, family history); the bots' lists in `patient_intake_form` are refreshed from the names |
| `intake_profile` | top health concerns (ranked, up to 5) and when the main problem began |
| `consent_records` | that the person agreed to the privacy notice at sign-up: notice version and time (one row per account) |
| `demographic_stats` | anonymised sign-up statistics: age group, sex, role, country, sign-up month. No user id, name, email or city, so rows can't be traced to an account |

Reused as-is: `patient_intake_form` (the triage and diet bots already read it), `medicines`, `medicine_prescriptions`, `appointments`, `connections`.

## Behaviour
- **Sign-up** requires ticking the privacy agreement (enforced by the server too; the notice version is recorded), needs city + country, sends a confirmation email and does **not** log the user in. It also writes one anonymised row to `demographic_stats`. When you change the notice wording, bump `PRIVACY_NOTICE_VERSION` in both `backend/app/privacy.py` and `frontend/static/js/app.js`. Login is blocked (403) until the link is used.
  Forgot-password and resend-confirmation always answer with the same generic message, so they can't be used to find out which emails exist.
- **Health intake** (patients) is required right after login. It is laid out like a paper intake form, with a table each for allergies, existing conditions and family history, a ranked list of top health concerns, an emergency contact and a location. Medicines are not on this form; they live on the Medicines page.
- **Availability**: doctors publish slots; connected patients book one; a booked slot disappears for everyone else; cancelling re-opens it.
  Patients can't create free-form appointments or reschedule; doctors still can create them directly.
- **Medicines**: patients mark each medicine taking / not taking (stored in `medicines.active`, which the bots read). There is no doctor Stop button;
  a medicine becomes inactive after its end date (swept at start-up, on list calls and hourly).
- **Emergency**: a patient-only button, and the triage bot's "Notify someone" action, email the saved contact after a confirmation. The email contains the
  patient's name, the time, their city/country and (for the bot trigger) the emergency category, never chat text or medical details.

## Configuration (variable NAMES only; put the values in `.env`)
- `SMTP_HOST`, `SMTP_PORT` (default 587; 465 uses SSL), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`
- `PUBLIC_APP_URL` (base URL used in emailed links; default `http://localhost:3002`)

Without `SMTP_HOST` nothing is sent. The API says so honestly (`dry_run`), and outside production the confirm / reset link is written to the server log
so sign-in still works in development. Tokens are never returned by the API.

## Known limits
- Login tokens are stateless 24 h JWTs, so an existing session stays valid after a password reset.
- The intake "required" gate is enforced by the portal UI, not by blocking other API calls.
- `backend/test_flow.py` predates email confirmation and needs its sign-up steps updated before it can be used again.
