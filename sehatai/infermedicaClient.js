// ============================================
// SehatAI: Infermedica Client (Stateless Engine API)
// Uses Infermedica's native specialist recommendation
// endpoint instead of manual condition mapping.
//
// FIXED (see extractSpecialists below): the real /recommend_specialist
// response shape, per Infermedica's docs, is a SINGULAR top-level
// "recommended_specialist" object — { recommended_specialist: { id,
// name }, recommended_channel }. The previous extraction logic checked
// .specialist, .specialists (plural), and top-level .name — none of
// which exist on the real response — so it silently fell through to
// the ['General Physician'] fallback on every single call, regardless
// of the actual condition. This was invisible in normal use because
// General Physician is always a plausible-looking answer.
// ============================================

import 'dotenv/config';

const INFERMEDICA_API_URL = 'https://api.infermedica.com/v3';

// ------------------------------------------------------------------
// HELPER: Get headers for Infermedica API
// ------------------------------------------------------------------
function getHeaders() {
  const appId = process.env.INFERMEDICA_APP_ID;
  const appKey = process.env.INFERMEDICA_APP_KEY;

  if (!appId || !appKey) {
    throw new Error(
      'Infermedica: Missing APP_ID or APP_KEY in environment variables.\n' +
      'Get your credentials at https://developer.infermedica.com/'
    );
  }

  return {
    'App-Id': appId,
    'App-Key': appKey,
    'Content-Type': 'application/json',
  };
}

// ------------------------------------------------------------------
// 1. CORE API FUNCTIONS
// ------------------------------------------------------------------

/**
 * Parse free text to extract symptom mentions
 * Endpoint: POST /parse
 */
