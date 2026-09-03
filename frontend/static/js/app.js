/* ============================================================
   Doctor-Patient Portal — Frontend App
   Plain JS (no framework). All state in memory.
   ============================================================ */

const API = 'http://localhost:8000';
// See architecture doc §01/§04 -- separate services, not CareLink's own backend.
const SEHATAI_API = 'http://localhost:3000';
const EVIDENCE_API = 'http://localhost:8002';

/* ── Auth helpers ── */
const auth = {
  token: () => localStorage.getItem('token'),
  user:  () => { try { return JSON.parse(localStorage.getItem('user')); } catch { return null; } },
  save(token, user) {
    localStorage.setItem('token', token);
    localStorage.setItem('user', JSON.stringify(user));
  },
  updateUser(user) { localStorage.setItem('user', JSON.stringify(user)); },
  clear() { localStorage.removeItem('token'); localStorage.removeItem('user'); }
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
}

/* ── Page routing ── */
let currentPage = null;

function showPage(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));

  const page = document.getElementById(`${id}-page`);
  if (page) page.classList.add('active');

  const link = document.querySelector(`.nav-link[data-page="${id}"]`);
  if (link) link.classList.add('active');

  const shell = document.getElementById('app-shell');
  if (id === 'auth') {
    shell.style.display = 'none';
    document.getElementById('auth-page').style.display = 'flex';
  } else {
    shell.style.display = 'flex';
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
  showPage('connections');
  loadConnections();
}

/* ============================================================
   CONNECTIONS
   ============================================================ */
let connectionsCache = [];
let reportsAccessCache = []; // patient-only: which doctors they've granted reports access to

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
      isDoctor ? '🩺' : '👤',
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
    incomingEl.innerHTML = emptyState('📬', 'No pending requests.', '');
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
    outgoingEl.innerHTML = emptyState('📤', 'No outgoing requests.', '');
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

function emptyState(icon, msg, hint) {
  return `<div class="empty-state">
    <div class="empty-icon">${icon}</div>
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
  div.className = `connection-card ${type}`;
  div.innerHTML = `
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
      ${type === 'accepted' && !isDoctorViewing ? `
        <label class="grant-toggle">
          <input type="checkbox" class="grant-toggle-input" data-doctor-id="${user.id}" ${isGranted ? 'checked' : ''}>
          Share reports
        </label>` : ''}
      ${type === 'accepted' && isDoctorViewing ? `
        <button class="btn btn-ghost btn-sm nickname-btn" data-conn-id="${conn.id}" data-current="${escHtml(conn.doctor_nickname || '')}">
          ✎ ${conn.doctor_nickname ? escHtml(conn.doctor_nickname) : 'Add nickname'}
        </button>` : ''}
      ${type === 'accepted' ? `<button class="btn btn-secondary btn-sm open-conv-btn" data-user-id="${user.id}" data-user-role="${user.role}">Open chat</button>` : ''}
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
  return div;
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
      list.innerHTML = emptyState('💬', 'No conversations yet.',
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
let convState = null; // { conv, other, msgLastId, reportLastId, pollTimer }

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
  convState = { conv, other, msgLastId: 0, pollTimer: null };

  document.getElementById('conv-other-name').textContent = other.name;
  document.getElementById('conv-other-role').textContent = other.role;

  const thread = document.getElementById('conv-thread');
  thread.innerHTML = '<div class="skeleton skeleton-line w60" style="margin:20px auto"></div>';

  showPage('conv');

  await loadConvThread();
  startConvPolling();
  scrollToBottom(thread);
}

async function loadConvThread() {
  if (!convState) return;
  const { conv } = convState;

  // Load messages, reports, and prescriptions in parallel
  const [msgRes, repRes, prescRes] = await Promise.all([
    apiFetch(`/conversations/${conv.id}/messages`),
    apiFetch(`/conversations/${conv.id}/reports`),
    apiFetch(`/conversations/${conv.id}/prescriptions`)
  ]);
  const messages = msgRes.ok ? await msgRes.json() : [];
  const reports  = repRes.ok ? await repRes.json() : [];
  const prescriptions = prescRes.ok ? await prescRes.json() : [];

  if (messages.length) convState.msgLastId = messages[messages.length - 1].id;

  // Merge and sort by timestamp
  const items = [
    ...messages.map(m => ({ ...m, _type: 'msg' })),
    ...reports.map(r  => ({ ...r, _type: 'rep', timestamp: r.timestamp })),
    ...prescriptions.map(p => ({ ...p, _type: 'presc', timestamp: p.timestamp }))
  ].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

  const thread = document.getElementById('conv-thread');
  thread.innerHTML = '';
  items.forEach(item => {
    if (item._type === 'msg') thread.appendChild(renderMessage(item, conv));
    else if (item._type === 'rep') thread.appendChild(renderReportInline(item));
    else thread.appendChild(renderPrescriptionInline(item));
  });
  scrollToBottom(thread);
}

function renderMessage(msg, conv) {
  const me = auth.user();
  const isMine = msg.sender_id === me.id;
  const senderName = isMine ? 'You' : (me.id === conv.patient_id ? conv.doctor.name : conv.patient.name);

  const row = document.createElement('div');
  row.className = `msg-row${isMine ? ' mine' : ''}`;
  row.dataset.msgId = msg.id;
  row.innerHTML = `
    ${!isMine ? `<div class="avatar" title="${escHtml(senderName)}">${initials(senderName)}</div>` : ''}
    <div>
      <div class="msg-bubble">${escHtml(msg.text)}</div>
      <div class="msg-meta">${isMine ? '' : escHtml(senderName) + ' · '}${fmtTime(msg.timestamp)}</div>
    </div>`;
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
      const res = await apiFetch(presc.pdf_url);
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      overlay.querySelector('#presc-pdf-preview').innerHTML = `<iframe src="${objUrl}" title="PDF preview"></iframe>`;
      overlay.querySelector('#presc-download-btn').addEventListener('click', () => {
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = (presc.display_name || 'prescription') + '.pdf';
        a.click();
      });
    } catch {
      overlay.querySelector('#presc-pdf-preview').innerHTML = `<div class="pdf-loading" style="color:var(--red)">Could not load PDF preview.</div>`;
    }
  })();
}

