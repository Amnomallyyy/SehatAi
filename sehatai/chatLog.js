// ============================================
// SehatAI: Chat Log & Session Management
//
// COMPLIANCE POLICY (non-negotiable): no value that originated from an
// Infermedica API response is ever kept past the single request/
// response cycle that produced it. Not in Supabase (see
// hydrateSessionState/persistSessionState/logChatMessage below —
// already no-ops), and NOT in RAM either, across turns.
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
import { supabase } from './supabaseClient.js';
import { shareSynonymWord } from './symptomSynonyms.js';

const INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000; // 30 Minutes

// ---- In-memory store (RAM only) ----
// sessionId -> { accumulatedSymptoms: [...], lastSubject, ... }
const sessionExtraStore = new Map();

/**
 * Sweeps RAM every 5 minutes to purge sessions inactive > 30 mins.
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
}, 5 * 60 * 1000).unref();

/**
 * The default shape of a session's RAM-cached extra state. Factored out
 * so hydrateSessionState (below) can merge a persisted row over these
 * same defaults — a persisted row from an older version of this app
 * that's missing a field newer code expects (e.g. lastQuestionAsked
 * didn't always exist) still comes back with a safe default for it,
 * rather than `undefined` leaking into code that expects a boolean or
 * an array.
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
  extra.awaitingFinalConfirmation = false;
  extra.awaitingDisambiguationAnswer = false;
  extra.pendingAmbiguousValue = null;
  extra.awaitingEmotionalFollowUp = false;
  extra.lastAccessed = Date.now();
}

// ---- Session Management ----
export async function getOrCreateSession(patientId, providedSessionId = null) {
  if (providedSessionId) {
    const { data } = await supabase
      .from('chat_sessions')
      .select('id')
      .eq('id', providedSessionId)
      .maybeSingle();

    if (data) return data;
  }

  // Retry once before falling back to a bare, DB-less id. This matters
  // more now than it used to: chat_session_state.session_id REFERENCES
  // chat_sessions(id) (see persistSessionState below), so falling back
  // to an id with no chat_sessions row doesn't just skip creating a
  // session — it silently and PERMANENTLY breaks persistence for this
  // entire conversation, since every later persistSessionState upsert
  // will fail its foreign-key constraint (caught and logged, but easy
  // to miss buried in per-turn logs, and with no way to recover short
  // of starting a new session). A brief retry turns a merely transient
  // blip into a non-event instead of a whole conversation silently
  // losing crash/restart survival.
  let lastError = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    const { data, error } = await supabase
      .from('chat_sessions')
      .insert([{ patient_id: patientId }])
      .select('id')
      .single();
    if (!error) return data;
    lastError = error;
    if (attempt === 0) await new Promise((resolve) => setTimeout(resolve, 250));
  }

  console.error(
    'Error creating chat_sessions row after retrying — falling back to a bare id with NO database row. ' +
    'chat_session_state persistence (hydrateSessionState/persistSessionState) will silently fail for ' +
    'this ENTIRE session from here on (a foreign-key violation on every persistSessionState upsert, ' +
    'caught and logged per-turn) since there is no chat_sessions row for it to reference:',
    lastError
  );
  return { id: providedSessionId || randomUUID() };
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
 * PERSISTED (unlike the rest of this file): this pointer is stored in
 * Supabase, not RAM, in its own `diet_session_pointers` table —
 *   create table if not exists diet_session_pointers (
 *     patient_id text primary key,
 *     diet_session_id text not null,
 *     updated_at timestamptz not null default now()
 *   );
 * This is deliberately the ONLY piece of session state persisted at
 * all. It's SehatAI-and-DietBot bookkeeping — a foreign key to the
 * diet bot's own session row — and contains nothing derived from
 * Infermedica, so it carries none of the restriction that applies to
 * anything Infermedica-sourced. No in-memory cache sits in front of
 * this — Supabase is the only source of truth, on purpose, to keep
 * this simple and avoid a second thing that could go stale.
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
  try {
    const { data, error } = await supabase
      .from('diet_session_pointers')
      .select('diet_session_id')
      .eq('patient_id', patientId)
      .maybeSingle();
    if (error) {
      console.error('[chatLog] getDietSessionId failed (non-fatal, treated as no prior session):', error.message);
      return null;
    }
    return data?.diet_session_id || null;
  } catch (err) {
    console.error('[chatLog] getDietSessionId failed (non-fatal, treated as no prior session):', err.message);
    return null;
  }
}

/**
 * @param {string} patientId
 * @param {string} dietSessionId
 * @param {string|null} [sehataiSessionId] - the chat_sessions.id this
 *   diet turn used (see getOrResumeDietSession below) — stored alongside
 *   diet_session_id so the NEXT diet turn can resume the exact same
 *   SehatAI-side session (needed for diet mode's own emergency-
 *   acknowledgment flags, which live in sessionExtraStore keyed by THIS
 *   id, not DietBot's own session_id).
 * @returns {Promise<void>}
 */
