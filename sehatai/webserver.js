// ============================================
// SehatAI: Minimal Browser UI Server
//
// Zero new dependencies — built entirely on Node's built-in `http` and
// `fs` modules, no express, nothing to `npm install`. It's a thin HTTP
// wrapper around the SAME processPatientMessage()/processDietMessage()
// pipeline that testChat.js (CLI) and testEdgeCases.js already
// exercise — this file adds a browser front-end as a NEW consumer of
// already-tested code, it doesn't touch or duplicate any pipeline
// logic, so it carries none of the regression risk a change to
// processMessage.js/chatLog.js/infermedicaClient.js would.
//
// The UI mirrors testChat.js's explicit mode-switch design (see
// runSharedSafetyGates() in processMessage.js) as clickable buttons
// instead of /diet and /symptom CLI commands — useful for showing the
// final implementation to someone who isn't going to type slash
// commands into a terminal.
//
// HARDENED (this pass — see auth.js's doc comment for the full
// reasoning): this used to trust a `patientId` supplied directly in
// the request body with NO verification at all — a real IDOR against
// health data, since anyone could pass any patient's UUID and get
// recommendations grounded in that patient's real allergies/meds/labs.
// Every request now MUST carry a bearer token (see issuetoken.js),
// and the patientId used for the rest of the pipeline comes ONLY from
// that token — never from the request body, which is untrusted client
// input. A body-supplied `patientId` is now ignored entirely.
//
// Also added: a body-size cap (memory-exhaustion protection), a
// simple per-token rate limiter (cost/abuse protection — this app
// makes several paid LLM calls per message), and error responses that
// no longer leak internal error messages/stack details to the client.
// None of this is meant to be a production-grade API gateway — it's
// the minimum a demo server handling real health data shouldn't ship
// without.
//
// Usage:
//   node sehatai/issuetoken.js <patientId>   (mint a token first — see auth.js)
//   node sehatai/webserver.js
// Then open http://localhost:3000 in a browser and paste the token in
// when prompted (see public/index.html).
//
// For local development ONLY, set SEHATAI_ALLOW_UNAUTHENTICATED=true in
// .env to skip the token check and fall back to DEFAULT_PATIENT_ID —
// never set this in anything reachable outside your own machine.
//
// Requires the SAME environment (.env with Infermedica/Groq/Supabase
// credentials) as testChat.js — this doesn't add any new required env
// vars beyond the optional dev-mode flag above. DIETBOT_API_URL must
// point at a running DietBot service for diet-mode messages to get a
// real answer; if it's not running, diet messages get the same
// graceful "having trouble reaching the diet assistant" fallback
// testChat.js shows, not a crash.
// ============================================

import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { processPatientMessage, processDietMessage } from './processMessage.js';
import { verifyApiToken, extractBearerToken } from './auth.js';
import { getOrResumeSession, getOrResumeDietSession } from './chatLog.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT) || 3000;
const DEFAULT_PATIENT_ID = process.argv[2] || '398c2344-975e-44ad-b483-d1375e1376c0'; // <-- replace with your real seeded patient id
const ALLOW_UNAUTHENTICATED = process.env.SEHATAI_ALLOW_UNAUTHENTICATED === 'true';

if (ALLOW_UNAUTHENTICATED) {
  console.warn(
    '⚠️  SEHATAI_ALLOW_UNAUTHENTICATED=true — every request is treated as ' +
    `patient ${DEFAULT_PATIENT_ID} with NO token check. This is a local-dev-only ` +
    'escape hatch. Never set this in anything reachable outside your own machine.'
  );
}

// ------------------------------------------------------------------
// Body size cap — an unbounded readJsonBody would buffer an
// attacker-supplied request of arbitrary size straight into memory
// before ever validating anything about it. 64KB is generous for a
// chat message.
// ------------------------------------------------------------------
const MAX_BODY_BYTES = 64 * 1024;

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    let bytes = 0;
    let rejected = false;
    req.on('data', (chunk) => {
      if (rejected) return;
      bytes += chunk.length;
      if (bytes > MAX_BODY_BYTES) {
        rejected = true;
        reject(Object.assign(new Error('Request body too large'), { statusCode: 413 }));
        req.destroy();
        return;
      }
      raw += chunk;
    });
    req.on('end', () => {
      if (rejected) return;
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(Object.assign(new Error('Invalid JSON body'), { statusCode: 400 }));
      }
    });
    req.on('error', (err) => {
      if (!rejected) reject(err);
    });
  });
}

