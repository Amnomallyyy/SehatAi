// ============================================
// SehatAI: API Token Authentication
//
// WHY THIS EXISTS: webServer.js's /api/chat endpoint used to take
// `patientId` straight from the untrusted request body with zero
// verification —
//   const pid = patientId || DEFAULT_PATIENT_ID;
// — meaning anyone who could reach the endpoint could pass ANY
// patient's real UUID and get a recommendation grounded in THEIR real
// profile: allergies, medications, lab values, prior conditions
// (getPatientProfile / getPatientHistory / getRelevantLabValues all
// just trust whatever id arrives). That's a textbook IDOR against real
// health data, not a cosmetic gap.
//
// This is intentionally NOT a full user-account system (no signup,
// login form, password reset, etc.) — that's a much bigger feature
// this app doesn't have anywhere else yet. What it IS: a per-patient
// bearer token, generated out-of-band (see issueToken.js) and checked
// on every request, so the server — not the client — is what decides
// which patientId a request is allowed to act as. That's the actual
// vulnerability (client-supplied identity) and this closes exactly
// that, without pretending to be more than it is.
//
// Tokens are stored HASHED (SHA-256), never in plaintext, following
// the same principle as a password hash: even a full database read
// only exposes hashes, not usable credentials. The raw token is shown
// to the operator exactly once, at issuance time (see issueToken.js).
//
// Schema (create this table in Supabase before using auth):
//
//   create table if not exists patient_api_tokens (
//     token_hash text primary key,
//     patient_id uuid not null references patients(id),
//     created_at timestamptz not null default now(),
//     revoked boolean not null default false
//   );
//   create index if not exists patient_api_tokens_patient_id_idx
//     on patient_api_tokens (patient_id);
// ============================================

import crypto from 'node:crypto';
import { supabase } from './supabaseClient.js';

const TOKEN_BYTES = 32; // 256 bits of entropy — not brute-forceable

/**
 * SHA-256 hex digest of a raw token string. Deterministic (same input
 * -> same hash), so it doubles as the lookup key: we never need to
 * store or compare raw tokens, only hashes.
 *
 * @param {string} rawToken
 * @returns {string}
 */
export function hashToken(rawToken) {
  return crypto.createHash('sha256').update(String(rawToken), 'utf8').digest('hex');
}

/**
 * Generates a new random API token for a patient and stores its HASH
 * in Supabase. The raw token is returned exactly once here — the
 * caller (issueToken.js) is responsible for showing/saving it; nothing
 * in this app can ever recover it again afterward (by design — the
 * database only ever holds the hash).
 *
 * @param {string} patientId
 * @returns {Promise<string>} the raw token — show this to the
 *   operator/patient ONCE; it cannot be retrieved again
 */
export async function issueApiToken(patientId) {
  if (!patientId) throw new Error('issueApiToken requires a patientId');

  const rawToken = crypto.randomBytes(TOKEN_BYTES).toString('base64url');
  const tokenHash = hashToken(rawToken);

  const { error } = await supabase
    .from('patient_api_tokens')
    .insert([{ token_hash: tokenHash, patient_id: patientId }]);

  if (error) {
    throw new Error(`Failed to store new API token: ${error.message}`);
  }

  return rawToken;
}

/**
 * Verifies a raw bearer token against the stored hash and returns the
 * patientId it's bound to, or null if the token is missing, unknown,
 * or revoked. This is the ONLY function that should ever be trusted to
 * decide "which patient is this request allowed to act as" — never the
 * patientId in a request body, which is attacker-controlled.
 *
 * @param {string|null|undefined} rawToken
 * @returns {Promise<string|null>}
 */
export async function verifyApiToken(rawToken) {
  if (!rawToken || typeof rawToken !== 'string') return null;

  const tokenHash = hashToken(rawToken.trim());

  try {
    const { data, error } = await supabase
      .from('patient_api_tokens')
      .select('patient_id, revoked')
      .eq('token_hash', tokenHash)
      .maybeSingle();

    if (error) {
      console.error('[auth] token lookup failed:', error.message);
      return null;
    }
    if (!data || data.revoked) return null;
    return data.patient_id;
  } catch (err) {
    console.error('[auth] token lookup threw:', err.message);
    return null;
  }
}

/**
 * Revokes a token so it can no longer authenticate — used when a token
 * leaks or a patient wants theirs invalidated. Revoking rather than
 * deleting keeps an audit trail of what existed.
 *
 * @param {string} rawToken
 */
export async function revokeApiToken(rawToken) {
  if (!rawToken) return;
  const tokenHash = hashToken(rawToken.trim());
  const { error } = await supabase
    .from('patient_api_tokens')
    .update({ revoked: true })
    .eq('token_hash', tokenHash);
  if (error) {
    console.error('[auth] revoke failed:', error.message);
  }
}

/**
 * Pulls a bearer token out of a standard `Authorization: Bearer <token>`
 * header. Returns null for anything else (missing header, wrong scheme).
 *
 * @param {string|undefined} authorizationHeader
 * @returns {string|null}
 */
export function extractBearerToken(authorizationHeader) {
  if (!authorizationHeader || typeof authorizationHeader !== 'string') return null;
  const match = authorizationHeader.match(/^Bearer\s+(.+)$/i);
  return match ? match[1].trim() : null;
}