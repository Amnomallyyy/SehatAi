// ============================================
// SehatAI: Chat Log & Session Management
//
// COMPLIANCE POLICY (non-negotiable): no value that originated from an
// Infermedica API response is ever kept past the single request/
// response cycle that produced it. Not in the database (nothing in this
// file writes to it at all — sessions are RAM-only, see SESSION_TTL_MS),
// and NOT in RAM either, across turns.
//
// ARCHITECTURE (tightened further — explicit decision): Infermedica
// isn't just kept out of storage, it's kept out of the conversation
// entirely until a recommendation is actually being produced. While
// the patient is describing symptoms and Groq is asking follow-up
// questions, ZERO Infermedica calls happen — everything is gathered
// into `accumulatedSymptoms` below, which holds only OUR OWN Groq
// classification of what the patient said (see symptomClassifier.js),
// never anything Infermedica returned and never the patient's literal
// sentence. Infermedica is called exactly once per "round" — when
// Groq has asked "is there anything else before I recommend a
// specialist?" and the patient says no — to turn that accumulated
// list into real evidence and get a real triage/specialist/lab-test
// answer. See processMessage.js's STAGE 7 for the full state machine.
//
// What this file keeps in RAM between turns:
//   - accumulatedSymptoms — an array of {term, present, duration,
//     severity, finalized} objects, one per distinct symptom Groq has
//     extracted so far this session. `finalized: true` marks a symptom
//     that was already included in a completed recommendation, so a
//     LATER addition (after the patient already got one answer) can be
//     told apart from what's new — without ever storing anything
//     Infermedica itself returned (see processMessage.js's finalize
//     step for how the cross-domain check uses this split).
//   - unaccountedComplaints — complaints Infermedica's /parse couldn't
//     map to anything, discovered only at finalize time now (since
//     /parse itself only runs at finalize time) and normalized through
//     symptomClassifier.js's normalizeComplaint before storage.
//   - this app's own bookkeeping: clarificationCount (a plain integer,
//     shared safety-valve counter for both "targeted" clarifying
//     questions and the final "anything else?" gate);
//     awaitingClarificationAnswer / awaitingFinalConfirmation (plain
//     booleans marking which kind of question was just asked);
//     lastSubject (Groq's subject-detection result, not Infermedica's).
// ============================================

import { randomUUID } from 'crypto';
import { shareSynonymWord } from './symptomSynonyms.js';

// PRODUCT REQUIREMENT: nothing about a chat is stored in the database.
// A session lives in this process's RAM only, for 24 hours since its
// last activity, and is deleted immediately when the patient starts a
// new one (see getOrResumeSession / endPatientSessions). Trade-off,
// accepted on purpose: a server restart ends every open conversation.
const SESSION_TTL_MS = 24 * 60 * 60 * 1000;
const INACTIVITY_TIMEOUT_MS = SESSION_TTL_MS;

// ---- In-memory store (RAM only) ----
// sessionId -> { accumulatedSymptoms: [...], lastSubject, ... }
const sessionExtraStore = new Map();

// `${patientId}|${mode}` -> { sessionId, dietSessionId, lastActivity } —
// which session a patient's next symptom/diet message resumes.
const activeSessionPointers = new Map();

/**
 * Sweeps RAM every 5 minutes to purge sessions inactive > 24h.
 *
 * .unref() so this timer never by itself keeps the Node process alive.
 * A real long-running server (webServer.js) has other listeners doing
 * that anyway; a short-lived script — testChat.js, a one-off diagnostic,
 * a future test runner — that just imports this module for its exported
 * functions would otherwise hang forever after its own work is done,
 * with no visible error, only a process that never exits. This was live
 * long enough to matter: a 3-message driver script ran to completion in
 * under 2 minutes and then sat there for 30+ more with no output, purely
 * because of this dangling interval.
 */
setInterval(() => {
  const now = Date.now();
  for (const [sessionId, extra] of sessionExtraStore.entries()) {
    if (now - extra.lastAccessed > INACTIVITY_TIMEOUT_MS) {
      sessionExtraStore.delete(sessionId);
    }
  }
  for (const [key, pointer] of activeSessionPointers.entries()) {
    if (now - pointer.lastActivity > SESSION_TTL_MS) activeSessionPointers.delete(key);
  }
}, 5 * 60 * 1000).unref();

/**
 * The default shape of a session's RAM-only extra state.
 */
