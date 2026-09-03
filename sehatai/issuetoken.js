// ============================================
// SehatAI: API Token Issuance (operator CLI utility)
//
// Run this out-of-band (never from inside the web server itself) to
// mint a new bearer token for a specific patient. The raw token is
// printed ONCE — copy it somewhere safe immediately. Only its SHA-256
// hash is ever stored (see auth.js), so if you lose the raw value the
// only fix is issuing a new token (and optionally revoking the old
// one — see revokeToken.js).
//
// Usage:
//   node issueToken.js <patientId>
//
// Requires the `patient_api_tokens` table — see auth.js's doc comment
// for the create-table SQL.
// ============================================

import { issueApiToken } from './auth.js';

async function main() {
  const patientId = process.argv[2];
  if (!patientId) {
    console.error('Usage: node issueToken.js <patientId>');
    process.exit(1);
  }

  const token = await issueApiToken(patientId);
  console.log('\nNew API token issued for patient:', patientId);
  console.log('Token (copy this now — it will never be shown again):\n');
  console.log(token);
  console.log('\nClient requests must send it as:');
  console.log(`  Authorization: Bearer ${token}\n`);
}

main().catch((err) => {
  console.error('Failed to issue token:', err.message);
  process.exit(1);
});