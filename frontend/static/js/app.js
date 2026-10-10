/* ============================================================
   Doctor-Patient Portal — Frontend App
   Plain JS (no framework). All state in memory.
   ============================================================ */

// Service base URLs come from window.SEHAT_CONFIG, which frontend_main.py
// renders into index.html from its own env vars -- so the same build works
// on localhost (separate ports, the defaults below) and behind the gateway
// (same-origin paths like /api). See architecture doc §01/§04 -- these are
// separate services, not CareLink's own backend.
const SEHAT_CONFIG = window.SEHAT_CONFIG || {};
const API = SEHAT_CONFIG.apiBase || 'http://localhost:8000';
const SEHATAI_API = SEHAT_CONFIG.sehataiBase || 'http://localhost:3000';
const EVIDENCE_API = SEHAT_CONFIG.evidenceBase || 'http://localhost:8002';

/* ── Auth helpers ──
   sessionStorage, not localStorage, deliberately: localStorage is shared
   across every tab on this origin, so two tabs (e.g. a doctor and a
   patient account open side by side) would silently steal each other's
   session on every login/reload -- apiFetch() re-reads the token fresh
   on every call, so the failure isn't just a stale UI, actions in one
   tab start silently authenticating as whichever account logged in last
   in ANY tab. sessionStorage is per-tab, so this can't happen. */
const auth = {
  token: () => sessionStorage.getItem('token'),
  user:  () => { try { return JSON.parse(sessionStorage.getItem('user')); } catch { return null; } },
  save(token, user) {
    sessionStorage.setItem('token', token);
    sessionStorage.setItem('user', JSON.stringify(user));
  },
  updateUser(user) { sessionStorage.setItem('user', JSON.stringify(user)); },
  clear() { sessionStorage.removeItem('token'); sessionStorage.removeItem('user'); }
};

const DOCTOR_SPECIALIZATIONS = [
  'Cardiologist', 'Dermatologist', 'Endocrinologist', 'Gastroenterologist',
  'General Practitioner', 'Nephrologist', 'Neurologist', 'Obstetrician/Gynecologist',
  'Oncologist', 'Ophthalmologist', 'Orthopedist', 'Pediatrician', 'Psychiatrist',
  'Pulmonologist', 'Rheumatologist', 'Urologist', 'Other'
];

/* ── Fetch wrapper ── */
async function apiFetch(path, opts = {}) {
  const token = auth.token();
  const headers = { ...(opts.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';

  const res = await fetch(`${API}${path}`, { ...opts, headers });

  if (res.status === 401) {
    auth.clear();
    showPage('auth');
    throw new Error('Session expired. Please log in again.');
  }
  return res;
}

function errMsg(data) {
  if (!data || !data.detail) return 'An unexpected error occurred.';
  if (typeof data.detail === 'string') return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map(e => e.msg).join('; ');
  return String(data.detail);
}

/* ── File downloads ── */
const MIME_EXT = {
  'application/pdf': 'pdf', 'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png',
  'image/gif': 'gif', 'image/tiff': 'tif', 'image/bmp': 'bmp', 'image/webp': 'webp',
};

/* The server's own filename (Content-Disposition) wins; otherwise the
   extension comes from the file's real type -- a JPG saved as ".pdf" was
   the "unreadable PDF" download bug. */
function downloadNameFor(res, blob, baseName) {
  const cd = res.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const safeBase = (baseName || 'document').replace(/[\\/:*?"<>|]+/g, '_').trim() || 'document';
  const ext = MIME_EXT[(blob.type || '').split(';')[0].trim()] ||
    (m ? (decodeURIComponent(m[1]).split('.').pop() || '').toLowerCase() : '') || 'pdf';
  return `${safeBase.replace(/\.(pdf|jpe?g|png|gif|tiff?|bmp|webp)$/i, '')}.${ext}`;
}

/* fetch -> check -> blob. Throws with the server's message on failure, so
   an error JSON body is never saved to disk as if it were the file. */
async function fetchFileBlob(path) {
  const res = await apiFetch(path);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(errMsg(data) || `Download failed (${res.status})`);
  }
  return { res, blob: await res.blob() };
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

async function downloadFile(path, baseName) {
  try {
    const { res, blob } = await fetchFileBlob(path);
    saveBlob(blob, downloadNameFor(res, blob, baseName));
  } catch (err) { toast(err.message || 'Download failed.', 'error'); }
}

/* ── Toast ── */
function toast(msg, type = '') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = `toast${type ? ' toast-' + type : ''}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

/* ── Time formatting ── */
function relTime(utcStr) {
  // Treat as UTC (no offset suffix per contract)
  const d = new Date(utcStr + (utcStr.endsWith('Z') ? '' : 'Z'));
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 5)   return 'just now';
  if (diff < 60)  return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
function fmtTime(utcStr) {
  const d = new Date(utcStr + (utcStr.endsWith('Z') ? '' : 'Z'));
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
function fmtDateTime(utcStr) {
  const d = new Date(utcStr + (utcStr.endsWith('Z') ? '' : 'Z'));
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/* ── Avatars ── */
function initials(name) {
  if (!name) return '?';
  return name.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();
}

/* Renders a user's photo into an avatar element (fetched via the
   authenticated download endpoint), falling back to initials. Reused for
   both the sidebar avatar and the profile page preview. */
async function loadAvatarInto(el, user) {
  if (!el) return;
  el.textContent = initials(user.name);
  el.style.backgroundImage = '';
  if (!user.avatar_url) return;
  try {
    const res = await apiFetch(user.avatar_url);
    if (!res.ok) return;
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    el.textContent = '';
    el.style.backgroundImage = `url(${objUrl})`;
    el.style.backgroundSize = 'cover';
    el.style.backgroundPosition = 'center';
  } catch { /* leave initials fallback */ }
}

/* Shows/hides the "complete your profile" sidebar nudge for doctors with
   no specialization set yet. */
function applyProfileNudge() {
  const user = auth.user();
  const nudge = document.getElementById('sidebar-nudge');
  if (!nudge) return;
  nudge.style.display = (user?.role === 'doctor' && !user.specialization) ? 'block' : 'none';
}

/* Doctor-only UI affordances that aren't per-render-function conditional
   (e.g. the prescription upload trigger, shared across conversations). */
function applyRoleVisibility() {
  const isDoctor = auth.user()?.role === 'doctor';
  const prescBtn = document.getElementById('presc-upload-toggle-btn');
  if (prescBtn) prescBtn.style.display = isDoctor ? '' : 'none';
  // See architecture doc §06 -- symptom/diet chat is patient-only, the
  // clinical-evidence search is doctor-only. Server-side write guards are
  // what actually matter for anything that mutates data (see §06's note);
  // these two are read-only chat tools, so hiding the tab is sufficient.
  const assistantLink = document.getElementById('assistant-nav-link');
  if (assistantLink) assistantLink.style.display = isDoctor ? 'none' : '';
  const evidenceLink = document.getElementById('evidence-nav-link');
  if (evidenceLink) evidenceLink.style.display = isDoctor ? '' : 'none';
  // Lab-report upload (structured extraction) is patient-only -- see
  // routers/lab_reports.py's require_patient_role.
  const uploadWrap = document.getElementById('upload-lab-report-wrap');
  if (uploadWrap) uploadWrap.style.display = isDoctor ? 'none' : '';
}

/* ── Page routing ── */
let currentPage = null;

function showPage(id) {
  // Leaving the chat (by any route, not just its Back button) ends its
  // polling and drops its state, so nothing keeps running in the
  // background. A report opened FROM the chat is part of it: polling just
  // pauses, and the report's Back button resumes it (startConvPolling
  // always clears the old timer first -- each report round-trip used to
  // add another 3s poll on top of the existing one).
  if (convState && id !== 'conv') {
    if (id === 'report' && currentPage === 'conv') stopConvPolling();
    else closeConversation();
  }
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));

  const page = document.getElementById(`${id}-page`);
  if (page) page.classList.add('active');

  const link = document.querySelector(`.nav-link[data-page="${id}"]`);
  if (link) link.classList.add('active');

  const shell = document.getElementById('app-shell');
  if (id === 'auth') {
    shell.style.display = 'none';
    // 'block', not 'flex' -- #auth-page no longer centers its own content
    // via flex (that moved to .auth-split's grid when the login page was
    // rebuilt into a split hero/form layout); a stale 'flex' here made
    // .auth-split shrink-to-fit as a flex item instead of filling the
    // viewport, since it had no flex-grow of its own.
    document.getElementById('auth-page').style.display = 'block';
  } else {
    // 'block', not 'flex' -- same bug as above, one level up: #app is a
    // single child with its OWN `display:flex` (sidebar + main), so it
    // doesn't need #app-shell to be a flex container at all. Setting
    // this to 'flex' made #app a flex item with no flex-grow, so it
    // shrank to its content's width instead of filling the viewport --
    // confirmed live, #app measured ~710px wide on a 1536px screen.
    shell.style.display = 'block';
    document.getElementById('auth-page').style.display = 'none';
  }
  currentPage = id;
}

/* ============================================================
   AUTH
   ============================================================ */
function initAuth() {
  const tabs = document.querySelectorAll('.auth-tab');
  const loginForm = document.getElementById('login-form');
  const signupForm = document.getElementById('signup-form');

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const mode = tab.dataset.tab;
      loginForm.style.display  = mode === 'login'  ? 'block' : 'none';
      signupForm.style.display = mode === 'signup' ? 'block' : 'none';
    });
  });

  /* Login */
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = loginForm.querySelector('button[type="submit"]');
    const errEl = loginForm.querySelector('.error-banner');
    errEl.style.display = 'none';
    btn.classList.add('btn-loading');

    try {
      const res = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email:    loginForm.querySelector('[name="email"]').value.trim(),
          password: loginForm.querySelector('[name="password"]').value
        })
      });
      const data = await res.json();
      if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
      auth.save(data.access_token, data.user);
      onLogin();
    } catch (err) {
      errEl.textContent = 'Could not reach the server. Is it running?';
      errEl.style.display = 'block';
    } finally { btn.classList.remove('btn-loading'); }
  });

  /* Signup: show/require date of birth + sex only for the patient role --
     unlike the Profile page's identical-looking doctor/patient split (a
     one-time render keyed off an already-known server-side role), this
     form's role is live-selected by the user, so it needs a real listener. */
  const patientFieldsWrap = document.getElementById('signup-patient-fields');
  const dobInput = document.getElementById('signup-dob-input');
  const sexSelect = document.getElementById('signup-sex-select');
  function toggleSignupPatientFields() {
    const show = signupForm.querySelector('[name="role"]:checked')?.value === 'patient';
    patientFieldsWrap.style.display = show ? '' : 'none';
    dobInput.required = show;
    sexSelect.required = show;
  }
  signupForm.querySelector('.role-picker').addEventListener('change', toggleSignupPatientFields);
  // Sync immediately, not just on change -- a browser restoring a
  // previously-checked radio (bfcache) on refresh sets .checked directly
  // without firing 'change', which would otherwise leave the visible/
  // required state out of sync with the actually-selected role.
  toggleSignupPatientFields();

  /* Signup */
  signupForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = signupForm.querySelector('button[type="submit"]');
    const errEl = signupForm.querySelector('.error-banner');
    errEl.style.display = 'none';
    const role = signupForm.querySelector('[name="role"]:checked')?.value;
    if (!role) { errEl.textContent = 'Please choose a role.'; errEl.style.display = 'block'; return; }

    btn.classList.add('btn-loading');
    try {
      const body = {
        name:     signupForm.querySelector('[name="name"]').value.trim(),
        email:    signupForm.querySelector('[name="email"]').value.trim(),
        password: signupForm.querySelector('[name="password"]').value,
        role
      };
      if (role === 'patient') {
        // Guard against an empty string reaching Pydantic's date/Literal
        // types as a raw parse error instead of the backend's clean 400 --
        // same pattern as saveProfile()'s patient branch.
        const dob = dobInput.value;
        const sex = sexSelect.value;
        if (dob) body.date_of_birth = dob;
        if (sex) body.sex = sex;
      }
      const res = await fetch(`${API}/auth/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
      auth.save(data.access_token, data.user);
      onLogin();
    } catch (err) {
      errEl.textContent = 'Could not reach the server. Is it running?';
      errEl.style.display = 'block';
    } finally { btn.classList.remove('btn-loading'); }
  });
}

function onLogin() {
  const user = auth.user();
  document.getElementById('sidebar-name').textContent = user.name;
  document.getElementById('sidebar-role').textContent = user.role === 'doctor' ? 'Doctor' : 'Patient';
  loadAvatarInto(document.getElementById('sidebar-avatar'), user);
  applyProfileNudge();
  applyRoleVisibility();
  updatePendingBadge();
  if (user.role === 'doctor') loadDoctorNotifications();
  showPage('connections');
  loadConnections();
}

/* ============================================================
   CONNECTIONS
   ============================================================ */
let connectionsCache = [];
let reportsAccessCache = []; // patient-only: which doctors they've granted reports access to
let doctorNotificationsCache = []; // doctor-only: unreviewed lab documents across all granted patients

async function loadConnections() {
  try {
    const res = await apiFetch('/connections');
    const data = await res.json();
    if (!res.ok) { toast(errMsg(data), 'error'); return; }
    connectionsCache = data;
    if (auth.user()?.role === 'patient') await loadReportsAccessCache();
    renderConnections(data);
    updatePendingBadge();
  } catch (err) { toast('Failed to load connections.', 'error'); }
}

async function loadReportsAccessCache() {
  try {
    const res = await apiFetch('/reports-access');
    reportsAccessCache = res.ok ? await res.json() : [];
  } catch { reportsAccessCache = []; }
}

function updatePendingBadge() {
  const me = auth.user();
  if (!me) return;
  const pending = connectionsCache.filter(c => c.status === 'pending' && c.requested_by_id !== me.id);
  const badge = document.getElementById('conn-badge');
  if (pending.length) { badge.textContent = pending.length; badge.style.display = 'flex'; }
  else badge.style.display = 'none';
}

/* Doctor-side notification badge on the Reports nav item -- see
   structured_reports.py's GET /structured/notifications. */
async function loadDoctorNotifications() {
  try {
    const res = await apiFetch('/structured/notifications');
    doctorNotificationsCache = res.ok ? await res.json() : [];
  } catch { doctorNotificationsCache = []; }
  updateReportsBadge();
}

function updateReportsBadge() {
  const badge = document.getElementById('reports-badge');
  if (!badge) return;
  const count = doctorNotificationsCache.length;
  if (count) { badge.textContent = count; badge.style.display = 'flex'; }
  else badge.style.display = 'none';
}

function renderConnections(connections) {
  const me = auth.user();
  const accepted  = connections.filter(c => c.status === 'accepted');
  const incoming  = connections.filter(c => c.status === 'pending' && c.requested_by_id !== me.id);
  const outgoing  = connections.filter(c => c.status === 'pending' && c.requested_by_id === me.id);

  const isDoctor = me.role === 'doctor';

  /* Connected */
  const connectedEl = document.getElementById('connected-list');
  document.getElementById('connected-count').textContent = accepted.length;
  connectedEl.innerHTML = '';
  if (accepted.length === 0) {
    connectedEl.innerHTML = emptyState(
      isDoctor ? 'people' : 'person',
      isDoctor ? 'No patients connected yet.' : 'No doctors connected yet.',
      isDoctor ? 'Accept a patient request below to get started.' : 'Send a connection request to your doctor using their email address.'
    );
  } else {
    accepted.forEach(c => {
      const other = isDoctor ? c.patient : c.doctor;
      connectedEl.appendChild(connectionCard(other, 'accepted', c));
    });
  }

  /* Incoming */
  const incomingEl = document.getElementById('incoming-list');
  document.getElementById('incoming-count').textContent = incoming.length;
  incomingEl.innerHTML = '';
  if (incoming.length === 0) {
    incomingEl.innerHTML = emptyState('inbox', 'No pending requests.', '');
  } else {
    incoming.forEach(c => {
      const other = isDoctor ? c.patient : c.doctor;
      incomingEl.appendChild(connectionCard(other, 'incoming', c));
    });
  }

  /* Outgoing */
  const outgoingEl = document.getElementById('outgoing-list');
  document.getElementById('outgoing-count').textContent = outgoing.length;
  outgoingEl.innerHTML = '';
  if (outgoing.length === 0) {
    outgoingEl.innerHTML = emptyState('send', 'No outgoing requests.', '');
  } else {
    outgoing.forEach(c => {
      const other = isDoctor ? c.patient : c.doctor;
      outgoingEl.appendChild(connectionCard(other, 'outgoing', c));
    });
  }

  /* Update send form label */
  const roleLabel = isDoctor ? 'patient' : 'doctor';
  document.getElementById('connect-placeholder').placeholder = `Enter your ${roleLabel}'s email`;
  document.getElementById('connect-submit-btn').textContent = isDoctor ? 'Add patient' : 'Connect with doctor';
}

/* Thin-stroke line icons only, matching the sidebar nav / compliance-badge
   treatment elsewhere in the app -- these used to be emoji (📬📤💬⚠️🩺👈🔒📄🗓️),
   which read as an inconsistent leftover against the rest of the Industry
   design system's monochrome, corner-bracket blueprint look. Several reuse
   the exact path data already used for a sidebar nav icon or the auth
   page's compliance badge, kept in sync by hand. */
const EMPTY_STATE_ICONS = {
  inbox: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>',
  message: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  people: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  person: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  pointLeft: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></svg>',
  lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
};

function emptyState(iconKey, msg, hint) {
  const svg = EMPTY_STATE_ICONS[iconKey] || EMPTY_STATE_ICONS.alert;
  return `<div class="blueprint empty-state">
    <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
    <div class="empty-icon">${svg}</div>
    <p>${msg}</p>
    ${hint ? `<p class="empty-hint">${hint}</p>` : ''}
  </div>`;
}