/* Polling */
function startConvPolling() {
  if (!convState) return;
  convState.pollTimer = setInterval(pollMessages, 3000);
}
function stopConvPolling() {
  if (convState?.pollTimer) clearInterval(convState.pollTimer);
}

async function pollMessages() {
  if (!convState) return;
  try {
    const res = await apiFetch(`/conversations/${convState.conv.id}/messages?after_id=${convState.msgLastId}`);
    const newMsgs = await res.json();
    if (!res.ok || !newMsgs.length) return;
    convState.msgLastId = newMsgs[newMsgs.length - 1].id;
    const thread = document.getElementById('conv-thread');
    const wasBottom = thread.scrollHeight - thread.scrollTop <= thread.clientHeight + 80;
    newMsgs.forEach(msg => thread.appendChild(renderMessage(msg, convState.conv)));
    if (wasBottom) scrollToBottom(thread);
  } catch {}
}

function scrollToBottom(el) {
  setTimeout(() => { el.scrollTop = el.scrollHeight; }, 30);
}

/* Composer */
function initComposer() {
  const textarea = document.getElementById('msg-textarea');
  const btn = document.getElementById('msg-send-btn');

  async function sendMsg() {
    const text = textarea.value.trim();
    if (!text || !convState) return;
    btn.disabled = true;
    textarea.disabled = true;
    try {
      const res = await apiFetch(`/conversations/${convState.conv.id}/messages`, {
        method: 'POST', body: JSON.stringify({ text })
      });
      const data = await res.json();
      if (!res.ok) { toast(errMsg(data), 'error'); return; }
      textarea.value = '';
      textarea.style.height = '';
      const thread = document.getElementById('conv-thread');
      thread.appendChild(renderMessage(data, convState.conv));
      scrollToBottom(thread);
      convState.msgLastId = data.id;
    } catch { toast('Failed to send message.', 'error'); }
    finally { btn.disabled = false; textarea.disabled = false; textarea.focus(); }
  }

  btn.addEventListener('click', sendMsg);
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
  textarea.addEventListener('input', () => {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + 'px';
  });
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
      const thread = document.getElementById('conv-thread');
      thread.appendChild(renderReportInline(data));
      scrollToBottom(thread);
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
      const thread = document.getElementById('conv-thread');
      thread.appendChild(renderPrescriptionInline(data));
      scrollToBottom(thread);
      toast('Prescription uploaded.', 'success');
    } catch { errEl.textContent = 'Upload failed. Please try again.'; errEl.style.display = 'block'; }
    finally { submitBtn.classList.remove('btn-loading'); submitBtn.disabled = false; }
  });
}

/* ============================================================
   REPORT DETAIL
   ============================================================ */
let reportState = null; // { report, commentLastId, pollTimer }

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
    const res = await apiFetch(report.pdf_url);
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    area.innerHTML = `<iframe src="${objUrl}" title="PDF preview"></iframe>`;

    // Wire download btn
    const dlBtn = document.getElementById('download-btn');
    if (dlBtn) {
      dlBtn.addEventListener('click', () => {
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = (report.display_name || 'report') + '.pdf';
        a.click();
      }, { once: true });
    }
  } catch {
    area.innerHTML = `<div class="pdf-loading" style="color:var(--red)">Could not load PDF preview.</div>`;
  }
}

