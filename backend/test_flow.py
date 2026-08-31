"""
Manual end-to-end smoke test against a running server. Not part of the
deliverable -- just how I verified the API before handing it over.
Run with the server already up on :8000.
"""
import io
import sys

import requests

BASE = "http://127.0.0.1:8000"
failures = []


def check(label, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label} {extra}")
    if not condition:
        failures.append(label)


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------
r = requests.post(f"{BASE}/auth/signup", json={
    "name": "Dr. Alice Smith", "email": "Alice@Example.com", "password": "doctorpass123", "role": "doctor",
})
check("signup doctor -> 201", r.status_code == 201, r.text)
doctor = r.json()
doctor_token, doctor_id = doctor["access_token"], doctor["user"]["id"]
check("signup lowercases email", doctor["user"]["email"] == "alice@example.com", doctor["user"]["email"])

r = requests.post(f"{BASE}/auth/signup", json={
    "name": "Dr. Bob Lee", "email": "bob@example.com", "password": "doctorpass123", "role": "doctor",
})
doctor2_token, doctor2_id = r.json()["access_token"], r.json()["user"]["id"]

r = requests.post(f"{BASE}/auth/signup", json={
    "name": "Pat Jones", "email": "pat@example.com", "password": "patientpass123", "role": "patient",
})
check("signup patient -> 201", r.status_code == 201, r.text)
patient = r.json()
patient_token, patient_id = patient["access_token"], patient["user"]["id"]

r = requests.post(f"{BASE}/auth/signup", json={
    "name": "Pat2", "email": "pat2@example.com", "password": "patientpass123", "role": "patient",
})
patient2_token, patient2_id = r.json()["access_token"], r.json()["user"]["id"]

r = requests.post(f"{BASE}/auth/signup", json={
    "name": "dup", "email": "pat@example.com", "password": "whatever123", "role": "patient",
})
check("signup duplicate email -> 400", r.status_code == 400, r.text)

r = requests.post(f"{BASE}/auth/login", json={"email": "pat@example.com", "password": "wrongpass"})
check("login wrong password -> 401", r.status_code == 401, r.text)

r = requests.get(f"{BASE}/users/me")
check("no token -> 401", r.status_code == 401, r.text)
r = requests.get(f"{BASE}/users/me", headers=auth_headers("garbage.token.value"))
check("garbage token -> 401", r.status_code == 401, r.text)

# ---------------------------------------------------------------------
# Visibility BEFORE any connection exists -- everything should be empty/blocked
# ---------------------------------------------------------------------
r = requests.get(f"{BASE}/users?role=doctor", headers=auth_headers(patient_token))
check("pre-connection: patient sees no doctors", r.status_code == 200 and r.json() == [], r.text)

r = requests.get(f"{BASE}/users?role=patient", headers=auth_headers(doctor_token))
check("pre-connection: doctor sees no patients", r.status_code == 200 and r.json() == [], r.text)

r = requests.get(f"{BASE}/users/{doctor_id}", headers=auth_headers(patient_token))
check("pre-connection: patient can't view doctor profile -> 403", r.status_code == 403, r.text)

r = requests.post(f"{BASE}/conversations", json={"patient_id": patient_id, "doctor_id": doctor_id}, headers=auth_headers(patient_token))
check("pre-connection: create conversation -> 403", r.status_code == 403, r.text)

# ---------------------------------------------------------------------
# Connection handshake: patient -> doctor
# ---------------------------------------------------------------------
r = requests.post(f"{BASE}/connections", json={"email": "nobody@example.com"}, headers=auth_headers(patient_token))
check("connect to unknown email -> 404", r.status_code == 404, r.text)

r = requests.post(f"{BASE}/connections", json={"email": "pat@example.com"}, headers=auth_headers(patient_token))
check("connect to self -> 400", r.status_code == 400, r.text)

r = requests.post(f"{BASE}/connections", json={"email": "pat2@example.com"}, headers=auth_headers(patient_token))
check("connect to same-role (patient->patient) -> 400", r.status_code == 400, r.text)