export async function saveDietSessionId(patientId, dietSessionId, sehataiSessionId = null) {
  if (!patientId || !dietSessionId) return;
  try {
    const row = { patient_id: patientId, diet_session_id: dietSessionId, updated_at: new Date().toISOString() };
    if (sehataiSessionId) row.sehatai_session_id = sehataiSessionId;
    await supabase.from('diet_session_pointers').upsert(row);
  } catch (err) {
    console.error('[chatLog] saveDietSessionId failed (non-fatal):', err.message);
  }
}

// ---- 24h Session Resume / Expiry ("clean storage") ----
//
// Both symptom and diet mode get their own independent, resumable
// "terminal": switching between modes never loses either conversation,
// a browser refresh resumes the same session, and a session only ends
// when the patient explicitly starts a new one OR 24 hours pass with no
// activity — whichever comes first. On expiry (or an explicit new-
// session request), the OLD session's rows are actively deleted rather
// than just left inert — there's no compliance reason to keep them
// (chat_session_state never holds anything Infermedica-sourced, see
// logChatMessage's doc comment above), this is purely a "don't let
// completed conversations pile up forever" preference.
//
// Schema (create/alter in Supabase before relying on this — without it,
// these functions fail closed to always creating a fresh session, logged
// but non-fatal):
//
//   create table if not exists active_session_pointers (
//     patient_id text not null,
//     mode text not null,
//     session_id uuid not null references chat_sessions(id),
//     primary key (patient_id, mode)
//   );
//   alter table diet_session_pointers
//     add column if not exists sehatai_session_id uuid references chat_sessions(id);
//   alter table diet_session_pointers alter column diet_session_id drop not null;

const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

/**
 * @param {string} sessionId
 * @returns {Promise<boolean>} true if this session's last known activity
 *   was more than 24h ago, OR if activity can't be determined at all
 *   (fails toward starting fresh rather than resuming something unknown).
 */
async function isSessionStale(sessionId) {
  const { data: state } = await supabase
    .from(SESSION_STATE_TABLE)
    .select('updated_at')
    .eq('session_id', sessionId)
    .maybeSingle();
  let lastActivity = state?.updated_at;
  if (!lastActivity) {
    // No chat_session_state row yet (a session created but no turn ever
    // completed — e.g. diet mode, which never calls persistSessionState
    // at all) — fall back to when the session was created.
    const { data: session } = await supabase
      .from('chat_sessions')
      .select('started_at')
      .eq('id', sessionId)
      .maybeSingle();
    lastActivity = session?.started_at;
  }
  if (!lastActivity) return true;
  return Date.now() - new Date(lastActivity).getTime() > SESSION_TTL_MS;
}

/**
 * Deletes an old session's rows entirely ("clean storage") — called
 * whenever getOrResumeSession/getOrResumeDietSession decide a session is
 * being replaced (expired or an explicit new-session request), never on
 * a session still in active use.
 * @param {string} sessionId
 */
async function deleteSessionRows(sessionId) {
  if (!sessionId) return;
  try {
    await supabase.from(SESSION_STATE_TABLE).delete().eq('session_id', sessionId);
    await supabase.from('chat_sessions').delete().eq('id', sessionId);
  } catch (err) {
    console.error('[chatLog] deleteSessionRows failed (non-fatal — the old row is just left behind):', err.message);
  }
  sessionExtraStore.delete(sessionId);
}

/**
 * Symptom mode's resume-or-create. The one function a caller (e.g.
 * webServer.js) should use instead of blindly trusting a client-supplied
 * sessionId — this is what actually implements "switching modes and
 * refreshing never loses the conversation, but 24h of inactivity starts
 * a fresh one."
 *
 * @param {string} patientId
 * @param {{forceNew?: boolean}} [opts] - forceNew: true for an explicit
 *   "start a new session" request from the patient — closes out and
 *   cleans up whatever session was active, same as an expiry would,
 *   just patient-triggered instead of time-triggered.
 * @returns {Promise<{sessionId: string, isNew: boolean}>}
 */
