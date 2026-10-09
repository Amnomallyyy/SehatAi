// ============================================
// SehatAI: Reply Composer
//
// The last step before a reply leaves the triage pipeline (see
// processMessage.js's runPatientMessageTurn). PRODUCT REQUIREMENT: the
// conversation should feel AI-driven and natural, in the patient's own
// language, while the safety nets stay deterministic.
//
//   - FIXED replies (the pipeline's templated questions, refusals,
//     removal/emergency notes, the "So far I have: ... go ahead?"
//     confirmation) are REWRITTEN by the AI in a natural voice, in the
//     language/script the patient is using.
//   - Replies the AI already wrote, plus recommendations and emergency
//     notices, are only TRANSLATED — and only when the patient isn't
//     writing in English. Their content was produced/verified upstream
//     and must not be reworded.
//   - Symptom lists are swapped for placeholders before the AI sees the
//     text and restored verbatim afterwards, so the list the patient
//     confirms is exactly the list sent to the clinical engine.
//   - Every AI output is checked (validateComposed); on any failure the
//     original fixed text is sent instead. A composer problem can never
//     block or change a reply's meaning — it can only fall back.
// ============================================

import { callAIStructured } from './callAi.js';
import { DISMISSIVE_PHRASES } from './groundingVerifier.js';

// Wrap a symptom list in these when building a reply (see listMarker) so
// the composer can protect it. Stripped before anything is sent.
const LIST_OPEN = '⟦';
const LIST_CLOSE = '⟧';
const LIST_RE = /⟦([^⟧]*)⟧/g;

/**
 * Marks an exact symptom list inside a reply so the composer never lets
 * the AI rewrite it.
 * @param {string} text
 * @returns {string}
 */
export function listMarker(text) {
  return `${LIST_OPEN}${text}${LIST_CLOSE}`;
}

/** Removes list markers, leaving the plain text. */
export function stripListMarkers(text) {
  return String(text || '').replace(LIST_RE, '$1');
}

// Common Roman Urdu / Hinglish function words. Two or more in the
// patient's recent messages (or any Urdu-script character) means the
// conversation isn't in English.
const ROMAN_URDU_WORDS = new Set([
  'mujhe', 'mujh', 'mera', 'meri', 'mere', 'hai', 'hain', 'hay', 'nahi', 'nahin', 'nhi', 'aur', 'bhi', 'kya', 'kyun',
  'mein', 'main', 'se', 'ka', 'ki', 'ke', 'ko', 'raha', 'rahi', 'rahe', 'tha', 'thi', 'ho', 'hota', 'hoti',
  'bohat', 'bahut', 'boht', 'dard', 'din', 'haan', 'han', 'ji', 'jee', 'bas', 'abhi', 'kal', 'thora', 'thoda',
  'zyada', 'sar', 'pait', 'bukhar', 'khansi', 'ulti', 'chakkar', 'jalan', 'kuch', 'koi', 'wala', 'wali', 'lag', 'lagta',
]);

/**
 * @param {string[]} patientTexts - recent patient messages, latest last
 * @returns {boolean}
 */
export function looksNonEnglish(patientTexts) {
  const text = (patientTexts || []).join(' ');
  if (/[؀-ۿ]/.test(text)) return true;
  const words = text.toLowerCase().match(/[a-z]+/g) || [];
  let hits = 0;
  for (const w of words) if (ROMAN_URDU_WORDS.has(w)) hits += 1;
  return hits >= 2;
}

// Diagnosis wording a rewrite must never introduce (English checks; the
// prompt carries the rule for other languages).
const DIAGNOSIS_WORDING = [
  /\byou\s+(?:likely\s+|probably\s+)?have\s+(?:a|an)\s+\w+/i,
  /\b(?:sounds|looks)\s+like\s+(?:a|an)\s+\w+/i,
  /\bdiagnos(?:is|ed)\b/i,
];
const EMERGENCY_WORDING = /\b(?:emergency|ambulance|1122|911|hospital right away)\b/i;

/**
 * Deterministic checks on an AI-composed reply. Returns null when it's
 * safe to use, or a short reason it isn't.
 *
 * @param {string} original - the reply before composing (placeholders in place of lists)
 * @param {string} composed - the AI's version (placeholders still in place)
 * @param {string[]} placeholders - e.g. ['{{LIST_1}}']
 * @param {{mode: 'rewrite'|'translate', mustKeep?: string[]}} opts
 * @returns {string|null}
 */
export function validateComposed(original, composed, placeholders, { mode, mustKeep = [] } = {}) {
  const out = String(composed || '').trim();
  if (!out) return 'empty';
  if (out.length > Math.max(500, original.length * 3)) return 'too long';
  for (const p of placeholders) {
    const count = out.split(p).length - 1;
    if (count !== 1) return `symptom list placeholder ${p} appears ${count} times`;
  }
  if (/\{\{[^}]*\}\}/.test(out.replace(/\{\{LIST_\d+\}\}/g, ''))) return 'invented a placeholder';
  if (/[⟦⟧]/.test(out)) return 'contains list markers';
  if (/[?؟]/.test(original) && !/[?؟]/.test(out)) return 'dropped the question';
  // Every number in the original (24 hours, 8/10, 3 days) must survive —
  // Urdu-script digits count as the same number.
  const toWestern = (s) => s.replace(/[۰-۹٠-٩]/g, (d) => String((d.charCodeAt(0) & 0xf)));
  const numbersIn = (s) => (toWestern(s).replace(/\{\{LIST_\d+\}\}/g, '').match(/\d+(?:[./]\d+)?/g) || []);
  const outNumbers = new Set(numbersIn(out));
  const lostNumber = numbersIn(original).find((n) => !outNumbers.has(n));
  if (lostNumber) return `changed or dropped the number ${lostNumber}`;
  for (const keep of mustKeep) {
    if (keep && !out.toLowerCase().includes(String(keep).toLowerCase())) return `dropped "${keep}"`;
  }
  if (mode === 'rewrite') {
    for (const re of DIAGNOSIS_WORDING) if (re.test(out) && !re.test(original)) return 'added diagnosis wording';
    for (const re of DISMISSIVE_PHRASES) if (re.test(out) && !re.test(original)) return 'added reassurance';
    if (EMERGENCY_WORDING.test(out) && !EMERGENCY_WORDING.test(original)) return 'added emergency wording';
  }
  return null;
}