r = requests.post(f"{BASE}/connections", json={"email": "Alice@Example.com"}, headers=auth_headers(patient_token))
check("patient requests connection to doctor -> 200", r.status_code == 200, r.text)
conn = r.json()
conn_id = conn["id"]
check("connection starts pending, requested_by patient", conn["status"] == "pending" and conn["requested_by_id"] == patient_id, conn)
check("connection has nested patient/doctor", conn["patient"]["id"] == patient_id and conn["doctor"]["id"] == doctor_id, conn)

r = requests.post(f"{BASE}/connections", json={"email": "alice@example.com"}, headers=auth_headers(patient_token))
check("re-request while pending -> same id, still pending (idempotent)", r.status_code == 200 and r.json()["id"] == conn_id and r.json()["status"] == "pending", r.text)

r = requests.get(f"{BASE}/connections", headers=auth_headers(patient_token))
check("patient sees outgoing pending request", any(c["id"] == conn_id for c in r.json()), r.text)

r = requests.get(f"{BASE}/connections?status=pending", headers=auth_headers(doctor_token))
check("doctor sees incoming pending request", any(c["id"] == conn_id for c in r.json()), r.text)

r = requests.patch(f"{BASE}/connections/{conn_id}", json={"status": "accepted"}, headers=auth_headers(patient_token))
check("requester tries to accept own request -> 403", r.status_code == 403, r.text)

r = requests.patch(f"{BASE}/connections/{conn_id}", json={"status": "accepted"}, headers=auth_headers(patient2_token))
check("non-participant tries to respond -> 403", r.status_code == 403, r.text)

r = requests.patch(f"{BASE}/connections/{conn_id}", json={"status": "accepted"}, headers=auth_headers(doctor_token))
check("doctor (recipient) accepts -> 200", r.status_code == 200 and r.json()["status"] == "accepted", r.text)

r = requests.patch(f"{BASE}/connections/{conn_id}", json={"status": "rejected"}, headers=auth_headers(doctor_token))
check("responding again to an already-decided connection -> 400", r.status_code == 400, r.text)

r = requests.post(f"{BASE}/connections", json={"email": "alice@example.com"}, headers=auth_headers(patient_token))
check("re-request while accepted -> same id, still accepted (idempotent)", r.status_code == 200 and r.json()["id"] == conn_id and r.json()["status"] == "accepted", r.text)

# ---------------------------------------------------------------------
# Rejection + re-request flow (separate pair: patient2 <-> doctor2)
# ---------------------------------------------------------------------
r = requests.post(f"{BASE}/connections", json={"email": "bob@example.com"}, headers=auth_headers(patient2_token))
conn2_id = r.json()["id"]
r = requests.patch(f"{BASE}/connections/{conn2_id}", json={"status": "rejected"}, headers=auth_headers(doctor2_token))
check("doctor rejects a request -> 200", r.status_code == 200 and r.json()["status"] == "rejected", r.text)

r = requests.post(f"{BASE}/conversations", json={"patient_id": patient2_id, "doctor_id": doctor2_id}, headers=auth_headers(patient2_token))
check("conversation still blocked after rejection -> 403", r.status_code == 403, r.text)

r = requests.post(f"{BASE}/connections", json={"email": "bob@example.com"}, headers=auth_headers(patient2_token))
check("re-request after rejection -> same row, flipped back to pending", r.status_code == 200 and r.json()["id"] == conn2_id and r.json()["status"] == "pending", r.text)

r = requests.patch(f"{BASE}/connections/{conn2_id}", json={"status": "accepted"}, headers=auth_headers(doctor2_token))
check("doctor accepts the re-request -> 200", r.status_code == 200 and r.json()["status"] == "accepted", r.text)