export async function getOrResumeSession(patientId, { forceNew = false } = {}) {
  if (!patientId) throw new Error('getOrResumeSession requires a patientId');

  const { data: pointer, error: pointerError } = await supabase
    .from('active_session_pointers')
    .select('session_id')
    .eq('patient_id', patientId)
    .eq('mode', 'symptom')
    .maybeSingle();
  if (pointerError) {
    console.error('[chatLog] getOrResumeSession pointer lookup failed (starting fresh):', pointerError.message);
  }

  if (pointer?.session_id && !forceNew && !(await isSessionStale(pointer.session_id))) {
    return { sessionId: pointer.session_id, isNew: false };
  }

  if (pointer?.session_id) {
    // BUG (found live): active_session_pointers.session_id has a foreign
    // key to chat_sessions(id) — deleting the chat_sessions row FIRST,
    // while this pointer row still references it, gets silently rejected
    // by the FK constraint (Supabase returns an error here, caught and
    // logged by deleteSessionRows, never thrown) — the "old session
    // deleted" part of clean storage just quietly failed every time. The
    // pointer row itself has to go first.
    await supabase.from('active_session_pointers').delete().eq('patient_id', patientId).eq('mode', 'symptom');
    await deleteSessionRows(pointer.session_id);
  }

  const session = await getOrCreateSession(patientId, null);
  const { error: upsertError } = await supabase
    .from('active_session_pointers')
    .upsert({ patient_id: patientId, mode: 'symptom', session_id: session.id });
  if (upsertError) {
    console.error('[chatLog] getOrResumeSession pointer upsert failed (non-fatal — this turn still works, just not resumable next time):', upsertError.message);
  }
  return { sessionId: session.id, isNew: true };
}

/**
 * Diet mode's resume-or-create — same contract as getOrResumeSession
 * above, plus it also hands back the DietBot-side session_id (if one is
 * still valid) so the caller can pass it straight through to
 * processDietMessage without a separate getDietSessionId lookup.
 *
 * @param {string} patientId
 * @param {{forceNew?: boolean}} [opts]
 * @returns {Promise<{sessionId: string, dietSessionId: string|null, isNew: boolean}>}
 */
export async function getOrResumeDietSession(patientId, { forceNew = false } = {}) {
  if (!patientId) throw new Error('getOrResumeDietSession requires a patientId');

  const { data: row, error } = await supabase
    .from('diet_session_pointers')
    .select('sehatai_session_id, diet_session_id, updated_at')
    .eq('patient_id', patientId)
    .maybeSingle();
  if (error) {
    console.error('[chatLog] getOrResumeDietSession lookup failed (starting fresh):', error.message);
  }

  const stale = row?.updated_at ? Date.now() - new Date(row.updated_at).getTime() > SESSION_TTL_MS : true;

  if (row?.sehatai_session_id && !forceNew && !stale) {
    return { sessionId: row.sehatai_session_id, dietSessionId: row.diet_session_id || null, isNew: false };
  }

  // Stale, forceNew, or no pointer at all.
  if (row?.sehatai_session_id) {
    // Same FK-ordering bug as getOrResumeSession above, fixed the same
    // way: clear this row's reference to the old session before trying
    // to delete that session, not after.
    await supabase.from('diet_session_pointers').delete().eq('patient_id', patientId);
    await deleteSessionRows(row.sehatai_session_id);
  }

  const session = await getOrCreateSession(patientId, null);
  // Write a placeholder pointer immediately (diet_session_id left null —
  // see the schema note above, this column had to be made nullable for
  // exactly this) so a SECOND resume call before any diet message has
  // actually been sent still finds this session and resumes it, instead
  // of creating a different fresh session every time it's asked (found
  // live: that's exactly what happened before this write existed).
  // saveDietSessionId overwrites this same row with the real
  // diet_session_id once DietBot actually returns one. NOTE: none of
  // this reaches DietBot's own local-disk session fallback
  // (recommender.py's .session_<patient_id> file) — that's entirely on
  // DietBot's side, left alone here same as its own message-history cap.
  const { error: placeholderError } = await supabase
    .from('diet_session_pointers')
    .upsert({ patient_id: patientId, sehatai_session_id: session.id, diet_session_id: null, updated_at: new Date().toISOString() });
  if (placeholderError) {
    console.error('[chatLog] getOrResumeDietSession placeholder pointer write failed (non-fatal — this turn still works, just not resumable next time):', placeholderError.message);
  }
  return { sessionId: session.id, dietSessionId: null, isNew: true };
}