function connectionCard(user, type, conn) {
  const me = auth.user();
  const isDoctorViewing = me.role === 'doctor';
  const grantedDoctorIds = new Set(reportsAccessCache.filter(g => g.status === 'granted').map(g => g.doctor_id));
  const isGranted = grantedDoctorIds.has(user.id);

  const div = document.createElement('div');
  div.className = `blueprint connection-card ${type}`;
  div.innerHTML = `
    <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
    <div class="avatar">${initials(user.name)}</div>
    <div class="connection-info">
      <div class="connection-name">${escHtml(user.name)}</div>
      <div class="connection-meta">
        <span class="role-badge ${user.role}">${user.role}</span>
        ${type === 'outgoing' ? ' · Waiting for response' : ''}
        ${type === 'accepted' ? ` · Connected ${relTime(conn.responded_at || conn.created_at)}` : ''}
        ${type === 'incoming' ? ` · Requested ${relTime(conn.created_at)}` : ''}
      </div>
    </div>
    <div class="connection-actions">
      ${type === 'incoming' ? `
        <button class="btn btn-success btn-sm accept-btn" data-id="${conn.id}">Accept</button>
        <button class="btn btn-danger btn-sm reject-btn" data-id="${conn.id}">Decline</button>
      ` : ''}
      ${type === 'accepted' && isDoctorViewing ? `
        <button class="btn btn-ghost btn-sm nickname-btn" data-conn-id="${conn.id}" data-current="${escHtml(conn.doctor_nickname || '')}">
          ✎ ${conn.doctor_nickname ? escHtml(conn.doctor_nickname) : 'Add nickname'}
        </button>` : ''}
      ${type === 'accepted' ? `<button class="btn btn-secondary btn-sm open-conv-btn" data-user-id="${user.id}" data-user-role="${user.role}">Open chat</button>` : ''}
      ${type === 'accepted' ? `<button class="btn btn-ghost btn-sm disconnect-btn" data-id="${conn.id}" data-name="${escHtml(user.name)}">Disconnect</button>` : ''}
      ${type === 'outgoing' ? `<button class="btn btn-ghost btn-sm cancel-request-btn" data-id="${conn.id}" data-name="${escHtml(user.name)}">Cancel</button>` : ''}
    </div>`;

  div.querySelectorAll('.accept-btn').forEach(b => b.addEventListener('click', () => respondConnection(conn.id, 'accepted', b)));
  div.querySelectorAll('.reject-btn').forEach(b => b.addEventListener('click', () => respondConnection(conn.id, 'rejected', b)));
  div.querySelectorAll('.open-conv-btn').forEach(b => b.addEventListener('click', async () => {
    const me = auth.user();
    const patientId = me.role === 'patient' ? me.id : user.id;
    const doctorId  = me.role === 'doctor'  ? me.id : user.id;
    await openConversation(patientId, doctorId, user);
  }));
  div.querySelectorAll('.grant-toggle-input').forEach(cb => cb.addEventListener('change', () => toggleReportsAccess(cb)));
  div.querySelectorAll('.nickname-btn').forEach(b => b.addEventListener('click', () => editNickname(b)));
  div.querySelectorAll('.disconnect-btn').forEach(b => b.addEventListener('click', () => deleteConnection(
    b.dataset.id, b,
    'Disconnect?',
    `You'll no longer share reports or be able to message ${b.dataset.name}. Your existing conversation history is kept. You can reconnect later by sending a new request.`,
    'Disconnect', 'Disconnected.'
  )));
  div.querySelectorAll('.cancel-request-btn').forEach(b => b.addEventListener('click', () => deleteConnection(
    b.dataset.id, b,
    'Cancel request?',
    `Your pending request to ${b.dataset.name} will be withdrawn.`,
    'Cancel request', 'Request cancelled.'
  )));
  return div;
}

function confirmDialog(title, message, confirmLabel = 'Confirm') {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal-card" style="max-width:380px">
        <div class="modal-header"><h2>${escHtml(title)}</h2></div>
        <div style="padding:16px 20px;font-size:.9375rem;color:var(--text-mid);line-height:1.5">${escHtml(message)}</div>
        <div class="modal-footer" style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn-ghost btn-sm" id="confirm-cancel-btn">Never mind</button>
          <button class="btn btn-danger btn-sm" id="confirm-ok-btn">${escHtml(confirmLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const cleanup = (result) => { overlay.remove(); resolve(result); };
    overlay.querySelector('#confirm-cancel-btn').addEventListener('click', () => cleanup(false));
    overlay.querySelector('#confirm-ok-btn').addEventListener('click', () => cleanup(true));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(false); });
  });
}

async function deleteConnection(id, btn, title, message, confirmLabel, successMsg) {
  const ok = await confirmDialog(title, message, confirmLabel);
  if (!ok) return;
  btn.disabled = true;
  try {
    const res = await apiFetch(`/connections/${id}`, { method: 'DELETE' });
    if (!res.ok && res.status !== 204) {
      const data = await res.json().catch(() => ({}));
      toast(errMsg(data), 'error');
      return;
    }
    toast(successMsg, 'success');
    await loadConnections();
  } catch { toast('Failed to update connection.', 'error'); }
  finally { btn.disabled = false; }
}

async function toggleReportsAccess(cb) {
  cb.disabled = true;
  const doctorId = Number(cb.dataset.doctorId);
  try {
    if (cb.checked) {
      const res = await apiFetch('/reports-access/grant', { method: 'POST', body: JSON.stringify({ doctor_id: doctorId }) });
      const data = await res.json();
      if (!res.ok) { toast(errMsg(data), 'error'); cb.checked = false; return; }
      toast('Reports history shared with this doctor.', 'success');
    } else {
      const grant = reportsAccessCache.find(g => g.doctor_id === doctorId && g.status === 'granted');
      if (grant) {
        const res = await apiFetch(`/reports-access/${grant.id}/revoke`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) { toast(errMsg(data), 'error'); cb.checked = true; return; }
      }
      toast('Reports history access revoked.', 'success');
    }
    await loadReportsAccessCache();
  } catch { toast('Failed to update sharing setting.', 'error'); cb.checked = !cb.checked; }
  finally { cb.disabled = false; }
}

async function editNickname(btn) {
  const current = btn.dataset.current || '';
  const value = window.prompt('Set a private nickname for this patient (only visible to you):', current);
  if (value === null) return;
  try {
    const res = await apiFetch(`/connections/${btn.dataset.connId}/nickname`, {
      method: 'PATCH', body: JSON.stringify({ doctor_nickname: value.trim() || null })
    });
    const data = await res.json();
    if (!res.ok) { toast(errMsg(data), 'error'); return; }
    toast('Nickname updated.', 'success');
    await loadConnections();
  } catch { toast('Failed to update nickname.', 'error'); }
}