export async function parsePatientMessage(text, age = 30) {
  const response = await fetch(`${INFERMEDICA_API_URL}/parse`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({
      text,
      age: { value: Number(age) },
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    throw new Error(`Infermedica /parse failed (${response.status}): ${errorBody}`);
  }

  const data = await response.json();
  return data.mentions || [];
}

/**
 * Get diagnosis (conditions, lab tests, observations)
 * Endpoint: POST /diagnosis
 */
export async function getDiagnosisStep({ age = 30, sex = 'female', evidence = [] }) {
  const response = await fetch(`${INFERMEDICA_API_URL}/diagnosis`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({
      sex,
      age: { value: Number(age) },
      evidence,
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    throw new Error(`Infermedica /diagnosis failed (${response.status}): ${errorBody}`);
  }

  return response.json();
}

/**
 * Get triage assessment (emergency/urgent/routine)
 * Endpoint: POST /triage
 */
export async function getTriageAssessment({ age = 30, sex = 'female', evidence = [] }) {
  const response = await fetch(`${INFERMEDICA_API_URL}/triage`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({
      sex,
      age: { value: Number(age) },
      evidence,
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    throw new Error(`Infermedica /triage failed (${response.status}): ${errorBody}`);
  }

  return response.json();
}

/**
 * Get recommended specialist (NATIVE Infermedica endpoint)
 * Endpoint: POST /recommend_specialist
 *
 * This is the CORRECT way to get specialists from Infermedica.
 * No manual condition → specialist mapping needed.
 */
export async function getRecommendedSpecialist({ age = 30, sex = 'female', evidence = [] }) {
  const response = await fetch(`${INFERMEDICA_API_URL}/recommend_specialist`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify({
      sex,
      age: { value: Number(age) },
      evidence,
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    throw new Error(`Infermedica /recommend_specialist failed (${response.status}): ${errorBody}`);
  }

  return response.json();
}

// ------------------------------------------------------------------
// 2. INTEGRATION HELPERS
// ------------------------------------------------------------------
//
// NOTE: this file used to also expose /suggest (getNextQuestion) and a
// /diagnosis-driven interview loop (getDiagnosisInterviewStep,
// buildQuestionFromDiagnosisQuestion, interpretPendingAnswer) for
// deciding WHAT clarifying question to ask a patient. Per a deliberate
// architecture decision, Infermedica no longer has any say in what
// question gets asked — see clarificationCheck.js's
// generateClarifyingQuestion (Groq-composed, informed only by this
// app's own accumulated symptom names) and processMessage.js's STAGE
// 7b. Those functions have been removed rather than left as dead code.
// Infermedica is still used for everything else here: /parse (evidence
// extraction), /triage, /recommend_specialist, and /diagnosis (now
// purely for lab-test suggestions, via getTriageForEvidence's internal
// fallback below — its question/should_stop/has_emergency_evidence
// fields are simply never read anymore).

/**
 * Convert Infermedica's real triage_level to this app's coarse
 * emergency/urgent/routine bucket.
 *
 * FIXED: this table previously only had keys "emergency", "urgent", and
 * "routine" — none of which are values Infermedica's /triage endpoint
 * actually returns. Per Infermedica's docs, triage_level is one of FIVE
 * values: "emergency_ambulance", "emergency", "consultation_24",
 * "consultation", "self_care". That meant "emergency_ambulance" — the
 * single most severe outcome, meaning the patient may need to call an
 * ambulance right now — fell through to the `|| 'routine'` default and
 * was silently treated as a routine, non-emergency case. "consultation"
 * and "consultation_24" fell through the same way. Only literal
 * "emergency" ever matched. This is now fixed to map all five real
 * values; the old "urgent"/"routine" keys are kept too in case anything
 * ever calls this with an already-bucketed value.
 *
 * See getTriageForEvidence() below for the raw triage_level (and
 * emergency_ambulance vs. emergency distinction) preserved alongside
 * this bucket, and describeChannel()/CONSULTATION_NOTES for the
 * consultation-type and self-care guidance this unlocks.
 */
export function mapTriageToUrgency(triageLevel) {
  const map = {
    emergency_ambulance: 'emergency',
    emergency: 'emergency',
    consultation_24: 'urgent',
    consultation: 'urgent',
    self_care: 'routine',
    // legacy/defensive — not real Infermedica values, kept in case an
    // already-bucketed urgency ever gets passed back through here
    urgent: 'urgent',
    routine: 'routine',
  };
  return map[triageLevel] || 'routine';
}

/**
 * Human-readable guidance keyed by Infermedica's raw triage_level,
 * for the granular cases the coarse urgency bucket above collapses:
 * "emergency" vs. "emergency_ambulance" both bucket to 'emergency', and
 * "consultation" vs. "consultation_24" vs. "self_care" all bucket to
 * 'urgent'/'routine'. processMessage.js uses these to add one
 * deterministic, templated sentence on top of the LLM's reply — never
 * fed to the LLM itself, same pattern as buildCrossDomainNote().
 */
export const TRIAGE_LEVEL_NOTES = {
  emergency_ambulance:
    '🚨 This may require an ambulance. Please call your local emergency number right now — do not wait or try to drive yourself.',
  emergency:
    '🚨 This may be a medical emergency. Please go to the nearest emergency room now, or call your local emergency number if you cannot get there safely on your own.',
  consultation_24: 'You should aim to see a doctor within the next 24 hours.',
  consultation: null, // the normal case — the specialist recommendation below already covers this
  self_care:
    "Based on what you've described, this often doesn't need a specialist visit — home care and monitoring may be enough. See a doctor if it doesn't improve or gets worse.",
};

/**
 * Human-readable label for /recommend_specialist's recommended_channel —
 * the TYPE of consultation Infermedica suggests (in person vs. remote),
 * separate from urgency. Per Infermedica's docs there are exactly four
 * values.
 *
 * @param {string|null} channel
 * @returns {string|null}
 */
const CHANNEL_LABELS = {
  personal_visit: 'an in-person visit',
  video_teleconsultation: 'a video consultation',
  audio_teleconsultation: 'a phone (audio-only) consultation',
  text_teleconsultation: 'a chat-based consultation',
};
export function describeChannel(channel) {
  return CHANNEL_LABELS[channel] || null;
}

/**
 * Extract lab test names from diagnosis response
 */
export function extractLabTests(diagnosisResponse) {
  const tests = [];

  // Check top-level lab_tests field
  if (diagnosisResponse.lab_tests && Array.isArray(diagnosisResponse.lab_tests)) {
    for (const test of diagnosisResponse.lab_tests) {
      tests.push(test.name || test.id);
    }
  }

  // Check each condition's evidence for lab tests
  if (diagnosisResponse.conditions) {
    for (const condition of diagnosisResponse.conditions) {
      if (condition.evidence) {
        for (const ev of condition.evidence) {
          if (ev.type === 'lab_test' && ev.name) {
            tests.push(ev.name);
          }
        }
      }
    }
  }

  // Check observations
  if (diagnosisResponse.observations) {
    for (const obs of diagnosisResponse.observations) {
      if (obs.type === 'lab_test' && obs.name) {
        tests.push(obs.name);
      }
    }
  }

  return [...new Set(tests)];
}

/**
 * Extracts a specialist name list from a raw /recommend_specialist
 * response. Real shape (per Infermedica's docs):
 *   { recommended_specialist: { id: "sp_7", name: "Ophthalmologist" },
 *     recommended_channel: "personal_visit" }
 * — singular "recommended_specialist", not "specialist"/"specialists".
 * Returns [] (not a fallback) so callers decide the fallback
 * explicitly and it stays visible in logs when it happens, rather than
 * being silently baked into this helper.
 *
 * Exported (was module-private) so processMessage.js's cross-domain
 * complaint check (see getSpecialistNamesForEvidence below) and any
 * other caller can extract from a raw response without duplicating
 * this parsing logic.
 *
 * @param {object|null} specialistRecommendation
 * @returns {string[]}
 */
export function extractSpecialists(specialistRecommendation) {
  const rec = specialistRecommendation?.recommended_specialist;
  if (rec?.name) return [rec.name];
  if (rec?.id) return [rec.id];
  return [];
}

/**
 * @param {object|null} specialistRecommendation - raw /recommend_specialist response
 * @returns {string|null} - raw channel value (e.g. "personal_visit"); see describeChannel() for a human label
 */
export function extractRecommendedChannel(specialistRecommendation) {
  return specialistRecommendation?.recommended_channel || null;
}

/**
 * Runs triage + specialist recommendation + diagnosis for an EXISTING
 * evidence array — no /parse call. This is what processMessage.js uses
 * turn-by-turn, because evidence is now accumulated ACROSS turns (see
 * chatLog.js's getAccumulatedEvidence/mergeEvidenceArrays) — getFullTriage()
 * below always re-parses from a single message, so "headache" on turn 1
 * and "neck pain" on turn 2 would never be considered together if this
 * were used turn-by-turn instead.
 *
 * FIXED: triage/diagnosis/specialist calls used to share one try/catch,
 * so a /recommend_specialist failure (Infermedica explicitly does not
 * support this endpoint for emergency/emergency_ambulance triage
 * levels — see their docs) would land in the catch block and return
 * urgency: 'routine', DISCARDING an already-successfully-computed
 * emergency urgency from the /triage call that ran first. That's a
 * masked-emergency bug, not just a wrong-specialist one. Triage is now
 * isolated in its own try/catch and its urgency is never overwritten
 * by a later step failing. /recommend_specialist is also skipped
 * entirely (not just tolerated) when urgency is emergency, since
 * Infermedica won't serve it anyway.
 *
 * @param {{age:number, sex:string, evidence:Array}} params
 */
export async function getTriageForEvidence({ age = 30, sex = 'female', evidence = [] }) {
  if (!evidence || evidence.length === 0) {
    return {
      triage: null,
      diagnosis: null,
      specialistRecommendation: null,
      specialists: [],
      labTests: [],
      urgency: 'routine',
      triageLevel: null,
      recommendedChannel: null,
      error: 'No evidence',
    };
  }

  // --- Triage: authoritative for urgency. Nothing below is allowed
  // to overwrite this once it succeeds. ---
  let triage;
  let urgency;
  try {
    triage = await getTriageAssessment({ age, sex, evidence });
    urgency = mapTriageToUrgency(triage.triage_level);
  } catch (error) {
    console.error('[Infermedica] getTriageForEvidence: /triage failed:', error.message);
    return {
      triage: null,
      diagnosis: null,
      specialistRecommendation: null,
      specialists: [],
      labTests: [],
      // UPDATED: was 'routine'. Silently defaulting a clinical-engine
      // OUTAGE to the LEAST cautious urgency bucket is the wrong
      // failure mode for a triage app, and this app has no second
      // clinically-validated engine to fall back to (an ad hoc scoring
      // system built here would be worse — unvalidated triage logic
      // dressed up as real triage). 'urgent' is the honest middle
      // ground: not a false emergency alarm, but not a false "this can
      // wait" either, when nothing was actually assessed.
      urgency: 'urgent',
      triageLevel: null,
      recommendedChannel: null,
      // Lets processMessage.js tell "genuinely triaged and it came back
      // low-urgency" apart from "the engine was unreachable, nothing
      // was actually evaluated" — these must never look the same to the
      // patient. See finalizeAndRecommend, which short-circuits to a
      // distinct, honest, deterministic reply when this is true, rather
      // than letting the model narrate a confident-sounding
      // recommendation built on a fabricated specialist placeholder and
      // no real urgency assessment.
      engineUnavailable: true,
      error: error.message,
    };
  }

  // --- Diagnosis, called ONLY for its suggested lab-test names now
  // (see the architecture note above INTEGRATION HELPERS) — its
  // question/should_stop/has_emergency_evidence fields are never read.
  // Non-fatal — a failure here still leaves triage/urgency intact. ---
  let diagnosis = null;
  let labTests = [];
  try {
    diagnosis = await getDiagnosisStep({ age, sex, evidence });
    labTests = extractLabTests(diagnosis);
  } catch (error) {
    console.error('[Infermedica] getTriageForEvidence: /diagnosis failed (non-fatal):', error.message);
  }

  // --- Specialist recommendation. Skipped outright for emergency
  // urgency (Infermedica doesn't serve it) rather than calling it and
  // tolerating a predictable failure. Also non-fatal for any other
  // error — falls back to General Physician, logged so it's visible
  // this happened instead of silently looking like a real routing
  // decision. ---
  let specialistRecommendation = null;
  let specialists = [];
  let recommendedChannel = null;
  if (urgency === 'emergency') {
    console.warn(
      '[Infermedica] Skipping /recommend_specialist — not available for emergency triage levels.'
    );
  } else {
    try {
      specialistRecommendation = await getRecommendedSpecialist({ age, sex, evidence });
      specialists = extractSpecialists(specialistRecommendation);
      recommendedChannel = extractRecommendedChannel(specialistRecommendation);
      if (specialists.length === 0) {
        console.warn(
          '[Infermedica] /recommend_specialist returned no recognizable specialist — raw response:',
          JSON.stringify(specialistRecommendation)
        );
      }
    } catch (error) {
      console.error(
        '[Infermedica] getTriageForEvidence: /recommend_specialist failed (non-fatal, defaulting to General Physician):',
        error.message
      );
    }
  }
  if (specialists.length === 0) specialists = ['General Physician'];

  // Raw triage_level, preserved alongside the coarse `urgency` bucket —
  // this is what lets processMessage.js tell "emergency_ambulance" apart
  // from "emergency", and "self_care"/"consultation_24" apart from a
  // normal "consultation", for the templated notes in TRIAGE_LEVEL_NOTES.
  const triageLevel = triage?.triage_level || null;

  return { triage, diagnosis, specialistRecommendation, specialists, labTests, urgency, triageLevel, recommendedChannel };
}

/**
 * Given a small subset of evidence — typically just THIS turn's fresh
 * /parse mentions, not the whole session's accumulated evidence —
 * returns the specialist name(s) Infermedica would recommend for that
 * subset alone.
 *
 * Why this exists: getTriageForEvidence() above always recommends a
 * specialist for the FULL merged evidence array, and Infermedica's
 * /recommend_specialist only ever returns ONE specialist for whatever
 * evidence it's given. That means once a session has some evidence
 * accumulated, a genuinely unrelated new complaint added on a later
 * turn (e.g. neck pain -> then "I also have stomach pain") gets folded
 * into the SAME specialist recommendation with no signal that it's
 * actually a different issue — the model then either invents a second
 * specialist (blocked by groundingVerifier) or silently drops it.
 *
 * processMessage.js uses this to check the new symptom(s) in isolation
 * and compare the result against the combined-evidence specialist; see
 * its "cross-domain complaint" handling. This does NOT change routing
 * for the session — it only powers a deterministic, templated notice
 * to the patient, never something handed to the LLM to phrase.
 *
 * Non-fatal by design: returns [] on any failure (including empty
 * evidence) so callers can treat "couldn't tell" the same as "no
 * distinct specialist found" — i.e. skip the notice rather than guess.
 *
 * @param {{age:number, sex:string, evidence:Array}} params
 * @returns {Promise<string[]>}
 */
export async function getSpecialistNamesForEvidence({ age = 30, sex = 'female', evidence = [] }) {
  if (!evidence || evidence.length === 0) return [];
  try {
    const rec = await getRecommendedSpecialist({ age, sex, evidence });
    return extractSpecialists(rec);
  } catch (error) {
    console.error('[Infermedica] getSpecialistNamesForEvidence failed (non-fatal):', error.message);
    return [];
  }
}

// ------------------------------------------------------------------
// MAX_CLARIFICATION_ROUNDS — hard safety valve for the Groq-composed
// clarifying-question loop in processMessage.js (STAGE 7b) and
// clarificationCheck.js's checkNeedsClarification. Not tied to any
// Infermedica signal (it used to gate an Infermedica-/diagnosis-driven
// interview loop; that loop and its should_stop/has_emergency_evidence
// dependency are gone — see the architecture note above INTEGRATION
// HELPERS). Exists only so a session that never gives a clean
// duration/severity signal can't be asked forever.
// ------------------------------------------------------------------
export const MAX_CLARIFICATION_ROUNDS = 3;

// ------------------------------------------------------------------
// REMOVED: getFullTriage() used to live here — a "one-stop"
// parse -> triage -> specialist -> diagnosis convenience function for a
// SINGLE message, from before evidence was accumulated across turns.
// It had zero remaining call sites (processMessage.js uses
// getTriageForEvidence() above, over the session's accumulated
// evidence, exclusively) and, left in place, was an active landmine:
// calling it would have silently bypassed the accumulated-evidence
// architecture, the symptom sanity gate, and grounding verification
// entirely — every safety layer built since it was superseded. Deleted
// rather than kept as an unused, misleadingly-named "convenience" export.
// ------------------------------------------------------------------