// ---- Session State Persistence ----
//
// UPDATED (this was previously disabled by design — see the git history
// of this comment block for the old reasoning). The RAM-only design had
// a real cost: this app can only ever run as a SINGLE process, because
// a second instance (for load-balancing, or just a rolling deploy) has
// its own empty `sessionExtraStore` and would silently "forget" any
// session that happened to land on it instead of the instance that
// started it. Worse, ANY server restart or crash mid-conversation loses
// every in-progress session with no warning — the patient just gets
// treated as a brand-new patient on their next message.
//
// The original hesitation was about NOT wanting anything
// Infermedica-derived, or anything from the raw conversation, sitting
// in the database — see this file's top-of-file compliance note, which
// is still fully in force and untouched by this change. But
// `sessionExtraStore` never held anything like that in the first
// place: it's Groq's OWN symptom classification (never Infermedica's),
// plus this app's own bookkeeping (counters, booleans, the composed
// question text). There was never a compliance reason blocking this —
// just a simplicity choice, made before the single-instance/
// restart-loss cost of that choice had been weighed against it.
//
// Design: sessionExtraStore stays as the fast, synchronous, per-process
// cache it always was (every get/set in this file still reads/writes it
// directly, unchanged) — this is now a cache IN FRONT OF Supabase, not
// a replacement for persistence. hydrateSessionState is called once per
// incoming message (see processMessage.js) and only does a DB round
// trip on an actual cache miss (a fresh process, or a session that
// aged out of a previous process's RAM) — the common case, the same
// process handling turn 2+ of a session it already saw, costs nothing
// extra. persistSessionState upserts the current in-RAM object after
// each turn (see processMessage.js's try/finally).
//
// This does NOT make the system safe for concurrent writes to the same
// session across MULTIPLE instances (last-write-wins) — that's a
// harder problem (would need row-level locking or optimistic
// concurrency) out of scope for this pass. It does fix the two more
// common failures: a restart/crash losing a conversation, and a single
// instance being a hard architectural requirement.
//
// Schema (create this table in Supabase before relying on persistence —
// without it, hydrateSessionState/persistSessionState fail closed to
// their old RAM-only behavior, logged but non-fatal):
//
//   create table if not exists chat_session_state (
//     session_id uuid primary key references chat_sessions(id),
//     state jsonb not null,
//     updated_at timestamptz not null default now()
//   );
const SESSION_STATE_TABLE = 'chat_session_state';

/**
 * Loads a session's extra state from Supabase into the RAM cache, but
 * ONLY on a genuine cache miss (sessionExtraStore doesn't already have
 * this sessionId) — the normal case, a later turn of a session this
 * same process already saw, does zero DB work. Failures here are
 * logged but non-fatal: the session just starts fresh in RAM, exactly
 * like the old always-disabled behavior, rather than blocking the
 * conversation on a persistence-layer problem.
 *
 * @param {string} sessionId
 */
export async function hydrateSessionState(sessionId) {
  if (!sessionId || sessionExtraStore.has(sessionId)) return;
  try {
    const { data, error } = await supabase
      .from(SESSION_STATE_TABLE)
      .select('state')
      .eq('session_id', sessionId)
      .maybeSingle();
    if (error) {
      console.error('[chatLog] hydrateSessionState failed (starting this session fresh in RAM):', error.message);
      return;
    }
    if (data?.state) {
      // Merged over defaultExtra() so a row written by an older version
      // of this app, missing a field this version expects, still comes
      // back with a safe default rather than `undefined`.
      sessionExtraStore.set(sessionId, { ...defaultExtra(), ...data.state, lastAccessed: Date.now() });
    }
  } catch (err) {
    console.error('[chatLog] hydrateSessionState threw (starting this session fresh in RAM):', err.message);
  }
}

/**
 * Upserts the session's current RAM state to Supabase. Called once per
 * turn, after the pipeline finishes (see processMessage.js's
 * try/finally) — so even a turn that returned early (an emergency,
 * off-topic, etc.) still gets its state saved. Failures here are
 * logged but non-fatal: the RAM cache in THIS process still has the
 * correct state for as long as this process keeps running, so a
 * transient DB write failure doesn't break the current conversation —
 * it only means a restart before the NEXT successful write would lose
 * this turn's update.
 *
 * @param {string} sessionId
 */
export async function persistSessionState(sessionId) {
  if (!sessionId) return;
  const extra = sessionExtraStore.get(sessionId);
  if (!extra) return;
  try {
    const { lastAccessed, ...persistable } = extra; // lastAccessed is a RAM-cache-only bookkeeping field
    const { error } = await supabase
      .from(SESSION_STATE_TABLE)
      .upsert({ session_id: sessionId, state: persistable, updated_at: new Date().toISOString() });
    if (error) {
      console.error('[chatLog] persistSessionState failed (non-fatal — this process still has it in RAM):', error.message);
    }
  } catch (err) {
    console.error('[chatLog] persistSessionState threw (non-fatal — this process still has it in RAM):', err.message);
  }
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
// this function's write to it, are gone entirely now. The ONLY thing
// that persists to the database for a chat session is the bare
// `chat_sessions` row itself (id + patient_id) — see the note above
// hydrateSessionState/persistSessionState. If a `chat_messages` table
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