async function respondConnection(id, status, btn) {
  btn.disabled = true;
  try {
    const res = await apiFetch(`/connections/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status })
    });
    const data = await res.json();
    if (!res.ok) { toast(errMsg(data), 'error'); return; }
    toast(status === 'accepted' ? 'Connection accepted.' : 'Request declined.', 'success');
    await loadConnections();
  } catch { toast('Failed to update connection.', 'error'); }
  finally { btn.disabled = false; }
}

function initConnectionsPage() {
  const form = document.getElementById('send-request-form');
  const errEl = document.getElementById('send-request-error');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errEl.style.display = 'none';
    const emailInput = document.getElementById('connect-placeholder');
    const email = emailInput.value.trim();
    const btn = document.getElementById('connect-submit-btn');
    btn.disabled = true;
    try {
      const res = await apiFetch('/connections', {
        method: 'POST',
        body: JSON.stringify({ email })
      });
      const data = await res.json();
      if (!res.ok) {
        const msg = res.status === 404 ? `No account found with email "${email}".` :
                    res.status === 400 ? errMsg(data) :
                    errMsg(data);
        errEl.textContent = msg;
        errEl.style.display = 'block';
        return;
      }
      emailInput.value = '';
      toast('Connection request sent.', 'success');
      await loadConnections();
    } catch { errEl.textContent = 'Could not send request. Check server.'; errEl.style.display = 'block'; }
    finally { btn.disabled = false; }
  });
}

/* ============================================================
   DASHBOARD
   ============================================================ */
async function loadDashboard() {
  const list = document.getElementById('conversations-list');
  list.innerHTML = '<div class="skeleton skeleton-line w80"></div><div class="skeleton skeleton-line w60"></div>';
  try {
    const res = await apiFetch('/conversations');
    const data = await res.json();
    if (!res.ok) { toast(errMsg(data), 'error'); return; }
    renderDashboard(data);
  } catch { toast('Failed to load conversations.', 'error'); }
}

function renderDashboard(convs) {
  const me = auth.user();
  const list = document.getElementById('conversations-list');
  list.innerHTML = '';

  if (convs.length === 0) {
    // Show accepted connections as "start a conversation"
    const accepted = connectionsCache.filter(c => c.status === 'accepted');
    if (accepted.length === 0) {
      list.innerHTML = emptyState('message', 'No conversations yet.',
        'Connect with your ' + (me.role === 'doctor' ? 'patients' : 'doctor') + ' first in the Connections tab.');
    } else {
      list.innerHTML = `<p class="t-sm" style="color:var(--text-mid);margin-bottom:12px">Start a conversation with one of your connections:</p>`;
      accepted.forEach(c => {
        const other = me.role === 'doctor' ? c.patient : c.doctor;
        const card = document.createElement('div');
        card.className = 'conv-card';
        card.innerHTML = `
          <div class="avatar avatar-lg">${initials(other.name)}</div>
          <div class="conv-card-info">
            <div class="conv-card-name">${escHtml(other.name)}</div>
            <div class="conv-card-preview" style="color:var(--navy)">Start conversation →</div>
          </div>
          <span class="role-badge ${other.role}">${other.role}</span>`;
        card.addEventListener('click', async () => {
          const patientId = me.role === 'patient' ? me.id : other.id;
          const doctorId  = me.role === 'doctor'  ? me.id : other.id;
          await openConversation(patientId, doctorId, other);
        });
        list.appendChild(card);
      });
    }
    return;
  }

  convs.forEach(conv => {
    const other = me.id === conv.patient_id ? conv.doctor : conv.patient;
    const card = document.createElement('div');
    card.className = 'conv-card';
    card.innerHTML = `
      <div class="avatar avatar-lg">${initials(other.name)}</div>
      <div class="conv-card-info">
        <div class="conv-card-name">${escHtml(other.name)}</div>
        <div class="conv-card-preview">Tap to open</div>
      </div>
      <span class="role-badge ${other.role}">${other.role}</span>`;
    card.addEventListener('click', () => openConvById(conv, other));
    list.appendChild(card);
  });
}

/* ============================================================
   CONVERSATION VIEW
   ============================================================ */
/* ------------------------------------------------------------
   One chat implementation serves BOTH portals (doctor and patient
   open the same #conv-page through openConvById), so every fix here
   applies to both.

   DUPLICATE-MESSAGE FIX (root cause): sendMsg appended the server's copy
   of a sent message to the DOM, and pollMessages appended every message
   newer than msgLastId -- with no check that a message was already on
   screen. A poll in flight when Send was pressed returned the same
   message, so it showed twice (one request, two entries). Sending also
   jumped msgLastId to the new id, which could skip the other side's
   messages that arrived in between.

   Now: one keyed collection (convState.items) is the only source of
   truth and the thread is always rendered from it with replaceChildren.
   A sent message is a pending entry with a local-only clientId; the
   server copy (POST response or poll) replaces it instead of adding a
   second entry. Polls from a previous conversation are discarded.
   ------------------------------------------------------------ */
let convState = null; // { conv, other, items: Map, msgLastId, pollTimer, polling }

// Keys in convState.items. Server messages are keyed by id so a message
// can never be added twice; pending sends are keyed by their clientId.
const msgKey = (id) => `m:${id}`;
const pendingKey = (clientId) => `c:${clientId}`;
const reportKey = (id) => `r:${id}`;
const prescriptionKey = (id) => `p:${id}`;

function newClientId() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  // Fallback for browsers without randomUUID (local-only id, never sent).
  return 'cid-' + Array.from(crypto.getRandomValues(new Uint32Array(4)), (n) => n.toString(16)).join('');
}

/* advanceCursor: only messages that came from a fetch of the message list
   move msgLastId. A POST response must not — the other side may have
   sent something with a lower id that the next poll still has to fetch. */
function addServerMessage(msg, advanceCursor = true) {
  if (!convState || convState.items.has(msgKey(msg.id))) return false;
  convState.items.set(msgKey(msg.id), { kind: 'msg', state: 'sent', msg });
  if (advanceCursor && msg.id > convState.msgLastId) convState.msgLastId = msg.id;
  return true;
}

/* Puts the server copy of a message on screen exactly once. Match order
   (from the spec): already known by server id -> nothing to add; this
   request's own pending entry (clientId known for a POST response); else
   the OLDEST unmatched pending entry from the same sender with exactly
   the same text. A pending entry consumes at most one echo, so the same
   text sent twice on purpose still shows twice. */
function reconcileServerMessage(msg, ownClientId = null) {
  if (!convState) return;
  if (convState.items.has(msgKey(msg.id))) {
    // Already shown (e.g. the poll beat the POST response): drop the
    // pending entry this response belonged to, if it's still there.
    if (ownClientId) convState.items.delete(pendingKey(ownClientId));
    return;
  }
  let matchKey = ownClientId && convState.items.has(pendingKey(ownClientId)) ? pendingKey(ownClientId) : null;
  if (!matchKey && msg.sender_id === auth.user()?.id) {
    for (const [key, item] of convState.items) {
      if (item.kind === 'pending' && !item.matched && item.text === msg.text) { matchKey = key; break; }
    }
  }
  if (matchKey) {
    const pending = convState.items.get(matchKey);
    pending.matched = true;
    convState.items.delete(matchKey);
    // Remember which server message replaced it, so this entry's own POST
    // response (arriving later) doesn't add the message a second time.
    convState.replacedBy.set(pending.clientId, msg.id);
  }
  addServerMessage(msg, !ownClientId);
}

function sortedThreadItems() {
  const ts = (item) => {
    const t = item.kind === 'msg' ? item.msg.timestamp : item.kind === 'pending' ? null : item.data.timestamp;
    return t ? new Date(t).getTime() : Infinity; // pending sends sort last
  };
  // Map iteration order is insertion order; it's the stable tie-break.
  return [...convState.items.values()]
    .map((item, i) => ({ item, i }))
    .sort((a, b) => (ts(a.item) - ts(b.item)) || (a.i - b.i))
    .map(({ item }) => item);
}

function isNearBottom(el) {
  return el.scrollHeight - el.scrollTop <= el.clientHeight + 80;
}

/* The only function that writes the message list. `scroll`: 'always' |
   'if-near-bottom' | 'never'. Newest message stays in view after a send
   or receive unless the reader has scrolled up. */
function renderConvThread(scroll = 'if-near-bottom') {
  if (!convState) return;
  const thread = document.getElementById('conv-thread');
  const wasNearBottom = isNearBottom(thread);
  const nodes = sortedThreadItems().map((item) => {
    if (item.kind === 'msg') return renderMessage(item.msg, convState.conv);
    if (item.kind === 'pending') return renderPendingMessage(item);
    if (item.kind === 'report') return renderReportInline(item.data);
    return renderPrescriptionInline(item.data);
  });
  thread.replaceChildren(...nodes);
  if (scroll === 'always' || (scroll === 'if-near-bottom' && wasNearBottom)) scrollToBottom(thread);
}

/* Leaving the chat: stop polling and ignore anything still in flight.
   Called from showPage whenever the conversation page is left, not only
   from its Back button (sidebar navigation used to leave the 3s poll
   running in the background). */
function closeConversation() {
  stopConvPolling();
  convState = null;
}

async function openConversation(patientId, doctorId, other) {
  const btn = event?.target;
  if (btn) btn.disabled = true;
  try {
    const res = await apiFetch('/conversations', {
      method: 'POST',
      body: JSON.stringify({ patient_id: patientId, doctor_id: doctorId })
    });
    const data = await res.json();
    if (!res.ok) { toast(errMsg(data), 'error'); return; }
    openConvById(data, other);
  } catch { toast('Could not open conversation.', 'error'); }
  finally { if (btn) btn.disabled = false; }
}

async function openConvById(conv, other) {
  stopConvPolling();
  convState = { conv, other, items: new Map(), msgLastId: 0, pollTimer: null, polling: false, replacedBy: new Map() };

  document.getElementById('conv-other-name').textContent = other.name;
  document.getElementById('conv-other-role').textContent = other.role;
  // The header avatar was never filled in -- it showed the template's "?"
  // placeholder for every conversation.
  loadAvatarInto(document.getElementById('conv-avatar'), other);

  const thread = document.getElementById('conv-thread');
  thread.innerHTML = '<div class="skeleton skeleton-line w60" style="margin:20px auto"></div>';

  showPage('conv');

  await loadConvThread();
  startConvPolling();
  scrollToBottom(thread);
}

async function loadConvThread() {
  if (!convState) return;
  const state = convState;
  const { conv } = state;

  // Load messages, reports, and prescriptions in parallel
  const [msgRes, repRes, prescRes] = await Promise.all([
    apiFetch(`/conversations/${conv.id}/messages`),
    apiFetch(`/conversations/${conv.id}/reports`),
    apiFetch(`/conversations/${conv.id}/prescriptions`)
  ]);
  const messages = msgRes.ok ? await msgRes.json() : [];
  const reports  = repRes.ok ? await repRes.json() : [];
  const prescriptions = prescRes.ok ? await prescRes.json() : [];
  if (convState !== state) return; // the user opened another chat meanwhile

  messages.forEach((m) => reconcileServerMessage(m));
  reports.forEach((r) => state.items.set(reportKey(r.id), { kind: 'report', data: r }));
  prescriptions.forEach((p) => state.items.set(prescriptionKey(p.id), { kind: 'prescription', data: p }));
  renderConvThread('always');
}

/* Message text is inserted with textContent only (never innerHTML);
   .msg-text keeps line breaks. */
function buildMessageRow({ isMine, senderName, text, metaText }) {
  const row = document.createElement('div');
  row.className = `msg-row${isMine ? ' mine' : ''}`;
  if (!isMine) {
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.title = senderName;
    avatar.textContent = initials(senderName);
    row.appendChild(avatar);
  }
  const col = document.createElement('div');
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble msg-text';
  bubble.textContent = text;
  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.textContent = metaText;
  col.append(bubble, meta);
  row.appendChild(col);
  return { row, col, bubble, meta };
}

function renderMessage(msg, conv) {
  const me = auth.user();
  const isMine = msg.sender_id === me.id;
  const senderName = isMine ? 'You' : (me.id === conv.patient_id ? conv.doctor.name : conv.patient.name);
  const { row } = buildMessageRow({
    isMine,
    senderName,
    text: msg.text,
    metaText: `${isMine ? '' : senderName + ' · '}${fmtTime(msg.timestamp)}`,
  });
  row.dataset.msgId = msg.id;
  return row;
}

/* A message the user sent that the server hasn't confirmed yet: shown as
   "Sending…", or "Not sent" with a Retry button. */
function renderPendingMessage(item) {
  const failed = item.state === 'failed';
  const { row, col, meta } = buildMessageRow({
    isMine: true,
    senderName: 'You',
    text: item.text,
    metaText: failed ? 'Not sent' : 'Sending…',
  });
  row.classList.add(failed ? 'msg-failed' : 'msg-pending');
  if (failed) {
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn-secondary btn-sm msg-retry-btn';
    retry.textContent = 'Retry';
    retry.addEventListener('click', () => retrySend(item.clientId), { once: true });
    col.appendChild(retry);
  }
  return row;
}

function renderReportInline(report) {
  const statusColor = {
    uploaded: 'var(--text-light)',
    processing: 'var(--amber)',
    awaiting_review: 'var(--navy)',
    reviewed: 'var(--green)'
  }[report.status] || 'var(--text-light)';

  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding: 4px 0;';
  wrap.innerHTML = `
    <div class="report-inline" data-report-id="${report.id}">
      <div class="report-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
        </svg>
      </div>
      <div class="report-inline-info">
        <div class="report-inline-name">${escHtml(report.display_name || 'Report')}</div>
        <div class="report-inline-meta" style="color:${statusColor}">${fmtStatus(report.status)}</div>
      </div>
      <div class="report-inline-status">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>`;
  wrap.querySelector('.report-inline').addEventListener('click', () => openReportDetail(report.id));
  return wrap;
}

function fmtStatus(s) {
  return { uploaded: 'Uploaded', processing: 'Processing…', awaiting_review: 'Awaiting review', reviewed: 'Reviewed' }[s] || s;
}

function renderPrescriptionInline(presc) {
  const wrap = document.createElement('div');
  wrap.style.cssText = 'padding: 4px 0;';
  wrap.innerHTML = `
    <div class="report-inline prescription-inline" data-prescription-id="${presc.id}">
      <div class="report-icon prescription-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z"/>
          <path d="M9 9h6M9 13h6M9 17h3"/>
        </svg>
      </div>
      <div class="report-inline-info">
        <div class="report-inline-name">${escHtml(presc.display_name || 'Prescription')}</div>
        <div class="report-inline-meta" style="color:var(--green)">Prescription</div>
      </div>
      <div class="report-inline-status">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>`;
  wrap.querySelector('.prescription-inline').addEventListener('click', () => openPrescriptionDetail(presc.id));
  return wrap;
}

async function openPrescriptionDetail(prescriptionId) {
  try {
    const res = await apiFetch(`/prescriptions/${prescriptionId}`);
    const presc = await res.json();
    if (!res.ok) { toast(errMsg(presc), 'error'); return; }
    showPrescriptionModal(presc);
  } catch { toast('Failed to load prescription.', 'error'); }
}

/* Prescriptions get a lightweight modal (PDF preview + download only) --
   deliberately no comments/AI-summary UI, since neither ever applies. */
function showPrescriptionModal(presc) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal-card">
      <div class="modal-header">
        <h2>${escHtml(presc.display_name || 'Prescription')}</h2>
        <button class="btn btn-ghost btn-sm" id="modal-close-btn">✕</button>
      </div>
      <div class="pdf-preview" id="presc-pdf-preview"><div class="pdf-loading">Loading preview…</div></div>
      <div class="modal-footer">
        <button class="btn btn-secondary btn-sm" id="presc-download-btn">Download PDF</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#modal-close-btn').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  (async () => {
    try {
      const { res, blob } = await fetchFileBlob(presc.pdf_url);
      const objUrl = URL.createObjectURL(blob);
      overlay.querySelector('#presc-pdf-preview').innerHTML = `<iframe src="${objUrl}" title="PDF preview"></iframe>`;
      overlay.querySelector('#presc-download-btn').addEventListener('click', () => {
        saveBlob(blob, downloadNameFor(res, blob, presc.display_name || 'prescription'));
      });
    } catch (err) {
      overlay.querySelector('#presc-pdf-preview').innerHTML = `<div class="pdf-loading" style="color:var(--red)">${escHtml(err.message || 'Could not load PDF preview.')}</div>`;
      overlay.querySelector('#presc-download-btn').disabled = true;
    }
  })();
}

/* Polling: one timer per open chat, cleared by stopConvPolling (Back
   button, opening another chat, or leaving the page via showPage). */
function startConvPolling() {
  if (!convState) return;
  stopConvPolling();
  convState.pollTimer = setInterval(pollMessages, 3000);
}
function stopConvPolling() {
  if (convState?.pollTimer) clearInterval(convState.pollTimer);
  if (convState) convState.pollTimer = null;
}

async function pollMessages() {
  const state = convState;
  if (!state || state.polling) return; // never two polls at once
  state.polling = true;
  try {
    const res = await apiFetch(`/conversations/${state.conv.id}/messages?after_id=${state.msgLastId}`);
    const newMsgs = await res.json();
    // A poll for a chat that has since been closed or switched is ignored.
    if (convState !== state || !res.ok || !newMsgs.length) return;
    newMsgs.forEach((msg) => reconcileServerMessage(msg));
    renderConvThread('if-near-bottom');
  } catch {
  } finally {
    state.polling = false;
  }
}

function scrollToBottom(el) {
  setTimeout(() => { el.scrollTop = el.scrollHeight; }, 30);
}

/* Sends one pending entry. The request body is unchanged ({ text }); the
   clientId stays in the browser. */
async function deliverPending(clientId) {
  const state = convState;
  const item = state?.items.get(pendingKey(clientId));
  if (!item || item.inFlight) return;
  item.inFlight = true;
  item.state = 'pending';
  renderConvThread('never');
  try {
    const res = await apiFetch(`/conversations/${state.conv.id}/messages`, {
      method: 'POST', body: JSON.stringify({ text: item.text })
    });
    const data = await res.json();
    if (convState !== state) return;
    if (!res.ok) {
      item.state = 'failed';
      toast(errMsg(data), 'error');
    } else if (state.replacedBy.get(clientId) !== data.id) {
      reconcileServerMessage(data, clientId);
    }
  } catch {
    if (convState === state) item.state = 'failed';
  } finally {
    item.inFlight = false;
    if (convState === state) renderConvThread('if-near-bottom');
  }
}

function retrySend(clientId) {
  deliverPending(clientId);
  document.getElementById('msg-textarea')?.focus();
}

/* Composer: ONE send path -- the form's submit handler. Enter (without
   Shift) requests that same submit; the Send button is type="submit".
   Bound once at boot; the AbortController lets a teardown remove every
   composer listener in one call. */
let composerController = null;

function initComposer() {
  if (composerController) return; // idempotent: never bind twice
  composerController = new AbortController();
  const { signal } = composerController;
  const form = document.getElementById('msg-form');
  const textarea = document.getElementById('msg-textarea');

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textarea.value.trim();
    if (!text || !convState) return; // empty messages are ignored
    // Clear synchronously so a second Enter/click can't send it again.
    textarea.value = '';
    textarea.style.height = '';
    const clientId = newClientId();
    convState.items.set(pendingKey(clientId), { kind: 'pending', clientId, text, state: 'pending', inFlight: false, matched: false });
    renderConvThread('if-near-bottom');
    deliverPending(clientId);
    textarea.focus();
  }, { signal });

  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  }, { signal });
  textarea.addEventListener('input', () => {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + 'px';
  }, { signal });
}

function teardownComposer() {
  composerController?.abort();
  composerController = null;
}

/* Upload */
function initUpload() {
  const toggleBtn = document.getElementById('upload-toggle-btn');
  const uploadForm = document.getElementById('upload-form');
  const cancelBtn  = document.getElementById('upload-cancel-btn');
  const submitBtn  = document.getElementById('upload-submit-btn');
  const fileInput  = document.getElementById('report-file-input');
  const errEl      = document.getElementById('upload-error');

  toggleBtn.addEventListener('click', () => { uploadForm.classList.toggle('open'); errEl.style.display = 'none'; });
  cancelBtn.addEventListener('click', () => { uploadForm.classList.remove('open'); errEl.style.display = 'none'; });

  submitBtn.addEventListener('click', async () => {
    errEl.style.display = 'none';
    const file = fileInput.files[0];
    if (!file) { errEl.textContent = 'Please choose a PDF file.'; errEl.style.display = 'block'; return; }
    if (!file.type.includes('pdf') && !file.name.endsWith('.pdf')) {
      errEl.textContent = 'Only PDF files are accepted.'; errEl.style.display = 'block'; return;
    }
    if (file.size > 20 * 1024 * 1024) {
      errEl.textContent = 'File is too large (max 20 MB).'; errEl.style.display = 'block'; return;
    }

    const displayName = document.getElementById('report-display-name').value.trim();
    const fd = new FormData();
    fd.append('file', file);
    if (displayName) fd.append('display_name', displayName);

    submitBtn.classList.add('btn-loading');
    submitBtn.disabled = true;
    try {
      const res = await apiFetch(`/conversations/${convState.conv.id}/reports`, {
        method: 'POST', body: fd
      });
      const data = await res.json();
      if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
      uploadForm.classList.remove('open');
      fileInput.value = '';
      document.getElementById('report-display-name').value = '';
      if (convState) {
        convState.items.set(reportKey(data.id), { kind: 'report', data });
        renderConvThread('always');
      }
      toast('Report uploaded.', 'success');
    } catch { errEl.textContent = 'Upload failed. Please try again.'; errEl.style.display = 'block'; }
    finally { submitBtn.classList.remove('btn-loading'); submitBtn.disabled = false; }
  });
}

/* Doctor-only prescription upload -- near-duplicate of initUpload(), pointed
   at the prescriptions endpoint. The trigger button itself is hidden for
   patients by applyRoleVisibility(). */
function initPrescriptionUpload() {
  const toggleBtn = document.getElementById('presc-upload-toggle-btn');
  const uploadForm = document.getElementById('presc-upload-form');
  const cancelBtn  = document.getElementById('presc-upload-cancel-btn');
  const submitBtn  = document.getElementById('presc-upload-submit-btn');
  const fileInput  = document.getElementById('prescription-file-input');
  const errEl      = document.getElementById('presc-upload-error');
  if (!toggleBtn) return;

  toggleBtn.addEventListener('click', () => { uploadForm.classList.toggle('open'); errEl.style.display = 'none'; });
  cancelBtn.addEventListener('click', () => { uploadForm.classList.remove('open'); errEl.style.display = 'none'; });

  submitBtn.addEventListener('click', async () => {
    errEl.style.display = 'none';
    const file = fileInput.files[0];
    if (!file) { errEl.textContent = 'Please choose a PDF file.'; errEl.style.display = 'block'; return; }
    if (!file.type.includes('pdf') && !file.name.endsWith('.pdf')) {
      errEl.textContent = 'Only PDF files are accepted.'; errEl.style.display = 'block'; return;
    }
    if (file.size > 20 * 1024 * 1024) {
      errEl.textContent = 'File is too large (max 20 MB).'; errEl.style.display = 'block'; return;
    }

    const displayName = document.getElementById('prescription-display-name').value.trim();
    const fd = new FormData();
    fd.append('file', file);
    if (displayName) fd.append('display_name', displayName);

    submitBtn.classList.add('btn-loading');
    submitBtn.disabled = true;
    try {
      const res = await apiFetch(`/conversations/${convState.conv.id}/prescriptions`, {
        method: 'POST', body: fd
      });
      const data = await res.json();
      if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
      uploadForm.classList.remove('open');
      fileInput.value = '';
      document.getElementById('prescription-display-name').value = '';
      if (convState) {
        convState.items.set(prescriptionKey(data.id), { kind: 'prescription', data });
        renderConvThread('always');
      }
      toast('Prescription uploaded.', 'success');
    } catch { errEl.textContent = 'Upload failed. Please try again.'; errEl.style.display = 'block'; }
    finally { submitBtn.classList.remove('btn-loading'); submitBtn.disabled = false; }
  });
}

/* ============================================================
   REPORT DETAIL
   ============================================================ */
let reportState = null; // { report, commentLastId, pollTimer }
let structuredDocState = null; // { doc, patientId, selectedMarker } -- see openStructuredDocument

async function openReportDetail(reportId) {
  stopConvPolling();
  stopCommentPolling();

  showPage('report');
  const layout = document.getElementById('report-layout');
  layout.innerHTML = '<div class="skeleton skeleton-line w80" style="margin:20px 0"></div>';

  try {
    const res = await apiFetch(`/reports/${reportId}`);
    const report = await res.json();
    if (!res.ok) { toast(errMsg(report), 'error'); return; }
    reportState = { report, commentLastId: 0, pollTimer: null };
    renderReportDetail(report);
    loadPdfPreview(report);
    loadComments(reportId);
    startCommentPolling(reportId);
    // Resume conv polling when returning
  } catch { toast('Failed to load report.', 'error'); }
}

function renderReportDetail(report) {
  const me = auth.user();
  const isDoctor = me.role === 'doctor';
  const layout = document.getElementById('report-layout');

  layout.innerHTML = `
    <!-- Header -->
    <div class="report-header">
      <div class="report-header-info">
        <h1>${escHtml(report.display_name || 'Report')}</h1>
        <div class="report-header-meta">Uploaded ${relTime(report.timestamp)}</div>
      </div>
      <div class="report-header-actions">
        ${isDoctor && report.status !== 'reviewed' ? `<button class="btn btn-primary btn-sm" id="mark-reviewed-btn">Mark reviewed</button>` : ''}
        <button class="btn btn-secondary btn-sm" id="back-to-conv-btn">← Back to chat</button>
      </div>
    </div>

    <!-- Status stepper -->
    ${renderStepper(report.status)}

    <!-- PDF section -->
    <div class="pdf-section">
      <div class="pdf-section-header">
        <h2>Report file</h2>
        <button class="btn btn-ghost btn-sm" id="download-btn">Download PDF</button>
      </div>
      <div class="pdf-preview" id="pdf-preview-area">
        <div class="pdf-loading">Loading preview…</div>
      </div>
    </div>

    <!-- AI Summary section -->
    <div class="ai-summary-section" style="position:relative" id="ai-summary-section">
      <div class="ai-loading-overlay" id="ai-loading-overlay">
        <div class="ai-spinner"></div>
        <div class="ai-loading-text">Generating AI summary…</div>
      </div>
      <div class="ai-summary-header">
        <h2><span class="ai-badge">AI</span> Summary</h2>
        <div class="ai-summary-actions" id="ai-summary-actions"></div>
      </div>
      <div class="ai-summary-body" id="ai-summary-body">
        ${renderSummaryBody(report.ai_summary, isDoctor)}
      </div>
    </div>

    <!-- Comments -->
    <div class="comments-section" id="comments-section">
      <div class="comments-header"><h2>Notes on this report</h2></div>
      <div class="comments-thread" id="comments-thread"><div class="t-xs" style="padding:8px 0">Loading…</div></div>
      ${isDoctor ? `
      <div class="comments-composer">
        <textarea id="comment-textarea" placeholder="Add a note…" rows="1"></textarea>
        <button class="btn btn-primary btn-sm" id="comment-send-btn">Send</button>
      </div>` : `<div class="t-xs" style="padding:10px 20px 16px;color:var(--text-light)">Only your doctor can add notes here.</div>`}
    </div>`;

  /* Wire up back button */
  layout.querySelector('#back-to-conv-btn').addEventListener('click', () => {
    stopCommentPolling();
    showPage('conv');
    startConvPolling();
  });

  /* Mark reviewed */
  const markBtn = layout.querySelector('#mark-reviewed-btn');
  if (markBtn) {
    markBtn.addEventListener('click', async () => {
      markBtn.disabled = true;
      try {
        const res = await apiFetch(`/reports/${report.id}/status`, {
          method: 'PATCH', body: JSON.stringify({ status: 'reviewed' })
        });
        const data = await res.json();
        if (!res.ok) { toast(errMsg(data), 'error'); return; }
        reportState.report = data;
        toast('Report marked as reviewed.', 'success');
        renderReportDetail(data);
        loadPdfPreview(data);
        loadComments(data.id);
        startCommentPolling(data.id);
      } catch { toast('Failed to update status.', 'error'); }
      finally { markBtn.disabled = false; }
    });
  }

  /* Download button */
  layout.querySelector('#download-btn').addEventListener('click', () => downloadPdf(report, false));

  /* AI summary actions */
  renderSummaryActions(report, isDoctor);

  /* Comment composer */
  initCommentComposer(report.id);
}

/* ============================================================
   STRUCTURED DOCUMENT DETAIL (Phase 2 of the reports rebuild)
   Real per-marker lab data from DataFetch's extraction pipeline
   (backend/app/routers/structured_reports.py), NOT CareLink's own
   `reports` table -- a document with no linked conversation report has
   no stepper, no AI summary, no comments (nothing to show for those).
   Renders into the same #report-page/#report-layout openReportDetail
   already uses -- no new page container needed.
   ============================================================ */
async function openStructuredDocument(documentId, patientId = null, verificationPollsLeft = 3) {
  stopConvPolling();
  stopCommentPolling();

  showPage('report');
  const layout = document.getElementById('report-layout');
  layout.innerHTML = '<div class="skeleton skeleton-line w80" style="margin:20px 0"></div>';

  const me = auth.user();
  patientId = me.role === 'patient' ? me.id : (patientId || structuredDocState?.patientId);
  if (!patientId) { layout.innerHTML = emptyState('alert', 'No patient selected.', ''); return; }

  try {
    const res = await apiFetch(`/structured/documents/${documentId}?patient_id=${patientId}`);
    const doc = await res.json();
    if (!res.ok) { toast(errMsg(doc), 'error'); return; }
    structuredDocState = { doc, patientId, selectedMarker: doc.default_trend_marker || null };
    renderStructuredDocument(doc);
    if (doc.default_trend_marker) loadMarkerTrend(documentId, doc.default_trend_marker);

    // The independent verification pass (Phase 2) runs as a background
    // task right after upload -- it may still be "running" by the time
    // this page loads. A few retries a few seconds apart are enough to
    // pick up the result without open-ended polling. Deliberately NOT
    // triggered by "not_run" -- that's also the permanent state of any
    // document that predates Phase 2 (or was inserted outside
    // lab_reports.py's upload route) and will never get a verification
    // row, so polling on it would retry forever for no reason.
    if (doc.audit?.verification_status === 'running' && verificationPollsLeft > 0) {
      setTimeout(() => {
        if (structuredDocState?.doc?.document_id === documentId) {
          openStructuredDocument(documentId, patientId, verificationPollsLeft - 1);
        }
      }, 6000);
    }
  } catch { toast('Failed to load document.', 'error'); }
}

function renderStructuredDocument(doc) {
  const layout = document.getElementById('report-layout');
  const dateLabel = doc.document_date ? fmtDateTime(doc.document_date) : 'Undated';
  const isDoctor = auth.user()?.role === 'doctor';

  layout.innerHTML = `
    <div>
      <button class="btn btn-ghost btn-sm" id="back-to-reports-btn" style="margin-bottom:8px">← All reports</button>
      <div class="report-header">
        <div class="report-header-info">
          <h1>${escHtml(doc.category || 'Lab document')}</h1>
          <div class="report-header-meta">${dateLabel}${doc.original_filename ? ' · ' + escHtml(doc.original_filename) : ''}</div>
        </div>
      </div>
      <div id="retraction-banner" class="retraction-banner" style="display:${doc.retracted ? 'block' : 'none'}">
        ⚠ This report has been retracted by a doctor — see Doctor's Notes below for what's correct.
      </div>
    </div>
    <div class="structured-detail-grid">
      <div class="structured-detail-main">
        <div class="blueprint ai-doc-summary">
          <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
          <div class="doc-kicker">AI summary</div>
          ${doc.ai_summary
            ? `<div class="ai-doc-summary-text">${escHtml(doc.ai_summary)}</div>
               <div class="ai-doc-summary-caveat">AI-generated from the uploaded report — not a diagnosis. Discuss results with your doctor.</div>`
            : `<div class="t-xs" style="margin-top:6px">Summary not available for this report.</div>`}
        </div>
        <div class="blueprint marker-table">
          <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
          <div class="marker-head">
            <div>Marker</div><div>Value</div><div>Reference range</div><div style="text-align:right">Δ vs previous</div>
          </div>
          <div id="marker-rows">${renderMarkerTable(doc.markers)}</div>
        </div>
        <div class="blueprint" style="padding:18px">
          <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
          <h3 style="margin:0 0 14px" id="trend-title">${doc.default_trend_marker ? escHtml(doc.markers.find(m => m.normalized_name === doc.default_trend_marker)?.test_name || '') + ' · trend' : 'Trend'}</h3>
          <div id="trend-trace-container"><div class="trend-empty">Select a marker to see its trend.</div></div>
        </div>
      </div>
      <div class="structured-detail-side">
        <div class="blueprint" style="padding:18px">
          <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
          <div class="doc-kicker">Source document</div>
          <div class="source-preview-box" id="doc-source-preview">
            <div class="t-xs">${doc.has_source_file ? 'Loading preview…' : 'Original file not stored'}</div>
          </div>
          <div class="source-actions">
            <button class="btn btn-secondary btn-sm" id="doc-download-btn" ${doc.has_source_file ? '' : 'disabled'}>Download</button>
            ${isDoctor ? `<button class="btn btn-primary btn-sm" id="doc-review-btn" ${doc.doctor_reviewed ? 'disabled' : ''}>${doc.doctor_reviewed ? 'Reviewed ✓' : 'Mark reviewed'}</button>` : ''}
          </div>
        </div>
        ${renderExtractionAudit(doc.audit)}
        <div class="blueprint" style="padding:18px">
          <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
          <div class="doc-kicker">Doctor's notes</div>
          <div id="doc-notes-list" class="t-xs">Loading…</div>
          ${isDoctor ? `
            <textarea id="doc-note-input" rows="3" placeholder="Add a note for this report (optional)" style="width:100%;margin-top:10px;resize:vertical"></textarea>
            <label class="t-xs" style="display:flex;align-items:center;gap:6px;margin-top:8px;cursor:pointer">
              <input type="checkbox" id="doc-note-retract-checkbox">
              Mark this report as retracted (requires a note explaining what's correct)
            </label>
            <button class="btn btn-secondary btn-sm" id="doc-note-save-btn" style="margin-top:8px">Save note</button>
          ` : ''}
        </div>
      </div>
    </div>
  `;

  layout.querySelector('#back-to-reports-btn').addEventListener('click', () => {
    showPage('reports');
    loadReportsPage();
  });

  layout.querySelectorAll('.marker-row').forEach((row) => {
    row.addEventListener('click', () => {
      const marker = row.dataset.normalizedName;
      const label = row.dataset.testName;
      layout.querySelectorAll('.marker-row').forEach((r) => r.classList.remove('is-selected'));
      row.classList.add('is-selected');
      layout.querySelector('#trend-title').textContent = `${label} · trend`;
      loadMarkerTrend(structuredDocState.doc.document_id, marker);
    });
  });

  const downloadBtn = layout.querySelector('#doc-download-btn');
  if (downloadBtn && doc.has_source_file) {
    // fetch+blob, not window.open/<a href> -- this is an authenticated
    // route (real medical documents, no public URL), and a plain
    // navigation can't carry the Authorization header. The file is
    // fetched once and reused for both the preview and the download, and
    // the extension follows the real file type (JPG/PNG uploads used to
    // be saved as ".pdf", which then wouldn't open).
    const filePath = `/structured/documents/${doc.document_id}/file?patient_id=${structuredDocState.patientId}`;
    const baseName = (doc.original_filename || doc.category || 'lab_report').replace(/\s+/g, '_');
    const filePromise = fetchFileBlob(filePath);
    filePromise.then(({ blob }) => {
      const box = layout.querySelector('#doc-source-preview');
      if (!box || structuredDocState?.doc?.document_id !== doc.document_id) return;
      const url = URL.createObjectURL(blob);
      if ((blob.type || '').startsWith('image/')) box.innerHTML = `<img src="${url}" alt="Uploaded lab report">`;
      else if ((blob.type || '').includes('pdf')) box.innerHTML = `<iframe src="${url}" title="Lab report preview"></iframe>`;
      else box.innerHTML = '<div class="t-xs">Preview not available for this file type.</div>';
    }).catch((err) => {
      const box = layout.querySelector('#doc-source-preview');
      if (box) box.innerHTML = `<div class="t-xs" style="color:var(--red)">${escHtml(err.message || 'Could not load the file.')}</div>`;
    });
    downloadBtn.addEventListener('click', async () => {
      downloadBtn.disabled = true;
      try {
        const { res, blob } = await filePromise.catch(() => fetchFileBlob(filePath));
        saveBlob(blob, downloadNameFor(res, blob, baseName));
      } catch (err) { toast(err.message || 'Download failed.', 'error'); }
      finally { downloadBtn.disabled = false; }
    });
  }

  loadDocumentNotes(doc.document_id, structuredDocState.patientId);
  const noteSaveBtn = layout.querySelector('#doc-note-save-btn');
  if (noteSaveBtn) {
    noteSaveBtn.addEventListener('click', async () => {
      const input = layout.querySelector('#doc-note-input');
      const retractCheckbox = layout.querySelector('#doc-note-retract-checkbox');
      const retracted = !!retractCheckbox?.checked;
      if (retracted && !input.value.trim()) { toast("A retraction needs a note explaining what's correct.", 'error'); return; }
      noteSaveBtn.disabled = true;
      try {
        const res = await apiFetch(`/structured/documents/${doc.document_id}/notes?patient_id=${structuredDocState.patientId}`, {
          method: 'PUT',
          body: JSON.stringify({ content: input.value, retracted }),
        });
        const data = await res.json().catch(() => null);
        if (!res.ok) { toast(errMsg(data), 'error'); return; }
        toast(data ? 'Note saved.' : 'Note cleared.', 'success');
        loadDocumentNotes(doc.document_id, structuredDocState.patientId);
      } catch { toast('Failed to save note.', 'error'); }
      finally { noteSaveBtn.disabled = false; }
    });
  }

  const reviewBtn = layout.querySelector('#doc-review-btn');
  if (reviewBtn && !doc.doctor_reviewed) {
    reviewBtn.addEventListener('click', async () => {
      reviewBtn.disabled = true;
      try {
        const res = await apiFetch(`/structured/documents/${doc.document_id}/review?patient_id=${structuredDocState.patientId}`, { method: 'POST' });
        const updated = await res.json().catch(() => ({}));
        if (!res.ok) { toast(errMsg(updated), 'error'); reviewBtn.disabled = false; return; }
        reviewBtn.textContent = 'Reviewed ✓';
        doc.doctor_reviewed = true;
        if (structuredDocState) structuredDocState.doc.doctor_reviewed = true;
        doctorNotificationsCache = doctorNotificationsCache.filter(n => n.document_id !== doc.document_id);
        updateReportsBadge();
        toast('Marked reviewed.', 'success');
      } catch { toast('Failed to mark reviewed.', 'error'); reviewBtn.disabled = false; }
    });
  }
}

function renderMarkerTable(markers) {
  if (!markers.length) return '<div class="t-xs" style="padding:16px 18px">No markers extracted from this document.</div>';
  return markers.map((m) => {
    const geometry = markerBarGeometry(m);
    const rangeCell = geometry
      ? `<div class="marker-range-bar">
           <div class="marker-range-band" style="left:${geometry.bandLeftPct}%;right:${geometry.bandRightPct}%"></div>
           <div class="marker-range-tick" style="left:${geometry.tickLeftPct}%"></div>
         </div>
         <div class="marker-range-text">${escHtml(m.normal_range || '')}</div>`
      : `<div class="marker-range-text">${escHtml(m.normal_range || '—')}</div>`;
    const deltaText = m.delta_value == null ? '—' : `${m.delta_value > 0 ? '+' : ''}${m.delta_value.toFixed(1)}`;
    const reviewFlag = m.needs_review
      ? ' <span class="marker-review-flag" title="The independent verifier read a different value for this marker — worth double-checking against the source document.">⚠</span>'
      : '';
    return `
      <div class="marker-row${m.is_abnormal ? ' is-abnormal' : ''}" data-normalized-name="${escHtml(m.normalized_name)}" data-test-name="${escHtml(m.test_name)}">
        <div><div class="marker-name">${escHtml(m.test_name)}${reviewFlag}</div><div class="marker-unit">${escHtml(m.unit || '')}</div></div>
        <div class="marker-value">${escHtml(m.value)}</div>
        <div class="marker-range-cell">${rangeCell}</div>
        <div class="marker-delta">${deltaText}</div>
      </div>`;
  }).join('');
}

function markerBarGeometry(marker) {
  if (marker.ref_low == null && marker.ref_high == null) return null;
  if (marker.value_numeric == null) return null;
  const low = marker.ref_low ?? (marker.ref_high - Math.abs(marker.ref_high) * 0.5);
  const high = marker.ref_high ?? (marker.ref_low + Math.abs(marker.ref_low) * 0.5);
  if (!(high > low)) return null;
  // Pad the domain 30% past the reference band on each side so the value
  // tick has room to sit outside the band when the marker is abnormal,
  // same visual idea as the mockup's fixed-but-plausible percentages.
  const pad = (high - low) * 0.3 || 1;
  const domainLow = Math.min(low - pad, marker.value_numeric - pad * 0.2);
  const domainHigh = Math.max(high + pad, marker.value_numeric + pad * 0.2);
  const span = domainHigh - domainLow || 1;
  const bandLeftPct = ((low - domainLow) / span) * 100;
  const bandRightPct = 100 - ((high - domainLow) / span) * 100;
  const tickLeftPct = Math.min(98, Math.max(0, ((marker.value_numeric - domainLow) / span) * 100));
  return { bandLeftPct: bandLeftPct.toFixed(1), bandRightPct: bandRightPct.toFixed(1), tickLeftPct: tickLeftPct.toFixed(1) };
}

async function loadMarkerTrend(documentId, normalizedMarkerName) {
  const container = document.getElementById('trend-trace-container');
  if (!container) return;
  container.innerHTML = '<div class="trend-empty">Loading…</div>';
  try {
    const res = await apiFetch(`/structured/documents/${documentId}/markers/${encodeURIComponent(normalizedMarkerName)}/history?patient_id=${structuredDocState.patientId}`);
    const points = res.ok ? await res.json() : [];
    container.innerHTML = renderTrendTrace(points);
  } catch { container.innerHTML = '<div class="trend-empty">Failed to load trend.</div>'; }
}

function renderTrendTrace(history) {
  const numeric = history.filter((p) => p.value_numeric != null);
  if (!numeric.length) return '<div class="trend-empty">No numeric history for this marker yet.</div>';
  if (numeric.length === 1) {
    const p = numeric[0];
    return `<div class="trend-trace"><div class="trend-bar-col">
      <div class="trend-bar-value">${p.value_numeric}</div>
      <div class="trend-bar is-latest" style="height:70px"></div>
      <div class="trend-bar-label">${p.document_date ? fmtDateTime(p.document_date).slice(0, 6) : '—'}</div>
    </div></div><div class="trend-empty">Only one reading on file — trend needs at least two.</div>`;
  }
  const vals = numeric.map((p) => p.value_numeric);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = (max - min) || 1;
  const bars = numeric.map((p, i) => {
    const heightPx = 20 + ((p.value_numeric - min) / span) * 110;
    const label = p.document_date ? fmtDateTime(p.document_date).slice(0, 6) : '—';
    return `<div class="trend-bar-col">
      <div class="trend-bar-value">${p.value_numeric}</div>
      <div class="trend-bar${i === numeric.length - 1 ? ' is-latest' : ''}" style="height:${heightPx}px"></div>
      <div class="trend-bar-label">${label}</div>
    </div>`;
  }).join('');
  return `<div class="trend-trace">${bars}</div>`;
}

async function loadDocumentNotes(documentId, patientId) {
  const listEl = document.getElementById('doc-notes-list');
  if (!listEl) return;
  try {
    const res = await apiFetch(`/structured/documents/${documentId}/notes?patient_id=${patientId}`);
    const notes = res.ok ? await res.json() : [];
    listEl.innerHTML = notes.length
      ? notes.map(n => `
          <div class="doc-note-row${n.retracted ? ' is-retracted' : ''}">
            <div class="t-xs" style="opacity:.7">${escHtml(n.doctor_name)} · ${relTime(n.updated_at)}${n.retracted ? ' · <strong>RETRACTED</strong>' : ''}</div>
            <div>${escHtml(n.content)}</div>
          </div>`).join('')
      : 'No notes yet.';

    // Authoritative retracted state for the banner is "any doctor's note
    // on this document is retracted" -- not just the current doctor's own
    // save response, since multiple doctors can share access to one
    // patient's reports.
    const banner = document.getElementById('retraction-banner');
    if (banner) banner.style.display = notes.some(n => n.retracted) ? 'block' : 'none';

    const input = document.getElementById('doc-note-input');
    const retractCheckbox = document.getElementById('doc-note-retract-checkbox');
    if (input) {
      const mine = notes.find(n => n.doctor_id === auth.user().id);
      input.value = mine ? mine.content : '';
      if (retractCheckbox) retractCheckbox.checked = !!(mine && mine.retracted);
    }
  } catch { listEl.innerHTML = 'Failed to load notes.'; }
}

function renderExtractionAudit(audit) {
  const statusFooter = {
    not_run: 'Verification not run yet.',
    no_source: 'Original file not stored — cannot verify.',
    running: 'Verification in progress…',
    complete: audit.verified_at ? `Verified ${relTime(audit.verified_at)}` : 'Verified.',
    failed: `Verification failed${audit.error ? ': ' + escHtml(audit.error) : '.'}`,
  }[audit.verification_status] || '';

  return `
    <div class="blueprint" style="padding:18px">
      <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
      <div class="doc-kicker">Extraction audit</div>
      <div class="audit-panel-row"><span>Markers found</span><span class="audit-value">${audit.markers_found}</span></div>
      <div class="audit-panel-row"><span>Confidence ≥ 0.95</span><span class="audit-value">${audit.high_confidence}</span></div>
      <div class="audit-panel-row"><span>Needs review</span><span class="audit-value${audit.needs_review > 0 ? ' is-flagged' : ''}">${audit.needs_review}</span></div>
      <div class="audit-footer">${statusFooter}</div>
    </div>`;
}

function renderStepper(currentStatus) {
  const steps = ['uploaded', 'processing', 'awaiting_review', 'reviewed'];
  const labels = { uploaded: 'Uploaded', processing: 'Processing', awaiting_review: 'Awaiting review', reviewed: 'Reviewed' };
  const currentIdx = steps.indexOf(currentStatus);
  let html = '<div class="status-stepper">';
  steps.forEach((s, i) => {
    const isDone    = i < currentIdx;
    const isCurrent = i === currentIdx;
    const cls = isDone ? 'done' : isCurrent ? 'current' : '';
    if (i > 0) html += `<div class="step-connector ${isDone ? 'done' : ''}"></div>`;
    html += `<div class="step ${cls}">
      <div class="step-dot">${isDone ? '✓' : i + 1}</div>
      <div class="step-label">${labels[s]}</div>
    </div>`;
  });
  html += '</div>';
  return html;
}

function renderSummaryBody(summary, isDoctor) {
  if (!summary) {
    return `<div class="t-sm" style="color:var(--text-light);padding:8px 0">
      No AI summary generated yet. ${isDoctor ? 'Use the button above to generate one.' : 'Your doctor can generate an AI summary.'}
    </div>`;
  }
  const kf = Array.isArray(summary.key_findings) ? summary.key_findings : [];
  const fv = Array.isArray(summary.flagged_values) ? summary.flagged_values : [];

  return `
    <div class="ai-field">
      <div class="ai-field-label">Overview</div>
      <div class="ai-field-content" data-field="summary">${escHtml(summary.summary || '')}</div>
      ${isDoctor ? `<div class="ai-field-edit"><textarea rows="3" data-field="summary">${escHtml(summary.summary || '')}</textarea></div>` : ''}
    </div>
    <div class="ai-field">
      <div class="ai-field-label">Key findings</div>
      <div class="ai-field-content" data-field="key_findings">
        <ul>${kf.map(f => `<li>${escHtml(f)}</li>`).join('')}</ul>
      </div>
      ${isDoctor ? `<div class="ai-field-edit"><textarea rows="4" data-field="key_findings" placeholder="One finding per line">${escHtml(kf.join('\n'))}</textarea></div>` : ''}
    </div>
    ${fv.length > 0 ? `
    <div class="ai-field">
      <div class="ai-field-label">⚠ Values requiring attention</div>
      <div class="ai-field-content" data-field="flagged_values">
        <ul class="flagged-list">${fv.map(f => `<li class="flagged-item">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          ${escHtml(f)}</li>`).join('')}
        </ul>
      </div>
      ${isDoctor ? `<div class="ai-field-edit"><textarea rows="3" data-field="flagged_values" placeholder="One value per line">${escHtml(fv.join('\n'))}</textarea></div>` : ''}
    </div>` : fv.length === 0 && summary ? `<div class="ai-field">
      <div class="ai-field-label">⚠ Values requiring attention</div>
      <div class="ai-field-content" style="color:var(--green)">No flagged values.</div>
      ${isDoctor ? `<div class="ai-field-edit"><textarea rows="2" data-field="flagged_values" placeholder="One value per line"></textarea></div>` : ''}
    </div>` : ''}
    <div class="ai-field">
      <div class="ai-field-label">Recommendation</div>
      <div class="ai-field-content" data-field="recommendation">${escHtml(summary.recommendation || '')}</div>
      ${isDoctor ? `<div class="ai-field-edit"><textarea rows="3" data-field="recommendation">${escHtml(summary.recommendation || '')}</textarea></div>` : ''}
    </div>`;
}

function renderSummaryActions(report, isDoctor) {
  const actionsEl = document.getElementById('ai-summary-actions');
  if (!actionsEl) return;
  actionsEl.innerHTML = '';
  if (!isDoctor) return;

  const hasSummary = !!report.ai_summary;

  if (hasSummary) {
    const editBtn = document.createElement('button');
    editBtn.className = 'btn btn-ghost btn-sm';
    editBtn.id = 'edit-summary-btn';
    editBtn.textContent = 'Edit';
    editBtn.addEventListener('click', () => toggleEditMode(editBtn, report.id));
    actionsEl.appendChild(editBtn);

    const regenBtn = document.createElement('button');
    regenBtn.className = 'btn btn-secondary btn-sm';
    regenBtn.textContent = 'Regenerate';
    regenBtn.addEventListener('click', () => generateSummary(report.id, report));
    actionsEl.appendChild(regenBtn);
  } else {
    const genBtn = document.createElement('button');
    genBtn.className = 'btn btn-primary btn-sm';
    genBtn.id = 'generate-summary-btn';
    genBtn.textContent = 'Generate AI summary';
    genBtn.addEventListener('click', () => generateSummary(report.id, report));
    actionsEl.appendChild(genBtn);
  }
}

function toggleEditMode(editBtn, reportId) {
  const body = document.getElementById('ai-summary-body');
  const isEditing = body.classList.toggle('edit-mode');
  editBtn.textContent = isEditing ? 'Save changes' : 'Edit';

  if (!isEditing) {
    // Save
    saveSummaryEdits(reportId);
  }
}

async function saveSummaryEdits(reportId) {
  const body = document.getElementById('ai-summary-body');
  const payload = {};

  body.querySelectorAll('.ai-field-edit textarea').forEach(ta => {
    const field = ta.dataset.field;
    const val = ta.value.trim();
    if (field === 'key_findings' || field === 'flagged_values') {
      payload[field] = val ? val.split('\n').map(s => s.trim()).filter(Boolean) : [];
    } else {
      payload[field] = val;
    }
  });

  try {
    const res = await apiFetch(`/reports/${reportId}/ai-summary`, {
      method: 'PATCH', body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) { toast(errMsg(data), 'error'); return; }
    toast('Summary updated.', 'success');
    reportState.report.ai_summary = data;
    // Re-render summary body
    document.getElementById('ai-summary-body').innerHTML = renderSummaryBody(data, true);
    renderSummaryActions(reportState.report, true);
  } catch { toast('Failed to save.', 'error'); }
}

async function generateSummary(reportId, report) {
  const overlay = document.getElementById('ai-loading-overlay');
  if (overlay) overlay.classList.add('show');
  const btn = document.getElementById('generate-summary-btn') || document.querySelector('#ai-summary-actions button');
  if (btn) btn.disabled = true;

  try {
    const res = await apiFetch(`/reports/${reportId}/ai-summary`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      const errEl = document.getElementById('ai-summary-body');
      errEl.innerHTML = `<div class="error-banner">${escHtml(errMsg(data))}</div>`;
      return;
    }
    reportState.report.ai_summary = data;
    document.getElementById('ai-summary-body').innerHTML = renderSummaryBody(data, true);
    renderSummaryActions(reportState.report, true);
    toast('AI summary generated.', 'success');
  } catch { toast('Failed to generate summary.', 'error'); }
  finally {
    if (overlay) overlay.classList.remove('show');
    if (btn) btn.disabled = false;
  }
}

async function loadPdfPreview(report) {
  const area = document.getElementById('pdf-preview-area');
  if (!area) return;
  try {
    const { blob } = await fetchFileBlob(report.pdf_url);
    const objUrl = URL.createObjectURL(blob);
    area.innerHTML = `<iframe src="${objUrl}" title="PDF preview"></iframe>`;
    // The Download button is wired once in renderReportDetail (downloadPdf);
    // a second listener here used to download the file twice.
  } catch (err) {
    area.innerHTML = `<div class="pdf-loading" style="color:var(--red)">${escHtml(err.message || 'Could not load PDF preview.')}</div>`;
  }
}

async function downloadPdf(report, fromEvent) {
  await downloadFile(report.pdf_url, report.display_name || 'report');
}

/* Comments */
async function loadComments(reportId) {
  const thread = document.getElementById('comments-thread');
  if (!thread) return;
  try {
    const res = await apiFetch(`/reports/${reportId}/comments`);
    const data = await res.json();
    if (!res.ok) return;
    thread.innerHTML = '';
    if (data.length === 0) {
      thread.innerHTML = `<div class="t-xs" style="padding:4px 0;color:var(--text-light)">No notes yet. Add the first one below.</div>`;
    } else {
      data.forEach(c => thread.appendChild(renderComment(c)));
    }
    if (data.length) reportState.commentLastId = data[data.length - 1].id;
  } catch {}
}

function renderComment(c) {
  const me = auth.user();
  const isMe = c.sender_id === me.id;
  const conv = convState?.conv;
  let name = isMe ? me.name : (conv ? (c.sender_id === conv.patient_id ? conv.patient.name : conv.doctor.name) : '…');

  const div = document.createElement('div');
  div.className = 'comment-item';
  div.dataset.commentId = c.id;
  div.innerHTML = `
    <div class="avatar" style="background:${isMe ? 'var(--sky-mid)' : '#E8E0FF'};color:${isMe ? 'var(--navy)' : '#6B3FA0'}">${initials(name)}</div>
    <div class="comment-body">
      <div class="comment-author">${escHtml(name)}</div>
      <div class="comment-text">${escHtml(c.text)}</div>
      <div class="comment-time">${relTime(c.timestamp)}</div>
    </div>`;
  return div;
}

function startCommentPolling(reportId) {
  stopCommentPolling();
  reportState.pollTimer = setInterval(() => pollComments(reportId), 3000);
}

function stopCommentPolling() {
  if (reportState?.pollTimer) clearInterval(reportState.pollTimer);
}

async function pollComments(reportId) {
  if (!reportState) return;
  try {
    const res = await apiFetch(`/reports/${reportId}/comments?after_id=${reportState.commentLastId}`);
    const data = await res.json();
    if (!res.ok || !data.length) return;
    reportState.commentLastId = data[data.length - 1].id;
    const thread = document.getElementById('comments-thread');
    if (!thread) return;
    // Clear empty state
    if (thread.querySelector('.t-xs')) thread.innerHTML = '';
    data.forEach(c => thread.appendChild(renderComment(c)));
    thread.scrollTop = thread.scrollHeight;
  } catch {}
}

function initCommentComposer(reportId) {
  const textarea = document.getElementById('comment-textarea');
  const btn = document.getElementById('comment-send-btn');
  if (!textarea || !btn) return;

  async function send() {
    const text = textarea.value.trim();
    if (!text) return;
    btn.disabled = true; textarea.disabled = true;
    try {
      const res = await apiFetch(`/reports/${reportId}/comments`, {
        method: 'POST', body: JSON.stringify({ text })
      });
      const data = await res.json();
      if (!res.ok) { toast(errMsg(data), 'error'); return; }
      textarea.value = '';
      const thread = document.getElementById('comments-thread');
      if (thread?.querySelector('.t-xs')) thread.innerHTML = '';
      if (thread) thread.appendChild(renderComment(data));
      reportState.commentLastId = data.id;
      if (thread) thread.scrollTop = thread.scrollHeight;
    } catch { toast('Failed to send.', 'error'); }
    finally { btn.disabled = false; textarea.disabled = false; textarea.focus(); }
  }

  btn.addEventListener('click', send);
  textarea.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
}

/* ============================================================
   REPORTS TAB (cross-conversation history)
   ============================================================ */
async function loadReportsPage() {
  const me = auth.user();
  const layout = document.getElementById('reports-layout');
  layout.innerHTML = '<div class="skeleton skeleton-line w60"></div><div class="skeleton skeleton-line w80"></div>';
  if (me.role === 'patient') {
    await renderPatientReportsPage(layout);
  } else {
    await renderDoctorReportsPicker(layout);
  }
}

async function renderPatientReportsPage(layout) {
  const me = auth.user();
  try {
    const [repRes, prescRes, docRes] = await Promise.all([
      apiFetch(`/reports?patient_id=${me.id}`),
      apiFetch(`/prescriptions?patient_id=${me.id}`),
      apiFetch(`/structured/documents?patient_id=${me.id}`)
    ]);
    const reports = repRes.ok ? await repRes.json() : [];
    const prescriptions = prescRes.ok ? await prescRes.json() : [];
    const documents = docRes.ok ? await docRes.json() : [];
    renderReportsAndPrescriptionsList(layout, reports, prescriptions, documents, me.id);
  } catch { layout.innerHTML = emptyState('alert', 'Failed to load reports.', ''); }
}

/* ============================================================
   LAB REPORT UPLOAD (Phase 4) -- patient-only, feeds the structured
   extraction pipeline via POST /me/lab-reports (routers/lab_reports.py).
   That route shells out to DataFetch's OCR pipeline SYNCHRONOUSLY (up to
   its own 180s timeout) before responding, so this really can take up to
   three minutes -- the button says so rather than looking stuck.
   ============================================================ */
function initLabReportUpload() {
  const btn = document.getElementById('upload-lab-report-btn');
  const input = document.getElementById('lab-report-file-input');
  if (!btn || !input) return;
  btn.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const file = input.files[0];
    input.value = ''; // allow re-selecting the same file next time
    if (file) uploadLabReport(file);
  });
}

async function uploadLabReport(file) {
  const btn = document.getElementById('upload-lab-report-btn');
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Extracting… (up to 3 min)';

  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await apiFetch('/me/lab-reports', { method: 'POST', body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(errMsg(data), 'error'); return; }

    if (data.status === 'queued' && data.job_id) {
      toast('Upload received — still extracting. This page will update when it finishes.', '');
      btn.textContent = 'Processing…';
      const final = await pollLabReportJob(data.job_id);
      announceLabReportOutcome(final);
    } else {
      announceLabReportOutcome(data);
    }
    if (currentPage === 'reports') loadReportsPage();
  } catch (err) {
    toast('Upload failed: ' + (err.message || 'could not reach the server'), 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function announceLabReportOutcome(data) {
  if (!data) {
    toast('Still processing after 5 minutes — check the Reports page again shortly.', '');
  } else if (data.status === 'failed') {
    toast(data.detail || 'Extraction failed. Please try again.', 'error');
  } else if (data.status === 'duplicate') {
    toast('This report was already uploaded — showing the existing results.', '');
  } else {
    toast('Report processed — markers and AI summary are ready.', 'success');
  }
}

/* Uploads that outlive the request come back "queued"; poll the job until
   it finishes (or ~5 min) so a slow extraction or a background failure is
   reported instead of the report silently never appearing. */
async function pollLabReportJob(jobId) {
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 5000));
    try {
      const res = await apiFetch(`/me/lab-reports/jobs/${jobId}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { status: 'failed', detail: errMsg(data) };
      if (data.status !== 'queued') return data;
    } catch { /* transient network error -- keep polling */ }
  }
  return null;
}