// ------------------------------------------------------------------
// Rate limiting — fixed-window counter per auth key (token, or the
// caller's IP when running unauthenticated in dev mode). This app
// makes multiple paid Groq calls per message (more now than before —
// see the safety-gate hardening in processMessage.js/safetyCheck.js),
// so an unthrottled endpoint is both a cost risk and a trivial DoS
// vector. This is intentionally simple (in-memory, single-process,
// resets on restart) — matching the rest of this app's current
// single-instance architecture — not a distributed rate limiter; if
// this ever runs behind a load balancer with multiple instances, this
// needs to move to a shared store (e.g. a Supabase/Redis-backed
// counter) instead.
// ------------------------------------------------------------------
const RATE_LIMIT_WINDOW_MS = 60 * 1000;
const RATE_LIMIT_MAX_REQUESTS = 20; // per key, per window
const rateLimitBuckets = new Map(); // key -> { count, windowStart }

function checkRateLimit(key) {
  const now = Date.now();
  const bucket = rateLimitBuckets.get(key);
  if (!bucket || now - bucket.windowStart > RATE_LIMIT_WINDOW_MS) {
    rateLimitBuckets.set(key, { count: 1, windowStart: now });
    return true;
  }
  bucket.count += 1;
  return bucket.count <= RATE_LIMIT_MAX_REQUESTS;
}

// Sweep old buckets periodically so this Map doesn't grow forever.
setInterval(() => {
  const now = Date.now();
  for (const [key, bucket] of rateLimitBuckets.entries()) {
    if (now - bucket.windowStart > RATE_LIMIT_WINDOW_MS) rateLimitBuckets.delete(key);
  }
}, 5 * 60 * 1000);