function defaultExtra() {
  return {
    accumulatedSymptoms: [],
    // Chronic conditions/risk factors mentioned live in chat this
    // session ("I have diabetes", "I'm pregnant") — kept SEPARATE from
    // accumulatedSymptoms (a condition is not a bodily complaint) and
    // separate from profileFacts (that's stored Supabase data; this is
    // what the patient said in THIS conversation). See
    // getMentionedConditions/appendMentionedConditions below and
    // processMessage.js's use of it to widen getRiskFactorEvidence's
    // input beyond just stored profile data.
    mentionedConditions: [],
    lastSubject: null,
    clarificationCount: 0,
    // True for exactly one turn: the turn right after Groq asked a
    // TARGETED clarifying question (duration/severity/associated
    // symptom — see clarificationCheck.js's generateClarifyingQuestion).
    awaitingClarificationAnswer: false,
    // See getLastQuestionAsked/setLastQuestionAsked below — only
    // meaningful while awaitingClarificationAnswer is true.
    lastQuestionAsked: null,
    // Plain symptom terms the pending question proposed (e.g. "joint
    // swelling", "joint redness") — see getLastQuestionCandidates.
    lastQuestionCandidates: [],
    // The recorded symptom the pending question is about, if it's about
    // one — see getLastQuestionSymptom.
    lastQuestionSymptom: null,
    // Already-finalized symptom terms the patient re-opened in the
    // current round — see getReopenedTerms.
    reopenedTerms: [],
    // True for exactly one turn: the turn right after Groq asked the
    // FINAL "is there anything else before I recommend a specialist?"
    // gate. Distinct from awaitingClarificationAnswer so
    // processMessage.js's STAGE 7 knows which kind of question is
    // being answered.
    awaitingFinalConfirmation: false,
    // True for exactly one turn: the turn right after this app asked
    // the OTHER meta-question it generates — the disambiguation re-ask
    // for a bare ambiguous number ("does '7' mean 7 days, or a severity
    // of 7 out of 10?" — see symptomClassifier.js's ambiguous rule).
    // Kept distinct from awaitingClarificationAnswer specifically so a
    // reply to THIS question is resolved by its own dedicated
    // classifier (resolveDisambiguationAnswer, clarificationCheck.js)
    // instead of the general-purpose classifySymptoms — see that
    // function's doc comment for why routing the bot's own
    // meta-questions through a classifier tuned for real clinical
    // questions was the root cause behind several false-emergency bugs.
    awaitingDisambiguationAnswer: false,
    // The exact ambiguous bare value ("7", "seven") the pending
    // disambiguation question is about — only meaningful for the one
    // turn awaitingDisambiguationAnswer is true, same one-turn lifetime
    // as lastQuestionAsked.
    pendingAmbiguousValue: null,
    // True for exactly one turn: the turn right after this app asked a
    // conversational emotional-support follow-up question ("how low
    // have you been feeling? is anything physical bothering you too?"
    // — see composeEmotionalFollowUp in safetyCheck.js). Lets
    // processMessage.js's STAGE 3b tell "a fresh emotional statement"
    // apart from "an answer to our own emotional check-in", the same
    // way awaitingClarificationAnswer distinguishes a fresh message
    // from an answer to a symptom question.
    awaitingEmotionalFollowUp: false,
    // The exact text of that follow-up question, kept only for the one
    // turn it's pending — same rationale and same one-turn lifetime as
    // lastQuestionAsked above (interpretEmotionalFollowUpAnswer uses it
    // to judge the reply in context rather than in isolation).
    lastEmotionalQuestion: null,
    unaccountedComplaints: [],
    // How many CONSECUTIVE times finalizeAndRecommend's Infermedica
    // /parse call has come back with zero grounded evidence for the
    // accumulated symptoms this round. Used as a safety net: after a
    // couple of failures in a row (see processMessage.js's
    // finalizeAndRecommend), stop asking the patient to rephrase
    // (which was looping indefinitely with no way out) and fall back
    // to a generic recommendation instead of dead-ending the
    // conversation. Reset to 0 on any successful finalize or any
    // fresh symptom addition.
    unmatchedFinalizeAttempts: 0,
    // How many CONSECUTIVE turns, while a question was pending, the
    // domain classifier judged genuinely off-topic (see
    // clarificationCheck.js's classifyPendingAnswerRelevance). This
    // is a SEPARATE, smaller budget from clarificationCount on
    // purpose: pure noise ("I love apples") shouldn't eat into the
    // real Q&A budget a cooperative patient gets, but it still can't
    // be allowed to stall the conversation forever either. Reset to
    // 0 the moment a turn actually contributes something real
    // (a symptom, an understood confirmation, etc).
    offTopicStreak: 0,
    // How many CONSECUTIVE turns produced ZERO accumulated symptoms at
    // all, with no question ever pending — a DIFFERENT failure mode
    // than offTopicStreak above, which only ever engages while a
    // question IS pending. Real, demonstrated bug: an early-exit gate
    // (emergency, off-topic-as-concerning, diagnosis-decline) discards
    // the triggering message entirely without ever classifying it — no
    // symptoms accumulate, and NO pending-question flag gets set
    // either. A patient continuing the conversation afterward with
    // short replies then hits the SAME "I couldn't identify any
    // symptoms" reply forever, since every later message also arrives
    // fresh with nothing accumulated and nothing pending to answer —
    // offTopicStreak's safety valve never engages because
    // wasAnsweringPendingQuestion is never true. See
    // processMessage.js's STAGE 7 "accumulated.length === 0" branch,
    // which uses this to break the loop after a couple of repeats
    // instead of repeating the exact same question forever. Reset to 0
    // the moment any turn actually produces a real symptom.
    noSymptomStreak: 0,
    // True for exactly one turn: the turn right after a PHYSICAL
    // emergency reply (severity/keyword/dangerous-event — never the
    // crisis/mental-health gate, which stays a hard stop with no
    // acknowledgment path). Lets the patient explicitly choose to
    // continue the chat instead of the app assuming either way. See
    // processMessage.js's emergency-acknowledgment handling.
    awaitingEmergencyAcknowledgment: false,
    // DELIBERATE, BOUNDED EXCEPTION to this app's usual "never store the
    // patient's literal sentence" policy — see processMessage.js's doc
    // comment on this for the full reasoning. Holds the RAW message that
    // triggered the emergency reply, ONLY for the one turn
    // awaitingEmergencyAcknowledgment is true, so that choosing to
    // "Continue" can actually pick up from what was said instead of
    // asking the patient to repeat themselves into a void. Cleared the
    // instant it's used (or superseded by anything else), same one-turn
    // lifetime as lastQuestionAsked above.
    pendingEmergencyMessage: null,
    // FIXED (root cause, demonstrated live): a real, serious bug —
    // the stashed message alone isn't enough. STAGE 0a clears
    // awaitingClarificationAnswer/awaitingDisambiguationAnswer/
    // awaitingFinalConfirmation at the very TOP of every turn, before
    // the severity gate (which runs later, the same turn) ever fires.
    // So a message that triggered a physical emergency WHILE answering
    // one of this app's own pending questions (e.g. "10 severity"
    // answering a disambiguation re-ask) got stashed as a bare
    // fragment with NO memory of what it was answering. Replaying it
    // later on "Continue" then ran it through the pipeline as a
    // context-free standalone message — and a bare fragment like "10
    // severity", stripped of the context that gave it any meaning,
    // was misread by the crisis classifier as something completely
    // unrelated to what actually happened. This field remembers WHICH
    // pending question (if any) was active — 'clarification' |
    // 'disambiguation' | 'finalConfirmation' | null — so "Continue"
    // can restore that exact pending state before replaying, instead
    // of replaying a meaningless fragment into a void.
    pendingEmergencyQuestionType: null,
    // See getPendingEmergencyReply/setPendingEmergencyReply's doc
    // comment — the exact reply to re-show on any non-"Continue" reply
    // while an emergency/crisis notice is awaiting acknowledgment.
    pendingEmergencyReply: null,
    // Categories of every physical emergency flagged this session (e.g.
    // 'ambulance', 'emergency_room') — never the patient's text. Lets the
    // bot keep the earlier flag in mind after the patient continues; see
    // recordEmergencyFlag.
    emergencyHistory: [],
    // True right after the patient continues past an emergency, until the
    // next conversational reply acknowledges it once.
    emergencyNoticePending: false,
    // EXPLICIT PRODUCT DECISION: a short, IN-RAM-ONLY buffer of the
    // last few turns (patient message + bot reply), never written to
    // Supabase. This is NOT a reversal of the "Audit Log: REMOVED BY
    // DESIGN" decision further down this file (that removed PERMANENT
    // raw-text storage) — this is bounded (RECENT_MESSAGES_LIMIT
    // entries), lives only as long as this in-RAM session object does,
    // and vanishes on server restart or the same 24h session-expiry
    // this app already enforces everywhere else. Added because the
    // relevance/off-topic classifier (offtopiccheck.js's classifyDomain)
    // only ever saw the CURRENT message plus a one-line description of
    // the last question — with no memory of the actual conversation, it
    // had no way to read an odd reply ("no just leave it, go ahead") in
    // light of what had actually been discussed. See
    // getRecentMessages/appendRecentMessage below.
    recentMessages: [],
    lastAccessed: Date.now(),
  };
}