async function renderDoctorReportsPicker(layout) {
  try {
    const patRes = await apiFetch('/users?role=patient');
    if (!patRes.ok) { layout.innerHTML = emptyState('alert', 'Failed to load your patients.', errMsg(await patRes.json().catch(() => ({})))); return; }
    const patients = await patRes.json();

    if (patients.length === 0) {
      layout.innerHTML = emptyState('people', 'No connected patients yet.', 'Connect with a patient first from the Connections tab.');
      return;
    }

    layout.innerHTML = `<div class="patient-picker" id="patient-picker"></div><div id="doctor-reports-list"></div>`;
    const picker = layout.querySelector('#patient-picker');
    const resultsEl = layout.querySelector('#doctor-reports-list');
    resultsEl.innerHTML = emptyState('pointLeft', 'Select a patient to view their reports.', '');

    patients.forEach(p => {
      const conn = connectionsCache.find(c => c.status === 'accepted' && c.patient_id === p.id);
      const label = conn?.doctor_nickname ? `${conn.doctor_nickname} (${p.name})` : p.name;
      const btn = document.createElement('button');
      btn.className = 'patient-picker-item';
      btn.innerHTML = `
        <div class="avatar">${initials(p.name)}</div>
        <span>${escHtml(label)}</span>`;
      btn.addEventListener('click', () => {
        picker.querySelectorAll('.patient-picker-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        loadDoctorPatientReports(p, resultsEl);
      });
      picker.appendChild(btn);
    });
  } catch { layout.innerHTML = emptyState('alert', 'Failed to load.', ''); }
}

/* A connected doctor sees ALL of the patient's reports: lab reports the
   patient uploaded on their own side, plus everything shared in any chat. */
async function loadDoctorPatientReports(patient, resultsEl) {
  resultsEl.innerHTML = '<div class="skeleton skeleton-line w60"></div>';
  try {
    const [repRes, prescRes, docRes] = await Promise.all([
      apiFetch(`/reports?patient_id=${patient.id}`),
      apiFetch(`/prescriptions?patient_id=${patient.id}`),
      apiFetch(`/structured/documents?patient_id=${patient.id}`)
    ]);
    const failed = [repRes, prescRes, docRes].find(r => !r.ok);
    if (failed) {
      const data = await failed.json().catch(() => ({}));
      resultsEl.innerHTML = emptyState('alert', `Couldn't load ${patient.name}'s reports.`, errMsg(data));
      return;
    }
    const [reports, prescriptions, documents] = await Promise.all([repRes.json(), prescRes.json(), docRes.json()]);
    renderReportsAndPrescriptionsList(resultsEl, reports, prescriptions, documents, patient.id);
  } catch { resultsEl.innerHTML = emptyState('alert', 'Failed to load reports.', ''); }
}

/* Shared by both the patient's own view and a doctor's per-patient view.
   patientId is the CareLink id whose reports these are -- passed through
   explicitly to reportListCard so a doctor's very first click on a
   structured document has one to send. It can't be derived from
   structuredDocState here (that's only populated *inside*
   openStructuredDocument, after it already needed this value). */
function renderReportsAndPrescriptionsList(container, reports, prescriptions, documents = [], patientId = null) {
  const items = [
    ...reports.map(r => ({ ...r, _type: 'rep', _date: r.timestamp })),
    ...documents.map(d => ({ ...d, _type: 'doc', _date: d.document_date || d.uploaded_at })),
    ...prescriptions.map(p => ({ ...p, _type: 'presc', _date: p.timestamp }))
  ].sort((a, b) => new Date(b._date || 0) - new Date(a._date || 0));

  // "Labs" groups CareLink reports + structured documents together --
  // matches the mockup's exact two-category chip split (All / Labs /
  // Prescriptions). They stay separate _type values (different detail
  // views, different click targets) purely for filtering here.
  const labsCount = reports.length + documents.length;

  container.innerHTML = `
    <div class="reports-toolbar">
      <div class="reports-filter-tabs">
        <button class="tag tag-accent report-filter-chip active" data-filter="all">All ${items.length}</button>
        <button class="tag tag-outline report-filter-chip" data-filter="labs">Labs ${labsCount}</button>
        <button class="tag tag-outline report-filter-chip" data-filter="presc">Prescriptions ${prescriptions.length}</button>
      </div>
    </div>
    <div class="reports-grid" id="reports-list"></div>`;

  const listEl = container.querySelector('#reports-list');
  function matches(item, filter) {
    if (filter === 'all') return true;
    if (filter === 'labs') return item._type === 'rep' || item._type === 'doc';
    return item._type === filter;
  }
  function renderFiltered(filter) {
    const filtered = items.filter(i => matches(i, filter));
    listEl.innerHTML = '';
    if (filtered.length === 0) { listEl.innerHTML = emptyState('file', 'Nothing here yet.', ''); return; }
    filtered.forEach(item => listEl.appendChild(reportListCard(item, patientId)));
  }
  container.querySelectorAll('.report-filter-chip').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.report-filter-chip').forEach((t) => {
        t.classList.remove('active', 'tag-accent');
        t.classList.add('tag-outline');
      });
      tab.classList.add('active', 'tag-accent');
      tab.classList.remove('tag-outline');
      renderFiltered(tab.dataset.filter);
    });
  });
  renderFiltered('all');
}