# ---------------------------------------------------------------------
# Visibility AFTER acceptance (patient <-> doctor, the first pair)
# ---------------------------------------------------------------------
r = requests.get(f"{BASE}/users?role=doctor", headers=auth_headers(patient_token))
check("post-connection: patient sees connected doctor", r.status_code == 200 and any(u["id"] == doctor_id for u in r.json()), r.text)
check("post-connection: patient does NOT see unconnected doctor2", all(u["id"] != doctor2_id for u in r.json()), r.text)

r = requests.get(f"{BASE}/users?role=patient", headers=auth_headers(doctor_token))
check("post-connection: doctor sees connected patient", r.status_code == 200 and any(u["id"] == patient_id for u in r.json()), r.text)

r = requests.get(f"{BASE}/users/{doctor_id}", headers=auth_headers(patient_token))
check("post-connection: patient can view doctor profile -> 200", r.status_code == 200, r.text)

r = requests.get(f"{BASE}/users/{doctor2_id}", headers=auth_headers(patient_token))
check("still can't view UNCONNECTED doctor2's profile -> 403", r.status_code == 403, r.text)

r = requests.get(f"{BASE}/users?role=doctor", headers=auth_headers(patient_token))
check("GET /users with no role -> 422 still required", requests.get(f"{BASE}/users", headers=auth_headers(patient_token)).status_code == 422)

# ---------------------------------------------------------------------
# Conversations (now unlocked)
# ---------------------------------------------------------------------
r = requests.post(f"{BASE}/conversations", json={"patient_id": patient_id, "doctor_id": doctor_id}, headers=auth_headers(patient_token))
check("create conversation now succeeds -> 200", r.status_code == 200, r.text)
conv = r.json()
conv_id = conv["id"]

r = requests.post(f"{BASE}/conversations", json={"patient_id": patient_id, "doctor_id": doctor_id}, headers=auth_headers(patient_token))
check("create same conversation again -> same id (get-or-create)", r.status_code == 200 and r.json()["id"] == conv_id, r.text)

r = requests.post(f"{BASE}/conversations", json={"patient_id": doctor_id, "doctor_id": doctor_id}, headers=auth_headers(doctor_token))
check("conversation with patient_id pointing at a doctor -> 400", r.status_code == 400, r.text)

r = requests.get(f"{BASE}/conversations", headers=auth_headers(patient_token))
check("list my conversations -> includes conv", r.status_code == 200 and any(c["id"] == conv_id for c in r.json()), r.text)

r = requests.get(f"{BASE}/conversations/{conv_id}", headers=auth_headers(patient2_token))
check("get conversation as non-participant -> 403", r.status_code == 403, r.text)

# ---------------------------------------------------------------------
# Messages (+ polling)
# ---------------------------------------------------------------------
r = requests.post(f"{BASE}/conversations/{conv_id}/messages", json={"text": "Hello doctor"}, headers=auth_headers(patient_token))
check("send message 1 -> 201", r.status_code == 201, r.text)
msg1_id = r.json()["id"]

r = requests.post(f"{BASE}/conversations/{conv_id}/messages", json={"text": "Hello, how are you feeling?"}, headers=auth_headers(doctor_token))
check("send message 2 -> 201", r.status_code == 201, r.text)
msg2_id = r.json()["id"]
msg2_ts = r.json()["timestamp"]

r = requests.get(f"{BASE}/conversations/{conv_id}/messages", headers=auth_headers(patient_token))
msgs = r.json()
check("list messages -> both, ascending", r.status_code == 200 and [m["id"] for m in msgs] == [msg1_id, msg2_id], msgs)

r = requests.get(f"{BASE}/conversations/{conv_id}/messages?after_id={msg1_id}", headers=auth_headers(patient_token))
check("poll after_id -> only msg2", r.status_code == 200 and [m["id"] for m in r.json()] == [msg2_id], r.text)

since_z = msg2_ts + "Z" if not msg2_ts.endswith("Z") else msg2_ts
r = requests.get(f"{BASE}/conversations/{conv_id}/messages", params={"since": since_z}, headers=auth_headers(patient_token))
check("poll since=<ts>Z (aware) excludes msg2 itself", r.status_code == 200 and msg2_id not in [m["id"] for m in r.json()], r.text)