// How many recent turns (patient+bot pairs) getRecentMessages keeps —
// enough for a classifier to read short-term context, small enough to
// stay a cheap, bounded prompt addition rather than a growing log.
const RECENT_MESSAGES_LIMIT = 4;

/**
 * Appends one turn's patient message and the bot's reply to this
 * session's short, in-RAM-only recent-message buffer — see
 * defaultExtra's own doc comment for why this exists and why it's
 * deliberately NOT a reversal of the earlier "never persist raw text"
 * decision. Trims to the oldest RECENT_MESSAGES_LIMIT*2 entries (patient
 * + bot per turn) so the buffer never grows unbounded across a long
 * conversation.
 *
 * @param {string} sessionId
 * @param {string} patientMessage
 * @param {string} botReply
 */
export function appendRecentMessage(sessionId, patientMessage, botReply) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  if (patientMessage) extra.recentMessages.push({ role: 'patient', text: String(patientMessage) });
  if (botReply) extra.recentMessages.push({ role: 'bot', text: String(botReply) });
  const maxEntries = RECENT_MESSAGES_LIMIT * 2;
  if (extra.recentMessages.length > maxEntries) {
    extra.recentMessages = extra.recentMessages.slice(-maxEntries);
  }
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {Array<{role:'patient'|'bot', text:string}>} oldest first,
 *   empty array for a session with no prior turns yet (never errors).
 */
export function getRecentMessages(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.recentMessages || [] : [];
}

// Words too generic on their own to prove two symptom terms refer to
// the same thing — a match on "pain" alone would wrongly link "chest
// pain" and "eye pain". Used only by findLikelySameSymptom's denial
// fallback below, never for the ordinary exact-match merge path.
const GENERIC_SYMPTOM_WORDS = new Set(['pain', 'ache', 'aches', 'aching', 'feeling', 'sensation', 'problem', 'issue']);

/**
 * Fallback for a DENIAL whose wording doesn't exactly match any
 * existing accumulated term — see appendAccumulatedSymptoms' doc
 * comment for why this exists. Matches a currently-PRESENT existing
 * entry that shares at least one real, non-generic word (4+ letters)
 * with the denied term ("blood in urine" / "red-colored urine" share
 * "urine"). Deliberately conservative: only ever called for a denial,
 * and only ever matches against something still marked present (a
 * symptom already denied or never tracked can't be what's being
 * denied again).
 *
 * @param {Array} accumulated
 * @param {string} deniedKey - already lowercased/trimmed
 * @returns {object|null}
 */
function findLikelySameSymptom(accumulated, deniedKey) {
  const deniedWords = deniedKey
    .split(/\s+/)
    .filter((w) => w.length >= 4 && !GENERIC_SYMPTOM_WORDS.has(w));
  if (deniedWords.length === 0) return null;
  return (
    accumulated.find((e) => {
      if (!e.present) return false;
      const existingWords = e.term.toLowerCase().trim().split(/\s+/);
      // Exact shared word first, then the curated synonym backstop
      // (see symptomSynonyms.js) for a pair like "stomach"/"abdominal"
      // that shares no word at all — a real, demonstrated case where
      // "I don't have stomach pain" failed to cancel a tracked
      // "abdominal pain" entry without this.
      return deniedWords.some((w) => existingWords.includes(w)) || shareSynonymWord(deniedWords, existingWords);
    }) || null
  );
}

function getOrInitExtra(sessionId) {
  let extra = sessionExtraStore.get(sessionId);
  if (!extra) {
    extra = defaultExtra();
    sessionExtraStore.set(sessionId, extra);
  }
  return extra;
}

/**
 * How many clarifying-style questions this session has already asked —
 * counts BOTH targeted questions and the final "anything else?" gate
 * against the same MAX_CLARIFICATION_ROUNDS safety valve, so a patient
 * who never gives a clean answer still gets forced to a recommendation
 * eventually. Pure app bookkeeping — a counter, not anything
 * Infermedica returned.
 *
 * @param {string} sessionId
 * @returns {number}
 */
export function getClarificationCount(sessionId) {
  if (!sessionId) return 0;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.clarificationCount || 0 : 0;
}

/**
 * @param {string} sessionId
 */
export function incrementClarificationCount(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.clarificationCount = (extra.clarificationCount || 0) + 1;
  extra.lastAccessed = Date.now();
}

/**
 * Resets the round counter — called once a recommendation has actually
 * been given, so anything the patient adds AFTER that point gets its
 * own fresh gathering phase instead of immediately hitting the
 * MAX_CLARIFICATION_ROUNDS cap from the previous round.
 *
 * @param {string} sessionId
 */