/* Same 4-corner blueprint card the mockup uses for every report-list
   entry, branched three ways by _type -- same visual language, different
   content/click target per source. */
function reportListCard(item, patientId = null) {
  const div = document.createElement('div');
  div.className = 'blueprint report-card';
  const corners = '<i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>';

  if (item._type === 'doc') {
    const label = (item.category || 'Lab document').replace(/_/g, ' ');
    const dateLabel = item.document_date ? fmtDateTime(item.document_date) : 'Undated';
    const isUnreviewed = auth.user()?.role === 'doctor' && !item.doctor_reviewed;
    const badges = [
      item.retracted ? '<span class="tag" style="padding:1px 6px;font-size:10px;background:var(--color-accent-700);color:#fff">Retracted</span>' : '',
      isUnreviewed ? '<span class="tag tag-accent" style="padding:1px 6px;font-size:10px">New</span>' : '',
    ].filter(Boolean).join(' ');
    div.innerHTML = `${corners}
      <div class="report-card-kicker">${escHtml(label)} · ${dateLabel}${badges ? ' · ' + badges : ''}</div>
      <h3 class="report-card-title">${escHtml(label)}</h3>
      <p class="report-card-desc">${item.marker_count} marker${item.marker_count === 1 ? '' : 's'}${item.abnormal_count ? ' · ' + item.abnormal_count + ' outside reference range' : ' · all within range'}</p>
      <div class="report-card-spark">${sparkBars(item.spark)}</div>`;
    div.addEventListener('click', () => openStructuredDocument(item.document_id, patientId));
  } else if (item._type === 'presc') {
    div.innerHTML = `${corners}
      <div class="report-card-kicker">Prescription · ${relTime(item.timestamp)}</div>
      <h3 class="report-card-title">${escHtml(item.display_name || 'Prescription')}</h3>
      <p class="report-card-desc">Shared ${relTime(item.timestamp)}</p>
      <span class="tag tag-outline">Prescription</span>`;
    div.addEventListener('click', () => openPrescriptionDetail(item.id));
  } else {
    const reviewed = item.status === 'reviewed';
    div.innerHTML = `${corners}
      <div class="report-card-kicker">Report · ${relTime(item.timestamp)}</div>
      <h3 class="report-card-title">${escHtml(item.display_name || 'Report')}</h3>
      <p class="report-card-desc">${fmtStatus(item.status)}${item.ai_summary ? ' · AI summary ready' : ''}</p>
      <span class="tag ${reviewed ? 'tag-outline' : 'tag-accent'}">${fmtStatus(item.status)}</span>`;
    div.addEventListener('click', () => openReportDetail(item.id));
  }
  return div;
}

/* Small sparkline used on a lab-document card -- values are already
   normalized 0-1 by the backend (StructuredDocumentSummaryOut.spark). */
function sparkBars(values) {
  if (!values || !values.length) return '';
  return values.map((v) => `<div class="spark-bar" style="height:${4 + v * 16}px"></div>`).join('');
}

/* ============================================================
   MEDICINES
   Doctor: pick a connected patient, write medicines (optional file),
   edit/stop the ones they prescribed. Patient: read-only list.
   Server: routers/medicines.py -- writes into the shared `medicines`
   table, so the AI symptom checker also sees what was prescribed.
   ============================================================ */
let medicinesState = null; // { patientId, patientName } -- doctor view only