r = requests.post(f"{BASE}/conversations/{conv_id}/messages", json={"text": "hi"}, headers=auth_headers(patient2_token))
check("send message as non-participant -> 403", r.status_code == 403, r.text)

# ---------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------
fake_pdf = io.BytesIO(b"%PDF-1.4\n%fake pdf for testing\n%%EOF")
r = requests.post(
    f"{BASE}/conversations/{conv_id}/reports",
    files={"file": ("bloodwork.pdf", fake_pdf, "application/pdf")},
    data={"display_name": "March Bloodwork Panel"},
    headers=auth_headers(patient_token),
)
check("upload report -> 201", r.status_code == 201, r.text)
report = r.json()
report_id = report["id"]
check("report status starts 'uploaded'", report["status"] == "uploaded", report)
check("report pdf_url shape", report["pdf_url"] == f"/reports/{report_id}/file", report)
check("report display_name is what we sent", report["display_name"] == "March Bloodwork Panel", report)

fallback_pdf = io.BytesIO(b"%PDF-1.4\n%fake pdf for testing\n%%EOF")
r = requests.post(
    f"{BASE}/conversations/{conv_id}/reports",
    files={"file": ("xray_results.pdf", fallback_pdf, "application/pdf")},
    headers=auth_headers(patient_token),
)
check("upload with no display_name falls back to filename", r.status_code == 201 and r.json()["display_name"] == "xray_results", r.text)

bad_file = io.BytesIO(b"not a pdf")
r = requests.post(
    f"{BASE}/conversations/{conv_id}/reports",
    files={"file": ("notes.txt", bad_file, "text/plain")},
    headers=auth_headers(patient_token),
)
check("upload non-pdf -> 400", r.status_code == 400, r.text)

r = requests.get(f"{BASE}/reports", params={"patient_id": patient_id}, headers=auth_headers(patient_token))
check("list reports by patient (self) -> 200", r.status_code == 200 and any(x["id"] == report_id for x in r.json()), r.text)

r = requests.get(f"{BASE}/reports", params={"patient_id": patient_id}, headers=auth_headers(patient2_token))
check("list reports by patient (other patient) -> 403", r.status_code == 403, r.text)