export function resetClarificationCount(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.clarificationCount = 0;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {number}
 */
export function getUnmatchedFinalizeAttempts(sessionId) {
  if (!sessionId) return 0;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.unmatchedFinalizeAttempts || 0 : 0;
}

/**
 * @param {string} sessionId
 */
export function incrementUnmatchedFinalizeAttempts(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.unmatchedFinalizeAttempts = (extra.unmatchedFinalizeAttempts || 0) + 1;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 */
export function resetUnmatchedFinalizeAttempts(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.unmatchedFinalizeAttempts = 0;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {number}
 */
export function getOffTopicStreak(sessionId) {
  if (!sessionId) return 0;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.offTopicStreak || 0 : 0;
}

/**
 * @param {string} sessionId
 */
export function incrementOffTopicStreak(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.offTopicStreak = (extra.offTopicStreak || 0) + 1;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 */
export function resetOffTopicStreak(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.offTopicStreak = 0;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {number}
 */
export function getNoSymptomStreak(sessionId) {
  if (!sessionId) return 0;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.noSymptomStreak || 0 : 0;
}

/**
 * @param {string} sessionId
 */
export function incrementNoSymptomStreak(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.noSymptomStreak = (extra.noSymptomStreak || 0) + 1;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 */
export function resetNoSymptomStreak(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.noSymptomStreak = 0;
  extra.lastAccessed = Date.now();
}

// ---- Emergency Acknowledgment (physical emergency only — see defaultExtra's doc comment) ----
export function getAwaitingEmergencyAcknowledgment(sessionId) {
  if (!sessionId) return false;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? Boolean(extra.awaitingEmergencyAcknowledgment) : false;
}

/**
 * Remembers that a physical emergency was flagged in this session, so the
 * conversation can carry on afterwards without forgetting it (product
 * requirement: the patient may continue past an emergency, and the bot
 * must still take it into account — see processMessage.js's STAGE -1 and
 * finalizeAndRecommend).
 *
 * @param {string} sessionId
 * @param {string|null} category - emergencyCategory from the envelope
 */
export function recordEmergencyFlag(sessionId, category) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  if (!Array.isArray(extra.emergencyHistory)) extra.emergencyHistory = [];
  extra.emergencyHistory.push({ category: category || 'unspecified', at: Date.now() });
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {Array<{category: string, at: number}>}
 */
export function getEmergencyHistory(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  return extra && Array.isArray(extra.emergencyHistory) ? extra.emergencyHistory : [];
}

/**
 * @param {string} sessionId
 * @param {boolean} value
 */
export function setEmergencyNoticePending(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.emergencyNoticePending = Boolean(value);
  extra.lastAccessed = Date.now();
}

/**
 * Returns whether the one-time "continuing after an emergency" note is
 * due, and clears it.
 * @param {string} sessionId
 * @returns {boolean}
 */
export function consumeEmergencyNoticePending(sessionId) {
  if (!sessionId) return false;
  const extra = sessionExtraStore.get(sessionId);
  if (!extra || !extra.emergencyNoticePending) return false;
  extra.emergencyNoticePending = false;
  return true;
}

export function setAwaitingEmergencyAcknowledgment(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.awaitingEmergencyAcknowledgment = Boolean(value);
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {string|null}
 */
export function getPendingEmergencyMessage(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.pendingEmergencyMessage || null : null;
}

/**
 * @param {string} sessionId
 * @param {string|null} message
 */
export function setPendingEmergencyMessage(sessionId, message) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.pendingEmergencyMessage = message || null;
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {'clarification'|'disambiguation'|'finalConfirmation'|null}
 */
export function getPendingEmergencyQuestionType(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.pendingEmergencyQuestionType || null : null;
}

/**
 * @param {string} sessionId
 * @param {'clarification'|'disambiguation'|'finalConfirmation'|null} type
 */
export function setPendingEmergencyQuestionType(sessionId, type) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.pendingEmergencyQuestionType = type || null;
  extra.lastAccessed = Date.now();
}

/**
 * EXPLICIT PRODUCT DECISION: while an emergency/crisis notice is
 * awaiting acknowledgment, ANY reply other than "Continue" must re-show
 * the SAME notice (with the same Notify/Continue actions) instead of
 * being processed as an ordinary message — the conversation stays
 * "locked" on it until the patient actually continues. This is what
 * lets processMessage.js's STAGE -1 reconstruct that exact reply
 * without re-running the crisis/severity/keyword check a second time
 * (which could theoretically return a different category on a retry).
 *
 * @param {string} sessionId
 * @returns {object|null} the stashed emergency reply fields (kind,
 *   reply, guidance, source, emergencyCategory, urgency, isEmergency)
 */
export function getPendingEmergencyReply(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.pendingEmergencyReply || null : null;
}

/**
 * @param {string} sessionId
 * @param {object|null} replyFields
 */
export function setPendingEmergencyReply(sessionId, replyFields) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.pendingEmergencyReply = replyFields || null;
  extra.lastAccessed = Date.now();
}

/**
 * Resets a session's entire extra state back to fresh defaults —
 * accumulated symptoms, every pending-question flag, every counter —
 * while keeping the same sessionId (the conversation thread itself
 * isn't discarded, just everything it had accumulated). Used
 * specifically when a mental-health CRISIS is flagged: per the explicit
 * product decision behind this, a crisis message gets no "continue"
 * option (unlike a physical emergency) and, on top of that, nothing it
 * or anything before it contributed should keep feeding an ordinary
 * symptom-triage flow afterward — if the patient does message again
 * later, it starts genuinely fresh rather than carrying forward
 * whatever was accumulated pre-crisis.
 *
 * @param {string} sessionId
 */
export function resetSessionState(sessionId) {
  if (!sessionId) return;
  sessionExtraStore.set(sessionId, defaultExtra());
}

/**
 * Clears every "awaiting an answer to X" flag (targeted clarifying
 * question, final-confirmation gate, disambiguation, emotional
 * follow-up) WITHOUT touching accumulatedSymptoms, mentionedConditions,
 * or anything else — unlike resetSessionState, which wipes everything.
 *
 * Found live: a hard keyword-matched physical emergency (chest pain,
 * etc. — processMessage.js's runSharedSafetyGates) deliberately leaves
 * accumulated session state alone (a false-positive keyword match
 * shouldn't wipe legitimate symptoms already on file), but left EVERY
 * pending-question flag alone too — so the very next message after the
 * emergency interrupt got silently misread as answering whatever
 * question was pending BEFORE the emergency fired, rather than being
 * treated as a fresh reply. Demonstrated live: an emotional follow-up
 * question was pending, a chest-pain emergency fired in between, and
 * the patient's "continue" (meant to move past the emergency) instead
 * got routed into answering the stale emotional follow-up, producing a
 * mental-health referral with no connection to the chest pain at all.
 *
 * @param {string} sessionId
 */
export function clearPendingQuestionFlags(sessionId) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.awaitingClarificationAnswer = false;
  extra.lastQuestionAsked = null;
  extra.lastQuestionCandidates = [];
  extra.lastQuestionSymptom = null;
  extra.awaitingFinalConfirmation = false;
  extra.awaitingDisambiguationAnswer = false;
  extra.pendingAmbiguousValue = null;
  extra.awaitingEmotionalFollowUp = false;
  extra.lastAccessed = Date.now();
}

// ---- Turn rollback ----
// BUG (found live): a turn clears "which question is pending" near its
// start; when an AI call then failed mid-turn (rate limits), the turn
// ended in an error with that already cleared, so the patient's NEXT
// message was treated as a brand-new conversation — "it forgot I was
// talking about joint pain". processMessage.js snapshots the session
// before each turn and restores it when the turn errors, so a failed
// turn changes nothing.

/**
 * @param {string} sessionId
 * @returns {object|null} a deep copy of the session's state
 */
export function snapshotSession(sessionId) {
  const extra = sessionExtraStore.get(sessionId);
  return extra ? structuredClone(extra) : null;
}

/**
 * @param {string} sessionId
 * @param {object|null} snapshot - from snapshotSession
 */
export function restoreSession(sessionId, snapshot) {
  if (!sessionId || !snapshot) return;
  sessionExtraStore.set(sessionId, { ...snapshot, lastAccessed: Date.now() });
}

// ---- Session Management (RAM only — see SESSION_TTL_MS above) ----

function isSessionLive(sessionId) {
  const extra = sessionExtraStore.get(sessionId);
  return Boolean(extra) && Date.now() - extra.lastAccessed <= SESSION_TTL_MS;
}

/**
 * Resumes `providedSessionId` if it's still live in RAM, otherwise
 * starts a fresh in-memory session. Never touches the database.
 *
 * @param {string} patientId
 * @param {string|null} [providedSessionId]
 * @returns {Promise<{id: string}>}
 */
export async function getOrCreateSession(patientId, providedSessionId = null) {
  if (providedSessionId && isSessionLive(providedSessionId)) {
    getOrInitExtra(providedSessionId).lastAccessed = Date.now();
    return { id: providedSessionId };
  }
  const id = randomUUID();
  getOrInitExtra(id);
  return { id };
}

/**
 * Deletes a session's RAM state outright.
 * @param {string} sessionId
 */
export function deleteSession(sessionId) {
  if (sessionId) sessionExtraStore.delete(sessionId);
}

/**
 * "New session" / sign-out: deletes the patient's active session(s)
 * immediately, rather than waiting for the next message or the 24h
 * expiry.
 *
 * @param {string} patientId
 * @param {'symptom'|'diet'|null} [mode] - null ends both modes
 * @returns {number} how many sessions were deleted
 */
export function endPatientSessions(patientId, mode = null) {
  if (!patientId) return 0;
  let ended = 0;
  for (const m of mode ? [mode] : ['symptom', 'diet']) {
    const key = `${patientId}|${m}`;
    const pointer = activeSessionPointers.get(key);
    if (pointer) {
      deleteSession(pointer.sessionId);
      activeSessionPointers.delete(key);
      ended += 1;
    }
  }
  return ended;
}

// ---- Accumulated Symptoms (Groq's own classification — no Infermedica involved) ----

/**
 * Returns every distinct symptom Groq has extracted from the patient's
 * messages this session, chronological-ish (new terms appended,
 * existing terms updated in place — see appendAccumulatedSymptoms).
 * NEVER the patient's raw sentences, and NEVER anything Infermedica
 * returned — see symptomClassifier.js and processMessage.js's STAGE 6.
 * This is the ONLY thing processMessage.js's finalize step hands to
 * Infermedica's /parse, and only at the moment a recommendation is
 * actually being produced.
 *
 * @param {string} sessionId
 * @returns {Array<{term:string, present:boolean, duration:string|null, severity:string|null, finalized:boolean}>}
 */
export function getAccumulatedSymptoms(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  if (!extra) return [];

  const now = Date.now();
  if (now - extra.lastAccessed > INACTIVITY_TIMEOUT_MS) {
    sessionExtraStore.delete(sessionId);
    return [];
  }

  extra.lastAccessed = now;
  return extra.accumulatedSymptoms;
}

/**
 * Merges this turn's classifySymptoms() result into the session's
 * running list. A term already present (case-insensitive match) has
 * its present/duration/severity updated to the newest statement rather
 * than being duplicated — e.g. the patient mentioning "headache" again
 * later with a duration doesn't create a second "headache" entry.
 * New entries start with finalized: false.
 *
 * @param {string} sessionId
 * @param {Array<{term:string, present:boolean, duration:string|null, severity:string|null}>} newSymptoms
 */
export function appendAccumulatedSymptoms(sessionId, newSymptoms = []) {
  if (!sessionId || !newSymptoms.length) return;
  const extra = getOrInitExtra(sessionId);

  for (const s of newSymptoms) {
    const key = s.term.toLowerCase().trim();
    let existing = extra.accumulatedSymptoms.find((e) => e.term.toLowerCase().trim() === key);

    // FIXED (root cause, not a one-off): a DENIAL naming an already-
    // tracked symptom in DIFFERENT WORDING than how it was originally
    // stored ("blood in urine" denying an entry stored as "red-colored
    // urine") used to fail this exact-match lookup entirely, silently
    // creating an unrelated orphan "blood in urine: denied" entry
    // instead of cancelling the real one — the original entry stayed
    // present:true forever, with no way for the patient to ever retract
    // it short of repeating its EXACT stored wording. symptomClassifier.js's
    // prompt now also instructs the model to reuse the known term's exact
    // wording when a denial clearly refers to it, but that's a prompt-level
    // instruction, not a guarantee — this is the deterministic backstop.
    // Only applied to a DENIAL (never a fresh present:true report, which
    // really can be a genuinely distinct new symptom): fall back to a
    // shared-significant-word match against an existing PRESENT entry.
    // Sharing one real word (4+ letters, generic filler excluded) is
    // treated as the same tracked symptom rather than spawning a
    // duplicate the patient has no way to reach.
    if (!existing && s.present === false) {
      existing = findLikelySameSymptom(extra.accumulatedSymptoms, key);
    }

    if (existing) {
      existing.present = s.present;
      if (s.duration) {
        existing.duration = s.duration;
        // ADDED (found live): a blanket compound-answer guess ("6 and
        // few days" applied to two different symptoms at once, neither
        // named specifically) looks identical to a real, confirmed
        // value here — durationGuessed marks it so the round-gathering
        // logic (processMessage.js) knows not to treat it as settled.
        // Any REAL, non-guessed update (this branch, with
        // s.durationGuessed falsy) clears the flag — the patient has
        // now engaged with this specific symptom's duration directly.
        existing.durationGuessed = !!s.durationGuessed;
      } else if (s.durationCorrected) {
        // ADDED (found live): a null duration normally means "this turn
        // didn't mention it" and correctly leaves the existing value
        // alone — but that's wrong for an explicit ATTRIBUTE CORRECTION
        // ("my eye pain is not for 6 days"), where the patient is
        // saying a wrongly-recorded duration IS wrong, not just failing
        // to restate it. Respect it here, clearing the stale value
        // (and its guessed flag, now moot) instead of silently keeping
        // either.
        existing.duration = null;
        existing.durationGuessed = false;
      }
      if (s.severity) {
        existing.severity = s.severity;
        existing.severityGuessed = !!s.severityGuessed;
      } else if (s.severityCorrected) {
        existing.severity = null;
        existing.severityGuessed = false;
      }
      // NOTE: deliberately NOT resetting finalized to false here — a
      // symptom restated with more detail after already being included
      // in a recommendation isn't treated as "new" for the cross-domain
      // check; only a genuinely NEW term is.
    } else {
      extra.accumulatedSymptoms.push({
        term: s.term,
        present: s.present,
        duration: s.duration || null,
        durationGuessed: !!s.durationGuessed,
        severity: s.severity || null,
        severityGuessed: !!s.severityGuessed,
        finalized: false,
      });
    }
  }
  extra.lastAccessed = Date.now();
}

/**
 * Clears duration/severity on the named, already-known entries — used
 * by processMessage.js right after a symptom that was `finalized` (part
 * of an earlier recommendation this session) gets restated as present
 * in a genuinely new round with no fresh duration/severity of its own.
 * That old detail describes a DIFFERENT, already-treated episode; left
 * in place, it silently satisfies assessIntake's "already has enough
 * detail" check for a round that hasn't actually gathered anything yet.
 * Does NOT touch `finalized` itself — that flag still has to reflect
 * "already covered by an earlier recommendation" for the separate
 * cross-domain specialist check (finalizeAndRecommend's
 * priorRoundSymptoms/newRoundSymptoms split) to work correctly.
 *
 * @param {string} sessionId
 * @param {Set<string>} terms - lowercase, trimmed term strings
 */
export function clearStaleDurationSeverity(sessionId, terms) {
  if (!sessionId || !terms || !terms.size) return;
  const extra = sessionExtraStore.get(sessionId);
  if (!extra) return;
  for (const entry of extra.accumulatedSymptoms) {
    if (terms.has(entry.term.toLowerCase().trim())) {
      entry.duration = null;
      entry.severity = null;
    }
  }
}

/**
 * @param {string} sessionId
 * @returns {string[]}
 */
export function getMentionedConditions(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  if (!extra) return [];

  const now = Date.now();
  if (now - extra.lastAccessed > INACTIVITY_TIMEOUT_MS) {
    sessionExtraStore.delete(sessionId);
    return [];
  }

  extra.lastAccessed = now;
  return extra.mentionedConditions || [];
}

/**
 * Merges this turn's classifySymptoms() mentionedConditions into the
 * session's running list — same dedup-by-lowercase shape as
 * appendAccumulatedSymptoms, just simpler (no present/duration/severity
 * to reconcile, just distinct condition names).
 *
 * @param {string} sessionId
 * @param {string[]} newConditions
 */
export function appendMentionedConditions(sessionId, newConditions = []) {
  if (!sessionId || !newConditions.length) return;
  const extra = getOrInitExtra(sessionId);
  if (!extra.mentionedConditions) extra.mentionedConditions = [];

  const existingLower = new Set(extra.mentionedConditions.map((c) => c.toLowerCase().trim()));
  for (const c of newConditions) {
    const key = c.toLowerCase().trim();
    if (!existingLower.has(key)) {
      extra.mentionedConditions.push(c);
      existingLower.add(key);
    }
  }
  extra.lastAccessed = Date.now();
}

/**
 * Marks every currently-accumulated symptom as finalized — called
 * right after a recommendation is produced, so a symptom added in a
 * LATER round is the only thing treated as "new" for that later
 * round's cross-domain check.
 *
 * @param {string} sessionId
 */
export function markAllSymptomsFinalized(sessionId) {
  if (!sessionId) return;
  const extra = sessionExtraStore.get(sessionId);
  if (!extra) return;
  for (const s of extra.accumulatedSymptoms) s.finalized = true;
  // The round these were re-opened in is now closed too.
  extra.reopenedTerms = [];
  // So is any emergency flagged during it: the recommendation that closes
  // the round has already repeated the warning, and a NEW round gets the
  // full emergency checks again (see processMessage.js's
  // continuedPastEmergency).
  extra.emergencyHistory = [];
  extra.lastAccessed = Date.now();
}

/**
 * Already-finalized symptom terms the patient brought back up in the
 * CURRENT round (e.g. "my joint pain is 8/10 now" after a recommendation
 * on joint pain). BUG (found live): processMessage.js only counted a
 * finalized term as part of the new round on the exact turn it was
 * restated, so on the very next turn ("yes" to "any swelling or redness
 * around those joints?") joint pain silently dropped out of the round,
 * the round looked empty, and the patient got "I couldn't tell what new
 * symptoms you're experiencing". Remembered here until the round is
 * finalized (cleared by markAllSymptomsFinalized above).
 *
 * @param {string} sessionId
 * @returns {string[]} lowercased, trimmed terms
 */
export function getReopenedTerms(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  return extra && Array.isArray(extra.reopenedTerms) ? extra.reopenedTerms : [];
}

/**
 * @param {string} sessionId
 * @param {string[]} terms
 */
export function addReopenedTerms(sessionId, terms) {
  if (!sessionId || !terms?.length) return;
  const extra = getOrInitExtra(sessionId);
  const merged = new Set(Array.isArray(extra.reopenedTerms) ? extra.reopenedTerms : []);
  for (const t of terms) merged.add(String(t).toLowerCase().trim());
  extra.reopenedTerms = [...merged];
  extra.lastAccessed = Date.now();
}

// ---- Unaccounted Complaints (clause-level, across the session) ----

/**
 * Complaints Infermedica's /parse couldn't map to anything, discovered
 * at finalize time (see processMessage.js) and normalized through
 * symptomClassifier.js's normalizeComplaint before ever reaching this
 * store — never the patient's literal wording, never anything
 * Infermedica returned.
 *
 * @param {string} sessionId
 * @returns {string[]}
 */
export function getUnaccountedComplaints(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  if (!extra) return [];

  const now = Date.now();
  if (now - extra.lastAccessed > INACTIVITY_TIMEOUT_MS) {
    sessionExtraStore.delete(sessionId);
    return [];
  }

  extra.lastAccessed = now;
  return extra.unaccountedComplaints;
}

/**
 * @param {string} sessionId
 * @param {string[]} complaints - already-normalized complaints (see
 *   symptomClassifier.js's normalizeComplaint); deduped
 *   (case/whitespace-insensitive) against what's already stored.
 */
export function appendUnaccountedComplaints(sessionId, complaints = []) {
  if (!sessionId || !complaints.length) return;
  const extra = getOrInitExtra(sessionId);

  const seen = new Set(extra.unaccountedComplaints.map((c) => c.toLowerCase().trim()));
  for (const complaint of complaints) {
    const key = complaint.toLowerCase().trim();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    extra.unaccountedComplaints.push(complaint);
  }
  extra.lastAccessed = Date.now();
}

// ---- Subject Carryover (self vs. dependent, across turns) ----

/**
 * @param {string} sessionId
 * @returns {{subject:string, relation?:string|null, ageGroup?:string|null}|null}
 */
export function getLastSubject(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.lastSubject : null;
}

/**
 * @param {string} sessionId
 * @param {object} subjectInfo - result of subjectDetection.detectSubject() — a Groq call, not Infermedica
 */
export function saveLastSubject(sessionId, subjectInfo) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.lastSubject = subjectInfo;
  extra.lastAccessed = Date.now();
}

// ---- Diet Bot Session Carryover (patient-scoped, independent of SehatAI's own sessionId) ----

/**
 * The diet bot (api.py — a separate Python/FastAPI service run
 * standalone, see dietbot-integration-architecture.md) owns its own
 * session concept in its own Supabase tables (diet_chat_sessions /
 * diet_chat_messages), keyed by patient_id rather than SehatAI's own
 * sessionId. The two chat threads are deliberately kept fully
 * independent — no attempt is made to merge them into one session
 * object.
 *
 * The pointer to DietBot's session lives in RAM only, on the patient's
 * diet-mode entry in activeSessionPointers (same 24h lifetime as every
 * other session here). DietBot's OWN storage is a separate service and
 * out of scope for this file.
 *
 * Passing this back explicitly (rather than relying on the diet bot's
 * own local-disk fallback — recommender.py writes a
 * `.session_<patient_id>` file when no session_id is given) avoids a
 * second single-instance, wiped-on-restart state hazard.
 *
 * @param {string} patientId
 * @returns {Promise<string|null>}
 */
export async function getDietSessionId(patientId) {
  if (!patientId) return null;
  return activeSessionPointers.get(`${patientId}|diet`)?.dietSessionId || null;
}

/**
 * Remembers (in RAM only) which DietBot-side session this patient's
 * diet thread is using, so the next diet turn resumes it.
 *
 * @param {string} patientId
 * @param {string} dietSessionId
 * @param {string|null} [sehataiSessionId] - this app's own session id for
 *   the diet thread (diet mode's emergency-acknowledgment flags live in
 *   sessionExtraStore under it)
 * @returns {Promise<void>}
 */
export async function saveDietSessionId(patientId, dietSessionId, sehataiSessionId = null) {
  if (!patientId || !dietSessionId) return;
  const key = `${patientId}|diet`;
  const pointer = activeSessionPointers.get(key);
  activeSessionPointers.set(key, {
    sessionId: sehataiSessionId || pointer?.sessionId || null,
    dietSessionId,
    lastActivity: Date.now(),
  });
}

// ---- 24h Session Resume / Expiry (RAM only) ----
//
// Symptom and diet mode each get their own resumable thread per patient:
// switching modes or refreshing the page resumes the same session, and a
// session ends when the patient explicitly starts a new one OR 24 hours
// pass with no activity — whichever comes first. Ending a session deletes
// its state immediately. Nothing here touches the database.

/**
 * Shared resume-or-create for both modes.
 * @returns {{sessionId: string, dietSessionId: string|null, isNew: boolean}}
 */
function resumeOrStart(patientId, mode, forceNew) {
  const key = `${patientId}|${mode}`;
  const pointer = activeSessionPointers.get(key);
  const fresh = pointer && Date.now() - pointer.lastActivity <= SESSION_TTL_MS && isSessionLive(pointer.sessionId);

  if (pointer && fresh && !forceNew) {
    pointer.lastActivity = Date.now();
    getOrInitExtra(pointer.sessionId).lastAccessed = Date.now();
    return { sessionId: pointer.sessionId, dietSessionId: pointer.dietSessionId || null, isNew: false };
  }

  // Expired, explicitly replaced, or never existed: delete the old one now.
  if (pointer) deleteSession(pointer.sessionId);
  const sessionId = randomUUID();
  getOrInitExtra(sessionId);
  activeSessionPointers.set(key, { sessionId, dietSessionId: null, lastActivity: Date.now() });
  return { sessionId, dietSessionId: null, isNew: true };
}

/**
 * Symptom mode's resume-or-create. The one function a caller (e.g.
 * webServer.js) should use instead of trusting a client-supplied
 * sessionId.
 *
 * @param {string} patientId
 * @param {{forceNew?: boolean}} [opts] - forceNew: true for an explicit
 *   "start a new session" request — the old session is deleted now.
 * @returns {Promise<{sessionId: string, isNew: boolean}>}
 */
export async function getOrResumeSession(patientId, { forceNew = false } = {}) {
  if (!patientId) throw new Error('getOrResumeSession requires a patientId');
  const { sessionId, isNew } = resumeOrStart(patientId, 'symptom', forceNew);
  return { sessionId, isNew };
}

/**
 * Diet mode's resume-or-create — same contract as getOrResumeSession,
 * plus the DietBot-side session_id (if one is still valid) so the caller
 * can pass it straight through to processDietMessage.
 *
 * @param {string} patientId
 * @param {{forceNew?: boolean}} [opts]
 * @returns {Promise<{sessionId: string, dietSessionId: string|null, isNew: boolean}>}
 */
export async function getOrResumeDietSession(patientId, { forceNew = false } = {}) {
  if (!patientId) throw new Error('getOrResumeDietSession requires a patientId');
  return resumeOrStart(patientId, 'diet', forceNew);
}

// ---- Question-Answer Flags ----
// Two one-turn flags so the gates in processMessage.js know what kind
// of question, if any, is being answered THIS turn (so a short reply
// like "yes"/"no" isn't misread as off-topic/a greeting). Both are
// plain booleans — zero Infermedica-authored content.
//
// UPDATED: the TARGETED question's own text is now also kept, for
// exactly the one turn it's pending (see getLastQuestionAsked below) —
// a deliberate reversal of the earlier "never even the text" rule.
// Reason: a real, demonstrated bug. classifySymptoms, given only the
// KNOWN SYMPTOM NAMES, misread "no but i have eye pain" (answering
// "have you noticed nausea or light sensitivity?") as denying the
// unrelated already-known "headache" — there was no way for it to
// know the question wasn't about headache at all. Giving it the
// question's own text lets it correctly attribute the answer instead
// of guessing from symptom names alone. This is still Groq's own
// composed text, never Infermedica-derived, and still gone the moment
// it's read back (overwritten by the next question or left stale and
// unused — nothing reads it outside the one turn right after it was
// asked).
export function getAwaitingClarificationAnswer(sessionId) {
  if (!sessionId) return false;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? Boolean(extra.awaitingClarificationAnswer) : false;
}

export function setAwaitingClarificationAnswer(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.awaitingClarificationAnswer = Boolean(value);
  extra.lastAccessed = Date.now();
}

/**
 * The exact text of the targeted clarifying question this app just
 * asked (Groq-composed, or the fixed-template fallback — see
 * clarificationCheck.js) — only ever meaningful for the one turn right
 * after it was asked (i.e. paired with awaitingClarificationAnswer
 * being true). See the doc comment above getAwaitingClarificationAnswer
 * for why this is stored now, unlike the final-confirmation gate's
 * text (which stays un-stored — its meaning is fixed and known to the
 * resolver already, so it doesn't have this ambiguity problem).
 *
 * @param {string} sessionId
 * @returns {string|null}
 */
export function getLastQuestionAsked(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.lastQuestionAsked || null : null;
}

/**
 * @param {string} sessionId
 * @param {string|null} questionText
 */
export function setLastQuestionAsked(sessionId, questionText) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.lastQuestionAsked = questionText || null;
  // A new question invalidates the previous one's candidates and symptom;
  // the assessIntake call site sets fresh ones right after this.
  extra.lastQuestionCandidates = [];
  extra.lastQuestionSymptom = null;
  extra.lastAccessed = Date.now();
}

/**
 * The recorded symptom the pending targeted question is about (e.g.
 * "leg pain" for "How long have you had the leg pain, and how severe is
 * it?"), or null when the question isn't about one specific symptom. BUG
 * (found live): the answer "2 days and 5 out of 10" to that question was
 * applied to joint pain too, overwriting its real values, and the bot
 * then kept re-asking about both.
 *
 * @param {string} sessionId
 * @returns {string|null}
 */
export function getLastQuestionSymptom(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.lastQuestionSymptom || null : null;
}

/**
 * @param {string} sessionId
 * @param {string|null} term
 */
export function setLastQuestionSymptom(sessionId, term) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.lastQuestionSymptom = term ? String(term).trim() : null;
  extra.lastAccessed = Date.now();
}

/**
 * The NEW symptom terms (not yet recorded) that the pending targeted
 * question asked about, as declared by assessIntake alongside the
 * question text — e.g. ["joint swelling", "joint redness"] for "have you
 * noticed any swelling or redness around those joints?". Lets a bare
 * "yes" be recorded deterministically even when classifySymptoms' own
 * affirmation rule is missed (found live: "yes" to exactly that question
 * came back with no symptoms at all).
 *
 * @param {string} sessionId
 * @returns {string[]}
 */
export function getLastQuestionCandidates(sessionId) {
  if (!sessionId) return [];
  const extra = sessionExtraStore.get(sessionId);
  return extra && Array.isArray(extra.lastQuestionCandidates) ? extra.lastQuestionCandidates : [];
}

/**
 * @param {string} sessionId
 * @param {string[]} terms
 */
export function setLastQuestionCandidates(sessionId, terms) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.lastQuestionCandidates = (terms || [])
    .filter((t) => typeof t === 'string' && t.trim())
    .map((t) => t.trim());
  extra.lastAccessed = Date.now();
}