/* "2026-10-10" -> local date (no UTC shift, unlike new Date("2026-10-10")). */
function parseLocalDate(isoDate) {
  if (!isoDate) return null;
  const [y, m, d] = String(isoDate).slice(0, 10).split('-').map(Number);
  return (y && m && d) ? new Date(y, m - 1, d) : null;
}

function localTodayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function fmtMedDate(isoDate) {
  const d = parseLocalDate(isoDate);
  return d ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
}

function medicineDurationLabel(med) {
  const start = parseLocalDate(med.start_date);
  const end = parseLocalDate(med.end_date);
  if (start && end) {
    const days = Math.round((end - start) / 86400000) + 1;
    return `${fmtMedDate(med.start_date)} – ${fmtMedDate(med.end_date)} (${days} day${days === 1 ? '' : 's'})`;
  }
  if (start) return `From ${fmtMedDate(med.start_date)} · ongoing`;
  if (end) return `Until ${fmtMedDate(med.end_date)}`;
  return 'No dates given';
}

/* Past its end date counts as finished even if never explicitly stopped. */
function medicineIsCurrent(med) {
  if (!med.active) return false;
  const end = parseLocalDate(med.end_date);
  if (!end) return true;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return end >= today;
}

async function loadMedicinesPage() {
  const me = auth.user();
  const layout = document.getElementById('medicines-layout');
  const subtitle = document.getElementById('medicines-subtitle');
  layout.innerHTML = '<div class="skeleton skeleton-line w60"></div><div class="skeleton skeleton-line w80"></div>';
  if (me.role === 'doctor') {
    subtitle.textContent = 'Write and manage medicines for your connected patients.';
    await renderDoctorMedicines(layout);
  } else {
    subtitle.textContent = 'Medicines prescribed by your doctors. Only your doctor can change these.';
    await renderPatientMedicines(layout);
  }
}

function medicineCard(med, { canEdit = false } = {}) {
  const current = medicineIsCurrent(med);
  const div = document.createElement('div');
  div.className = `blueprint medicine-card${current ? '' : ' stopped'}`;
  div.dataset.id = med.id;
  const by = med.source === 'lab_report'
    ? 'Found in an uploaded lab report'
    : `Prescribed by ${med.doctor_name ? 'Dr. ' + escHtml(med.doctor_name.replace(/^dr\.?\s+/i, '')) : 'your doctor'}`;
  div.innerHTML = `
    <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
    <div class="medicine-main">
      <div class="medicine-name">${escHtml(med.name)}</div>
      ${med.dosage ? `<div class="medicine-dosage">${escHtml(med.dosage)}</div>` : ''}
      <div class="medicine-meta">
        <span>${escHtml(medicineDurationLabel(med))}</span>
        <span>${by}</span>
      </div>
      ${med.notes ? `<div class="medicine-notes">${escHtml(med.notes)}</div>` : ''}
    </div>
    <div class="medicine-actions">
      <span class="status-pill ${current ? 'accepted' : 'rejected'}">${current ? 'Active' : 'Stopped'}</span>
      ${med.has_attachment ? '<button class="btn btn-secondary btn-sm med-attachment-btn">View file</button>' : ''}
      ${canEdit ? '<button class="btn btn-ghost btn-sm med-edit-btn">Edit</button>' : ''}
      ${canEdit && current ? '<button class="btn btn-danger btn-sm med-stop-btn">Stop</button>' : ''}
    </div>`;
  const attBtn = div.querySelector('.med-attachment-btn');
  if (attBtn) attBtn.addEventListener('click', () => openMedicineAttachment(med, attBtn));
  return div;
}

async function openMedicineAttachment(med, btn) {
  btn.disabled = true;
  try {
    const { res, blob } = await fetchFileBlob(`/medicines/${med.id}/attachment`);
    const url = URL.createObjectURL(blob);
    const isViewable = (blob.type || '').startsWith('image/') || (blob.type || '').includes('pdf');
    if (!isViewable) { saveBlob(blob, downloadNameFor(res, blob, med.attachment_name || med.name)); return; }
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal-card">
        <div class="modal-header">
          <h2>${escHtml(med.name)}</h2>
          <button class="btn btn-ghost btn-sm" id="modal-close-btn">✕</button>
        </div>
        <div class="pdf-preview">${(blob.type || '').startsWith('image/')
          ? `<img src="${url}" alt="Prescription file" style="max-width:100%;display:block;margin:0 auto">`
          : `<iframe src="${url}" title="Prescription file"></iframe>`}</div>
        <div class="modal-footer">
          <button class="btn btn-secondary btn-sm" id="med-file-download-btn">Download</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = () => { overlay.remove(); URL.revokeObjectURL(url); };
    overlay.querySelector('#modal-close-btn').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('#med-file-download-btn').addEventListener('click', () =>
      saveBlob(blob, downloadNameFor(res, blob, med.attachment_name || med.name)));
  } catch (err) { toast(err.message || 'Could not open the file.', 'error'); }
  finally { btn.disabled = false; }
}

function renderMedicineList(container, meds, { canEditFor = null, emptyMsg, emptyHint = '' } = {}) {
  container.innerHTML = '';
  if (!meds.length) { container.innerHTML = emptyState('file', emptyMsg, emptyHint); return; }
  const current = meds.filter(medicineIsCurrent);
  const past = meds.filter(m => !medicineIsCurrent(m));
  const section = (title, list) => {
    if (!list.length) return;
    const h = document.createElement('div');
    h.className = 'medicine-section-title';
    h.textContent = `${title} (${list.length})`;
    container.appendChild(h);
    const wrap = document.createElement('div');
    wrap.className = 'medicine-list';
    list.forEach(m => {
      const canEdit = canEditFor != null && m.source === 'doctor' && m.doctor_id === canEditFor;
      const card = medicineCard(m, { canEdit });
      if (canEdit) wireDoctorMedicineCard(card, m);
      wrap.appendChild(card);
    });
    container.appendChild(wrap);
  };
  section('Current', current);
  section('Past / stopped', past);
}

/* ── Patient: read-only ── */
async function renderPatientMedicines(layout) {
  try {
    const res = await apiFetch('/medicines/me');
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { layout.innerHTML = emptyState('alert', 'Failed to load medicines.', errMsg(data)); return; }
    layout.innerHTML = '<div class="medicine-readonly-note">These are set by your doctor and can\'t be edited here. Ask your doctor if something looks wrong.</div><div id="medicine-list-wrap"></div>';
    renderMedicineList(layout.querySelector('#medicine-list-wrap'), data, {
      emptyMsg: 'No medicines yet.',
      emptyHint: 'Medicines your doctor prescribes will appear here.',
    });
  } catch { layout.innerHTML = emptyState('alert', 'Failed to load medicines.', ''); }
}

/* ── Doctor: patient picker + write form + list ── */
async function renderDoctorMedicines(layout) {
  let patients = [];
  try {
    const res = await apiFetch('/users?role=patient');
    if (!res.ok) { layout.innerHTML = emptyState('alert', 'Failed to load your patients.', errMsg(await res.json().catch(() => ({})))); return; }
    patients = await res.json();
  } catch { layout.innerHTML = emptyState('alert', 'Failed to load your patients.', ''); return; }

  if (!patients.length) {
    layout.innerHTML = emptyState('people', 'No connected patients yet.', 'Connect with a patient first from the Connections tab.');
    return;
  }

  const today = localTodayIso();
  layout.innerHTML = `
    <div class="medicines-toolbar">
      <div class="field">
        <label for="med-patient-select">Patient</label>
        <select id="med-patient-select">
          <option value="">Select a patient…</option>
          ${patients.map(p => {
            const conn = connectionsCache.find(c => c.status === 'accepted' && c.patient_id === p.id);
            const label = conn?.doctor_nickname ? `${conn.doctor_nickname} (${p.name})` : p.name;
            return `<option value="${p.id}">${escHtml(label)}</option>`;
          }).join('')}
        </select>
      </div>
      <button class="btn btn-primary btn-sm" id="med-add-toggle-btn" disabled>+ Add medicine</button>
    </div>
    <form class="blueprint medicine-form" id="med-add-form" style="display:none" novalidate>
      <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
      <div class="error-banner" id="med-form-error" style="display:none"></div>
      <div class="upload-form-row">
        <div class="field">
          <label for="med-name">Medicine name *</label>
          <input type="text" id="med-name" maxlength="200" placeholder="e.g. Amoxicillin 500 mg" required>
        </div>
        <div class="field">
          <label for="med-dosage">Dosage / how to take</label>
          <input type="text" id="med-dosage" maxlength="200" placeholder="e.g. 1 tablet, 3 times a day after meals">
        </div>
      </div>
      <div class="upload-form-row">
        <div class="field">
          <label for="med-start">Start date</label>
          <input type="date" id="med-start" value="${today}">
        </div>
        <div class="field">
          <label for="med-end">End date (leave empty if ongoing)</label>
          <input type="date" id="med-end">
        </div>
      </div>
      <div class="field">
        <label for="med-notes">Notes for the patient (optional)</label>
        <textarea id="med-notes" rows="2" maxlength="1000" placeholder="e.g. Avoid dairy within 2 hours"></textarea>
      </div>
      <div class="field">
        <label for="med-file">Prescription file (optional — PDF, JPG or PNG, max 20 MB)</label>
        <input type="file" id="med-file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png">
      </div>
      <div class="upload-form-row">
        <button type="submit" class="btn btn-primary btn-sm" id="med-submit-btn">Save medicine</button>
        <button type="button" class="btn btn-ghost btn-sm" id="med-cancel-btn">Cancel</button>
      </div>
    </form>
    <div id="medicine-list-wrap">${emptyState('pointLeft', 'Select a patient to see and write their medicines.', '')}</div>`;

  const select = layout.querySelector('#med-patient-select');
  const toggleBtn = layout.querySelector('#med-add-toggle-btn');
  const form = layout.querySelector('#med-add-form');
  const errEl = layout.querySelector('#med-form-error');

  const resetForm = () => {
    form.reset();
    layout.querySelector('#med-start').value = localTodayIso();
    errEl.style.display = 'none';
  };

  select.addEventListener('change', () => {
    const id = Number(select.value);
    const p = patients.find(x => x.id === id);
    medicinesState = p ? { patientId: p.id, patientName: p.name } : null;
    toggleBtn.disabled = !p;
    form.style.display = 'none';
    resetForm();
    if (p) loadDoctorPatientMedicines();
    else layout.querySelector('#medicine-list-wrap').innerHTML = emptyState('pointLeft', 'Select a patient to see and write their medicines.', '');
  });
  toggleBtn.addEventListener('click', () => {
    form.style.display = form.style.display === 'none' ? '' : 'none';
    if (form.style.display === '') layout.querySelector('#med-name').focus();
  });
  layout.querySelector('#med-cancel-btn').addEventListener('click', () => { form.style.display = 'none'; resetForm(); });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!medicinesState) return;
    const name = layout.querySelector('#med-name').value.trim();
    const start = layout.querySelector('#med-start').value;
    const end = layout.querySelector('#med-end').value;
    const showErr = (msg) => { errEl.textContent = msg; errEl.style.display = 'block'; };
    if (!name) { showErr('Please enter the medicine name.'); return; }
    if (start && end && end < start) { showErr("End date can't be before the start date."); return; }

    const fd = new FormData();
    fd.append('name', name);
    fd.append('dosage', layout.querySelector('#med-dosage').value.trim());
    if (start) fd.append('start_date', start);
    if (end) fd.append('end_date', end);
    fd.append('notes', layout.querySelector('#med-notes').value.trim());
    const file = layout.querySelector('#med-file').files[0];
    if (file) fd.append('file', file);

    const submitBtn = layout.querySelector('#med-submit-btn');
    submitBtn.disabled = true; submitBtn.textContent = 'Saving…';
    try {
      const res = await apiFetch(`/medicines/patients/${medicinesState.patientId}`, { method: 'POST', body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showErr(errMsg(data)); return; }
      toast(`${data.name} added for ${medicinesState.patientName}.`, 'success');
      form.style.display = 'none';
      resetForm();
      loadDoctorPatientMedicines();
    } catch (err) { showErr(err.message || 'Could not reach the server.'); }
    finally { submitBtn.disabled = false; submitBtn.textContent = 'Save medicine'; }
  });

  // Keep the previously selected patient when coming back to the page.
  if (medicinesState && patients.some(p => p.id === medicinesState.patientId)) {
    select.value = String(medicinesState.patientId);
    select.dispatchEvent(new Event('change'));
  } else {
    medicinesState = null;
  }
}

async function loadDoctorPatientMedicines() {
  const wrap = document.getElementById('medicine-list-wrap');
  if (!wrap || !medicinesState) return;
  const { patientId, patientName } = medicinesState;
  wrap.innerHTML = '<div class="skeleton skeleton-line w60"></div>';
  try {
    const res = await apiFetch(`/medicines/patients/${patientId}`);
    const data = await res.json().catch(() => ({}));
    if (medicinesState?.patientId !== patientId) return; // switched patient meanwhile
    if (!res.ok) { wrap.innerHTML = emptyState('alert', `Couldn't load ${patientName}'s medicines.`, errMsg(data)); return; }
    renderMedicineList(wrap, data, {
      canEditFor: auth.user().id,
      emptyMsg: `No medicines for ${patientName} yet.`,
      emptyHint: 'Use "+ Add medicine" to write one.',
    });
  } catch { wrap.innerHTML = emptyState('alert', 'Failed to load medicines.', ''); }
}

function wireDoctorMedicineCard(card, med) {
  const stopBtn = card.querySelector('.med-stop-btn');
  if (stopBtn) {
    stopBtn.addEventListener('click', async () => {
      if (!confirm(`Stop ${med.name}? It will stay in the patient's history as stopped.`)) return;
      const fd = new FormData();
      fd.append('stop', 'true');
      await patchMedicine(med, fd, stopBtn, `${med.name} stopped.`);
    });
  }
  const editBtn = card.querySelector('.med-edit-btn');
  if (editBtn) editBtn.addEventListener('click', () => openMedicineEditor(card, med));
}

function openMedicineEditor(card, med) {
  const main = card.querySelector('.medicine-main');
  const actions = card.querySelector('.medicine-actions');
  actions.style.display = 'none';
  main.innerHTML = `
    <div class="error-banner med-edit-error" style="display:none"></div>
    <div class="upload-form-row" style="flex-wrap:wrap;margin-bottom:10px">
      <div class="field"><label>Medicine name *</label><input type="text" class="med-edit-name" maxlength="200" value="${escHtml(med.name)}"></div>
      <div class="field"><label>Dosage / how to take</label><input type="text" class="med-edit-dosage" maxlength="200" value="${escHtml(med.dosage || '')}"></div>
    </div>
    <div class="upload-form-row" style="flex-wrap:wrap;margin-bottom:10px">
      <div class="field"><label>Start date</label><input type="date" class="med-edit-start" value="${escHtml(med.start_date || '')}"></div>
      <div class="field"><label>End date</label><input type="date" class="med-edit-end" value="${escHtml(med.end_date || '')}"></div>
    </div>
    <div class="field"><label>Notes for the patient</label><textarea class="med-edit-notes" rows="2" maxlength="1000">${escHtml(med.notes || '')}</textarea></div>
    <div class="field"><label>Replace prescription file (optional)</label><input type="file" class="med-edit-file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"></div>
    <div class="upload-form-row">
      <button type="button" class="btn btn-primary btn-sm med-edit-save">Save changes</button>
      <button type="button" class="btn btn-ghost btn-sm med-edit-cancel">Cancel</button>
    </div>`;
  main.querySelector('.med-edit-cancel').addEventListener('click', () => loadDoctorPatientMedicines());
  const saveBtn = main.querySelector('.med-edit-save');
  saveBtn.addEventListener('click', async () => {
    const errEl = main.querySelector('.med-edit-error');
    const name = main.querySelector('.med-edit-name').value.trim();
    const start = main.querySelector('.med-edit-start').value;
    const end = main.querySelector('.med-edit-end').value;
    const showErr = (msg) => { errEl.textContent = msg; errEl.style.display = 'block'; };
    if (!name) { showErr('Please enter the medicine name.'); return; }
    if (start && end && end < start) { showErr("End date can't be before the start date."); return; }
    const fd = new FormData();
    fd.append('name', name);
    fd.append('dosage', main.querySelector('.med-edit-dosage').value.trim());
    if (start) fd.append('start_date', start);
    if (end) fd.append('end_date', end);
    fd.append('notes', main.querySelector('.med-edit-notes').value.trim());
    const file = main.querySelector('.med-edit-file').files[0];
    if (file) fd.append('file', file);
    await patchMedicine(med, fd, saveBtn, `${name} updated.`, showErr);
  });
}