r = requests.get(f"{BASE}/reports/{report_id}/file", headers=auth_headers(patient_token))
check("download file as participant -> 200 pdf bytes", r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF"), r.status_code)

r = requests.get(f"{BASE}/reports/{report_id}/file", headers=auth_headers(patient2_token))
check("download file as non-participant -> 403", r.status_code == 403, r.text)

r = requests.patch(f"{BASE}/reports/{report_id}/status", json={"status": "processing"}, headers=auth_headers(patient_token))
check("status uploaded -> processing -> 200", r.status_code == 200 and r.json()["status"] == "processing", r.text)

r = requests.patch(f"{BASE}/reports/{report_id}/status", json={"status": "uploaded"}, headers=auth_headers(patient_token))
check("status backward move -> 400", r.status_code == 400, r.text)

# ---------------------------------------------------------------------
# AI Summaries -- these call the real Groq API. If GROQ_API_KEY isn't set
# (or the report has no extractable text), the endpoint correctly returns
# 502 rather than crashing -- that's treated as an informational skip
# below, not a failure, since it's expected in an unconfigured environment.
# If reportlab is installed, we upload a report with REAL extractable text
# first so the happy path gets fully exercised when a key IS configured.
# ---------------------------------------------------------------------
ai_report_id = report_id
try:
    import reportlab.pdfgen.canvas as _canvas

    buf = io.BytesIO()
    c = _canvas.Canvas(buf)
    c.drawString(100, 750, "Bloodwork Report - Patient: Test Patient")
    c.drawString(100, 730, "LDL Cholesterol: 145 mg/dL (Reference: <100 mg/dL) - HIGH")
    c.drawString(100, 710, "Fasting Glucose: 92 mg/dL (Reference: 70-99 mg/dL) - Normal")
    c.save()
    buf.seek(0)
    r = requests.post(
        f"{BASE}/conversations/{conv_id}/reports",
        files={"file": ("real_bloodwork.pdf", buf, "application/pdf")},
        data={"display_name": "Real Text Bloodwork (for AI test)"},
        headers=auth_headers(patient_token),
    )
    if r.status_code == 201:
        ai_report_id = r.json()["id"]
except ImportError:
    print("[SKIP] reportlab not installed -- AI summary test will use a text-less PDF (expect 502 even with a valid key)")

r = requests.post(f"{BASE}/reports/{ai_report_id}/ai-summary", headers=auth_headers(doctor_token))
if r.status_code == 502:
    print(f"[INFO] AI summary generation returned 502 (expected if GROQ_API_KEY isn't set yet): {r.json().get('detail')}")
elif r.status_code == 201:
    summary1 = r.json()
    check("create ai-summary -> 201 with structured fields", all(k in summary1 for k in ("summary", "key_findings", "flagged_values", "recommendation")), summary1)

    r = requests.get(f"{BASE}/reports/{ai_report_id}", headers=auth_headers(patient_token))
    check("report auto-advanced to awaiting_review", r.json()["status"] == "awaiting_review", r.json())

    r = requests.patch(f"{BASE}/reports/{ai_report_id}/ai-summary", json={"summary": "edited by doctor"}, headers=auth_headers(patient_token))
    check("patient editing ai-summary -> 403", r.status_code == 403, r.text)

    r = requests.patch(f"{BASE}/reports/{ai_report_id}/ai-summary", json={"summary": "edited by doctor"}, headers=auth_headers(doctor_token))
    check("doctor editing ai-summary -> 200, in place", r.status_code == 200 and r.json()["id"] == summary1["id"] and r.json()["summary"] == "edited by doctor", r.text)

    r = requests.post(f"{BASE}/reports/{ai_report_id}/ai-summary", headers=auth_headers(doctor_token))
    summary2_id = r.json().get("id")
    r = requests.get(f"{BASE}/reports/{ai_report_id}/ai-summary/history", headers=auth_headers(patient_token))
    hist = r.json()
    check("history has both summaries, newest first", len(hist) == 2 and hist[0]["id"] == summary2_id, hist)
else:
    check(f"create ai-summary -> unexpected status {r.status_code}", False, r.text)

# ---------------------------------------------------------------------
# Report status -> reviewed (doctor-only) + comments
# ---------------------------------------------------------------------
r = requests.patch(f"{BASE}/reports/{report_id}/status", json={"status": "reviewed"}, headers=auth_headers(patient_token))
check("patient marks reviewed -> 403", r.status_code == 403, r.text)

r = requests.patch(f"{BASE}/reports/{report_id}/status", json={"status": "reviewed"}, headers=auth_headers(doctor_token))
check("doctor marks reviewed -> 200", r.status_code == 200 and r.json()["status"] == "reviewed", r.text)

r = requests.post(f"{BASE}/reports/{report_id}/comments", json={"text": "What does this mean for my diet?"}, headers=auth_headers(patient_token))
c1_id = r.json()["id"]
r = requests.post(f"{BASE}/reports/{report_id}/comments", json={"text": "Cut back on red meat, otherwise fine."}, headers=auth_headers(doctor_token))
c2_id = r.json()["id"]

r = requests.get(f"{BASE}/reports/{report_id}/comments", headers=auth_headers(patient_token))
check("list comments -> both, ascending", [c["id"] for c in r.json()] == [c1_id, c2_id], r.json())

r = requests.post(f"{BASE}/reports/{report_id}/comments", json={"text": "hi"}, headers=auth_headers(patient2_token))
check("add comment as non-participant -> 403", r.status_code == 403, r.text)

print()
if failures:
    print(f"=== {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
else:
    print("=== ALL CHECKS PASSED ===")