/**
 * True for exactly one turn: the turn right after Groq asked "is there
 * anything else before I recommend a specialist?" — see
 * processMessage.js's STAGE 7. If the patient's next message adds no
 * new symptom info, that's read as confirmation to go ahead and call
 * Infermedica now; if it adds something new, the gathering loop
 * continues instead.
 */
export function getAwaitingFinalConfirmation(sessionId) {
  if (!sessionId) return false;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? Boolean(extra.awaitingFinalConfirmation) : false;
}

export function setAwaitingFinalConfirmation(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.awaitingFinalConfirmation = Boolean(value);
  extra.lastAccessed = Date.now();
}

// ---- Disambiguation Answer Flag (bare-number ambiguity re-ask) ----
// Same one-turn-pending pattern as the flags above, for the ambiguous-
// number disambiguation question — see defaultExtra()'s doc comment on
// awaitingDisambiguationAnswer for why this is kept separate from
// awaitingClarificationAnswer.
export function getAwaitingDisambiguationAnswer(sessionId) {
  if (!sessionId) return false;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? Boolean(extra.awaitingDisambiguationAnswer) : false;
}

export function setAwaitingDisambiguationAnswer(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.awaitingDisambiguationAnswer = Boolean(value);
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {string|null}
 */
export function getPendingAmbiguousValue(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.pendingAmbiguousValue || null : null;
}

/**
 * @param {string} sessionId
 * @param {string|null} value
 */
export function setPendingAmbiguousValue(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.pendingAmbiguousValue = value || null;
  extra.lastAccessed = Date.now();
}

// ---- Emotional Follow-Up Flags ----
// Same one-turn-pending pattern as awaitingClarificationAnswer /
// lastQuestionAsked above, applied to the conversational emotional-
// support check-in (see processMessage.js's STAGE 3b and
// safetyCheck.js's composeEmotionalFollowUp /
// interpretEmotionalFollowUpAnswer).
export function getAwaitingEmotionalFollowUp(sessionId) {
  if (!sessionId) return false;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? Boolean(extra.awaitingEmotionalFollowUp) : false;
}

export function setAwaitingEmotionalFollowUp(sessionId, value) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.awaitingEmotionalFollowUp = Boolean(value);
  extra.lastAccessed = Date.now();
}