async function patchMedicine(med, formData, btn, successMsg, showErr = null) {
  btn.disabled = true;
  try {
    const res = await apiFetch(`/medicines/${med.id}`, { method: 'PATCH', body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { (showErr || ((m) => toast(m, 'error')))(errMsg(data)); return; }
    toast(successMsg, 'success');
    loadDoctorPatientMedicines();
  } catch (err) { toast(err.message || 'Could not reach the server.', 'error'); }
  finally { btn.disabled = false; }
}


/* ============================================================
   MED CALENDAR
   ============================================================ */
async function loadCalendarPage() {
  const layout = document.getElementById('calendar-layout');
  const me = auth.user();
  layout.innerHTML = `
    <div class="calendar-toolbar">
      <div class="calendar-scope-tabs">
        <button class="tag tag-accent calendar-scope-chip active" data-scope="upcoming">Upcoming</button>
        <button class="tag tag-outline calendar-scope-chip" data-scope="past">Past</button>
      </div>
      <button class="btn btn-primary btn-sm" id="add-appointment-btn">+ Add appointment</button>
    </div>
    <div class="blueprint add-appointment-form" id="add-appointment-form">
      <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
      <div class="error-banner" id="appt-form-error" style="display:none"></div>
      <div class="upload-form-row">
        <div class="field">
          <label for="appt-other-select">${me.role === 'doctor' ? 'Patient' : 'Doctor'}</label>
          <select id="appt-other-select"></select>
        </div>
        <div class="field">
          <label for="appt-datetime">Date &amp; time</label>
          <input type="datetime-local" id="appt-datetime">
        </div>
      </div>
      <div class="field">
        <label for="appt-reason">Reason (optional)</label>
        <input type="text" id="appt-reason" maxlength="500" placeholder="e.g. Follow-up checkup">
      </div>
      <div class="upload-form-row">
        <button type="button" class="btn btn-primary btn-sm" id="appt-submit-btn">Create appointment</button>
        <button type="button" class="btn btn-ghost btn-sm" id="appt-cancel-btn">Cancel</button>
      </div>
    </div>
    <div class="appointments-list" id="appointments-list"></div>`;

  await populateAppointmentOtherSelect();
  wireCalendarToolbar(layout);
  await loadAppointmentsList('upcoming');
}

async function populateAppointmentOtherSelect() {
  const me = auth.user();
  const role = me.role === 'doctor' ? 'patient' : 'doctor';
  const sel = document.getElementById('appt-other-select');
  try {
    const res = await apiFetch(`/users?role=${role}`);
    const users = res.ok ? await res.json() : [];
    if (users.length === 0) { sel.innerHTML = `<option value="">No connected ${role}s</option>`; return; }
    sel.innerHTML = users.map(u => {
      const conn = connectionsCache.find(c => c.status === 'accepted' &&
        (role === 'patient' ? c.patient_id === u.id : c.doctor_id === u.id));
      const label = (role === 'patient' && conn?.doctor_nickname) ? `${conn.doctor_nickname} (${u.name})` : u.name;
      return `<option value="${u.id}">${escHtml(label)}</option>`;
    }).join('');
  } catch { sel.innerHTML = `<option value="">Failed to load</option>`; }
}

function wireCalendarToolbar(layout) {
  const form = document.getElementById('add-appointment-form');
  const toggleBtn = document.getElementById('add-appointment-btn');
  const cancelBtn = document.getElementById('appt-cancel-btn');
  const submitBtn = document.getElementById('appt-submit-btn');
  const errEl = document.getElementById('appt-form-error');

  toggleBtn.addEventListener('click', () => { form.classList.toggle('open'); errEl.style.display = 'none'; });
  cancelBtn.addEventListener('click', () => { form.classList.remove('open'); errEl.style.display = 'none'; });

  layout.querySelectorAll('.calendar-scope-chip').forEach(tab => {
    tab.addEventListener('click', () => {
      layout.querySelectorAll('.calendar-scope-chip').forEach((t) => {
        t.classList.remove('active', 'tag-accent');
        t.classList.add('tag-outline');
      });
      tab.classList.add('active', 'tag-accent');
      tab.classList.remove('tag-outline');
      loadAppointmentsList(tab.dataset.scope);
    });
  });

  submitBtn.addEventListener('click', async () => {
    errEl.style.display = 'none';
    const me = auth.user();
    const otherId = Number(document.getElementById('appt-other-select').value);
    const dtVal = document.getElementById('appt-datetime').value;
    const reason = document.getElementById('appt-reason').value.trim();
    if (!otherId) { errEl.textContent = `Please choose a ${me.role === 'doctor' ? 'patient' : 'doctor'}.`; errEl.style.display = 'block'; return; }
    if (!dtVal) { errEl.textContent = 'Please choose a date and time.'; errEl.style.display = 'block'; return; }

    const patientId = me.role === 'patient' ? me.id : otherId;
    const doctorId  = me.role === 'doctor'  ? me.id : otherId;
    // datetime-local has no timezone suffix, so the Date is parsed as local
    // time; convert to UTC and strip the 'Z' to match this app's naive-UTC
    // timestamp convention (see API_CONTRACT.md).
    const scheduledAt = new Date(dtVal).toISOString().replace('Z', '');

    submitBtn.classList.add('btn-loading'); submitBtn.disabled = true;
    try {
      const res = await apiFetch('/appointments', {
        method: 'POST',
        body: JSON.stringify({ patient_id: patientId, doctor_id: doctorId, scheduled_at: scheduledAt, reason: reason || null })
      });
      const data = await res.json();
      if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
      form.classList.remove('open');
      document.getElementById('appt-datetime').value = '';
      document.getElementById('appt-reason').value = '';
      toast('Appointment created.', 'success');
      layout.querySelectorAll('.calendar-scope-chip').forEach((t) => {
        const isUpcoming = t.dataset.scope === 'upcoming';
        t.classList.toggle('active', isUpcoming);
        t.classList.toggle('tag-accent', isUpcoming);
        t.classList.toggle('tag-outline', !isUpcoming);
      });
      loadAppointmentsList('upcoming');
    } catch { errEl.textContent = 'Failed to create appointment.'; errEl.style.display = 'block'; }
    finally { submitBtn.classList.remove('btn-loading'); submitBtn.disabled = false; }
  });
}

async function loadAppointmentsList(scope) {
  const listEl = document.getElementById('appointments-list');
  listEl.innerHTML = '<div class="skeleton skeleton-line w60"></div>';
  try {
    const res = await apiFetch(`/appointments?scope=${scope}`);
    const data = res.ok ? await res.json() : [];
    listEl.innerHTML = '';
    if (data.length === 0) {
      listEl.innerHTML = emptyState('calendar', scope === 'upcoming' ? 'No upcoming appointments.' : 'No past appointments.', '');
      return;
    }
    data.forEach(a => listEl.appendChild(appointmentCard(a)));
  } catch { listEl.innerHTML = emptyState('alert', 'Failed to load appointments.', ''); }
}

const REMINDER_LABELS = { '2h': 'In ~2 hours', '1d': 'Tomorrow', '3d': 'In ~3 days' };

function appointmentCard(appt) {
  const me = auth.user();
  const other = me.id === appt.patient_id ? appt.doctor : appt.patient;
  const div = document.createElement('div');
  div.className = 'blueprint appointment-card';
  const reminderBadge = appt.active_reminder
    ? `<span class="reminder-tag reminder-${appt.active_reminder}">${REMINDER_LABELS[appt.active_reminder]}</span>` : '';
  const cancelledBadge = appt.status === 'cancelled' ? `<span class="status-tag-cancelled">Cancelled</span>` : '';
  div.innerHTML = `
    <i class="corner tl"></i><i class="corner tr"></i><i class="corner bl"></i><i class="corner br"></i>
    <div class="avatar">${initials(other.name)}</div>
    <div class="conv-card-info">
      <div class="conv-card-name">${escHtml(other.name)}${other.specialization ? ` · ${escHtml(other.specialization)}` : ''}</div>
      <div class="conv-card-preview">${fmtDateTime(appt.scheduled_at)}${appt.reason ? ' · ' + escHtml(appt.reason) : ''}</div>
    </div>
    ${reminderBadge}${cancelledBadge}
    ${appt.status === 'scheduled' ? `<button class="btn btn-danger btn-sm cancel-appt-btn" data-id="${appt.id}">Cancel</button>` : ''}`;

  div.querySelectorAll('.cancel-appt-btn').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const res = await apiFetch(`/appointments/${appt.id}`, { method: 'PATCH', body: JSON.stringify({ status: 'cancelled' }) });
      const data = await res.json();
      if (!res.ok) { toast(errMsg(data), 'error'); return; }
      toast('Appointment cancelled.', 'success');
      const activeScope = document.querySelector('.calendar-scope-chip.active')?.dataset.scope || 'upcoming';
      loadAppointmentsList(activeScope);
    } catch { toast('Failed to cancel.', 'error'); }
    finally { b.disabled = false; }
  }));
  return div;
}

/* ============================================================
   PROFILE PAGE
   ============================================================ */
async function loadProfilePage() {
  const layout = document.getElementById('profile-layout');
  layout.innerHTML = '<div class="skeleton skeleton-line w60"></div>';
  try {
    const res = await apiFetch('/users/me');
    const user = await res.json();
    if (!res.ok) { toast(errMsg(user), 'error'); return; }
    auth.updateUser(user);
    renderProfilePage(user);
  } catch { layout.innerHTML = emptyState('alert', 'Failed to load profile.', ''); }
}

function renderProfilePage(user) {
  const isDoctor = user.role === 'doctor';
  const layout = document.getElementById('profile-layout');
  const hasCustomSpec = !!user.specialization && !DOCTOR_SPECIALIZATIONS.includes(user.specialization);
  const selectedPreset = hasCustomSpec ? 'Other' : (user.specialization || '');

  layout.innerHTML = `
    <div class="profile-card">
      <div class="avatar-upload" id="avatar-upload" title="Click to change photo">
        <div class="avatar avatar-xl" id="profile-avatar-preview">${initials(user.name)}</div>
        <div class="avatar-upload-overlay">Change photo</div>
      </div>
      <input type="file" id="avatar-file-input" accept="image/png,image/jpeg,image/webp" style="display:none">
      <div class="error-banner" id="avatar-error" style="display:none"></div>

      <div class="field">
        <label for="profile-name-input">Full name</label>
        <input type="text" id="profile-name-input" value="${escHtml(user.name)}" maxlength="200">
      </div>

      ${isDoctor ? `
      <div class="field">
        <label for="profile-specialization-select">Specialization</label>
        <select id="profile-specialization-select">
          <option value="" ${selectedPreset === '' ? 'selected' : ''}>Select specialization…</option>
          ${DOCTOR_SPECIALIZATIONS.map(s => `<option value="${s}" ${selectedPreset === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
      </div>
      <div class="field" id="profile-specialization-other-field" style="${selectedPreset === 'Other' ? '' : 'display:none'}">
        <label for="profile-specialization-other">Custom specialization</label>
        <input type="text" id="profile-specialization-other" maxlength="100" value="${hasCustomSpec ? escHtml(user.specialization) : ''}" placeholder="e.g. Sports Medicine">
      </div>` : `
      <div class="field">
        <label for="profile-dob-input">Date of birth</label>
        <input type="date" id="profile-dob-input" value="${user.date_of_birth || ''}">
      </div>
      <div class="field">
        <label for="profile-sex-select">Sex</label>
        <select id="profile-sex-select">
          <option value="" ${!user.sex ? 'selected' : ''}>Select…</option>
          <option value="male" ${user.sex === 'male' ? 'selected' : ''}>Male</option>
          <option value="female" ${user.sex === 'female' ? 'selected' : ''}>Female</option>
        </select>
      </div>
      <p class="t-xs" style="color:var(--text-light);margin-top:-8px">
        Used by the AI Assistant's symptom triage to give an age/sex-appropriate recommendation — without
        this on file, it can't give you one.
      </p>`}

      <div class="error-banner" id="profile-save-error" style="display:none"></div>
      <button class="btn btn-primary btn-sm" id="profile-save-btn">Save changes</button>
    </div>`;

  if (isDoctor) {
    const sel = document.getElementById('profile-specialization-select');
    const otherField = document.getElementById('profile-specialization-other-field');
    sel.addEventListener('change', () => { otherField.style.display = sel.value === 'Other' ? '' : 'none'; });
  }

  document.getElementById('avatar-upload').addEventListener('click', () => document.getElementById('avatar-file-input').click());
  document.getElementById('avatar-file-input').addEventListener('change', (e) => uploadAvatar(e.target.files[0]));
  document.getElementById('profile-save-btn').addEventListener('click', () => saveProfile(isDoctor));

  loadAvatarInto(document.getElementById('profile-avatar-preview'), user);
}

async function saveProfile(isDoctor) {
  const btn = document.getElementById('profile-save-btn');
  const errEl = document.getElementById('profile-save-error');
  errEl.style.display = 'none';

  const payload = {};
  const name = document.getElementById('profile-name-input').value.trim();
  if (name) payload.name = name;
  if (isDoctor) {
    const sel = document.getElementById('profile-specialization-select').value;
    const other = document.getElementById('profile-specialization-other')?.value.trim();
    const spec = sel === 'Other' ? other : sel;
    if (spec) payload.specialization = spec;
  } else {
    const dob = document.getElementById('profile-dob-input').value;
    const sex = document.getElementById('profile-sex-select').value;
    if (dob) payload.date_of_birth = dob;
    if (sex) payload.sex = sex;
  }

  btn.classList.add('btn-loading'); btn.disabled = true;
  try {
    const res = await apiFetch('/users/me', { method: 'PATCH', body: JSON.stringify(payload) });
    const data = await res.json();
    if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
    auth.updateUser(data);
    document.getElementById('sidebar-name').textContent = data.name;
    applyProfileNudge();
    toast('Profile updated.', 'success');
    renderProfilePage(data);
  } catch { errEl.textContent = 'Failed to save changes.'; errEl.style.display = 'block'; }
  finally { btn.classList.remove('btn-loading'); btn.disabled = false; }
}

async function uploadAvatar(file) {
  const errEl = document.getElementById('avatar-error');
  errEl.style.display = 'none';
  if (!file) return;
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    errEl.textContent = 'Only JPEG, PNG, or WebP images are accepted.'; errEl.style.display = 'block'; return;
  }
  if (file.size > 2 * 1024 * 1024) {
    errEl.textContent = 'Image is too large (max 2 MB).'; errEl.style.display = 'block'; return;
  }

  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await apiFetch('/users/me/avatar', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) { errEl.textContent = errMsg(data); errEl.style.display = 'block'; return; }
    auth.updateUser(data);
    toast('Photo updated.', 'success');
    loadAvatarInto(document.getElementById('profile-avatar-preview'), data);
    loadAvatarInto(document.getElementById('sidebar-avatar'), data);
  } catch { errEl.textContent = 'Upload failed.'; errEl.style.display = 'block'; }
}

/* ============================================================
   SIDEBAR / NAV
   ============================================================ */
function initNav() {
  document.querySelectorAll('.nav-link[data-page]').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const page = link.dataset.page;
      if (page === 'connections') {
        showPage('connections');
        loadConnections();
      } else if (page === 'dashboard') {
        showPage('dashboard');
        loadDashboard();
      } else if (page === 'reports') {
        showPage('reports');
        loadReportsPage();
      } else if (page === 'calendar') {
        showPage('calendar');
        loadCalendarPage();
      } else if (page === 'medicines') {
        showPage('medicines');
        loadMedicinesPage();
      } else if (page === 'profile') {
        showPage('profile');
        loadProfilePage();
      } else if (page === 'assistant') {
        showPage('assistant');
        initAssistantPage();
      } else if (page === 'evidence') {
        showPage('evidence');
      }
    });
  });

  document.getElementById('sidebar-nudge').addEventListener('click', (e) => {
    e.preventDefault();
    showPage('profile');
    loadProfilePage();
  });

  document.getElementById('logout-btn').addEventListener('click', () => {
    stopConvPolling();
    stopCommentPolling();
    auth.clear();
    connectionsCache = [];
    reportsAccessCache = [];
    convState = null;
    reportState = null;
    showPage('auth');
  });
}

/* ============================================================
   UTILITIES
   ============================================================ */
function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ============================================================
   AI ASSISTANT (patient-only) -- see architecture doc §04
   Talks to SehatAI's own backend (SEHATAI_API), not CareLink's. Auth
   works in two hops: CareLink's own token (already held via `auth`)
   authorizes a call to CareLink's /me/sehatai-token, which mints a
   SehatAI-specific bearer token; THAT token, not CareLink's, is what
   goes on every /api/chat call to SehatAI. Held in memory only for this
   page load -- never localStorage -- since §04's design deliberately
   makes tokens revoke-and-reissue rather than reusable across sessions.
   ============================================================ */
let sehataiToken = null;
// Separate session per mode -- mirrors public/index.html and
// public/diet.html being SEPARATE pages/sessions on SehatAI's own side
// (see processMessage.js's getOrResumeSession vs getOrResumeDietSession).
// Switching the toggle must not leak a symptom-triage session's state
// into a diet question or vice versa.
let sehataiSessionIds = { symptom: null, diet: null };
let assistantMode = 'symptom';
let assistantInitialized = false;

const ASSISTANT_GREETINGS = {
  symptom: "Hi — I'm here to help you figure out which kind of doctor to see. What symptoms are you experiencing, and how long have you had them?",
  diet: "Hi — ask me a diet or nutrition question and I'll tailor advice to your profile. (A physical emergency raised here won't be answered — I'll tell you to switch back to Symptoms.)",
};
const ASSISTANT_PLACEHOLDERS = {
  symptom: 'Describe a symptom… (Enter to send, Shift+Enter for new line)',
  diet: 'Ask a diet/nutrition question… (Enter to send, Shift+Enter for new line)',
};

// ---- Chat persistence (client-side only) ----
// SehatAI's own backend deliberately never stores raw message text
// server-side (see chatLog.js's own doc comment -- a privacy decision,
// not an oversight) -- only extracted symptom state persists there, for
// 24h. "See my chat again after a refresh" is a real, reasonable ask,
// but the right way to satisfy it is NOT to reverse that server-side
// decision -- it's to keep the visible thread in the browser's own
// storage, which never leaves this device and never touches SehatAI's
// database. Scoped by CareLink's own user id (not just "the assistant
// tab") so two different accounts logging into the same browser never
// see each other's chat -- a real scenario hit while testing tonight.
// 24h TTL mirrors the server's own session TTL, so a restored thread and
// a restored server-side session go stale at the same time.
//
// ONE thread, not one per mode: the original ask was "same chat window, a
// switch to change which pipeline it's talking to" -- an earlier version
// of this kept a fully separate visible thread per mode (mirroring how
// SehatAI's own SESSIONS really are separate server-side), which meant
// toggling to Diet made your Symptoms conversation visibly vanish. The
// backend sessions genuinely do stay separate (sehataiSessionIds still
// tracks one id per mode, each mode's messages still go to the right
// session) -- only the VISIBLE thread is now one continuous window, so
// switching the toggle only ever changes where the NEXT message routes,
// never what's already on screen.
const ASSISTANT_CHAT_TTL_MS = 24 * 60 * 60 * 1000;

function assistantStorageKey() {
  const userId = auth.user()?.id ?? 'anon';
  return `sehatai_chat_v1_${userId}`;
}

function saveAssistantThread() {
  try {
    const thread = document.getElementById('assistant-thread');
    const bubbles = Array.from(thread.querySelectorAll('.msg-row')).map((row) => ({
      mine: row.classList.contains('mine'),
      html: row.querySelector('.msg-bubble').innerHTML,
    }));
    localStorage.setItem(assistantStorageKey(), JSON.stringify({
      sessionIds: { ...sehataiSessionIds },
      savedAt: Date.now(),
      bubbles,
    }));
  } catch (err) {
    // localStorage can throw (private browsing, storage disabled, quota) --
    // non-fatal, the chat just won't survive a refresh this time.
  }
}

function loadAssistantThread() {
  try {
    const raw = localStorage.getItem(assistantStorageKey());
    if (!raw) return null;
    const saved = JSON.parse(raw);
    if (!saved.savedAt || Date.now() - saved.savedAt > ASSISTANT_CHAT_TTL_MS) {
      localStorage.removeItem(assistantStorageKey());
      return null;
    }
    return saved;
  } catch (err) {
    return null;
  }
}

function clearAssistantThread() {
  try { localStorage.removeItem(assistantStorageKey()); } catch (err) { /* ignore */ }
}

// FOUND LIVE: `if (sehataiToken) return sehataiToken;` alone only guards
// against a SECOND call after the FIRST has already resolved -- it does
// nothing for two calls that both start before either has finished (e.g.
// initAssistantPage's own status-check call racing a message send fired
// right after page load). Since /me/sehatai-token revokes-and-reissues
// (see its own doc comment -- there's no "return the existing valid one"
// option with a hash-only token store), two concurrent calls each mint
// their own token and each revoke whatever the OTHER just created; the
// LAST response to arrive silently wins in `sehataiToken = data.token`,
// which is a race, not a guarantee it's the still-valid one -- a patient
// hit exactly this live, with the earlier (now-revoked) token winning the
// race, sending every real chat message as "Missing or invalid token."
// Caching the in-flight PROMISE (not just the eventual value) collapses
// any concurrent callers onto the SAME single request.
let sehataiTokenPromise = null;

async function ensureSehataiToken() {
  if (sehataiToken) return sehataiToken;
  if (!sehataiTokenPromise) {
    sehataiTokenPromise = (async () => {
      const res = await apiFetch('/me/sehatai-token', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(errMsg(data));
      sehataiToken = data.token;
      return sehataiToken;
    })().finally(() => { sehataiTokenPromise = null; });
  }
  return sehataiTokenPromise;
}

function assistantBubble(role, html) {
  const thread = document.getElementById('assistant-thread');
  const row = document.createElement('div');
  row.className = 'msg-row' + (role === 'user' ? ' mine' : '');
  row.innerHTML = `<div class="msg-bubble">${html}</div>`;
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
  return row;
}

// SehatAI's backend answers in one shot -- no real intermediate stages to
// report, unlike EvidenceBoard's genuine NDJSON stream above. Rather than
// leave a single static "…" for however long a slow provider fallback
// takes (confirmed live tonight: DietBot alone can run 30s+), this
// rotates through plausible status phrases and ticks a real elapsed-time
// counter. The phrases are cosmetic (not a report of actual pipeline
// state); the timer is real. Returns a stop() that must be called before
// the bubble is removed, or the interval leaks.
const ASSISTANT_THINKING_PHRASES = {
  symptom: ['Reviewing what you\'ve described', 'Checking against safety guidelines', 'Weighing possible causes', 'Preparing a response'],
  diet: ['Reviewing your profile', 'Looking up nutrition guidelines', 'Checking recipe options', 'Preparing a response'],
};

function startThinkingIndicator(bubbleEl, mode) {
  const phrases = ASSISTANT_THINKING_PHRASES[mode] || ASSISTANT_THINKING_PHRASES.symptom;
  const bubble = bubbleEl.querySelector('.msg-bubble');
  const startedAt = Date.now();
  let phraseIndex = 0;

  const render = () => {
    const elapsedSec = Math.floor((Date.now() - startedAt) / 1000);
    bubble.innerHTML = `<span class="t-xs" style="color:var(--text-light)"><span class="thinking-dot">●</span> ${escHtml(phrases[phraseIndex % phrases.length])}… <span style="opacity:.6">${elapsedSec}s</span></span>`;
  };
  render();

  const phraseTimer = setInterval(() => { phraseIndex += 1; render(); }, 2500);
  const tickTimer = setInterval(render, 1000);

  return () => { clearInterval(phraseTimer); clearInterval(tickTimer); };
}

function renderAssistantReply(result) {
  const kind = result.kind || 'unknown';
  const kindLabel = kind.replace(/_/g, ' ');
  const isEmergency = kind === 'emergency';
  // Small talk reads like a normal chat bubble — no "GREETING" tag on it.
  const labelHtml = kind === 'greeting'
    ? ''
    : `<div class="t-xs" style="margin-bottom:4px;color:${isEmergency ? 'var(--red)' : 'var(--text-light)'};text-transform:uppercase;letter-spacing:.03em;font-weight:600">${escHtml(kindLabel)}</div>`;
  let html = `${labelHtml}${escHtml(result.reply || '(no reply)')}`;
  if (result.recommendation) {
    html += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:.85rem;color:var(--navy);font-weight:600">→ ${escHtml(result.recommendation.specialist_recommended)}</div>`;
  }
  const row = assistantBubble('bot', html);

  // Generic action-button renderer — mirrors public/index.html's own
  // (the standalone SehatAI page every action id here was first built
  // for). Covers the emergency Notify/Continue buttons (see
  // processMessage.js's markEmergencyAcknowledgeable) AND the
  // profile-completeness gate's "Complete your profile" button
  // (processMessage.js STAGE 5) with one mechanism, since all three
  // just send {actionable:true, actions:[{id,label}]} on the envelope.
  if (result.actionable && Array.isArray(result.actions) && result.actions.length) {
    const bubble = row.querySelector('.msg-bubble');
    const actionsDiv = document.createElement('div');
    actionsDiv.className = 'assistant-actions';
    actionsDiv.style.cssText = 'margin-top:10px;display:flex;gap:8px;flex-wrap:wrap';
    for (const action of result.actions) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-sm ' + (action.id === 'continue' ? 'btn-primary' : 'btn-secondary');
      btn.textContent = action.label;
      btn.addEventListener('click', () => {
        actionsDiv.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        if (action.id === 'continue') {
          // Matches EMERGENCY_CONTINUE_RE in processMessage.js exactly.
          sendAssistantMessage('Continue with this chat');
        } else if (action.id === 'complete_profile') {
          showPage('profile');
          loadProfilePage();
        } else {
          // "notify" -- no real integration to emergency services/
          // contacts in this app, just acknowledges the choice.
          actionsDiv.innerHTML = '<span class="t-xs" style="color:var(--text-light)">Okay — please reach out for help. We\'re here whenever you\'re ready to continue.</span>';
        }
      });
      actionsDiv.appendChild(btn);
    }
    bubble.appendChild(actionsDiv);
  }
}