function sendJson(res, statusCode, body) {
  res.writeHead(statusCode, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

/**
 * Resolves which patientId a request is authorized to act as. Returns
 * null (and the caller should respond 401) if authentication fails.
 * The request body's `patientId` field, if present, is IGNORED here on
 * purpose — see the file-level doc comment for why trusting it was the
 * actual vulnerability being fixed.
 *
 * @param {http.IncomingMessage} req
 * @returns {Promise<string|null>}
 */
async function authenticate(req) {
  if (ALLOW_UNAUTHENTICATED) return DEFAULT_PATIENT_ID;
  const token = extractBearerToken(req.headers['authorization']);
  if (!token) return null;
  return verifyApiToken(token);
}

// MAX_BODY_BYTES above only bounds how much DATA a request can send —
// it does nothing to bound HOW LONG a request can take to send it. A
// client that trickles bytes in slowly, staying under the cap forever
// and never sending a final chunk, would otherwise hold a socket (and
// this handler's data/end listeners) open indefinitely — a slow-loris-
// style resource exhaustion the byte cap alone doesn't close.
// requestTimeout/headersTimeout are Node's own built-in bounds on
// total request time and time-to-complete-headers respectively.
const REQUEST_TIMEOUT_MS = 30 * 1000;
const HEADERS_TIMEOUT_MS = 10 * 1000;

// Architecture doc §04 -- until now nothing here sent an
// Access-Control-Allow-Origin header at all, so a browser call from
// CareLink's frontend (a different origin/port) would be silently
// blocked by same-origin policy before this app's own token check ever
// ran. Auth here is a Bearer token in a header, never a cookie, so this
// is safe to allow without allow_credentials (same reasoning CareLink's
// own main.py CORS setup already uses) — but still scoped to a real
// origin, not '*', since unlike a token-verified request this reflects
// straight into a response header. Configure via CORS_ORIGIN in .env;
// defaults to CareLink's planned dev port (see architecture doc §01).
const CORS_ORIGIN = process.env.CORS_ORIGIN || 'http://localhost:3002';

const server = http.createServer({
  requestTimeout: REQUEST_TIMEOUT_MS,
  headersTimeout: HEADERS_TIMEOUT_MS,
}, async (req, res) => {
  try {
    res.setHeader('Access-Control-Allow-Origin', CORS_ORIGIN);
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    if (req.method === 'GET' && (req.url === '/' || req.url === '/index.html')) {
      const html = await readFile(path.join(__dirname, 'public', 'index.html'), 'utf8');
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html.replace('__DEFAULT_PATIENT_ID__', DEFAULT_PATIENT_ID));
      return;
    }

    // Separate page, separate script context — see public/diet.html's
    // header comment. Its `sessionId` lives in this page's own JS
    // memory, so switching between symptom and diet consoles means
    // navigating to a different page rather than flipping a button on
    // one shared page, which is what let one shared sessionId (and the
    // session-scoped chat_session_state it points at — accumulated
    // symptoms, pending-question flags, emergency-acknowledgment state)
    // leak across modes before this.
    if (req.method === 'GET' && req.url === '/diet') {
      const html = await readFile(path.join(__dirname, 'public', 'diet.html'), 'utf8');
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html.replace('__DEFAULT_PATIENT_ID__', DEFAULT_PATIENT_ID));
      return;
    }

    if (req.method === 'POST' && req.url === '/api/chat') {
      const patientId = await authenticate(req);
      if (!patientId) {
        sendJson(res, 401, { error: 'Missing or invalid API token. See issuetoken.js.' });
        return;
      }

      // Rate limit AFTER auth succeeds, keyed by the authenticated
      // token when present (falls back to remote address only in the
      // explicit dev-mode escape hatch, where there's no token to key
      // on) — keying by token rather than IP avoids one shared IP
      // (e.g. behind NAT) starving out unrelated patients, and avoids
      // an unauthenticated client controlling its own rate-limit key.
      const rateLimitKey = ALLOW_UNAUTHENTICATED
        ? `ip:${req.socket.remoteAddress}`
        : `token:${extractBearerToken(req.headers['authorization'])}`;
      if (!checkRateLimit(rateLimitKey)) {
        sendJson(res, 429, { error: 'Too many requests. Please slow down and try again shortly.' });
        return;
      }

      let body;
      try {
        body = await readJsonBody(req);
      } catch (err) {
        sendJson(res, err.statusCode || 400, { error: err.message || 'Invalid request body' });
        return;
      }

      const { mode, message, newSession } = body || {};
      if (!message || typeof message !== 'string' || !message.trim()) {
        sendJson(res, 400, { error: 'message is required' });
        return;
      }

      // patientId comes ONLY from the authenticated token above — a
      // patientId field in the body, if the client sent one, is simply
      // never read. sessionId works the same way now — the server
      // resolves which session to use (resume the active one, or start
      // fresh if it's stale/missing/explicitly requested) rather than
      // trusting whatever a client sends; see getOrResumeSession /
      // getOrResumeDietSession in chatLog.js for the 24h resume/expiry
      // logic this implements. A client no longer needs to track or send
      // its own sessionId at all — only `newSession: true` when the
      // patient explicitly wants to start over.
      const resumed = mode === 'diet'
        ? await getOrResumeDietSession(patientId, { forceNew: newSession === true })
        : await getOrResumeSession(patientId, { forceNew: newSession === true });

      const result = mode === 'diet'
        ? await processDietMessage(message, patientId, resumed.sessionId)
        : await processPatientMessage(message, patientId, resumed.sessionId);

      sendJson(res, 200, result);
      return;
    }

    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  } catch (err) {
    // Full detail goes to the server's own log only — never back to the
    // client. Returning err.message here used to leak internal details
    // (stack fragments, Supabase/API error text, file paths) to
    // whoever happened to trigger the failure.
    console.error('[webServer] request failed:', err);
    sendJson(res, 500, { error: 'Internal server error. Please try again.' });
  }
});

server.listen(PORT, () => {
  console.log(`SehatAI web UI running at http://localhost:${PORT}`);
  if (ALLOW_UNAUTHENTICATED) {
    console.log(`(dev mode — unauthenticated, treated as patient ${DEFAULT_PATIENT_ID})`);
  } else {
    console.log('Requests require an API token — issue one with: node sehatai/issuetoken.js <patientId>');
  }
  console.log('(same backend as `npm run chat` — this is just a browser front-end for it)');
});