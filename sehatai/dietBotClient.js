// ============================================
// SehatAI: Diet Bot Client
// Thin HTTP wrapper around the teammate's separate Python/FastAPI diet
// service (api.py — run standalone via `uvicorn api:app --port 8001`,
// see dietbot-integration-architecture.md). Same fetch-based client
// pattern as infermedicaClient.js: SehatAI never imports or runs any
// Python code directly, it just calls this one HTTP endpoint.
//
// The diet bot's endpoint contract (from its own api.py):
//   POST /diet  { patient_id, query, session_id? }
//   -> { reply, session_id }
// ============================================

import 'dotenv/config';

const DIETBOT_API_URL = process.env.DIETBOT_API_URL || 'http://localhost:8001';

/**
 * Calls the diet bot's POST /diet endpoint.
 *
 * @param {{patientId: string, query: string, sessionId?: string|null}} params
 * @returns {Promise<{reply: string, session_id: string|null}>}
 */
export async function getDietResponse({ patientId, query, sessionId = null }) {
  const response = await fetch(`${DIETBOT_API_URL}/diet`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      patient_id: patientId,
      query,
      session_id: sessionId || null,
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text().catch(() => '');
    throw new Error(`DietBot /diet failed (${response.status}): ${errorBody}`);
  }

  return response.json();
}