async function sendAssistantMessage(explicitText) {
  const input = document.getElementById('assistant-input');
  const text = explicitText != null ? explicitText : input.value.trim();
  if (!text) return;
  if (explicitText == null) input.value = '';
  const sendBtn = document.getElementById('assistant-send-btn');
  sendBtn.disabled = true;
  assistantBubble('user', escHtml(text));
  const thinking = assistantBubble('bot', '');
  const stopThinking = startThinkingIndicator(thinking, assistantMode);

  try {
    const token = await ensureSehataiToken();
    // FOUND LIVE: webserver.js never reads `sessionId` from the request
    // body at all (see its own comment -- only `newSession` decides
    // forceNew; the client-supplied session id was always ignored,
    // trusting the server's own per-patient pointer instead). This tab's
    // "New session" button and every fresh page load only ever cleared
    // sehataiSessionIds locally -- neither told the server to actually
    // start over, so the NEXT message silently resumed whatever session
    // already existed for this patient (sessions persist 24h by design).
    // The visible chat showed nothing from that old conversation (raw
    // text is deliberately never persisted -- see chatLog.js), while the
    // bot still acted on its full accumulated state -- confirmed live: a
    // patient's fresh-looking "nausea and fear" silently finalized
    // against hours-old "nausea and eye pain" data instead of asking
    // anything new. Sending newSession explicitly, exactly when this
    // client doesn't already hold a session id for this mode, keeps the
    // server's state honest with what's actually visible on screen.
    const isFirstMessageThisMode = !sehataiSessionIds[assistantMode];
    const sendChat = (bearer) => fetch(`${SEHATAI_API}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${bearer}` },
      body: JSON.stringify({ mode: assistantMode, message: text, newSession: isFirstMessageThisMode }),
    });
    let res = await sendChat(token);
    if (res.status === 401) {
      // Each mint revokes the patient's previous token (sehatai_bridge.py),
      // so opening the assistant in a second tab silently invalidated the
      // first tab's cached one -- every later message there failed until
      // a full reload. Re-mint once and retry instead.
      sehataiToken = null;
      res = await sendChat(await ensureSehataiToken());
    }
    const result = await res.json();
    stopThinking();
    thinking.remove();
    if (!res.ok) {
      assistantBubble('bot', `<span style="color:var(--red)">${escHtml(result.error || `HTTP ${res.status}`)}</span>`);
    } else {
      sehataiSessionIds[assistantMode] = result.sessionId || sehataiSessionIds[assistantMode];
      renderAssistantReply(result);
    }
    saveAssistantThread();
  } catch (err) {
    stopThinking();
    thinking.remove();
    assistantBubble('bot', `<span style="color:var(--red)">Could not reach the assistant: ${escHtml(err.message)}</span>`);
    saveAssistantThread();
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// Renders a saved thread (see saveAssistantThread) back into the DOM
// exactly as it looked before, and restores BOTH modes' server-side
// session ids so each mode's next message continues its own real
// session rather than the "always start fresh" behavior below applying
// to a conversation that's actually being knowingly restored, bubble-
// for-bubble, right in front of the patient.
function restoreAssistantThread(saved) {
  const thread = document.getElementById('assistant-thread');
  thread.innerHTML = '';
  for (const bubble of saved.bubbles) {
    const row = document.createElement('div');
    row.className = 'msg-row' + (bubble.mine ? ' mine' : '');
    row.innerHTML = `<div class="msg-bubble">${bubble.html}</div>`;
    thread.appendChild(row);
  }
  thread.scrollTop = thread.scrollHeight;
  sehataiSessionIds = { ...sehataiSessionIds, ...saved.sessionIds };
}

// Only ever changes where the NEXT message routes -- see the module doc
// comment above saveAssistantThread for why the visible thread itself is
// deliberately untouched here.
function switchAssistantMode(mode) {
  if (mode === assistantMode) return;
  assistantMode = mode;

  document.querySelectorAll('#assistant-mode-toggle .mode-toggle-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  document.getElementById('assistant-title').textContent = mode === 'diet' ? 'SehatAI Diet Assistant' : 'SehatAI Assistant';
  document.getElementById('assistant-input').placeholder = ASSISTANT_PLACEHOLDERS[mode];
}

function initAssistantPage() {
  const statusEl = document.getElementById('assistant-status');
  ensureSehataiToken()
    .then(() => { statusEl.textContent = 'Ready'; })
    .catch((err) => { statusEl.textContent = 'Connection failed'; toast(err.message, 'error'); });

  // This function runs every time the AI Assistant nav link is clicked
  // (see initNav's page router), not just the first time. Both the event
  // wiring below AND the initial greeting bubble at the bottom of this
  // function are guarded by this early return, so navigating away and
  // back leaves an in-progress conversation exactly as it was rather
  // than re-adding a duplicate greeting on top of it.
  if (assistantInitialized) return;
  assistantInitialized = true;

  const input = document.getElementById('assistant-input');
  const sendBtn = document.getElementById('assistant-send-btn');
  sendBtn.addEventListener('click', () => sendAssistantMessage());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAssistantMessage(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });

  document.querySelectorAll('#assistant-mode-toggle .mode-toggle-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchAssistantMode(btn.dataset.mode));
  });

  // See public/index.html's own newSessionBtn -- same idea, adapted to
  // the one-shared-thread model above: there's only one visible timeline
  // now, so "start over" clears both modes' sessions and the whole
  // visible thread, not just one mode's slice of it. Also clears the
  // saved copy -- a deliberate restart must not resurrect itself on the
  // next reload.
  document.getElementById('assistant-new-session-btn').addEventListener('click', () => {
    // Delete both server-side sessions now instead of on the next
    // message (sessions are RAM-only on the server; this is the deletion).
    // Best-effort: the next message also sends newSession: true anyway.
    if (sehataiToken) {
      fetch(`${SEHATAI_API}/api/session/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${sehataiToken}` },
        body: '{}',
      }).catch(() => {});
    }
    sehataiSessionIds = { symptom: null, diet: null };
    clearAssistantThread();
    document.getElementById('assistant-thread').innerHTML = '';
    assistantBubble('bot', ASSISTANT_GREETINGS[assistantMode]);
  });

  // Restore the saved chat (up to 24h old, client-side only -- see
  // ASSISTANT_CHAT_TTL_MS's doc comment) if one exists; otherwise a
  // plain greeting, same as this app's very first use ever.
  const saved = loadAssistantThread();
  if (saved) {
    restoreAssistantThread(saved);
  } else {
    assistantBubble('bot', ASSISTANT_GREETINGS[assistantMode]);
  }
}

/* ============================================================
   CLINICAL EVIDENCE (doctor-only) -- see architecture doc §01
   Talks directly to EvidenceBoard (EVIDENCE_API), a standalone tool with
   no auth of its own (single-user local tool by design, see its own API
   contract) -- nothing CareLink-specific to bridge here, unlike the
   patient assistant above.
   ============================================================ */
let evidenceInitialized = false;

function evidenceBubble(role, html, extraClass = '') {
  const thread = document.getElementById('evidence-thread');
  const row = document.createElement('div');
  row.className = 'msg-row' + (role === 'user' ? ' mine' : '');
  row.innerHTML = `<div class="msg-bubble${extraClass ? ' ' + extraClass : ''}" style="max-width:640px">${html}</div>`;
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
  return row;
}

/* The answer itself is drawn by static/js/evidence-view.js (EvidenceView):
   topic headings, short claim summaries and numbered citation chips that
   open each paper's details -- a deterministic function of the response,
   no extra requests. The old "Evidence score" tiles are gone: relevance
   reflects retrieval ranking only, never evidence quality. What stays
   here are the response's own notes around the answer. */
let evidenceAnswerCount = 0;

function renderEvidenceAnswer(report) {
  if (report.abstained) {
    const reasons = (report.abstain_reasons || []).join('; ') || 'insufficient evidence';
    evidenceBubble('bot', `<div class="t-xs" style="color:var(--amber);font-weight:600;margin-bottom:4px">ABSTAINED</div>${escHtml(reasons)}`);
    return;
  }
  const row = evidenceBubble('bot', '', 'evidence-answer-bubble');
  const bubble = row.querySelector('.msg-bubble');
  const viewHost = document.createElement('div');
  bubble.appendChild(viewHost);
  evidenceAnswerCount += 1;
  window.EvidenceView.render(viewHost, report, { idPrefix: `eb${evidenceAnswerCount}` });

  const note = (text, color, italic = false) => {
    const el = document.createElement('div');
    el.className = 't-xs';
    el.style.cssText = `margin-top:8px;color:${color}${italic ? ';font-style:italic' : ''}`;
    el.textContent = text;
    bubble.appendChild(el);
  };
  const f = report.funnel || {};
  if (f.claims_generated != null) {
    note(`${f.claims_generated} claims generated → ${f.claims_deleted} removed by verification → ${f.claims_kept} shown`, 'var(--text-light)');
  }
  if (report.unanswered_aspects && report.unanswered_aspects.length) {
    note(`Not addressed by the evidence: ${report.unanswered_aspects.join('; ')}`, 'var(--amber)');
  }
  if (report.disclaimer) note(report.disclaimer, 'var(--text-light)', true);
}

// Real per-stage labels for EvidenceBoard's actual pipeline (see its own
// api/contract.md's /api/ask/stream doc) -- unlike the AI Assistant's
// thinking indicator below, this reflects genuine progress: each line
// only appears once that real pipeline stage has actually started/
// finished, not a simulated countdown.
const EVIDENCE_STAGE_LABELS = {
  strategist: 'Planning search strategy',
  retrieval: 'Searching PubMed, Europe PMC, ClinicalTrials.gov',
  appraiser: 'Appraising evidence quality',
  synthesizer: 'Synthesizing answer',
  verifier: 'Verifying every claim',
  red_team: 'Red-teaming for weaknesses',
  complete: 'Finalizing',
};

function renderEvidenceThinking(el, doneStages, activeStage) {
  const lines = doneStages.map((s) => `<div class="t-xs" style="color:var(--good, #1A7F5A)">✓ ${escHtml(EVIDENCE_STAGE_LABELS[s] || s)}</div>`);
  if (activeStage) {
    lines.push(`<div class="t-xs" style="color:var(--text-light)"><span class="thinking-dot">●</span> ${escHtml(EVIDENCE_STAGE_LABELS[activeStage] || activeStage)}…</div>`);
  }
  el.querySelector('.msg-bubble').innerHTML = lines.join('');
}

async function sendEvidenceQuestion(explicitText) {
  const input = document.getElementById('evidence-input');
  const text = explicitText != null ? explicitText : input.value.trim();
  if (!text) return;
  if (explicitText == null) input.value = '';
  const sendBtn = document.getElementById('evidence-send-btn');
  sendBtn.disabled = true;
  evidenceBubble('user', escHtml(text));
  const thinking = evidenceBubble('bot', '<span class="t-xs" style="color:var(--text-light)">Connecting…</span>');
  const doneStages = [];

  try {
    // The gateway only lets a signed-in doctor through to EvidenceBoard
    // (auth_request against /auth/verify) -- it has no login of its own.
    // Same-origin (behind the gateway): send the doctor's token for the
    // gateway's check. Cross-origin (local dev, EvidenceBoard on its own
    // port): EvidenceBoard has no login and its CORS allows only
    // Content-Type, so an Authorization header made the browser block the
    // request ("Failed to fetch").
    const evidenceSameOrigin = new URL(EVIDENCE_API, location.href).origin === location.origin;
    const res = await fetch(`${EVIDENCE_API}/api/ask/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(evidenceSameOrigin ? { 'Authorization': `Bearer ${auth.token()}` } : {}),
      },
      body: JSON.stringify({ question: text }),
    });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      thinking.remove();
      evidenceBubble('bot', `<span style="color:var(--red)">${escHtml(errBody.error || `HTTP ${res.status}`)}</span>`);
      return;
    }

    // NDJSON: one JSON object per line, streamed as it happens -- see
    // EVIDENCE_STAGE_LABELS' doc comment above for why this is real
    // progress, not a simulation.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let report = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // last (possibly incomplete) line stays in the buffer

      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'stage' && event.status === 'start') {
          renderEvidenceThinking(thinking, doneStages, event.stage);
        } else if (event.type === 'stage' && event.status === 'done') {
          doneStages.push(event.stage);
          renderEvidenceThinking(thinking, doneStages, null);
        } else if (event.type === 'cache_hit') {
          renderEvidenceThinking(thinking, [], null);
        } else if (event.type === 'result') {
          report = event.report;
        }
      }
    }

    thinking.remove();
    if (report) {
      renderEvidenceAnswer(report);
    } else {
      evidenceBubble('bot', '<span style="color:var(--red)">Stream ended with no result.</span>');
    }
  } catch (err) {
    thinking.remove();
    evidenceBubble('bot', `<span style="color:var(--red)">Could not reach EvidenceBoard: ${escHtml(err.message)}</span>`);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

function initEvidenceComposer() {
  if (evidenceInitialized) return;
  evidenceInitialized = true;
  const input = document.getElementById('evidence-input');
  const sendBtn = document.getElementById('evidence-send-btn');
  sendBtn.addEventListener('click', () => sendEvidenceQuestion());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendEvidenceQuestion(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });
}

/* ============================================================
   BOOT
   ============================================================ */
document.addEventListener('DOMContentLoaded', () => {
  initAuth();
  initNav();
  initComposer();
  initUpload();
  initPrescriptionUpload();
  initLabReportUpload();
  initConnectionsPage();
  initEvidenceComposer();

  // Conv back button
  document.getElementById('conv-back-btn').addEventListener('click', () => {
    stopConvPolling();
    showPage('dashboard');
    loadDashboard();
  });

  if (auth.token() && auth.user()) {
    onLogin();
  } else {
    showPage('auth');
  }
});