async function downloadPdf(report, fromEvent) {
  try {
    const res = await apiFetch(report.pdf_url);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = (report.display_name || 'report') + '.pdf';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch { toast('Download failed.', 'error'); }
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
    const [repRes, prescRes] = await Promise.all([
      apiFetch(`/reports?patient_id=${me.id}`),
      apiFetch(`/prescriptions?patient_id=${me.id}`)
    ]);
    const reports = repRes.ok ? await repRes.json() : [];
    const prescriptions = prescRes.ok ? await prescRes.json() : [];
    renderReportsAndPrescriptionsList(layout, reports, prescriptions);
  } catch { layout.innerHTML = emptyState('⚠️', 'Failed to load reports.', ''); }
}

async function renderDoctorReportsPicker(layout) {
  try {
    const [patRes, grantRes] = await Promise.all([
      apiFetch('/users?role=patient'),
      apiFetch('/reports-access?status=granted')
    ]);
    const patients = patRes.ok ? await patRes.json() : [];
    const grants = grantRes.ok ? await grantRes.json() : [];
    const grantedPatientIds = new Set(grants.map(g => g.patient_id));

    if (patients.length === 0) {
      layout.innerHTML = emptyState('🩺', 'No connected patients yet.', 'Connect with a patient first from the Connections tab.');
      return;
    }

    layout.innerHTML = `<div class="patient-picker" id="patient-picker"></div><div id="doctor-reports-list"></div>`;
    const picker = layout.querySelector('#patient-picker');
    const resultsEl = layout.querySelector('#doctor-reports-list');
    resultsEl.innerHTML = emptyState('👈', 'Select a patient to view their reports.', '');

    patients.forEach(p => {
      const conn = connectionsCache.find(c => c.status === 'accepted' && c.patient_id === p.id);
      const label = conn?.doctor_nickname ? `${conn.doctor_nickname} (${p.name})` : p.name;
      const hasGrant = grantedPatientIds.has(p.id);
      const btn = document.createElement('button');
      btn.className = 'patient-picker-item';
      btn.innerHTML = `
        <div class="avatar">${initials(p.name)}</div>
        <span>${escHtml(label)}</span>
        <span class="status-pill ${hasGrant ? 'accepted' : 'rejected'}">${hasGrant ? 'Shared' : 'Not shared'}</span>`;
      btn.addEventListener('click', () => {
        picker.querySelectorAll('.patient-picker-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        loadDoctorPatientReports(p, hasGrant, resultsEl);
      });
      picker.appendChild(btn);
    });
  } catch { layout.innerHTML = emptyState('⚠️', 'Failed to load.', ''); }
}

async function loadDoctorPatientReports(patient, hasGrant, resultsEl) {
  if (!hasGrant) {
    resultsEl.innerHTML = emptyState('🔒', `${patient.name} hasn't shared their reports history with you yet.`,
      'They can turn this on from their Connections page.');
    return;
  }
  resultsEl.innerHTML = '<div class="skeleton skeleton-line w60"></div>';
  try {
    const [repRes, prescRes] = await Promise.all([
      apiFetch(`/reports?patient_id=${patient.id}`),
      apiFetch(`/prescriptions?patient_id=${patient.id}`)
    ]);
    const reports = repRes.ok ? await repRes.json() : [];
    const prescriptions = prescRes.ok ? await prescRes.json() : [];
    renderReportsAndPrescriptionsList(resultsEl, reports, prescriptions);
  } catch { resultsEl.innerHTML = emptyState('⚠️', 'Failed to load reports.', ''); }
}

/* Shared by both the patient's own view and a doctor's per-patient view. */
function renderReportsAndPrescriptionsList(container, reports, prescriptions) {
  const items = [
    ...reports.map(r => ({ ...r, _type: 'rep' })),
    ...prescriptions.map(p => ({ ...p, _type: 'presc' }))
  ].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));

  container.innerHTML = `
    <div class="reports-filter-tabs">
      <button class="filter-tab active" data-filter="all">All (${items.length})</button>
      <button class="filter-tab" data-filter="rep">Reports (${reports.length})</button>
      <button class="filter-tab" data-filter="presc">Prescriptions (${prescriptions.length})</button>
    </div>
    <div class="reports-list" id="reports-list"></div>`;

  const listEl = container.querySelector('#reports-list');
  function renderFiltered(filter) {
    const filtered = filter === 'all' ? items : items.filter(i => i._type === filter);
    listEl.innerHTML = '';
    if (filtered.length === 0) { listEl.innerHTML = emptyState('📄', 'Nothing here yet.', ''); return; }
    filtered.forEach(item => listEl.appendChild(reportListCard(item)));
  }
  container.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      renderFiltered(tab.dataset.filter);
    });
  });
  renderFiltered('all');
}