/**
 * @param {string} sessionId
 * @returns {string|null}
 */
export function getLastEmotionalQuestion(sessionId) {
  if (!sessionId) return null;
  const extra = sessionExtraStore.get(sessionId);
  return extra ? extra.lastEmotionalQuestion || null : null;
}

/**
 * @param {string} sessionId
 * @param {string|null} questionText
 */
export function setLastEmotionalQuestion(sessionId, questionText) {
  if (!sessionId) return;
  const extra = getOrInitExtra(sessionId);
  extra.lastEmotionalQuestion = questionText || null;
  extra.lastAccessed = Date.now();
}

// ---- Audit Log: REMOVED BY DESIGN ----
//
// This used to insert one row per turn into a `chat_messages` audit
// table — the patient's raw message, the bot's full response object
// (including, at various points, triage_level, specialist_recommended,
// and other Infermedica-derived fields), permanently. That table, and
// this function's write to it, are gone entirely now — nothing about a
// chat session is written to the database (sessions are RAM-only, see
// SESSION_TTL_MS at the top of this file). If a `chat_messages` table
// still exists in Supabase from before this change, it's simply never
// written to anymore; drop it with `drop table if exists
// chat_messages;` in the Supabase SQL editor if you want it gone from
// the schema too (optional — leaving it, empty and unused, is
// harmless).
//
// Kept as an exported no-op (rather than deleted, with its call sites
// removed from processMessage.js) so nothing else has to change.
export async function logChatMessage(_args) {
  // Intentionally does nothing — see the note above this function.
}