const COMPOSE_SCHEMA = {
  type: 'object',
  properties: { reply: { type: 'string' } },
  required: ['reply'],
};

const REWRITE_SYSTEM = `You rewrite ONE message from a medical-intake chat assistant so it reads naturally and warmly, like a caring person talking — not like a form. The assistant helps patients figure out which kind of doctor to see; it never diagnoses.

LANGUAGE: write in the same language and script the patient is using (see recent_patient_messages): English, Urdu script, Roman Urdu, or a natural mix. If they write in English, write English.

KEEP THE MEANING EXACTLY:
- Every question, instruction, refusal, warning and fact in the original must still be there. If the original asks something, your version asks the same thing.
- Add NOTHING: no medical information, advice, reassurance, causes, conditions or diagnoses, no new questions, no new symptoms. Do not change any number.
- If the original declines to diagnose, says it only helps with health symptoms, or tells them to get help, keep that just as clearly.
- Placeholders like {{LIST_1}} stand for an exact list of the patient's symptoms. Keep each one EXACTLY ONCE, unchanged, where the list belongs. Never write the list out yourself, never translate or reword a placeholder.
- Keep it about as short as the original; vary the wording so it doesn't sound canned.

SMALL TALK (only when message_kind is "greeting"): first respond to what the patient actually said in a few natural words — return their greeting or salaam, say you're doing well if they asked how you are, say "you're welcome" if they thanked you — then continue with the original message. Never claim to have feelings beyond a friendly "doing well".

Return JSON: {"reply": "..."}`;

const TRANSLATE_SYSTEM = `Translate ONE message from a medical-intake chat assistant into the language and script the patient is using (see recent_patient_messages): Urdu script, Roman Urdu, or a natural mix — match how they write.

Translate faithfully: keep every instruction, warning, recommendation and fact; add nothing; remove nothing; no extra reassurance or advice. Keep doctor/specialist names in English exactly as written (you may add a short translation in brackets after them). Placeholders like {{LIST_1}} must stay exactly once each, unchanged.

Return JSON: {"reply": "..."}`;

/**
 * Rewrites or translates `reply` per the policy at the top of this file.
 * Never throws: any failure returns the original (list markers stripped).
 *
 * @param {{
 *   reply: string,
 *   mode: 'rewrite'|'translate'|'none',
 *   recentPatientMessages: string[],
 *   kind?: string,
 *   mustKeep?: string[],
 * }} params
 * @returns {Promise<{text: string, composed: boolean, reason?: string}>}
 */
export async function composeReply({ reply, mode, recentPatientMessages = [], kind = null, mustKeep = [] }) {
  const original = String(reply || '');
  const plain = stripListMarkers(original);
  if (!original.trim() || mode === 'none') return { text: plain, composed: false };

  const lists = [];
  const withPlaceholders = original.replace(LIST_RE, (_, list) => {
    lists.push(list);
    return `{{LIST_${lists.length}}}`;
  });
  const placeholders = lists.map((_, i) => `{{LIST_${i + 1}}}`);

  try {
    const parsed = await callAIStructured({
      system: mode === 'translate' ? TRANSLATE_SYSTEM : REWRITE_SYSTEM,
      message: JSON.stringify({
        original_message: withPlaceholders,
        message_kind: kind,
        recent_patient_messages: recentPatientMessages.slice(-3),
      }),
      schema: COMPOSE_SCHEMA,
    });
    const composed = String(parsed?.reply || '').trim();
    const problem = validateComposed(withPlaceholders, composed, placeholders, { mode, mustKeep });
    if (problem) {
      console.warn(`[replyComposer] ${mode} rejected (${problem}) — sending the original text.`);
      return { text: plain, composed: false, reason: problem };
    }
    let text = composed;
    placeholders.forEach((p, i) => { text = text.replace(p, lists[i]); });
    return { text, composed: true };
  } catch (err) {
    console.warn('[replyComposer] AI call failed — sending the original text:', err.message);
    return { text: plain, composed: false, reason: 'ai_failed' };
  }
}

const TRANSLATE_ONLY_KINDS = new Set(['recommendation', 'emergency']);
const NEVER_COMPOSE_KINDS = new Set(['error']);

/**
 * Decides how a pipeline reply should be composed.
 *
 * @param {{kind: string, aiWritten?: boolean}} result
 * @param {boolean} nonEnglish - see looksNonEnglish
 * @returns {'rewrite'|'translate'|'none'}
 */
export function composeModeFor(result, nonEnglish) {
  if (!result || NEVER_COMPOSE_KINDS.has(result.kind)) return 'none';
  if (result.aiWritten || TRANSLATE_ONLY_KINDS.has(result.kind)) return nonEnglish ? 'translate' : 'none';
  return 'rewrite';
}