function reportListCard(item) {
  const isPresc = item._type === 'presc';
  const div = document.createElement('div');
  div.className = 'conv-card';
  div.innerHTML = `
    <div class="report-icon ${isPresc ? 'prescription-icon' : ''}">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
      </svg>
    </div>
    <div class="conv-card-info">
      <div class="conv-card-name">${escHtml(item.display_name || (isPresc ? 'Prescription' : 'Report'))}</div>
      <div class="conv-card-preview">${isPresc ? 'Prescription' : fmtStatus(item.status)} · ${relTime(item.timestamp)}</div>
    </div>`;
  div.addEventListener('click', () => { isPresc ? openPrescriptionDetail(item.id) : openReportDetail(item.id); });
  return div;
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
        <button class="filter-tab active" data-scope="upcoming">Upcoming</button>
        <button class="filter-tab" data-scope="past">Past</button>
      </div>
      <button class="btn btn-primary btn-sm" id="add-appointment-btn">+ Add appointment</button>
    </div>
    <div class="add-appointment-form" id="add-appointment-form">
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

  layout.querySelectorAll('.calendar-scope-tabs .filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      layout.querySelectorAll('.calendar-scope-tabs .filter-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
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
      layout.querySelectorAll('.calendar-scope-tabs .filter-tab').forEach(t => t.classList.toggle('active', t.dataset.scope === 'upcoming'));
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
      listEl.innerHTML = emptyState('🗓️', scope === 'upcoming' ? 'No upcoming appointments.' : 'No past appointments.', '');
      return;
    }
    data.forEach(a => listEl.appendChild(appointmentCard(a)));
  } catch { listEl.innerHTML = emptyState('⚠️', 'Failed to load appointments.', ''); }
}

const REMINDER_LABELS = { '2h': 'In ~2 hours', '1d': 'Tomorrow', '3d': 'In ~3 days' };

function appointmentCard(appt) {
  const me = auth.user();
  const other = me.id === appt.patient_id ? appt.doctor : appt.patient;
  const div = document.createElement('div');
  div.className = 'conv-card';
  const reminderBadge = appt.active_reminder
    ? `<span class="reminder-badge reminder-${appt.active_reminder}">${REMINDER_LABELS[appt.active_reminder]}</span>` : '';
  const cancelledBadge = appt.status === 'cancelled' ? `<span class="status-pill rejected">Cancelled</span>` : '';
  div.innerHTML = `
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
      const activeScope = document.querySelector('.calendar-scope-tabs .filter-tab.active')?.dataset.scope || 'upcoming';
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
  } catch { layout.innerHTML = emptyState('⚠️', 'Failed to load profile.', ''); }
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
  let html = `<div class="t-xs" style="margin-bottom:4px;color:${isEmergency ? 'var(--red)' : 'var(--text-light)'};text-transform:uppercase;letter-spacing:.03em;font-weight:600">${escHtml(kindLabel)}</div>${escHtml(result.reply || '(no reply)')}`;
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
    const res = await fetch(`${SEHATAI_API}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify({ mode: assistantMode, message: text, newSession: isFirstMessageThisMode }),
    });
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

function evidenceBubble(role, html) {
  const thread = document.getElementById('evidence-thread');
  const row = document.createElement('div');
  row.className = 'msg-row' + (role === 'user' ? ' mine' : '');
  row.innerHTML = `<div class="msg-bubble" style="max-width:640px">${html}</div>`;
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
  return row;
}

function renderEvidenceAnswer(report) {
  if (report.abstained) {
    const reasons = (report.abstain_reasons || []).join('; ') || 'insufficient evidence';
    evidenceBubble('bot', `<div class="t-xs" style="color:var(--amber);font-weight:600;margin-bottom:4px">ABSTAINED</div>${escHtml(reasons)}`);
    return;
  }
  const f = report.funnel || {};
  let html = `${escHtml(report.answer_text || '')}`;
  if (f.claims_generated != null) {
    html += `<div class="t-xs" style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border);color:var(--text-light)">${f.claims_generated} claims generated → ${f.claims_deleted} deleted → ${f.claims_kept} shown</div>`;
  }
  if (report.unanswered_aspects && report.unanswered_aspects.length) {
    html += `<div class="t-xs" style="margin-top:6px;color:var(--amber)">Not addressed by the evidence: ${escHtml(report.unanswered_aspects.join('; '))}</div>`;
  }
  if (report.disclaimer) {
    html += `<div class="t-xs" style="margin-top:8px;color:var(--text-light);font-style:italic">${escHtml(report.disclaimer)}</div>`;
  }
  evidenceBubble('bot', html);
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
    const res = await fetch(`${EVIDENCE_API}/api/ask/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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
