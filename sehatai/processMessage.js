// ============================================
// SehatAI: Message Processing Pipeline
// Uses Infermedica for triage, Groq for prose, and a separate DietBot
// service (see dietBotClient.js and dietbot-integration-architecture.md)
// for diet questions.
//
// UPDATED: routing between the symptom pipeline and the diet bot is no
// longer an AI-guessed classification on every message — it's an
// explicit mode the caller picks (e.g. testChat.js's /diet and
// /symptom commands, standing in for a real UI's mode buttons). That
// removes the one real risk auto-routing had (a misclassified message
// silently going to the wrong bot) at the cost of the user needing to
// say which one they want. Two entry points are exported:
//   - processPatientMessage() — the existing symptom pipeline
//   - processDietMessage()    — hands off to the DietBot service
// Both run through the SAME shared safety gates first (crisis,
// profanity, emergency keyword), so an emergency is caught no matter
// which mode the user happened to be in — see runSharedSafetyGates().
// ============================================

import { callAI, callAIStructured } from './callAi.js';
import { supabase } from './supabaseClient.js';
import {
  checkAISeverity,
  checkAICrisis,
  checkKeywordMatch,
  checkMisuseRequest,
  checkEmotionalKeywords,
  checkEmotionalConcern,
  composeEmotionalFollowUp,
  interpretEmotionalFollowUpAnswer,
  checkDangerousEvent,
  getEmergencyGuidance,
  CRISIS_PATTERNS,
  DIAGNOSIS_PATTERNS,
} from './safetyCheck.js';
import { checkObviouslyOffTopic, containsProfanity, checkOffTopic } from './offtopiccheck.js';
import { getPatientProfile } from './Patientprofile.js';
import { getProfileFacts } from './profileFact.js';
import { generateRecommendation } from './generaterecommendation.js';
import { verifyRecommendation } from './groundingVerifier.js';
import {
  getOrCreateSession,
  snapshotSession,
  restoreSession,
  logChatMessage,
  getAccumulatedSymptoms,
  appendAccumulatedSymptoms,
  clearStaleDurationSeverity,
  getMentionedConditions,
  appendMentionedConditions,
  markAllSymptomsFinalized,
  getLastSubject,
  saveLastSubject,
  getAwaitingClarificationAnswer,
  setAwaitingClarificationAnswer,
  getAwaitingFinalConfirmation,
  setAwaitingFinalConfirmation,
  getAwaitingDisambiguationAnswer,
  setAwaitingDisambiguationAnswer,
  getPendingAmbiguousValue,
  setPendingAmbiguousValue,
  getClarificationCount,
  incrementClarificationCount,
  resetClarificationCount,
  getUnaccountedComplaints,
  appendUnaccountedComplaints,
  getUnmatchedFinalizeAttempts,
  incrementUnmatchedFinalizeAttempts,
  resetUnmatchedFinalizeAttempts,
  getOffTopicStreak,
  incrementOffTopicStreak,
  resetOffTopicStreak,
  getNoSymptomStreak,
  incrementNoSymptomStreak,
  resetNoSymptomStreak,
  getAwaitingEmergencyAcknowledgment,
  setAwaitingEmergencyAcknowledgment,
  getPendingEmergencyMessage,
  setPendingEmergencyMessage,
  setPendingEmergencyReply,
  getPendingEmergencyQuestionType,
  getPendingEmergencyReply,
  recordEmergencyFlag,
  getEmergencyHistory,
  setEmergencyNoticePending,
  consumeEmergencyNoticePending,
  setPendingEmergencyQuestionType,
  resetSessionState,
  clearPendingQuestionFlags,
  appendRecentMessage,
  getRecentMessages,
  getLastQuestionAsked,
  setLastQuestionAsked,
  getLastQuestionCandidates,
  setLastQuestionCandidates,
  getLastQuestionSymptom,
  setLastQuestionSymptom,
  getReopenedTerms,
  addReopenedTerms,
  getAwaitingEmotionalFollowUp,
  setAwaitingEmotionalFollowUp,
  getLastEmotionalQuestion,
  setLastEmotionalQuestion,
  getDietSessionId,
  saveDietSessionId,
} from './chatLog.js';
import {
  getTriageForEvidence,
  parsePatientMessage,
  getSpecialistNamesForEvidence,
  TRIAGE_LEVEL_NOTES,
  describeChannel,
  MAX_CLARIFICATION_ROUNDS,
} from './infermedicaClient.js';
import { assessIntake, resolveFinalConfirmation, resolveDisambiguationAnswer, classifyPendingAnswerRelevance, generateEventFollowUp, isOverrideRequested, extractQuestionOptions } from './clarificationCheck.js';
import { classifySymptoms, formatClassifiedSymptoms, normalizeComplaint, describeSymptomsForParse, RESTART_INTENT_RE, BARE_AFFIRMATION_RE, PRONOUN_DENIAL_RE } from './symptomClassifier.js';
import { sanityFilterSymptoms } from './symptomSanityGate.js';
import { composeReply, composeModeFor, looksNonEnglish, listMarker, stripListMarkers } from './replyComposer.js';
import { getDietResponse } from './dietBotClient.js';
import { detectSubject } from './subjectDetection.js';
import { getRelevantLabValues } from './getlabvalues.js';
import { getPatientHistory } from './getPatientdata.js';
import { enforcePediatricRouting } from './pediatricSafetyNet.js';
import { extractUnaccountedComplaints } from './complaintDiff.js';

// CRISIS_PATTERNS and DIAGNOSIS_PATTERNS now come from safetyCheck.js
// (imported above) instead of being defined again here. This file used
// to keep its own byte-identical copies of both lists — harmless as long
// as nobody ever edited one without the other, but a genuine landmine:
// the next person to add a phrase to either regex list would have had
// two separate places to remember to edit, and safetyCheck.js already
// had a THIRD, already-divergent list of its own (DIAGNOSIS_REQUEST_PATTERNS,
// used internally by checkMisuseRequest) — three lists for two concepts,
// none of them reconciled. checkKeywordMatch was already imported from
// safetyCheck.js rather than duplicated, so this just extends the same
// "one source of truth, imported" convention to these two as well.

// How many CONSECUTIVE turns, while a question is pending, the domain
// classifier can judge genuinely off-topic before this app stops
// re-asking and just proceeds with whatever's on file (or, if nothing
// at all has been gathered yet, resets to a plain open question). See
// chatLog.js's offTopicStreak — a deliberately smaller, separate
// budget from MAX_CLARIFICATION_ROUNDS so noise doesn't eat a
// cooperative patient's real Q&A rounds, but still can't stall the
// conversation forever.
const MAX_OFF_TOPIC_STREAK = 2;

// "Which one?" follow-up to a bare "yes" on an either/or question (see the
// EITHER/OR backstop in STAGE 6). The prefix marks the follow-up so a
// second "yes" to it is read as "all of them".
const WHICH_OPTION_PREFIX = 'Just to be sure,';
const BOTH_ANSWER_RE = /^\s*(?:both|both of them|all|all of them|all of those|dono|dono\s+hi|donon|dono\s+hain|دونوں)\s*[.!]*\s*$/i;

// ------------------------------------------------------------------
// HELPERS
// ------------------------------------------------------------------

function cleanReply(text) {
  if (!text) return '';
  return text.replace(/\s+/g, ' ').trim();
}

function envelope(fields) {
  const env = {
    bot: 'specialist_router',
    kind: 'unknown',
    sessionId: null,
    isEmergency: false,
    isDiagnosisRequest: false,
    isOffTopic: false,
    offTopicReason: null,
    needsClarification: false,
    urgency: 'routine',
    subject: null,
    matchedSymptoms: [],
    matchSource: 'infermedica',
    source: 'infermedica',
    emergencyCategory: null,
    guidance: null,
    recommendation: null,
    verification: null,
    crossDomainNote: null,
    secondarySpecialist: null,
    triageLevel: null,
    recommendedChannel: null,
    // The actual numeric age/sex resolved for triage this turn (STAGE 5)
    // — null before that stage runs (crisis/off-topic/etc. exits above
    // it). Deliberately separate from `subject.ageGroup`, which is a
    // rough bucket used ONLY for a dependent's estimated age and is
    // null-by-design for a "self" subject — this is the real value
    // actually sent to Infermedica, surfaced here so it's visible in
    // testChat.js's debug output instead of easy to confuse with the
    // unrelated ageGroup field.
    resolvedAge: null,
    resolvedSex: null,
    reply: '',
    ...fields,
  };

  // Disclose that a recommendation used the patient's real, on-file
  // age/sex -- appended once, at finalization only. 'recommendation' and
  // 'emergency' are the only two kinds ever built with resolvedAge/
  // resolvedSex populated (every mid-conversation clarification/gate
  // envelope leaves them null), so this can never fire on anything but
  // an actual Infermedica-backed result. See STAGE 5's profile-
  // completeness gate for the other half of this fix.
  if (
    (env.kind === 'recommendation' || env.kind === 'emergency') &&
    env.resolvedAge != null &&
    env.resolvedSex != null &&
    env.reply
  ) {
    env.reply = `${env.reply} (This reflects your profile: age ${env.resolvedAge}, ${env.resolvedSex}.)`;
  }

  return env;
}

/**
 * Reply to small talk ("hi", "how are you", "thanks") — warm, not a
 * refusal. replyComposer.js lets the AI open by answering the small talk
 * itself (kind 'greeting'), in the patient's language; this text is the
 * part it must keep. Mid-conversation it invites updates instead of
 * starting over.
 *
 * @param {string} sessionId
 */
function greetingEnvelope(sessionId) {
  const ongoing = getAccumulatedSymptoms(sessionId).some((s) => s.present);
  const reply = ongoing
    ? "I'm still here. Is anything else bothering you, or has anything changed with what you told me about?"
    : "Hi! I'm here to help you work out which kind of doctor to see. How are you feeling — is anything bothering you physically?";
  return envelope({ kind: 'greeting', sessionId, reply });
}

function guidanceReply(guidance) {
  if (!guidance) return '';
  return `${guidance.headline} ${guidance.action}`.trim();
}

// ------------------------------------------------------------------
// EMERGENCY ACKNOWLEDGMENT — explicit product decision: a PHYSICAL
// emergency reply (severity/keyword/dangerous-event — never crisis/
// mental-health, which stays a hard stop) now gives the patient an
// actual choice instead of the app silently discarding whatever they
// said. Two buttons in the browser UI ("Notify someone" / "Continue
// with this chat"); typing the equivalent phrase works identically for
// any other client (CLI, curl, a future integration) — see
// EMERGENCY_CONTINUE_RE below.
//
// markEmergencyAcknowledgeable stores the RAW triggering message so
// choosing "Continue" can actually pick up from it — a deliberate,
// BOUNDED exception to this app's usual "never store the patient's
// literal sentence" rule (see chatLog.js's pendingEmergencyMessage doc
// comment): one turn only, cleared the instant it's used or superseded,
// never sent to Infermedica directly (it goes through the exact same
// classifySymptoms() path any other message would).
// ------------------------------------------------------------------
function markEmergencyAcknowledgeable(fields, sessionId, message, pendingQuestionType = null, { repeat = false } = {}) {
  // Re-showing the same notice isn't a new emergency.
  if (!repeat) recordEmergencyFlag(sessionId, fields?.emergencyCategory || null);
  setAwaitingEmergencyAcknowledgment(sessionId, true);
  setPendingEmergencyMessage(sessionId, message);
  // FIXED (root cause, demonstrated live): see chatLog.js's
  // pendingEmergencyQuestionType doc comment. Without this, a message
  // that triggered emergency WHILE answering a pending question (e.g.
  // "10 severity" answering a disambiguation re-ask) got replayed on
  // "Continue" as a bare, context-free fragment — and got catastrophically
  // misread by the crisis classifier. Storing which question type (if
  // any) was active lets STAGE -1 restore that exact state before
  // replaying, so the fragment is interpreted with the same meaning it
  // actually had, not as a stand-alone non-sequitur.
  setPendingEmergencyQuestionType(sessionId, pendingQuestionType);
  // EXPLICIT PRODUCT DECISION: the chat stays "locked" on this exact
  // notice until the patient actually continues — see
  // getPendingEmergencyReply's doc comment (chatLog.js). Stash the
  // reply fields as given (before actionable/actions get added below)
  // so STAGE -1 can re-show the identical notice on any reply that
  // isn't "Continue", without re-running the emergency check again.
  setPendingEmergencyReply(sessionId, fields);
  return {
    ...fields,
    actionable: true,
    actions: [
      { id: 'notify', label: 'Notify someone' },
      { id: 'continue', label: 'Continue with this chat' },
    ],
  };
}

// Matches the "Continue" button's own sent text AND any equivalent
// typed phrase, so this works identically from a client with no
// buttons (CLI, curl, a future integration) — not just the browser demo.
export const EMERGENCY_CONTINUE_RE = /^(?:ok(?:ay)?,?\s*|yes,?\s*|i\s+want\s+to\s+)?(continue|keep going|go ahead|carry on)(\s+with\s+(?:this|the)\s+chat)?(\s+please)?[.!]?$/i;

// Said once, on the first conversational reply after the patient carries
// on past an emergency notice — see STAGE -1 and runPatientMessageTurn.
export const EMERGENCY_CONTINUE_NOTE =
  "I'll keep in mind that something you described earlier was flagged as a possible emergency — " +
  'if it gets worse at any point, please call your local emergency number or go to the nearest emergency room. ';

// Appended to a recommendation when an emergency was flagged earlier in
// the same session — see finalizeAndRecommend.
export const EARLIER_EMERGENCY_REMINDER =
  ' Earlier in this chat, something you described was flagged as a possible emergency. If those symptoms ' +
  "come back or get worse, don't wait for an appointment — call your local emergency number or go to the " +
  'nearest emergency room.';

const DECLINE_DIAGNOSIS_NOTE =
  " I can't tell you what condition you have — that needs a doctor who can examine you. " +
  'What I can do is point you to the right kind of doctor.';

/**
 * Builds the deterministic, templated notice for a "cross-domain"
 * complaint — a symptom mentioned THIS turn that, evaluated on its
 * own, points to a different specialist than the one the session's
 * combined evidence is already using. See the STAGE 9c comment below
 * for why this is templated rather than left to the LLM.
 *
 * @param {string[]} newSymptomNames
 * @param {string} distinctSpecialist
 * @returns {string}
 */
// EXPLICIT PRODUCT DECISION: this used to defer ("raise it with a
// doctor separately, or start a new conversation") instead of actually
// recommending the second specialist — read live as a brush-off rather
// than a real answer, even though the specialist name was right there
// in the sentence. Now states it as a second, real recommendation
// alongside the first (see its call site below for how this is also
// surfaced on the structured envelope, not just in prose) — still
// never silently FOLDS the two into one combined recommendation
// (that's the actual reason this check exists at all), just doesn't
// make the patient do extra work to get the second answer either.
function buildCrossDomainNote(newSymptomNames, distinctSpecialist) {
  const names = newSymptomNames.filter(Boolean);
  const symptomText = names.length ? names.join(' and ') : 'what you just mentioned';
  const verb = names.length > 1 ? "don't" : "doesn't";
  return (
    ` I should also mention — ${symptomText} ${verb} look related to the rest of what we discussed. ` +
    `For that, you should see a${/^[aeiou]/i.test(distinctSpecialist) ? 'n' : ''} ${distinctSpecialist}.`
  );
}

/**
 * Which specialist (if any) the cross-domain note should name: one the
 * NEW round's symptoms point to that is neither the earlier round's
 * specialist nor already the main recommendation in this same reply.
 * BUG (found live): only the earlier round was checked, so the reply said
 * "Schedule an appointment with a Dermatologist ... redness and swelling
 * don't look related ... For that, you should see a Dermatologist" —
 * naming the main recommendation a second time as if it were separate.
 *
 * @param {{priorSpecialists: string[], newSpecialists: string[], mainSpecialists: string[]}} params
 * @returns {string|null}
 */
export function pickDistinctSpecialist({ priorSpecialists = [], newSpecialists = [], mainSpecialists = [] }) {
  if (!priorSpecialists.length) return null;
  const alreadyNamed = new Set([...priorSpecialists, ...mainSpecialists].map((s) => String(s).toLowerCase().trim()));
  return newSpecialists.find((s) => !alreadyNamed.has(String(s).toLowerCase().trim())) || null;
}

/**
 * The first present symptom still missing its duration and/or severity,
 * or null when every one has both. Used when the AI that composes the
 * next question is unavailable, so the fallback never re-asks something
 * the patient already answered.
 *
 * @param {Array<{term:string, present?:boolean, duration?:string|null, severity?:string|null}>} symptoms
 * @returns {{term: string, missing: 'duration'|'severity'|'both'}|null}
 */
export function findMissingDetail(symptoms) {
  for (const s of symptoms || []) {
    if (s.present === false) continue;
    const noDuration = !s.duration;
    const noSeverity = !s.severity;
    if (noDuration || noSeverity) {
      return { term: s.term, missing: noDuration && noSeverity ? 'both' : noDuration ? 'duration' : 'severity' };
    }
  }
  return null;
}

/**
 * @param {{term: string, missing: 'duration'|'severity'|'both'}} gap
 * @returns {string}
 */
export function fallbackDetailQuestion({ term, missing }) {
  if (missing === 'duration') return `How long have you had the ${term}?`;
  if (missing === 'severity') return `On a scale of 1 to 10, how bad is the ${term} right now?`;
  return `How long have you had the ${term}, and how bad is it on a scale of 1 to 10?`;
}

const GENERIC_SYMPTOM_WORDS_FOR_MENTION = new Set(['pain', 'ache', 'aches', 'aching', 'feeling', 'severe', 'mild', 'moderate']);

/**
 * When the pending question was about ONE recorded symptom ("How long have
 * you had the leg pain, and how severe is it?"), a bare answer ("2 days
 * and 5 out of 10") belongs to that symptom only. BUG (found live): the
 * classifier also applied it to every other known symptom — joint pain's
 * real "6/10, a few days" got overwritten, both looked like blanket
 * guesses, and the bot kept re-asking about each in turn. Drops entries
 * for OTHER already-known symptoms that the patient didn't name in this
 * message (they only carried the copied answer). New symptoms, denials,
 * and anything the patient actually named are kept.
 *
 * @param {Array} classified - classifySymptoms() output for this turn
 * @param {{askedSymptom: string|null, knownTerms: string[], message: string}} ctx
 * @returns {Array}
 */
export function keepAnswerOnAskedSymptom(classified, { askedSymptom, knownTerms = [], message = '' }) {
  if (!askedSymptom || !Array.isArray(classified) || classified.length < 2) return classified;
  const asked = askedSymptom.toLowerCase().trim();
  const known = new Set(knownTerms.map((t) => String(t).toLowerCase().trim()));
  const lowerMsg = String(message).toLowerCase();
  const namedInMessage = (term) =>
    term.toLowerCase().split(/\s+/).some((w) => w.length > 2 && !GENERIC_SYMPTOM_WORDS_FOR_MENTION.has(w) && lowerMsg.includes(w));
  return classified.filter((s) => {
    const key = String(s.term || '').toLowerCase().trim();
    if (key === asked) return true;
    if (s.present === false) return true;
    if (!known.has(key)) return true; // a newly mentioned symptom
    return namedInMessage(key);
  });
}

/**
 * The closing sentence about consultation type. BUG (found live): for an
 * in-person channel this read "This can likely be handled with an
 * in-person visit, though an in-person visit is always fine too".
 *
 * @param {string|null} channel - Infermedica recommended_channel
 * @returns {string} leading space included, or '' when there's no channel
 */
export function channelSentence(channel) {
  const label = describeChannel(channel);
  if (!label) return '';
  if (channel === 'personal_visit') return ' An in-person visit is the best way to have this looked at.';
  return ` This can likely be handled with ${label}, though an in-person visit is always fine too if you'd prefer it.`;
}

function normalizeText(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Infermedica's /parse can return MORE mentions than the patient
 * actually typed. For short or generic phrasing, its NLP sometimes
 * proposes several candidate findings under one general complaint,
 * expecting the caller to disambiguate with a follow-up question
 * rather than commit every candidate straight to evidence. Left
 * unfiltered, this let a symptom the patient never mentioned (e.g.
 * "Toothache") get silently added to accumulated evidence as
 * choice_id: 'present' from a message that only said "I also have eye
 * pain too" — which then fed real /triage calls for the rest of the
 * session and even triggered the cross-domain notice for a symptom
 * nobody reported.
 *
 * Each mention carries `orig_text` — the substring of the patient's
 * own message Infermedica says triggered the match. A mention is only
 * kept here if that substring genuinely appears in what the patient
 * typed this turn; anything else is a same-call NLP guess with no
 * basis in the actual text, so it's dropped rather than silently kept
 * as a confirmed finding. Same normalize-and-substring approach as
 * complaintDiff.js, for consistency.
 *
 * FIXED (root cause traced via the diagnostic logging in
 * finalizeAndRecommend below): Infermedica's /parse can return a
 * mention with `orig_text` MISSING entirely (not present in the
 * response at all) even when it's a perfectly correct match for the
 * text we just sent — a real logged case: "Abdominal pain, mild" came
 * back for a message that plainly said exactly that, with no
 * `orig_text` field at all. The old code treated "no orig_text" the
 * same as "orig_text present but doesn't match" and dropped both,
 * which silently zeroed out legitimate evidence and, combined with
 * other bugs (since fixed), produced an unrecoverable stuck loop.
 * These are NOT the same failure mode: /parse only ever runs against
 * the exact `message` we just sent it in this same call, so a mention
 * with orig_text OMITTED gives us nothing to verify against but also
 * no actual reason to distrust it — there is no separate list or
 * synonym table being consulted here, just Infermedica's own answer
 * about the text we handed it a moment ago. A mention that DOES carry
 * orig_text but that text genuinely isn't in what we sent is the real
 * red flag (a same-call invented guess) and is still dropped exactly
 * as before. This is a narrower, better-justified fail path, not a
 * blanket fail-open.
 *
 * @param {Array} rawMentions - raw /parse mentions for this turn
 * @param {string} message - the patient's raw message this turn
 * @returns {{ kept: Array, dropped: Array }}
 */
function filterGroundedMentions(rawMentions, message) {
  const normMsg = normalizeText(message);
  const kept = [];
  const dropped = [];
  for (const m of rawMentions) {
    const rawOrigText = m.orig_text;
    if (rawOrigText == null || String(rawOrigText).trim() === '') {
      kept.push(m);
      continue;
    }
    const origText = normalizeText(rawOrigText);
    if (origText && normMsg.includes(origText)) {
      kept.push(m);
    } else {
      dropped.push(m);
    }
  }
  return { kept, dropped };
}

/**
 * Builds Infermedica evidence for the patient's own on-file risk
 * factors (pregnancy, chronic conditions) — explicit product decision:
 * urgency previously only ever depended on symptom evidence, with
 * profileFacts used ONLY to ground the generated prose afterward, never
 * sent to Infermedica's /triage, /recommend_specialist, or /diagnosis
 * calls at all. That meant abdominal pain in a pregnant patient, or
 * chest pain in a diabetic, was scored identically to the same symptom
 * in a patient with neither — Infermedica's own triage engine DOES
 * weigh risk factors differently, it just was never being told about
 * them. Reuses the exact same /parse -> grounded-mention pipeline
 * already used for symptom text (parsePatientMessage +
 * filterGroundedMentions), restricted to mentions Infermedica itself
 * classifies as type 'risk_factor' — a condition string that happens to
 * ALSO read like a symptom is deliberately excluded here, so this can
 * never accidentally inject something into the CURRENT complaint's
 * symptom evidence or matchedSymptoms display list.
 *
 * Only 'condition' category profileFacts are used — allergies and
 * medications aren't risk factors in Infermedica's triage sense, and
 * family history belongs to a relative, not the patient.
 *
 * Non-fatal by design: a failure here still lets the recommendation
 * proceed on symptom evidence alone — this improves urgency accuracy,
 * it isn't a hard requirement to produce any recommendation at all.
 *
 * @param {Array<{value:string, category:string}>} profileFacts - by the
 *   time this is called, already merged (see the call site) to include
 *   both stored Supabase data AND any condition the patient mentioned
 *   live in this conversation — WIDENED (found live): this used to only
 *   ever see STORED profileFacts, so a condition mentioned in chat but
 *   never added to the patient's on-file profile was invisible to
 *   Infermedica's own risk-factor recognition. Confirmed live that
 *   Infermedica's /parse correctly recognizes something like "I am
 *   pregnant, currently 28 weeks" as a risk_factor mention with the same
 *   quality as stored data, so widening the INPUT to this same existing,
 *   already-safe pipeline (still grounded via filterGroundedMentions
 *   below) was all that was needed — no new trust placed in anything.
 * @param {number} age
 * @returns {Promise<Array<{id:string, choice_id:string, source:string}>>}
 */
async function getRiskFactorEvidence(profileFacts, age) {
  const conditionValues = (profileFacts || [])
    .filter((f) => f?.category === 'condition')
    .map((f) => f.value)
    .filter(Boolean);
  if (!conditionValues.length) return [];

  const text = conditionValues.map((v) => `${v}.`).join(' ');
  try {
    const rawMentions = await parsePatientMessage(text, age);
    const { kept } = filterGroundedMentions(rawMentions, text);
    const riskFactorMentions = kept.filter((m) => m.type === 'risk_factor');
    return riskFactorMentions.map((m) => ({
      id: m.id,
      choice_id: m.choice_id || 'present',
      source: 'initial',
    }));
  } catch (err) {
    console.error('[processMessage] getRiskFactorEvidence failed (non-fatal, proceeding on symptom evidence alone):', err.message);
    return [];
  }
}

/**
 * The AI emergency-severity check, as a reusable gate: runs
 * checkAISeverity and, if it flags the message, builds and logs the
 * exact same emergency envelope STAGE 2 always has. Returns null when
 * clear to continue.
 *
 * Factored out so it can run BEFORE a HEALTH_EVENT follow-up question
 * is composed (see STAGE 1 below), not just at STAGE 2 — a real gap:
 * "I took something I am allergic to" gets classified as HEALTH_EVENT
 * and used to return straight to "are you having any reaction?" with
 * NO severity check at all, since that return happens before STAGE 2
 * ever runs. An exposure/event message is exactly the kind of thing
 * that can already BE an emergency (a bad allergic reaction, a
 * poisoning) independent of whatever the patient says about current
 * symptoms, so it needs this check before, not only after, deciding to
 * just ask a follow-up.
 *
 * UPDATED (performance pass): split into checkAISeverity (the actual AI
 * call — pure, no side effects) below and buildSeverityEmergencyEnvelope
 * (the logging + envelope construction) so processMessage.js can fire
 * the AI call SPECULATIVELY, before it's known which (if any) of the 3
 * call sites that need a severity read will actually be reached this
 * turn — see processMessage.js's severityCheckPromise. Firing the OLD,
 * unsplit version speculatively would have been a real bug: it called
 * logChatMessage internally, so firing it for every message (even ones
 * that turn out to be off-topic/concerning and exit via a different
 * gate first) could write a spurious "emergency" audit log entry the
 * patient never actually saw. Splitting means the classification
 * (side-effect-free) can run early and unconditionally, while the
 * logging/envelope step only ever runs at the one call site that
 * actually consumes the result — this function is now just those two
 * steps composed together, kept for any caller that still wants the
 * simple non-speculative one-call version.
 *
 * @param {{message:string, callAI:Function, context?:string|null, sessionId:string, patientId:string}} params
 * @returns {Promise<object|null>}
 */
async function checkAiSeverityGate({ message, callAI, context = null, sessionId, patientId, pendingQuestionType = null }) {
  const aiSafety = await checkAISeverity(message, callAI, context);
  return buildSeverityEmergencyEnvelope({ aiSafety, message, sessionId, patientId, pendingQuestionType });
}

/**
 * The logging + envelope half of checkAiSeverityGate, split out so it
 * can be called separately from a speculatively-fired, already-resolved
 * checkAISeverity result — see checkAiSeverityGate's doc comment above.
 * Safe to call from more than one place in a turn's control flow, but
 * processMessage.js's branches are mutually exclusive (HEALTH_EVENT,
 * pendingEventFollowUp, and STAGE 2 never more than one per turn), so in
 * practice this only ever runs once per turn, exactly like before.
 *
 * @param {{aiSafety: {isEmergency:boolean, source?:string}|null, message:string, sessionId:string, patientId:string}} params
 * @returns {Promise<object|null>}
 */
async function buildSeverityEmergencyEnvelope({ aiSafety, message, sessionId, patientId, pendingQuestionType = null }) {
  if (!aiSafety?.isEmergency) return null;
  const category = 'default';
  const guidance = getEmergencyGuidance(category);
  const reply = guidanceReply(guidance);
  await logChatMessage({
    sessionId,
    patientId,
    message,
    isEmergency: true,
    urgency: 'emergency',
    response: { kind: 'emergency', source: aiSafety.source, category, guidance, reply },
  });
  return envelope(markEmergencyAcknowledgeable({
    kind: 'emergency',
    sessionId,
    isEmergency: true,
    urgency: 'emergency',
    guidance,
    reply: cleanReply(reply),
    source: aiSafety.source || 'ai_classifier',
    emergencyCategory: category,
  }, sessionId, message, pendingQuestionType));
}

/**
 * Dangerous-event gate — checked in ADDITION to checkAiSeverityGate
 * (never instead of it) at every point this app is about to compose a
 * HEALTH_EVENT follow-up question. See checkDangerousEvent's doc
 * comment (safetyCheck.js) for why these specific event categories
 * (allergen exposure, stabbing/gunshot/severe trauma, choking/
 * near-drowning, poisoning/overdose/electrocution/severe burns) skip
 * the "ask a follow-up, wait for the answer" flow every OTHER
 * HEALTH_EVENT still gets: a demonstrated live case showed neither the
 * opening message ("I ate something I am allergic too") nor a bare
 * "yess" answering "are you having a reaction?" individually reads as
 * urgent to checkAISeverity, even though together they describe a
 * patient who ate a known allergen and confirmed a reaction. The same
 * gap applies to any event that's inherently an emergency the moment
 * it's described ("I think I've been stabbed" needs no confirmed
 * symptom to already be urgent) — by the time that showed up as
 * classified symptoms, this app would already have asked and lost a
 * whole round to find out. This gate short-circuits that.
 *
 * @param {{message:string, callAI:Function, sessionId:string, patientId:string}} params
 * @returns {Promise<object|null>}
 */
async function checkDangerousEventGate({ message, callAI, sessionId, patientId, pendingQuestionType = null }) {
  const category = await checkDangerousEvent(message, callAI);
  if (!category) return null;
  const guidance = getEmergencyGuidance(category);
  const reply = guidanceReply(guidance);
  await logChatMessage({
    sessionId,
    patientId,
    message,
    isEmergency: true,
    urgency: 'emergency',
    response: { kind: 'emergency', source: 'dangerous_event', category, guidance, reply },
  });
  return envelope(markEmergencyAcknowledgeable({
    kind: 'emergency',
    sessionId,
    isEmergency: true,
    urgency: 'emergency',
    guidance,
    reply: cleanReply(reply),
    source: 'dangerous_event',
    emergencyCategory: category,
  }, sessionId, message, pendingQuestionType));
}

// ------------------------------------------------------------------
// SHARED SAFETY GATES — used by BOTH processPatientMessage() and
// processDietMessage(), so an emergency is caught regardless of which
// mode the user happens to be in. Profanity stays deterministic regex
// only (low-harm on a miss). The crisis gate below used to be
// deterministic regex ONLY, with no AI backstop at all — unlike the
// physical-emergency keyword check, which has always had checkAISeverity
// behind it. That meant a paraphrase of suicidal ideation or intent to
// harm someone that didn't match CRISIS_PATTERNS' fixed phrase list had
// NOTHING catching it. checkAICrisis (safetyCheck.js) now runs as a real
// AI backstop, judged by meaning, whenever the regex itself doesn't
// fire — mirroring the keyword+AI-classifier shape already used for
// physical emergencies. Both layers run UNCONDITIONALLY — never skipped
// for any reason (including a pending-question turn). Returns a
// ready-to-return envelope object if a gate fired, or null if the
// message is clear to continue.
// ------------------------------------------------------------------
async function runSharedSafetyGates({ message, sessionId, patientId, context = null, pendingQuestionType = null, mode = 'symptom', suppressHardEmergencyGate = false }) {
  // BUG (found live): replaying the ORIGINAL triggering message on
  // "Continue" (STAGE -1) will deterministically match the SAME
  // CRISIS_PATTERNS/EMERGENCY_TERMS keyword every single time — "I have
  // chest pain" replayed is still, obviously, "I have chest pain".
  // Without a way to skip these two checks specifically for that one
  // replay, Continue could never actually let a keyword-matched message
  // through to normal processing at all — it would just re-trigger the
  // identical emergency notice forever. suppressSeverityGate already
  // solves this exact problem for the AI-severity classifier (STAGE -1
  // sets it before replaying); suppressHardEmergencyGate is the same
  // idea for the deterministic crisis/keyword checks below, set by that
  // same STAGE -1 replay path and ONLY ever true for that one turn.
  // Every OTHER gate in this function (profanity, etc.) still runs
  // normally either way.
  if (!suppressHardEmergencyGate) {
  // Crisis gate: deterministic regex first (free, instant), AI backstop
  // second (catches paraphrases the regex list doesn't happen to cover).
  const crisisKeywordHit = CRISIS_PATTERNS.some((re) => re.test(message));
  let crisisFlagged = crisisKeywordHit;
  if (!crisisKeywordHit) {
    const aiCrisisFlag = await checkAICrisis(message, callAI, context);
    // FOUND live, then confirmed at 100% reproducibility (5/5 and 5/5
    // across two separate runs): the AI crisis backstop consistently
    // misreads plain physical chest complaints ("I have chest pain", "I
    // have chest tightness") as a mental-health crisis, with zero
    // genuine crisis language present — while correctly clearing an
    // unrelated physical complaint ("I have a bad headache") every time.
    // checkAICrisis is ONLY ever reached here when the deterministic
    // CRISIS_PATTERNS regex found NOTHING (see the short-circuit above),
    // so a genuine crisis that happens to also mention a physical
    // symptom ("I don't want to live anymore, my chest also hurts")
    // never reaches this cross-check at all — CRISIS_PATTERNS already
    // caught it upstream. What's left here is exactly the failure mode
    // observed: no deterministic crisis evidence whatsoever, but the AI
    // says crisis anyway, on a message that ALSO deterministically
    // matches a physical emergency term. Given that combination, a
    // misclassification is far more likely than a genuinely indirect,
    // paraphrased crisis with a coincidental physical-emergency keyword
    // — defer to the physical-emergency reading (handled by
    // checkKeywordMatch below, unaffected by this) rather than trust the
    // AI's crisis claim.
    if (aiCrisisFlag && checkKeywordMatch(message).isEmergency) {
      console.warn(
        `[processMessage] AI crisis classifier flagged "${message}" as CRISIS, but it also deterministically ` +
        'matches a physical-emergency keyword with no crisis-pattern evidence at all — treating as a likely ' +
        'misclassification and deferring to the physical-emergency path instead.'
      );
    } else {
      crisisFlagged = aiCrisisFlag;
    }
  }
  if (crisisFlagged) {
    const guidance = getEmergencyGuidance('mental_health');
    const reply = guidanceReply(guidance);
    const source = crisisKeywordHit ? 'crisis_keyword' : 'crisis_ai_classifier';
    await logChatMessage({
      sessionId,
      patientId,
      message,
      isEmergency: true,
      urgency: 'emergency',
      response: { kind: 'emergency', source, category: 'mental_health', guidance, reply },
    });
    // EXPLICIT PRODUCT DECISION (changed): a crisis now gets the SAME
    // Notify/Continue treatment as a physical emergency — always shows
    // "Notify someone", and the chat stays locked on this notice (see
    // markEmergencyAcknowledgeable/STAGE -1's re-show logic) until the
    // patient picks one. Clicking Continue replays the ORIGINAL
    // message through the normal pipeline exactly like a physical
    // emergency does — including any physical symptom content that
    // rode along with it. No longer wipes accumulated session state on
    // its own; clearPendingQuestionFlags still clears any unrelated
    // pending question from before the crisis fired, since that's a
    // different concern (stale question-routing) from the symptom data
    // itself.
    clearPendingQuestionFlags(sessionId);
    return envelope(markEmergencyAcknowledgeable({
      kind: 'emergency',
      sessionId,
      isEmergency: true,
      urgency: 'emergency',
      guidance,
      reply: cleanReply(reply),
      source,
      emergencyCategory: 'mental_health',
    }, sessionId, message, pendingQuestionType));
  }
  } // end !suppressHardEmergencyGate (crisis)

  // Profanity (regex, deterministic).
  if (containsProfanity(message)) {
    const reply = "Let's keep this respectful — I'm happy to help as soon as you tell me what you need.";
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'off_topic', offTopicReason: 'profanity', reply },
    });
    return envelope({
      kind: 'off_topic',
      sessionId,
      isOffTopic: true,
      offTopicReason: 'profanity',
      reply: cleanReply(reply),
    });
  }

  if (!suppressHardEmergencyGate) {
  // Emergency KEYWORD match (regex, e.g. "chest pain") — this is what
  // makes the shared-gate approach matter: it fires even if the user
  // is in diet mode and asks something like "I'm having chest pain,
  // what should I eat" — the message never reaches the diet bot.
  const keywordSafety = checkKeywordMatch(message);
  if (keywordSafety.isEmergency) {
    const category = keywordSafety.category || 'default';
    const guidance = getEmergencyGuidance(category);
    const reply = guidanceReply(guidance);
    await logChatMessage({
      sessionId,
      patientId,
      message,
      isEmergency: true,
      urgency: 'emergency',
      response: { kind: 'emergency', source: keywordSafety.source, category, guidance, reply },
    });
    // Diet mode never offers "Continue with this chat" for a physical
    // emergency — the diet bot isn't equipped to handle a symptom
    // emergency, so instead of stashing the message for replay (the
    // symptom pipeline's STAGE -1 behavior), just tell the patient to
    // switch to the specialist bot. No markEmergencyAcknowledgeable call
    // here on purpose: no actionable/continue state is set for diet mode.
    if (mode === 'diet') {
      return envelope({
        kind: 'emergency',
        sessionId,
        isEmergency: true,
        urgency: 'emergency',
        guidance,
        reply: cleanReply(`${reply} Please switch to the specialist bot for that.`),
        source: keywordSafety.source || 'keyword',
        emergencyCategory: category,
      });
    }
    // EXPLICIT PRODUCT DECISION (changed again): this briefly had no
    // actionable/continue state at all (on the reasoning that every
    // EMERGENCY_TERMS category is already curated to be unambiguously
    // time-critical, so extra latitude had no clear justification) —
    // reversed back to the original design: a hard keyword match gets
    // the same Notify/Continue treatment as any other physical
    // emergency, and now also the same locked-until-acknowledged
    // handling as crisis (see STAGE -1's re-show logic). Accumulated
    // session state (accumulatedSymptoms/mentionedConditions) is still
    // left alone here — a false-positive physical keyword match
    // shouldn't wipe legitimate symptoms already on file — but every
    // "awaiting an answer to X" pending-question flag from BEFORE this
    // emergency fired gets cleared (clearPendingQuestionFlags), a
    // separate, real bug found live: a pending emotional follow-up
    // question survived a chest-pain emergency in between, and
    // "continue" got misread as answering it, producing a mental-health
    // referral with no connection to the chest pain at all.
    clearPendingQuestionFlags(sessionId);
    return envelope(markEmergencyAcknowledgeable({
      kind: 'emergency',
      sessionId,
      isEmergency: true,
      urgency: 'emergency',
      guidance,
      reply: cleanReply(reply),
      source: keywordSafety.source || 'keyword',
      emergencyCategory: category,
    }, sessionId, message, pendingQuestionType));
  }
  } // end !suppressHardEmergencyGate (keyword match)

  return null;
}

// ------------------------------------------------------------------
// PER-SESSION SERIALIZATION
//
// The pipeline below reads accumulatedSymptoms/awaitingClarification-
// Answer/etc. (chatLog.js's sessionExtraStore) near the START of a
// turn and writes updates to them only after several `await`ed AI
// calls later. Node is single-threaded, but `await` still yields the
// event loop — so if the SAME sessionId gets two messages close
// together (a double-submit, a client retry after a slow response it
// gave up waiting on, two browser tabs on one session), their two
// pipeline runs can interleave: both read the same pre-turn state,
// and whichever one writes back second silently overwrites the
// other's update — e.g. a symptom the first turn added disappearing
// because the second turn's appendAccumulatedSymptoms was computed
// from a snapshot that didn't have it yet.
//
// Fix: serialize processing per sessionId. withSessionLock chains each
// new call for a given sessionId onto the promise for the PREVIOUS
// call for that same sessionId, so turn 2 doesn't start its own reads
// until turn 1 has fully finished (including its persistSessionState
// write). Different sessions are completely unaffected — this only
// serializes turns that could actually race with each other. This is
// an in-process lock (a plain Map), matching this app's current
// single-instance architecture (see chatLog.js's persistence doc
// comment) — it does NOT protect against two turns for the same
// session landing on two DIFFERENT server instances; that would need a
// database-level lock (e.g. a row version / SELECT ... FOR UPDATE on
// chat_session_state), out of scope for this pass.
// ------------------------------------------------------------------
const sessionQueues = new Map(); // sessionId -> promise the NEXT turn for this session waits on

// A turn that hangs — a network call with no timeout of its own that
// simply never resolves or rejects (a stalled connection, a dropped
// response) — would, without this cap, wedge EVERY LATER message for
// the SAME session behind it forever: the queue only advances once the
// previous link settles, and "never settling" means every future
// withSessionLock call for that sessionId never even starts, with no
// error surfaced anywhere (the caller's HTTP request for that later
// turn would just hang indefinitely too). This doesn't cancel the hung
// turn itself — nothing in this codebase wires callAI/Infermedica
// calls for cancellation, so there's no way to actually abort it — it
// only stops the QUEUE from waiting on it past this point, so later
// messages for the session can proceed. The hung turn's own caller
// still gets its real eventual result or error via the `run` promise
// returned below, whenever (if ever) that settles; only what the NEXT
// turn waits on is capped.
const SESSION_LOCK_TIMEOUT_MS = 45 * 1000;

function withSessionLock(sessionId, fn) {
  const previous = sessionQueues.get(sessionId) || Promise.resolve();
  // .catch(() => {}) before chaining `fn` so one turn throwing doesn't
  // wedge every LATER turn behind a permanently-rejected promise.
  const run = previous.catch(() => {}).then(fn);

  let timeoutHandle;
  const timedOut = new Promise((resolve) => {
    timeoutHandle = setTimeout(() => {
      console.error(
        `[processMessage] a turn for session ${sessionId} exceeded ${SESSION_LOCK_TIMEOUT_MS}ms — ` +
        "releasing the session lock so later messages for this session aren't wedged behind it. " +
        'The slow turn itself keeps running in the background and its own caller still gets its real result or error.'
      );
      resolve();
    }, SESSION_LOCK_TIMEOUT_MS);
  });

  // What the NEXT queued turn actually waits on: the real result,
  // capped by the timeout above, whichever comes first. `.then(() =>
  // {}, () => {})` normalizes `run`'s outcome (success or failure) to a
  // resolved value here, since the queue only cares "is the previous
  // slot free now," never the previous turn's actual result.
  const settledForQueue = Promise.race([run.then(() => {}, () => {}), timedOut])
    .finally(() => clearTimeout(timeoutHandle));

  sessionQueues.set(sessionId, settledForQueue);
  // Once this is still the most-recently-queued entry AND it has
  // resolved, remove it so sessionQueues doesn't grow forever holding
  // settled promises for sessions nobody is using anymore.
  settledForQueue.then(() => {
    if (sessionQueues.get(sessionId) === settledForQueue) sessionQueues.delete(sessionId);
  });

  return run;
}

// ------------------------------------------------------------------
// DIET BOT PIPELINE
// Explicit-mode entry point (the caller — e.g. testChat.js's /diet
// command, standing in for a real UI's mode button — decides the user
// wants diet advice, rather than an AI classifier guessing per
// message). Still runs through the same shared safety gates as the
// symptom pipeline, plus its own AI emergency fallback (there's no
// pending-question concept on the diet side, so this always runs,
// never skipped).
// ------------------------------------------------------------------
export async function processDietMessage(message, patientId, existingSessionId = null) {
  if (!patientId) throw new Error('processDietMessage requires a patientId');

  const session = await getOrCreateSession(patientId, existingSessionId);
  const sessionId = session.id;

  // See "PER-SESSION SERIALIZATION" above — resolved AFTER getting the
  // real sessionId (so two brand-new sessions, which can't race with
  // anything yet, aren't needlessly serialized against each other), but
  // BEFORE anything reads or writes session state below.
  try {
    return await withSessionLock(sessionId, () => runDietMessagePipeline({ message, patientId, sessionId }));
  } catch (err) {
    // Same fix as processPatientMessage's equivalent catch above — an
    // uncaught AI-provider failure (checkAISeverity, or any of the
    // several other unguarded AI calls in the diet gates) used to bubble
    // all the way to a bare "Internal server error" with no context.
    console.error('[processMessage] uncaught error during diet pipeline (likely an AI provider failure):', err.message);
    return envelope({
      kind: 'error',
      sessionId,
      reply: "I'm having trouble processing that right now — could you try sending it again in a moment?",
    });
  }
}

async function runDietMessagePipeline({ message, patientId, sessionId }) {
  // Diet mode never offers "Continue with this chat" for a physical
  // emergency (see runSharedSafetyGates' mode==='diet' branch and the
  // checkAISeverity branch below — both just tell the patient to switch
  // to the specialist bot instead of stashing the message for replay).
  // This defensively clears any leftover awaitingEmergencyAcknowledgment
  // state in case the SAME session had an emergency flagged while in
  // symptom mode moments earlier and the caller then routed the next
  // turn into diet mode instead of replaying "Continue" through the
  // symptom pipeline — that stashed message was for symptom triage, not
  // a diet query, so it's discarded here rather than replayed.
  if (getAwaitingEmergencyAcknowledgment(sessionId)) {
    setAwaitingEmergencyAcknowledgment(sessionId, false);
    setPendingEmergencyMessage(sessionId, null);
  }

  const gateResult = await runSharedSafetyGates({ message, sessionId, patientId, mode: 'diet' });
  if (gateResult) return gateResult;

  const aiSafety = await checkAISeverity(message, callAI);
  if (aiSafety?.isEmergency) {
    const guidance = getEmergencyGuidance('default');
    const reply = guidanceReply(guidance);
    await logChatMessage({
      sessionId,
      patientId,
      message,
      isEmergency: true,
      urgency: 'emergency',
      response: { kind: 'emergency', source: aiSafety.source, category: 'default', guidance, reply },
    });
    return envelope({
      kind: 'emergency',
      sessionId,
      isEmergency: true,
      urgency: 'emergency',
      guidance,
      reply: cleanReply(`${reply} Please switch to the specialist bot for that.`),
      source: aiSafety.source || 'ai_classifier',
      emergencyCategory: 'default',
    });
  }

  const dietSessionId = await getDietSessionId(patientId);
  try {
    const dietResult = await getDietResponse({ patientId, query: message, sessionId: dietSessionId });
    if (dietResult?.session_id) await saveDietSessionId(patientId, dietResult.session_id, sessionId);

    const reply = dietResult?.reply || "I couldn't come up with dietary advice for that just now — could you rephrase?";
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'diet', reply, dietSessionId: dietResult?.session_id || null },
    });
    return envelope({
      kind: 'diet',
      sessionId,
      source: 'dietbot',
      reply: cleanReply(reply),
    });
  } catch (err) {
    console.error('[processMessage] DietBot call failed:', err.message);
    const reply = "I'm having trouble reaching the diet assistant right now. Please try again in a moment.";
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'diet_error', reply },
    });
    return envelope({
      kind: 'diet_error',
      sessionId,
      source: 'dietbot',
      reply: cleanReply(reply),
    });
  }
}

// ------------------------------------------------------------------
// SYMPTOM PIPELINE (main)
// ------------------------------------------------------------------
export async function processPatientMessage(message, patientId, existingSessionId = null) {
  if (!patientId) throw new Error('processPatientMessage requires a patientId');

  const session = await getOrCreateSession(patientId, existingSessionId);
  const sessionId = session.id;

  // See "PER-SESSION SERIALIZATION" above — resolved AFTER getting the
  // real sessionId, BEFORE hydrateSessionState/the pipeline/
  // persistSessionState below run, so two turns for the SAME session
  // can never interleave their reads and writes of accumulatedSymptoms/
  // the awaiting-question flags/etc.
  return withSessionLock(sessionId, () => runPatientMessageTurn({ message, patientId, sessionId, session }));
}

/**
 * Records the symptoms in a message that triggered an emergency notice,
 * without replaying it as a turn — used when the patient carries on by
 * typing something new instead of pressing Continue, so what they first
 * described still reaches the final assessment.
 *
 * @param {string} sessionId
 * @param {string} flaggedMessage
 */
async function absorbFlaggedMessageSymptoms(sessionId, flaggedMessage) {
  const known = getAccumulatedSymptoms(sessionId).filter((s) => s.present).map((s) => s.term);
  const result = await classifySymptoms(flaggedMessage, known, null);
  if (result._classificationFailed) return;
  const symptoms = sanityFilterSymptoms(result.symptoms.filter((s) => s.present), 'emergency-carryover');
  if (symptoms.length) appendAccumulatedSymptoms(sessionId, symptoms);
  if (result.mentionedConditions?.length) appendMentionedConditions(sessionId, result.mentionedConditions);
}

// sessionId -> { removed, stillPresent } for the CURRENT turn only — set
// in the pipeline, consumed (and always cleared) by runPatientMessageTurn.
const pendingRemovalNotes = new Map();

function setRemovalNote(sessionId, removed, stillPresent) {
  pendingRemovalNotes.set(sessionId, { removed, stillPresent: stillPresent.map((s) => ({ ...s })) });
}

/**
 * Tells the patient which symptom(s) this turn removed and what's still on
 * the list — unless the reply already says it (the pure-retraction and
 * final-gate replies have their own wording for this).
 *
 * @param {object} result - envelope
 * @param {string[]} removed
 * @param {Array} stillPresent
 * @returns {object}
 */
export function withRemovalNote(result, removed, stillPresent) {
  if (!result || !removed?.length || !KINDS_THAT_CONTINUE_PHYSICAL_FLOW.has(result.kind)) return result;
  if (/no longer have/i.test(result.reply || '')) return result;
  const listed = /so far i (?:still )?have/i.test(result.reply || '');
  const remaining = stillPresent?.length && !listed
    ? ` What I have now: ${listMarker(formatClassifiedSymptoms(stillPresent))}.`
    : '';
  return { ...result, reply: `Got it, I've removed ${removed.join(', ')} from your list.${remaining} ${result.reply}` };
}

/**
 * Prepends the one-time "I'll keep the earlier emergency in mind" note to
 * the first conversational reply after the patient carries on past an
 * emergency notice. Never on another emergency reply (that has its own,
 * stronger guidance).
 *
 * @param {object} result - envelope
 * @param {string} sessionId
 * @returns {object}
 */
export function applyEmergencyContinueNote(result, sessionId) {
  if (!result || !KINDS_THAT_CONTINUE_PHYSICAL_FLOW.has(result.kind)) return result;
  if (!consumeEmergencyNoticePending(sessionId)) return result;
  return { ...result, reply: `${EMERGENCY_CONTINUE_NOTE}${result.reply}` };
}

/**
 * Last step before a reply leaves the pipeline: the AI rewrites fixed
 * replies naturally in the patient's language, and translates AI-written
 * replies / recommendations / emergency notices for non-English
 * conversations (see replyComposer.js for the policy and its
 * deterministic checks). Symptom lists stay exact.
 *
 * @param {object} result - envelope
 * @param {string} sessionId
 * @param {string} message - this turn's patient message
 * @returns {Promise<object>}
 */
async function composeFinalReply(result, sessionId, message) {
  const recentPatientMessages = [
    ...getRecentMessages(sessionId).filter((m) => m.role === 'patient').map((m) => m.text),
    message,
  ];
  const mode = composeModeFor(result, looksNonEnglish(recentPatientMessages));
  const mustKeep = result.kind === 'recommendation' && result.recommendation?.specialist_recommended
    ? [result.recommendation.specialist_recommended]
    : [];
  const { text, composed } = await composeReply({
    reply: result.reply,
    mode,
    recentPatientMessages,
    kind: result.kind,
    mustKeep,
  });
  const { aiWritten, ...rest } = result;
  // replyOriginal: the pipeline's own wording before composing — for
  // debugging and the test scripts, whose checks can't depend on the AI's
  // phrasing. The UI shows `reply`.
  return { ...rest, reply: text, replyOriginal: stripListMarkers(result.reply), replyComposed: composed };
}

async function runPatientMessageTurn({ message, patientId, sessionId, session }) {
  // Session state is RAM-only (see chatLog.js's SESSION_TTL_MS) — nothing
  // to load before the turn or save after it.
  pendingRemovalNotes.delete(sessionId);
  // If this turn fails, put the session back exactly as it was, so the
  // patient's retry (or next message) keeps the conversation's context —
  // see chatLog.js's snapshotSession.
  const before = snapshotSession(sessionId);
  try {
    const result = await runPatientMessagePipeline({ message, patientId, sessionId, session });
    if (result?.kind === 'error') {
      restoreSession(sessionId, before);
      pendingRemovalNotes.delete(sessionId);
      return composeFinalReply(result, sessionId, message);
    }
    const removal = pendingRemovalNotes.get(sessionId);
    pendingRemovalNotes.delete(sessionId);
    const noted = applyEmergencyContinueNote(
      withRemovalNote(
        applyMixedDiagnosisDecline(applyMixedEmotionalAcknowledgment(result, message), message),
        removal?.removed,
        removal?.stillPresent
      ),
      sessionId
    );
    // A fixed note added on top of an AI-written reply makes the whole
    // reply a template again, so it gets composed as one piece.
    if (noted !== result) noted.aiWritten = false;
    const finalResult = await composeFinalReply(noted, sessionId, message);
    // See chatLog.js's appendRecentMessage doc comment — short, in-RAM-
    // only turn history for the relevance classifier. Appended here
    // (not earlier) so it reflects the FINAL reply actually sent, after
    // the mixed-emotional/diagnosis-decline wrappers run.
    appendRecentMessage(sessionId, message, finalResult?.reply);
    return finalResult;
  } catch (err) {
    // FOUND live: checkAISeverity (and several other AI calls scattered
    // through the pipeline, e.g. checkOffTopic/classifyPendingAnswerRelevance
    // in the Promise.all above) have NO individual try/catch — they're
    // deliberately allowed to throw rather than silently fail (same
    // "fail loud, don't dress up a failure as a normal answer" principle
    // as checkAICrisis), which is the right call for a safety check. But
    // "fail loud" was reaching all the way past this function, past
    // webServer.js, and out as a bare "Internal server error. Please try
    // again." with zero indication of what actually happened — observed
    // live, during a period where Gemini's daily quota and Groq's daily
    // token budget were both exhausted at once. The full error still
    // goes to the server log (for real diagnosis); the patient gets an
    // honest, specific reply instead of a generic server error.
    console.error('[processMessage] uncaught error during pipeline (likely an AI provider failure):', err.message);
    pendingRemovalNotes.delete(sessionId);
    restoreSession(sessionId, before);
    return envelope({
      kind: 'error',
      sessionId,
      reply: "I'm having trouble processing that right now — could you try sending it again in a moment?",
    });
  }
}

// EXPLICIT PRODUCT DECISION: when a message mixes ordinary emotional
// language ("I feel really depressed...") with a physical symptom in
// the SAME message, STAGE 3b correctly classifies it NOT_EMOTIONAL_ONLY
// (checkEmotionalConcern's own few-shot example: "my chest has been
// tight and I've also been really anxious about it" -> route on the
// physical symptom, the anxiety can be acknowledged but shouldn't
// replace triage) — but nothing ever actually acknowledged it. The
// emotional content was silently dropped (correctly never tracked as a
// physical symptom, but also never mentioned back to the patient at
// all), which reads as the bot simply not having heard that part.
//
// This is the single choke point every physical-symptom-continuing
// reply passes through exactly once per turn — see
// runPatientMessageTurn above — regardless of which of the many
// internal STAGE 6/7/finalize branches produced it, so this doesn't
// need to be threaded through each of those return points individually.
// Deliberately a CHEAP, deterministic check (checkEmotionalKeywords —
// the same regex pre-filter STAGE 3b already trusts as a first pass),
// not a new AI call: the cost of a false positive here is just an extra
// warm sentence, not a safety-relevant miss, so it doesn't need the
// same AI backstop the actual routing decision (STAGE 3b) does.
//
// Only applied to a `kind` that means "continuing the physical-symptom
// conversation" — never to an emergency reply (would dilute real
// urgency guidance with an unrelated pleasantry) or to the dedicated
// emotional-support flow's own replies (kinds 'emotional_followup_question'
// / 'emotional_support' / 'emotional_support_resolved' already carry
// their own tailored acknowledgment — this would double up on it).
const MIXED_EMOTIONAL_ACK =
  "I'm sorry you're feeling that way — I'm a physical-health bot, so for that I'd really encourage " +
  'talking to a professional or someone close to you. ';
const KINDS_THAT_CONTINUE_PHYSICAL_FLOW = new Set(['clarification', 'recommendation']);
// BUG (found live): "just diagnose me already" — a fresh message with a
// clear DIAGNOSIS_PATTERNS match but no symptom content of its own —
// got judged 'off_topic' by STAGE 1's domain classifier (nothing
// health-specific to point to) BEFORE ever reaching
// applyMixedDiagnosisDecline, which only covered 'clarification'/
// 'recommendation'. The patient explicitly asked to be diagnosed and
// got a generic "I can only help with health symptoms..." redirect
// with zero acknowledgment that a diagnosis was even asked for.
// Diagnosis-decline coverage needs 'off_topic' too — deliberately a
// SEPARATE set from KINDS_THAT_CONTINUE_PHYSICAL_FLOW rather than
// adding 'off_topic' to the shared one, since the emotional-
// acknowledgment wrapper below has no equivalent demonstrated gap and
// widening its scope isn't warranted by anything actually observed.
const KINDS_THAT_GET_DIAGNOSIS_DECLINE = new Set(['clarification', 'recommendation', 'off_topic']);

export function applyMixedEmotionalAcknowledgment(result, message) {
  if (!result || !KINDS_THAT_CONTINUE_PHYSICAL_FLOW.has(result.kind)) return result;
  if (!checkEmotionalKeywords(message)) return result;
  return { ...result, reply: cleanReply(`${MIXED_EMOTIONAL_ACK}${result.reply}`) };
}

// Live-demonstrated case, same failure shape as the mixed-emotional bug
// above: "a couple of days. What do you think i could have" — answering
// a pending clarifying question (duration) WHILE also asking for a
// diagnosis in the same message. The diagnosis-request short-circuit
// higher up in this function (STAGE ~3, wasAnsweringPendingQuestion
// case) fires whenever checkMisuseRequest correctly flags this as a
// genuine diagnosis request — and until now, that meant returning
// `kind: 'diagnosis_declined'` IMMEDIATELY, discarding the "a couple of
// days" duration answer entirely rather than recording it and simply
// appending a decline note the way finalizeAndRecommend already does
// with DECLINE_DIAGNOSIS_NOTE for a diagnosis request answering the
// FINAL confirmation. Same fix shape as applyMixedEmotionalAcknowledgment:
// a cheap, deterministic post-check (DIAGNOSIS_PATTERNS — the same regex
// already used for the fresh-message fast path) applied AFTER the whole
// pipeline runs, so it catches a diagnosis-request phrase riding along
// with real information REGARDLESS of which internal STAGE actually
// produced this turn's reply, without needing to thread a flag through
// every one of this function's many return points. Guards against
// double-appending for the case where finalizeAndRecommend's own
// DECLINE_DIAGNOSIS_NOTE already fired this same turn.
export function applyMixedDiagnosisDecline(result, message) {
  if (!result || !KINDS_THAT_GET_DIAGNOSIS_DECLINE.has(result.kind)) return result;
  if (result.kind === 'diagnosis_declined' || result.isDiagnosisRequest) return result; // already handled
  // A diagnosis-decline note appended to a concerning-content/profanity
  // off-topic reply would read as a bizarre non-sequitur — those get
  // their own dedicated, serious reply and shouldn't be diluted with an
  // unrelated clinical note, same reasoning as the emergency exclusion
  // below.
  if (result.kind === 'off_topic' && (result.offTopicReason === 'concerning_content' || result.offTopicReason === 'profanity')) return result;
  if (!DIAGNOSIS_PATTERNS.some((re) => re.test(message))) return result;
  if (/can't tell you what condition|that needs a doctor who can examine you/i.test(result.reply || '')) return result;
  // Also stamp isDiagnosisRequest: true on the merged result — the
  // dedicated diagnosis_declined envelope used to be the ONLY place
  // this field was ever set, but a fresh diagnosis-request message that
  // ALSO has real content (see the STAGE 3 doc comment above) no longer
  // takes that path at all; it flows through normal processing and gets
  // the note appended here instead. Anything downstream (including this
  // app's own tests) that reads isDiagnosisRequest to know whether a
  // turn was a diagnosis request needs an accurate answer regardless of
  // which path produced the final reply.
  return { ...result, isDiagnosisRequest: true, reply: cleanReply(`${result.reply} ${DECLINE_DIAGNOSIS_NOTE.trim()}`) };
}

async function runPatientMessagePipeline({ message, patientId, sessionId, session }) {
  // ================================================================
  // STAGE -1: EMERGENCY ACKNOWLEDGMENT (before everything else,
  // including the crisis/severity gates below) — see
  // markEmergencyAcknowledgeable's doc comment above for the full
  // design. A PHYSICAL emergency reply left this flag set for exactly
  // one turn, with the raw triggering message stashed. Two outcomes:
  //   - the patient chose to continue (typed/clicked the equivalent of
  //     "Continue with this chat"): pick up from what they originally
  //     said instead of asking them to repeat it into a void. This
  //     REASSIGNS `message` to the stored original text and sets
  //     suppressSeverityGate (AI severity classifier) AND
  //     suppressHardEmergencyGate (deterministic crisis/keyword checks
  //     — found live: without this, the replayed text deterministically
  //     re-matches the exact same pattern every time, since it's the
  //     literal same message, and Continue could never actually let a
  //     keyword/crisis-matched message through to normal processing at
  //     all) so neither immediately re-flags the same content. Only for
  //     this one replay turn.
  //   - anything else — the patient typed something new instead of
  //     pressing Continue. PRODUCT DECISION (replaces the earlier
  //     "stay locked on the notice" behavior): that counts as carrying
  //     on too. The flagged message's symptoms are still recorded (so
  //     they reach the final assessment), then the NEW message is
  //     processed normally with every safety gate active — it's new
  //     content, so it can flag an emergency of its own.
  // Either way the bot remembers the emergency (recordEmergencyFlag in
  // markEmergencyAcknowledgeable): the next reply acknowledges it once,
  // and the recommendation reminds the patient about it.
  // Crisis/mental-health notices never reach here: they stay a hard stop.
  // ================================================================
  let suppressSeverityGate = false;
  let suppressHardEmergencyGate = false;
  if (getAwaitingEmergencyAcknowledgment(sessionId)) {
    const stashed = getPendingEmergencyMessage(sessionId);
    const stashedQuestionType = getPendingEmergencyQuestionType(sessionId);
    const stashedReply = getPendingEmergencyReply(sessionId);
    // A CRISIS notice (self-harm / suicide) is never typed past: anything
    // other than an explicit Continue shows the same notice again. Only
    // physical emergencies treat a new message as carrying on.
    if (stashedReply?.emergencyCategory === 'mental_health' && !EMERGENCY_CONTINUE_RE.test(message.trim())) {
      return envelope(markEmergencyAcknowledgeable({ ...stashedReply }, sessionId, stashed, stashedQuestionType, { repeat: true }));
    }
    setAwaitingEmergencyAcknowledgment(sessionId, false);
    setPendingEmergencyMessage(sessionId, null);
    setPendingEmergencyQuestionType(sessionId, null);
    setPendingEmergencyReply(sessionId, null);
    if (stashed) setEmergencyNoticePending(sessionId, true);
    if (stashed && !EMERGENCY_CONTINUE_RE.test(message.trim())) {
      await absorbFlaggedMessageSymptoms(sessionId, stashed);
    }
    if (stashed && EMERGENCY_CONTINUE_RE.test(message.trim())) {
      message = stashed;
      suppressSeverityGate = true;
      suppressHardEmergencyGate = true;
      // FIXED (root cause, demonstrated live — see
      // pendingEmergencyQuestionType's doc comment in chatLog.js):
      // restore whichever pending-question flag was active when the
      // emergency fired, so STAGE 0a below reads it correctly and
      // routes the replay through the SAME resolver it would have used
      // originally (e.g. resolveDisambiguationAnswer), with full
      // context — lastQuestionAsked/pendingAmbiguousValue were never
      // touched by the original attempt (it never reached STAGE 6), so
      // they're still exactly as they were. Without this, the replayed
      // fragment ("10 severity") would be interpreted as a bare,
      // stand-alone message with no memory of what it was answering —
      // which is exactly what produced a bare "10 severity" being
      // misread as a mental-health crisis in a real, live test.
    }
    // A new message typed instead of Continue may well be answering the
    // same pending question, so it gets the same restored context.
    if (stashed) {
      if (stashedQuestionType === 'disambiguation') setAwaitingDisambiguationAnswer(sessionId, true);
      else if (stashedQuestionType === 'finalConfirmation') setAwaitingFinalConfirmation(sessionId, true);
      else if (stashedQuestionType === 'clarification') setAwaitingClarificationAnswer(sessionId, true);
    }
  }

  // ================================================================
  // STAGE 0a: CHECK IF THIS TURN IS ANSWERING ONE OF OUR OWN QUESTIONS
  // (BEFORE gates)
  //
  // There are now two kinds of question this app can ask before
  // recommending a specialist — see STAGE 7 below for the full state
  // machine:
  //   - a TARGETED clarifying question (duration, severity, or whatever
  //     else a real intake would naturally ask next — WHETHER to ask
  //     AND what to ask are decided together by one AI judgment call,
  //     clarificationCheck.js's assessIntake, with a fixed template
  //     fallback if that call fails)
  //   - the FINAL "is there anything else before I recommend a
  //     specialist?" gate (a fixed template — this one stays templated
  //     since it's a structured yes/no checkpoint, not a place variety
  //     adds value)
  // Neither is Infermedica-sourced, and neither's TEXT is ever stored —
  // only a one-turn boolean flag for each (see chatLog.js).
  //
  // UPDATED: this flag used to also blanket-skip the off-topic domain
  // classifier and the AI emergency-severity fallback on this turn, to
  // avoid those context-blind checks misreading a short reply ("yes",
  // "severe") as off-topic or ambiguous. Both of those checks are now
  // context-aware instead (see STAGE 1 and STAGE 2 below, and
  // `pendingContext` just below), so they run on every turn, including
  // this one — only the diagnosis-request classifier and the emotional-
  // support keyword check remain skipped here (out of scope for this
  // pass; see their own stage comments). STAGE 7 uses the FINAL-
  // confirmation flag specifically to decide whether this turn means
  // "go ahead and recommend."
  // ================================================================
  const wasAnsweringClarification = getAwaitingClarificationAnswer(sessionId);
  const wasAnsweringFinalConfirmation = getAwaitingFinalConfirmation(sessionId);
  // See chatLog.js's awaitingDisambiguationAnswer doc comment — kept
  // distinct from wasAnsweringClarification so a reply to the bot's own
  // "does 'X' mean X days or a severity of X out of 10?" question gets
  // resolved by resolveDisambiguationAnswer (a dedicated classifier)
  // rather than the general-purpose classifySymptoms.
  const wasAnsweringDisambiguation = getAwaitingDisambiguationAnswer(sessionId);
  const wasAnsweringPendingQuestion = wasAnsweringClarification || wasAnsweringFinalConfirmation || wasAnsweringDisambiguation;
  // Captured BEFORE the flags below get cleared — see
  // markEmergencyAcknowledgeable's doc comment for why this exists: if
  // THIS turn ends up triggering a physical emergency, whichever gate
  // fires needs to know what was pending so it can be restored on
  // "Continue" instead of the reply being replayed as a meaningless,
  // context-free fragment.
  const pendingQuestionType = wasAnsweringDisambiguation
    ? 'disambiguation'
    : wasAnsweringFinalConfirmation
    ? 'finalConfirmation'
    : wasAnsweringClarification
    ? 'clarification'
    : null;
  if (wasAnsweringClarification) setAwaitingClarificationAnswer(sessionId, false);
  if (wasAnsweringFinalConfirmation) setAwaitingFinalConfirmation(sessionId, false);
  if (wasAnsweringDisambiguation) setAwaitingDisambiguationAnswer(sessionId, false);

  // A short, plain-language, THIS-APP-AUTHORED description of what
  // question (if any) is pending — never anything Infermedica-sourced,
  // and never the exact question TEXT itself (that stays un-stored, as
  // before). Passed to the domain classifier and the AI severity
  // fallback below so they can interpret a short reply ("severe", "3
  // days", "no") correctly instead of judging it in isolation — see
  // offtopiccheck.js's classifyDomain and safetyCheck.js's
  // checkAISeverity `context` params. Reconstructed fresh each turn
  // from data already on file (the accumulated symptom list), rather
  // than adding a new stored field for it.
  const priorPresentTermsForContext = getAccumulatedSymptoms(sessionId)
    .filter((s) => s.present)
    .map((s) => s.term);
  const pendingContext = wasAnsweringFinalConfirmation
    ? `The assistant just asked the patient "Is there anything else you'd like to add before I recommend a specialist? If not, just say no." Symptoms recorded so far: ${priorPresentTermsForContext.join(', ') || 'none yet'}.`
    : wasAnsweringDisambiguation
    ? `The assistant just asked the patient to clarify whether "${getPendingAmbiguousValue(sessionId)}" meant a number of days or a severity rating, for: ${priorPresentTermsForContext.join(', ') || 'their reported symptom(s)'}.`
    : wasAnsweringClarification
    ? `The assistant just asked the patient a follow-up question (duration, severity, or a related symptom) about: ${priorPresentTermsForContext.join(', ') || 'their reported symptom(s)'}.`
    : null;

  // ------------------------------------------------------------------
  // 0b1: Greeting pre-filter (skipped for a pending-question answer —
  // a bare "yes"/"no" should never be mistaken for a greeting).
  // ------------------------------------------------------------------
  const obvious = checkObviouslyOffTopic(message);
  if (obvious.offTopic && !wasAnsweringPendingQuestion) {
    return greetingEnvelope(sessionId);
  }

  // ================================================================
  // 0b2-0b4: SHARED DETERMINISTIC SAFETY GATES (crisis, profanity,
  // emergency keyword) — see runSharedSafetyGates() above. ALWAYS run,
  // including on a pending-question answer.
  //
  // FIXED (kept, still applies): wasAnsweringPendingQuestion used to
  // skip the ENTIRE off-topic check (where profanity detection lived)
  // and the ENTIRE safety check (where the emergency KEYWORD match
  // lived) whenever a message was flagged as answering a pending
  // clarifying question. A real answer can say anything — "no, but I
  // also have severe chest pain and can't breathe" or "no, this is
  // fucking annoying" — so skipping BOTH checks entirely for that turn
  // was never safe. These deterministic regex checks have no such
  // ambiguity problem and always run regardless — only the AI layers
  // below (off-topic classification, AI severity fallback) are still
  // skipped for a turn answering our own clarifying question.
  // ================================================================
  // ================================================================
  // PERFORMANCE: crisis gate (above) and domain classification (below)
  // are two independent AI calls on the same raw message — neither's
  // output depends on the other's. They used to run fully sequentially
  // (crisis call, THEN domain call), which is the main reason a single
  // turn's wall-clock time could stack up past the session-lock
  // timeout even though each individual provider call was healthy.
  // Firing them together with Promise.all cuts one full AI round-trip
  // off every turn's latency WITHOUT changing behavior: gateResult is
  // still checked and returned FIRST, before domainResult is even
  // looked at, so crisis still wins over domain classification exactly
  // like it did when the calls were sequential — only the wall-clock
  // ordering changed, not the decision priority.
  //
  // PERFORMANCE (2nd pass): the general AI severity check
  // (checkAiSeverityGate — STAGE 2 below) is ALSO independent of the
  // crisis gate and domain classification above — it's just another
  // classifier reading the same raw message, and its `context` param
  // only depends on wasAnsweringPendingQuestion/pendingContext, both
  // already known at this point. Every place this app needs a severity
  // read (the HEALTH_EVENT branches further down, and STAGE 2's own
  // unconditional check) uses this EXACT SAME message+context pair, so
  // rather than call it again in each of those 3 places sequentially,
  // fire it ONCE here and let it run in the background while the crisis/
  // domain calls above are still in flight (and while the synchronous
  // branching logic below executes) — by the time any of those 3 spots
  // actually need the answer, it's likely already resolved, so the
  // `await` there is cheap. This is a pure latency win with ZERO extra
  // API calls: severity was already being called exactly once per turn
  // (in whichever single branch applies), it just now starts earlier
  // instead of waiting its turn. Deliberately NOT included in the
  // Promise.all above — that would make a genuine crisis response wait
  // on this unrelated call before returning, which is the one case
  // where finishing fast matters more than saving a round-trip.
  // ================================================================
  // NOTE: this fires the PURE classification only (checkAISeverity has
  // no side effects) — never checkAiSeverityGate directly, which also
  // logs. Firing the logging version here would risk writing a
  // spurious "emergency" audit entry for a message that ultimately
  // exits via a different gate (off-topic/concerning) and never
  // actually consumes this result — see checkAiSeverityGate's doc
  // comment. Each of the 3 places below that need this result calls
  // buildSeverityEmergencyEnvelope itself, exactly once, only if it's
  // actually the branch that gets reached.
  // suppressSeverityGate (STAGE -1 above): the patient just explicitly
  // chose to continue past a physical emergency reply for this EXACT
  // message — skip the classifier entirely rather than let it
  // immediately re-flag the same content, and save the API call. All
  // three consumption sites below get "not an emergency" for free this
  // way, with no changes needed at any of them. Deterministic checks
  // (crisis, keyword) are NOT affected by this — they still run
  // normally elsewhere in this turn.
  // With no question pending, still tell the emergency check which
  // symptoms are on file — "it just got a lot worse" means nothing on its
  // own, but a lot after "chest pain".
  // BUG (found live): once the patient has continued past an emergency,
  // that same emergency's symptoms are in this context — so every later
  // answer ("8/10", "yes") was judged together with them, re-flagged as
  // an emergency, and the patient was asked to Continue again on every
  // single message. After an acknowledged emergency, judge only what the
  // NEW message itself says (a genuinely new emergency still fires; the
  // keyword/crisis gates are message-only already).
  const continuedPastEmergency = getEmergencyHistory(sessionId).length > 0;
  const severityContext = continuedPastEmergency
    ? null
    : wasAnsweringPendingQuestion
    ? pendingContext
    : priorPresentTermsForContext.length
    ? `The patient has been describing these symptoms earlier in this conversation: ${priorPresentTermsForContext.join(', ')}.`
    : null;
  // After the patient has continued past an emergency this round, the AI
  // severity check is not re-run: measured live, even judged alone, "20
  // minutes, 9 out of 10" is flagged EMERGENCY, so every answer re-locked
  // the chat behind Continue. The patient has been warned (and is reminded
  // in the recommendation); the deterministic emergency-keyword and crisis
  // gates still run on every message, and Infermedica's triage still sets
  // the final urgency. Cleared when the round is finalized.
  const severityCheckPromise = suppressSeverityGate || continuedPastEmergency
    ? Promise.resolve({ isEmergency: false })
    : checkAISeverity(message, callAI, severityContext);
  // A speculative call rejecting with nobody listening yet (before its
  // first real `await` below) would surface as an unhandled promise
  // rejection — attach a no-op catch immediately so Node never sees an
  // unhandled rejection; the REAL error handling still happens at each
  // `await severityCheckPromise` below (this no-op only prevents a
  // process-level warning, it doesn't swallow the error there).
  severityCheckPromise.catch(() => {});

  const [gateResult, domainResult] = await Promise.all([
    runSharedSafetyGates({
      message,
      sessionId,
      patientId,
      context: wasAnsweringPendingQuestion ? pendingContext : null,
      pendingQuestionType,
      suppressHardEmergencyGate,
    }),
    wasAnsweringPendingQuestion
      ? classifyPendingAnswerRelevance({ message, callAI, context: pendingContext, recentHistory: getRecentMessages(sessionId) })
      : checkOffTopic(message, callAI, null, getRecentMessages(sessionId)),
  ]);
  if (gateResult) return gateResult;

  // ================================================================
  // STAGE 0b: EXPLICIT RESTART INTENT (deterministic, checked right
  // after the mandatory safety gates so it can never be skipped, and
  // BEFORE anything below gets a chance to hand this message to
  // resolveFinalConfirmation/resolveDisambiguationAnswer/classifySymptoms
  // — see RESTART_INTENT_RE's doc comment in symptomClassifier.js for
  // the live-demonstrated bug this fixes: those resolvers only
  // understand confirm/add/remove, so "let's start fresh" was getting
  // forced into that vocabulary and misread as denying whatever symptom
  // was on file, while ALSO re-asking about it — an incoherent result.
  // Checked regardless of wasAnsweringPendingQuestion — a restart
  // request means the same thing whether or not a question happens to
  // be pending.
  // ================================================================
  if (RESTART_INTENT_RE.test(message)) {
    resetSessionState(sessionId);
    const reply = "No problem — I've cleared everything and we're starting fresh. What's going on?";
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'session_restarted', reply },
    });
    return envelope({
      kind: 'session_restarted',
      sessionId,
      reply: cleanReply(reply),
    });
  }

  // ================================================================
  // STAGE 1: DOMAIN CLASSIFICATION (1 Groq call, fired above in
  // parallel with the crisis gate) — off-topic / concerning / diet /
  // health-event detection.
  //
  // UPDATED: this used to be skipped ENTIRELY on a turn answering one
  // of our own pending questions — including the CONCERNING-content
  // check, meaning violent/hateful content had no AI-level check on
  // those turns (the deterministic CRISIS_PATTERNS regex above still
  // caught overt cases, but a subtler one had nothing). That was a
  // real gap, not just an off-topic-detection gap. Per the explicit
  // decision to reuse this SAME classifier rather than build a
  // separate bespoke relevance heuristic, it now ALWAYS runs —
  // offtopiccheck.js's classifyDomain takes an optional `context` so
  // it can correctly read a short legitimate answer ("severe", "no")
  // as on-topic instead of misjudging it in isolation. A fresh
  // (non-pending) message behaves exactly as before: CONCERNING or
  // NOT_HEALTH_RELATED exits immediately. A pending-question turn is
  // handled differently — a bounded streak (see after STAGE 6) rather
  // than an immediate exit, since a genuine answer still needs to
  // reach classifySymptoms / resolveFinalConfirmation below.
  // ================================================================
  let offTopicThisTurn = false;
  let pendingEventFollowUp = null;
  // Set below when classifyPendingAnswerRelevance judges this reply's
  // MEANING as "stop asking, move on" (see its own doc comment) —
  // combined with isOverrideRequested's fixed-phrase check at the
  // assessIntake call site further down, so either the deterministic
  // list or the AI's own meaning-based read can trigger it.
  let skipAheadRequested = false;

  if (!wasAnsweringPendingQuestion) {
    const offTopicResult = domainResult;
    if (offTopicResult?.offTopic && offTopicResult.reason === 'social') {
      return greetingEnvelope(sessionId);
    }
    if (offTopicResult?.offTopic) {
      let reply;
      if (offTopicResult.reason === 'concerning_content') {
        reply = "I can't help with that, and I won't engage with it. If you're describing a health " +
          'symptom, tell me what you are feeling physically and I will help you find the right doctor.';
      } else if (offTopicResult.reason === 'profanity') {
        reply = "Let's keep this respectful — I'm happy to help as soon as you tell me what symptoms you're having.";
      } else {
        reply = "I can only help with health symptoms and which specialist to see. What are you feeling physically?";
      }

      await logChatMessage({
        sessionId,
        patientId,
        message,
        response: { kind: 'off_topic', offTopicReason: offTopicResult.reason, reply },
      });
      return envelope({
        kind: 'off_topic',
        sessionId,
        isOffTopic: true,
        offTopicReason: offTopicResult.reason,
        reply: cleanReply(reply),
      });
    }
    if (offTopicResult?.domain === 'HEALTH_EVENT') {
      // A fresh, stand-alone message describing an exposure/event with
      // no symptom of its own (e.g. opening with "I ate something I'm
      // allergic to"). Severity-checked FIRST (see checkAiSeverityGate's
      // doc comment) — an exposure/event can already be an emergency on
      // its own, before asking anything about current symptoms. Only
      // once that's clear does this ask the specific follow-up, instead
      // of falling through to STAGE 6, where classifySymptoms would
      // find nothing to extract and STAGE 7 would otherwise dead-end on
      // "I couldn't identify any symptoms in your message."
      //
      // Allergen exposure, penetrating trauma, choking/drowning, and
      // poisoning/overdose/electrocution/severe burns specifically skip
      // the follow-up entirely — see checkDangerousEventGate's doc
      // comment — checked first since it's the narrower, cheaper
      // category; checkAiSeverityGate still runs after for every other
      // kind of event this app doesn't have a dedicated fast-track for.
      // PERFORMANCE: these two checks are independent classifiers of the
      // same message (neither reads the other's result) — fired together
      // instead of sequentially. dangerousEventEmergency is still checked
      // and returned FIRST, so the priority ("dangerous event fast-track
      // wins over the general severity check") is unchanged. The severity
      // read reuses severityCheckPromise (fired speculatively above, same
      // message+context this branch would have used anyway) instead of
      // making a second AI call — envelope/logging happens HERE, exactly
      // once, since this is the branch actually consuming it.
      const [dangerousEventEmergency, aiSafety] = await Promise.all([
        // Skipped on the Continue replay itself — it would re-flag the same message.
        suppressSeverityGate ? null : checkDangerousEventGate({ message, callAI, sessionId, patientId, pendingQuestionType }),
        severityCheckPromise,
      ]);
      if (dangerousEventEmergency) return dangerousEventEmergency;
      const eventEmergency = await buildSeverityEmergencyEnvelope({ aiSafety, message, sessionId, patientId, pendingQuestionType });
      if (eventEmergency) return eventEmergency;
      const followUpQuestion = await generateEventFollowUp({ eventMessage: message });
      setAwaitingClarificationAnswer(sessionId, true);
      setLastQuestionAsked(sessionId, followUpQuestion);
      incrementClarificationCount(sessionId);
      return envelope({
        kind: 'clarification',
        sessionId,
        needsClarification: true,
        clarificationRound: getClarificationCount(sessionId),
        maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
        aiWritten: true,
        reply: cleanReply(followUpQuestion),
      });
    }
  } else {
    const relevance = domainResult; // already computed above, in parallel with the crisis gate
    if (relevance.classification === 'CONCERNING') {
      const reply = "I can't help with that, and I won't engage with it. If you're describing a health " +
        'symptom, tell me what you are feeling physically and I will help you find the right doctor.';
      await logChatMessage({
        sessionId,
        patientId,
        message,
        response: { kind: 'off_topic', offTopicReason: 'concerning_content', reply },
      });
      return envelope({
        kind: 'off_topic',
        sessionId,
        isOffTopic: true,
        offTopicReason: 'concerning_content',
        reply: cleanReply(reply),
      });
    }
    if (relevance.classification === 'SKIP_AHEAD') {
      skipAheadRequested = true;
    } else if (relevance.classification === 'OFF_TOPIC') {
      // Not exited immediately — see the offTopicStreak handling after
      // STAGE 6. A bounded number of consecutive irrelevant replies is
      // tolerated (and gently acknowledged) before this app stops
      // re-asking and moves on with whatever's already on file.
      offTopicThisTurn = true;
    } else if (relevance.classification === 'HEALTH_EVENT' && getClarificationCount(sessionId) < MAX_CLARIFICATION_ROUNDS) {
      pendingEventFollowUp = relevance.followUpQuestion;
    }
  }

  if (pendingEventFollowUp) {
    // Same severity check as the fresh-message HEALTH_EVENT branch
    // above, for the same reason — see checkAiSeverityGate's doc
    // comment. This is a DIFFERENT return point (the event surfaced
    // mid-conversation, while another question was pending) so it needs
    // its own check rather than relying on STAGE 2 below, which this
    // path also returns before reaching. Same dangerous-event fast-track
    // as the fresh-message branch above, checked first.
    // PERFORMANCE: same parallelization as the fresh-message HEALTH_EVENT
    // branch above — independent checks, dangerousEventEmergency still
    // takes priority when both come back. The severity read reuses
    // severityCheckPromise (fired speculatively above with this exact
    // context — this branch only runs when wasAnsweringPendingQuestion is
    // true, so severityCheckPromise's context is already pendingContext);
    // envelope/logging happens HERE since this is the branch consuming it.
    const [dangerousEventEmergency, aiSafety] = await Promise.all([
      // Skipped on the Continue replay itself — it would re-flag the same message.
      suppressSeverityGate ? null : checkDangerousEventGate({ message, callAI, sessionId, patientId, pendingQuestionType }),
      severityCheckPromise,
    ]);
    if (dangerousEventEmergency) return dangerousEventEmergency;
    const eventEmergency = await buildSeverityEmergencyEnvelope({ aiSafety, message, sessionId, patientId, pendingQuestionType });
    if (eventEmergency) return eventEmergency;

    // The patient mentioned a health-relevant event/exposure (not a
    // symptom) while a different question was pending — that's more
    // clinically useful to follow up on right now than to force the
    // original question. Counts against the same shared round budget
    // as any other clarifying question (bounded above), and this turn
    // clearly contributed something real, so any off-topic streak is
    // cleared.
    resetOffTopicStreak(sessionId);
    setAwaitingClarificationAnswer(sessionId, true);
    setLastQuestionAsked(sessionId, pendingEventFollowUp);
    incrementClarificationCount(sessionId);
    return envelope({
      kind: 'clarification',
      sessionId,
      needsClarification: true,
      clarificationRound: getClarificationCount(sessionId),
      maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
      aiWritten: true,
      reply: cleanReply(pendingEventFollowUp),
    });
  }

  // ================================================================
  // STAGE 2: EMERGENCY AI FALLBACK
  // The deterministic keyword layer already ran unconditionally above.
  // This is just the AI fallback for messages that didn't match a
  // keyword but might still describe an emergency.
  //
  // UPDATED: this used to be skipped on a pending-question answer,
  // same reasoning as the off-topic gate above ("yes, I am feeling
  // difficulty" being too ambiguous for a reliable blind AI read) —
  // and had the exact same fix available: give it the context it was
  // missing rather than skip it. checkAISeverity now takes that same
  // `pendingContext` (see STAGE 0a) so it can correctly weigh a short
  // reply against what it's actually answering, instead of either
  // guessing blind or not running at all.
  //
  // PERFORMANCE: reuses severityCheckPromise, fired speculatively back at
  // STAGE 0b2 with this exact same message+context — see that comment.
  // Only reached here at all when neither the HEALTH_EVENT branch nor
  // the pendingEventFollowUp branch above already consumed it and
  // returned, so this is always the first (and only) await of it in
  // that case — by now it's had the whole STAGE 1 domain-classification
  // round-trip to resolve in the background. Envelope/logging happens
  // HERE, exactly once, since this is the branch consuming it.
  // ================================================================
  {
    const aiSafety = await severityCheckPromise;
    const emergencyResult = await buildSeverityEmergencyEnvelope({ aiSafety, message, sessionId, patientId, pendingQuestionType });
    if (emergencyResult) return emergencyResult;
  }

  // ================================================================
  // STAGE 3: DIAGNOSIS REQUEST CHECK (Regex + AI fallback)
  //
  // UPDATED: this used to skip the AI fallback entirely on a
  // pending-question turn — same bug shape already fixed for the
  // off-topic and emergency-severity checks above (STAGE 0b3/STAGE 2).
  // checkMisuseRequest now takes the same `context` param those use.
  //
  // UPDATED AGAIN (adversarial review caught this): the fix above still
  // had a hole — DIAGNOSIS_PATTERNS right below is itself a context-BLIND
  // regex, and it used to run UNCONDITIONALLY, even on a pending-question
  // turn, before checkMisuseRequest (and its context param) ever got a
  // say. Several of its patterns are broad everyday phrasing (e.g. "what
  // is this" inside /\bwhat\s+(?:could|might|do\s+you\s+think)\s+(?:it|this)\s+(?:be|is)\b/i)
  // that a legitimate answer to a pending question can trivially contain
  // — "what could it be, the spicy food I had?" answering a cause/duration
  // question would have matched and short-circuited straight to
  // diagnosis_declined, never reaching the context-aware AI check at all.
  // So this regex fast-path is now ALSO only trusted on a fresh message —
  // once a question is pending, skip it and go straight to
  // checkMisuseRequest with context (which applies the identical gate to
  // its own internal regex list — see safetyCheck.js).
  // ================================================================
  let isDiagnosisRequest = false;
  if (!wasAnsweringPendingQuestion && DIAGNOSIS_PATTERNS.some((re) => re.test(message))) {
    isDiagnosisRequest = true;
  } else {
    const misuse = await checkMisuseRequest(
      message,
      callAI,
      wasAnsweringPendingQuestion ? pendingContext : null
    );
    isDiagnosisRequest = misuse?.isDiagnosisRequest || false;
  }

  // BUG (found live, two variants): this used to short-circuit here
  // whenever isDiagnosisRequest was true AND wasAnsweringPendingQuestion
  // was true — including the exact case the checkMisuseRequest
  // context-aware call just above exists to correctly identify: a
  // message that's both a real answer to a pending question AND a
  // diagnosis request in the same breath ("a couple of days. What do
  // you think i could have"). First fix narrowed the short-circuit to
  // only the FRESH-message case, on the assumption that a fresh
  // diagnosis request has "nothing else in the message to preserve" —
  // but that assumption is also just wrong (found live): "fever for 2
  // days, what disease do I have" is a FRESH message that both asks for
  // a diagnosis AND reports real, informative content (fever, 2 days),
  // and the old short-circuit discarded that content too, re-asking for
  // duration the patient had already given. There is no reliable way to
  // know in advance whether a diagnosis-request message also carries
  // real content without actually running it through classification —
  // so this never short-circuits at all anymore, in EITHER case.
  // isDiagnosisRequest stays true in scope for finalizeAndRecommend's
  // own DECLINE_DIAGNOSIS_NOTE if this turn reaches it, and
  // applyMixedDiagnosisDecline (see above) appends the decline note to
  // whatever real clarification/reply normal processing produces
  // instead — covering a fresh diagnosis-only message (which still gets
  // the normal "I couldn't identify any symptoms" fallback plus the
  // note) exactly the same way as a mixed one.

  // ================================================================
  // STAGE 3b: EMOTIONAL SUPPORT DETECTION
  // Check if this is primarily an emotional concern (loneliness, stress,
  // emotional support) rather than a physical symptom. Provide supportive
  // guidance instead of medical routing.
  //
  // UPDATED: this used to be pure regex (EMOTIONAL_KEYWORDS) with NO AI
  // backstop at all, and was skipped entirely whenever
  // wasAnsweringPendingQuestion was true. Both were real gaps — a
  // paraphrase the fixed word list didn't happen to cover ("I just feel
  // like nobody gets what I'm going through") was invisible everywhere,
  // and even a literal keyword hit went unexamined mid-clarification.
  // checkEmotionalConcern (safetyCheck.js) now backs the regex the same
  // way checkAISeverity backs the physical-emergency keyword list. A
  // fresh message still trusts a plain keyword hit outright (cheap,
  // reliable enough on its own); a pending-question turn always asks the
  // context-aware AI instead of trusting the regex blind (a keyword like
  // "stressed" inside an ordinary duration/severity answer shouldn't
  // short-circuit into emotional-support mode); and a fresh message with
  // NO keyword hit still gets one AI pass so a keyword-free paraphrase
  // isn't simply missed.
  //
  // UPDATED AGAIN — EXPLICIT PRODUCT DECISION: a first standalone
  // emotional message no longer jumps straight to the fixed referral
  // block. It gets ONE conversational follow-up first (composeEmotional-
  // FollowUp), same philosophy as assessIntake for physical symptoms.
  // The reply to that follow-up is then read by
  // interpretEmotionalFollowUpAnswer, which can send this turn down one
  // of three paths: a physical symptom was mentioned (fall through to
  // the normal pipeline below instead of returning here — e.g. "yeah
  // feeling really low AND blood in urine"), the patient retracted the
  // earlier statement (acknowledge it, don't repeat the referral — e.g.
  // "I'm not really feeling that low actually"), or they're still
  // struggling (give the referral now, having actually asked first).
  // ================================================================
  const awaitingEmotionalFollowUp = getAwaitingEmotionalFollowUp(sessionId);
  const pendingEmotionalQuestion = getLastEmotionalQuestion(sessionId);
  let fellThroughFromEmotionalCheckIn = false;

  if (awaitingEmotionalFollowUp) {
    // Whatever this message turns out to mean, our follow-up is answered
    // now — clear the one-turn flags up front so nothing downstream can
    // mistake a LATER message for still answering this same question.
    setAwaitingEmotionalFollowUp(sessionId, false);
    setLastEmotionalQuestion(sessionId, null);

    const interpretation = await interpretEmotionalFollowUpAnswer(
      message,
      callAIStructured,
      pendingEmotionalQuestion || 'How are you feeling?'
    );

    if (interpretation.hasPhysicalSymptom) {
      // A physical symptom rode along with the answer — let the normal
      // symptom pipeline below handle it instead of returning here. The
      // emotional side of it was heard; it just isn't what this turn's
      // reply needs to be about.
      fellThroughFromEmotionalCheckIn = true;
    } else if (interpretation.retracted) {
      const reply =
        "That's good to hear — thanks for letting me know. I'm still here if anything's " +
        'bothering you physically, or if you want to talk more later.';
      await logChatMessage({
        sessionId,
        patientId,
        message,
        response: { kind: 'emotional_support_resolved', reply },
      });
      return envelope({
        kind: 'emotional_support_resolved',
        sessionId,
        reply: cleanReply(reply),
      });
    } else {
      // stillStruggling (or genuinely ambiguous — interpretEmotional-
      // FollowUpAnswer fails toward this branch rather than dropping a
      // real concern) — the referral is warranted now, having asked
      // first instead of assuming.
      const reply =
        "Thanks for telling me more. It sounds like this has really been weighing on you, and " +
        "that's worth taking seriously. Please consider reaching out to a mental health " +
        'professional—a counselor, therapist, or psychologist—who can give you the kind of ' +
        "support I can't. And if anything physical comes up, I'm here for that too.";
      await logChatMessage({
        sessionId,
        patientId,
        message,
        response: { kind: 'emotional_support', reply },
      });
      return envelope({
        kind: 'emotional_support',
        sessionId,
        reply: cleanReply(reply),
      });
    }
  }

  let isEmotionalOnly = false;
  if (!fellThroughFromEmotionalCheckIn) {
    if (wasAnsweringPendingQuestion) {
      isEmotionalOnly = await checkEmotionalConcern(message, callAI, pendingContext);
    } else if (checkEmotionalKeywords(message)) {
      isEmotionalOnly = true;
    } else {
      isEmotionalOnly = await checkEmotionalConcern(message, callAI, null);
    }
  }

  if (isEmotionalOnly) {
    // First time we're hearing this — ask a warm, natural follow-up
    // instead of reciting the referral immediately (see the STAGE 3b
    // comment above). The referral itself only happens once the patient
    // has actually answered that follow-up.
    const followUpQuestion = await composeEmotionalFollowUp(message, callAI);
    setAwaitingEmotionalFollowUp(sessionId, true);
    setLastEmotionalQuestion(sessionId, followUpQuestion);

    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'emotional_followup_question', reply: followUpQuestion },
    });

    return envelope({
      kind: 'emotional_followup_question',
      sessionId,
      needsClarification: true,
      aiWritten: true,
      reply: cleanReply(followUpQuestion),
    });
  }

  // ================================================================
  // STAGE 4: SUBJECT DETECTION (self vs. dependent)
  //
  // UPDATED (explicit decision — dependent handling REMOVED): this used
  // to try to keep going for a dependent (or a message mixing self and
  // a dependent) by ESTIMATING an age from a rough ageGroup bucket and
  // routing it through the exact same pipeline as the patient's own
  // profile-backed data. Live testing showed this doesn't hold up: a
  // dependent has no real profile (no age, no history, no lab values,
  // no allergy/medication facts) to ground a recommendation in, the
  // ageGroup estimate is a guess dressed up as a number, subject
  // detection isn't reliable at telling a genuine dependent (a child, a
  // parent) apart from something that isn't a patient at all (a pet:
  // "my dog is coughing" was classified as a dependent with relation
  // "other"), and mixing a dependent's symptoms into the SAME
  // accumulated-symptom / recommendation flow as the patient's own
  // produced a confused, wrong combined recommendation (a child's cough
  // folded into the adult patient's own headache recommendation, both
  // attributed to the wrong person's age/history). Rather than layer
  // another heuristic on top of an already-unreliable estimate, this
  // app now declines dependent care outright and says so plainly — see
  // the check right below.
  // ================================================================
  const previousSubject = getLastSubject(sessionId);
  const subjectInfo = await detectSubject(message, callAIStructured, {
    previousSubject: previousSubject?.subject,
    previousRelation: previousSubject?.relation,
    previousAgeGroup: previousSubject?.ageGroup,
  });
  saveLastSubject(sessionId, subjectInfo);

  if (subjectInfo.subject === 'dependent' || subjectInfo.subject === 'mixed') {
    const who = subjectInfo.relation ? `your ${subjectInfo.relation}` : 'someone else';
    const reply =
      `I'm built to help with your OWN symptoms, using your own health profile — I can't reliably assess ` +
      `symptoms for ${who} (a child, another family member, or a pet), since I don't have their medical ` +
      `background to go on and could get it wrong. Please contact their doctor (a pediatrician for a child) ` +
      `or a vet directly. If you're also experiencing something yourself, tell me about that and I can help ` +
      `with your own care.`;
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'dependent_declined', reply },
    });
    return envelope({
      kind: 'dependent_declined',
      sessionId,
      subject: subjectInfo,
      reply: cleanReply(reply),
    });
  }

  // ================================================================
  // STAGE 5: GET PATIENT PROFILE / RESOLVE AGE + SEX FOR TRIAGE
  // Self only now — see the STAGE 4 note above.
  // ================================================================
  const patientProfile = await getPatientProfile(patientId);

  // FIXED (found live): this used to silently default to "age 30, male"
  // whenever a patient's own profile had no date_of_birth on file (true
  // for every CareLink-bridged patient today — the bridge only ever
  // writes name + consented_at) — with nothing but a server console
  // warning. The patient was never told a recommendation was computed
  // against fabricated demographics, and the pediatric safety net in
  // particular only fires if the age it receives is real. Hard-gate
  // instead: this sits AFTER the emergency/off-topic/dependent gates
  // above, so a real emergency is never blocked by an incomplete
  // profile — only the ordinary symptom-gathering path is. No backfill
  // for existing/seeded accounts — the gate applies immediately.
  if (patientProfile?.age == null || !patientProfile?.sex) {
    const reply =
      "Before I can give you a specialist recommendation, I need your age and sex on file — they affect " +
      "which conditions are actually likely. Please complete your profile, then come back and we'll pick " +
      "this up.";
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'profile_incomplete', reply },
    });
    return envelope({
      kind: 'profile_incomplete',
      sessionId,
      subject: subjectInfo,
      reply: cleanReply(reply),
      actionable: true,
      actions: [{ id: 'complete_profile', label: 'Complete your profile' }],
    });
  }
  // Infermedica only accepts ages 1–130 (found live: a date of birth
  // entered as this year gave age 0, and the patient only saw "trouble
  // connecting to the clinical engine" at the very end). Catch it here,
  // before any symptom gathering, with a message that says what to fix.
  if (!(patientProfile.age >= 1 && patientProfile.age <= 130)) {
    const reply =
      "The date of birth on your profile doesn't look right (it gives an age of " +
      `${patientProfile.age}). Please check it on your Profile page, then come back and we'll pick this up.`;
    await logChatMessage({
      sessionId,
      patientId,
      message,
      response: { kind: 'profile_incomplete', reply },
    });
    return envelope({
      kind: 'profile_incomplete',
      sessionId,
      subject: subjectInfo,
      reply: cleanReply(reply),
      actionable: true,
      actions: [{ id: 'complete_profile', label: 'Check your profile' }],
    });
  }
  const age = patientProfile.age;
  const sex = patientProfile.sex.toLowerCase();

  // ================================================================
  // STAGE 6: CLASSIFY & ACCUMULATE (Groq only — ZERO Infermedica calls)
  // Record this turn (only reached here because it's already past all
  // emergency/off-topic/diagnosis-request gates, so it's a legitimate
  // non-emergency health message).
  //
  // ARCHITECTURE: Infermedica is not called here at all. The patient's
  // message is run through our own Groq classifier (symptomClassifier.js)
  // and merged into the session's running symptom list — see
  // chatLog.js's accumulatedSymptoms. That's it. No /parse, no /triage,
  // nothing Infermedica-shaped happens on an ordinary gathering turn.
  // Infermedica gets called exactly once, in finalizeAndRecommend()
  // below, only when the patient confirms they're ready for a
  // recommendation.
  // ================================================================
  // Snapshot of present symptom terms BEFORE this turn's classification,
  // handed to classifySymptoms as context so a bare "3 days" / "moderate"
  // answer to our own targeted follow-up question (STAGE 7 below) gets
  // attached to the right symptom instead of being silently dropped for
  // not repeating its name — see symptomClassifier.js's doc comment.
  const priorPresentTerms = getAccumulatedSymptoms(sessionId)
    .filter((s) => s.present)
    .map((s) => s.term);

  // FIXED (properly this time — see clarificationCheck.js's doc comment
  // above resolveFinalConfirmation): a plain "no" answering the final
  // gate used to be read by the general-purpose classifySymptoms as
  // DENYING every accumulated symptom, because that function had no
  // idea it was resolving a confirm/add/remove decision — it was just
  // running its normal "extract any symptom mentioned" pass on a
  // message with nothing to extract, and over-applied its own denial
  // rule. Rather than blocking the LLM out of this turn entirely (the
  // earlier fix), resolveFinalConfirmation gives it the actual known
  // list and the actual three-way decision to make, so it can correctly
  // handle "no", "remove the neck pain", "add nausea too", or a
  // genuinely unclear reply — telling us explicitly when it isn't
  // confident, so STAGE 7 below can read the list back and ask again
  // instead of guessing.
  let classifiedSymptoms;
  let finalConfirmationResolution = null;
  // Set below when classifySymptoms can't tell whether a bare number the
  // patient replied with was answering the DURATION half or the
  // SEVERITY half of a compound question — see the block right after
  // this if/else for what happens when it's set.
  let ambiguousNumberAnswer = null;
  // Set below when either the plain-classification path OR the
  // final-confirmation resolver extracts a chronic condition mention
  // this turn ("I'm pregnant") — used further down to recognize/
  // acknowledge a turn whose only real content was a condition, not a
  // symptom (see the newRoundPresentSymptoms fix below for why this
  // matters).
  let turnMentionedConditions = [];
  if (wasAnsweringFinalConfirmation) {
    finalConfirmationResolution = await resolveFinalConfirmation({ message, knownSymptoms: priorPresentTerms });
    classifiedSymptoms = finalConfirmationResolution.additions;
    // SIBLING FIX: this path never called classifySymptoms, so a
    // condition mentioned only while answering "anything else before I
    // recommend?" used to be completely invisible — never added to
    // profileFacts/risk-factor evidence. resolveFinalConfirmation now
    // extracts it directly (see its own doc comment).
    if (finalConfirmationResolution.mentionedConditions?.length) {
      appendMentionedConditions(sessionId, finalConfirmationResolution.mentionedConditions);
      turnMentionedConditions = finalConfirmationResolution.mentionedConditions;
    }
  } else if (wasAnsweringDisambiguation) {
    // DEDICATED RESOLVER (root-cause architectural fix): a reply to the
    // bot's own disambiguation question is no longer run through the
    // general-purpose classifySymptoms at all — see
    // resolveDisambiguationAnswer's doc comment (clarificationCheck.js)
    // for the real bug this closes ("both" answering this question
    // previously matched classifySymptoms' rule for affirming named
    // candidates in a REAL clinical question, fabricating two symptoms
    // out of the bot's own question text and producing a false emergency).
    const ambiguousValue = getPendingAmbiguousValue(sessionId);
    const disambiguationResolution = await resolveDisambiguationAnswer({
      message,
      ambiguousValue,
      knownSymptoms: priorPresentTerms,
    });
    if (!disambiguationResolution.understood) {
      // Don't guess — re-ask the same question, bounded by the same
      // round cap every other clarifying question uses. Once the
      // budget's gone, give up gracefully (classifiedSymptoms stays
      // empty — nothing false gets recorded) rather than loop forever.
      if (getClarificationCount(sessionId) < MAX_CLARIFICATION_ROUNDS) {
        const disambiguationQuestion =
          `Just to double check — does "${ambiguousValue}" mean ${ambiguousValue} days, ` +
          `or a severity of ${ambiguousValue} out of 10?`;
        setAwaitingDisambiguationAnswer(sessionId, true);
        setPendingAmbiguousValue(sessionId, ambiguousValue);
        setLastQuestionAsked(sessionId, disambiguationQuestion);
        incrementClarificationCount(sessionId);
        return envelope({
          kind: 'clarification',
          sessionId,
          needsClarification: true,
          clarificationRound: getClarificationCount(sessionId),
          maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
          reply: cleanReply(disambiguationQuestion),
        });
      }
      classifiedSymptoms = [];
    } else {
      // Understood — apply the now-resolved value as duration and/or
      // severity to every symptom the compound question was about, the
      // same convention symptomClassifier.js's own compound-answer rule
      // uses when no single symptom is named in the reply.
      classifiedSymptoms = priorPresentTerms.map((term) => ({
        term,
        present: true,
        duration: disambiguationResolution.appliesTo === 'duration' || disambiguationResolution.appliesTo === 'both' ? ambiguousValue : null,
        severity: disambiguationResolution.appliesTo === 'severity' || disambiguationResolution.appliesTo === 'both' ? ambiguousValue : null,
      }));
    }
  } else {
    // FIXED (demonstrated live): pass the EXACT targeted question this
    // app just asked (see chatLog.js's getLastQuestionAsked), when
    // there is one, so classifySymptoms can correctly tell "no"
    // answering a question about an UNRELATED possible symptom apart
    // from "no" denying an already-known one — see
    // symptomClassifier.js's doc comment on the questionAsked param for
    // the real bug this fixes ("no but i have eye pain" wrongly denying
    // an already-known "headache" when the question actually asked
    // about nausea/light sensitivity, neither of which is tracked).
    const questionAsked = wasAnsweringClarification ? getLastQuestionAsked(sessionId) : null;
    // UPDATED: classifySymptoms now returns {symptoms, ambiguous,
    // ambiguousValue} instead of a bare array — see its doc comment.
    // This is the fix for a real, demonstrated bug: this app's own
    // clarifying questions can ask for duration AND severity together
    // ("how long have you had this, and how severe is it on a scale of
    // 1 to 10?"), and a bare reply like "7" was previously always read
    // as "7 days," silently discarding a severity rating the patient
    // most likely meant. Rather than keep guessing, a genuinely
    // ambiguous bare number is now caught here and the patient is asked
    // directly which one they meant (right below), instead of either
    // guess being recorded as fact.
    const classificationResult = await classifySymptoms(message, priorPresentTerms, questionAsked);
    // FOUND via adversarial testing: when every AI provider fails this
    // call (observed live under heavy rate-limiting), classifySymptoms
    // used to come back looking identical to "the patient said nothing
    // identifiable" — which then produced "I couldn't identify any
    // symptoms in your message" for a message that may well have been
    // perfectly clear ("I have a headache and eye pain"). Give an honest
    // reply instead of one that blames the patient's message for an
    // infrastructure failure.
    if (classificationResult._classificationFailed) {
      return envelope({
        kind: 'error',
        sessionId,
        resolvedAge: age,
        resolvedSex: sex,
        reply: "I'm having trouble processing that right now — could you try sending it again in a moment?",
      });
    }
    classifiedSymptoms = classificationResult.symptoms;
    if (wasAnsweringClarification) {
      classifiedSymptoms = keepAnswerOnAskedSymptom(classifiedSymptoms, {
        askedSymptom: getLastQuestionSymptom(sessionId),
        knownTerms: priorPresentTerms,
        message,
      });
    }
    // DETERMINISTIC BACKSTOP (found live): "yes" answering "have you
    // noticed any swelling or redness around those joints?" came back from
    // classifySymptoms with nothing extracted — its affirmation-to-
    // candidates rule isn't reliably followed — so the patient's answer
    // was lost and they got "I couldn't tell what new symptoms you're
    // experiencing". The question's own candidates were declared
    // structurally by assessIntake when it was asked (see
    // getLastQuestionCandidates), so a bare affirmation records exactly
    // those, with no AI guess involved. Never overrides anything the
    // classifier DID extract.
    //
    // EITHER/OR (found live): "yes" answering "have you noticed any changes
    // in your vision or any redness in the eye?" came back with NO
    // candidates declared, so it fell through to "I didn't catch any
    // symptoms". Now: recover the options from the question text when
    // they weren't declared, and when the question offered two or more,
    // ask which one(s) instead of guessing — a "yes" to "A or B?" doesn't
    // mean both. "both" (or "yes" again to that follow-up) records all.
    const isBareYes = BARE_AFFIRMATION_RE.test(message);
    const isBoth = BOTH_ANSWER_RE.test(message);
    if (wasAnsweringClarification && classifiedSymptoms.length === 0 && (isBareYes || isBoth)) {
      const lastQuestion = getLastQuestionAsked(sessionId) || '';
      let candidates = getLastQuestionCandidates(sessionId);
      if (!candidates.length && lastQuestion) {
        candidates = await extractQuestionOptions(lastQuestion, priorPresentTerms);
      }
      const answeringWhichOne = lastQuestion.startsWith(WHICH_OPTION_PREFIX);
      if (candidates.length === 1 || (candidates.length > 1 && (isBoth || answeringWhichOne))) {
        classifiedSymptoms = candidates.map((term) => ({ term, present: true, duration: null, severity: null }));
      } else if (candidates.length > 1) {
        const options = `${candidates.slice(0, -1).join(', ')} or ${candidates[candidates.length - 1]}`;
        const whichQuestion = `${WHICH_OPTION_PREFIX} which do you have — ${options}, or both?`;
        setAwaitingClarificationAnswer(sessionId, true);
        setLastQuestionAsked(sessionId, whichQuestion); // clears candidates, so set them after
        setLastQuestionCandidates(sessionId, candidates);
        return envelope({
          kind: 'clarification',
          sessionId,
          needsClarification: true,
          resolvedAge: age,
          resolvedSex: sex,
          reply: cleanReply(whichQuestion),
        });
      }
    }
    // "I don't have this symptom" with several on file: the classifier
    // rightly refuses to guess which one — ask, instead of silently doing
    // nothing. The answer is read with the "WHICH ONE IS GONE?" rule.
    if (classifiedSymptoms.length === 0 && priorPresentTerms.length > 1 && PRONOUN_DENIAL_RE.test(message)) {
      const whichQuestion = `Which one is gone now? So far I have: ${listMarker(priorPresentTerms.join(', '))}.`;
      setAwaitingClarificationAnswer(sessionId, true);
      setLastQuestionAsked(sessionId, `Which of these symptoms is gone now: ${priorPresentTerms.join(', ')}?`);
      return envelope({
        kind: 'clarification',
        sessionId,
        needsClarification: true,
        resolvedAge: age,
        resolvedSex: sex,
        reply: cleanReply(whichQuestion),
      });
    }
    if (classificationResult.ambiguous) ambiguousNumberAnswer = classificationResult.ambiguousValue;
    // Persist any chronic condition/risk-factor mentioned this turn
    // ("I have diabetes", "I'm pregnant") — see chatLog.js's
    // getMentionedConditions/appendMentionedConditions doc comment.
    // Read back and merged into risk-factor evidence at finalize time
    // (see getRiskFactorEvidence's call site below), widening it beyond
    // just stored profileFacts.
    if (classificationResult.mentionedConditions?.length) {
      appendMentionedConditions(sessionId, classificationResult.mentionedConditions);
      turnMentionedConditions = classificationResult.mentionedConditions;
    }
  }

  if (ambiguousNumberAnswer) {
    const disambiguationQuestion =
      `Just to double check — does "${ambiguousNumberAnswer}" mean ${ambiguousNumberAnswer} days, ` +
      `or a severity of ${ambiguousNumberAnswer} out of 10?`;
    // Routes the NEXT turn's reply to resolveDisambiguationAnswer (the
    // dedicated resolver) instead of the general-purpose
    // classifySymptoms — see wasAnsweringDisambiguation above.
    setAwaitingDisambiguationAnswer(sessionId, true);
    setPendingAmbiguousValue(sessionId, ambiguousNumberAnswer);
    setLastQuestionAsked(sessionId, disambiguationQuestion);
    incrementClarificationCount(sessionId);
    return envelope({
      kind: 'clarification',
      sessionId,
      needsClarification: true,
      clarificationRound: getClarificationCount(sessionId),
      maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
      reply: cleanReply(disambiguationQuestion),
    });
  }

  // SANITY GATE (root-cause fix): independent check between "the
  // classifier decided this is a symptom" and "trust it as evidence" —
  // see symptomSanityGate.js's doc comment for the four separate live
  // bugs (mood language, misread denials, a bogus disambiguation
  // question, a misfired affirmation-to-candidates rule) that all
  // reached Infermedica through this exact gap. Applied here, before
  // anything is stored, so a bad entry never even makes it into
  // already_known_symptoms context for a later turn.
  classifiedSymptoms = sanityFilterSymptoms(classifiedSymptoms, 'accumulate');
  // DETERMINISTIC OVERRIDE (found live): asking the model to self-report
  // durationGuessed/severityGuessed on the schema turned out unreliable
  // — even marked required, it consistently omitted them (confirmed
  // live, 0/6 across two separate test rounds; callAIStructured sends
  // the schema as a text instruction, not a strictly-enforced API mode,
  // so "required" has no real teeth here). This is derivable
  // deterministically instead: a compound-answer rule blanket-applying
  // ONE duration+severity pair to MULTIPLE symptoms in the same turn
  // (symptomClassifier.js's rule 2) produces two-or-more entries with
  // the EXACT SAME duration AND severity — a genuinely named,
  // per-symptom answer (rule 1) would naturally give each symptom its
  // OWN distinct value. Detect that shape directly and force the flags,
  // regardless of whatever the model itself reported.
  if (classifiedSymptoms.length > 1) {
    const pairCounts = new Map();
    for (const s of classifiedSymptoms) {
      if (!s.duration && !s.severity) continue;
      const pairKey = `${s.duration || ''}|${s.severity || ''}`;
      pairCounts.set(pairKey, (pairCounts.get(pairKey) || 0) + 1);
    }
    // BUG (found live): "about 20 minutes, it's a 9 out of 10" after
    // chest pain + arm pain + sweating marked ALL three as guessed, so the
    // bot asked "how severe is the chest pain?" right after being told.
    // The answer is real for the MAIN complaint (the first one listed);
    // only the others are guesses.
    const firstOfPair = new Set();
    for (const s of classifiedSymptoms) {
      if (!s.duration && !s.severity) continue;
      const pairKey = `${s.duration || ''}|${s.severity || ''}`;
      if (pairCounts.get(pairKey) > 1 && firstOfPair.has(pairKey)) {
        if (s.duration) s.durationGuessed = true;
        if (s.severity) s.severityGuessed = true;
      }
      firstOfPair.add(pairKey);
    }
  }
  // Snapshot of what was present before this turn's merge — used below to
  // tell the patient exactly what was removed (see removedThisTurn).
  const presentBeforeMerge = getAccumulatedSymptoms(sessionId).filter((s) => s.present).map((s) => s.term);
  if (classifiedSymptoms.length) {
    // BUG (found live): a symptom restated as present in a genuinely
    // NEW round, after already being `finalized` from an EARLIER
    // recommendation this session (see the thisTurnPresentTerms fix
    // above for why it correctly counts as "new round" at all), keeps
    // its OLD duration/severity from that earlier round — the merge in
    // chatLog.js's appendAccumulatedSymptoms only overwrites
    // duration/severity when THIS turn actually supplies a new value,
    // by design (restating more detail about an in-progress symptom
    // shouldn't erase what's already known). But a stale value from a
    // DIFFERENT, already-treated episode is a different situation: it
    // silently satisfies assessIntake's "does the main symptom already
    // have duration AND severity?" sufficiency check, even though this
    // round's real new complaint (e.g. "eye pain" arriving alongside a
    // restated "nausea") has nothing gathered for it at all —
    // demonstrated live: a fresh "I have nausea and eye pain" (nausea
    // already finalized with old detail, eye pain brand new) skipped
    // straight to a recommendation with zero clarifying questions
    // asked about either symptom. A revived symptom's PRIOR episode's
    // duration/severity isn't evidence about THIS episode — clear it
    // unless this turn also supplied a fresh value, so the round looks
    // exactly as informative as it actually is.
    const priorAccumulated = getAccumulatedSymptoms(sessionId);
    const revivedTerms = new Set(
      classifiedSymptoms
        .filter((s) => s.present !== false && !s.duration && !s.severity && !s.severityCorrected && !s.durationCorrected)
        .map((s) => s.term.toLowerCase().trim())
        .filter((term) => priorAccumulated.some((e) => e.term.toLowerCase().trim() === term && e.finalized))
    );
    appendAccumulatedSymptoms(sessionId, classifiedSymptoms);
    if (revivedTerms.size) {
      clearStaleDurationSeverity(sessionId, revivedTerms);
    }
  }
  if (finalConfirmationResolution?.removals?.length) {
    // Same merge-by-exact-term path as any other symptom update — just
    // flips present to false on the named, already-known entry rather
    // than deleting it (consistent with how a plain denial is recorded
    // everywhere else in this app).
    appendAccumulatedSymptoms(
      sessionId,
      finalConfirmationResolution.removals.map((term) => ({ term, present: false, duration: null, severity: null }))
    );
  }
  const accumulated = getAccumulatedSymptoms(sessionId);
  // PRODUCT REQUIREMENT: whenever the patient takes a symptom back, say so
  // and show what's left. A turn that ONLY takes things back already does
  // this (RETRACTION HANDLING below); this covers a removal that arrives
  // together with something new ("no more headache, but now I have a
  // fever"), which used to be applied silently. Applied in
  // runPatientMessageTurn, so it reaches whichever reply this turn produces.
  {
    const stillPresentLower = new Set(accumulated.filter((s) => s.present).map((s) => s.term.toLowerCase().trim()));
    const removedThisTurn = presentBeforeMerge.filter((t) => !stillPresentLower.has(t.toLowerCase().trim()));
    if (removedThisTurn.length) setRemovalNote(sessionId, removedThisTurn, accumulated.filter((s) => s.present));
  }

  // ================================================================
  // RETRACTION HANDLING (root-cause fix, general case): a turn whose
  // ENTIRE contribution was denying one or more already-tracked
  // symptoms (e.g. "I don't really have blood in urine anymore") used
  // to be indistinguishable, everywhere in this state machine EXCEPT
  // the final-confirmation gate, from a turn that contributed nothing
  // at all — assessIntake and the "new round, nothing new yet" branch
  // below only ever look for NEW present symptoms, so a genuine,
  // successfully-recognized denial got the same generic "I couldn't
  // tell what new symptoms you're experiencing" reply forever, with no
  // acknowledgment and no way out short of the (separate, narrower)
  // final-confirmation gate's own add/remove handling. Rather than add
  // a one-off branch, route it into that SAME well-tested gate:
  // acknowledge what changed, read the current list back, and let the
  // patient add more, remove more, or confirm — exactly the three-way
  // decision resolveFinalConfirmation already knows how to make on the
  // NEXT turn. Guarded to the non-final-confirmation path only — that
  // gate already handles its own removals via
  // finalConfirmationResolution.removals, so this never double-handles
  // the same turn.
  // ================================================================
  const pureRetractionTurn =
    !wasAnsweringFinalConfirmation &&
    classifiedSymptoms.length > 0 &&
    classifiedSymptoms.every((s) => s.present === false);

  if (pureRetractionTurn) {
    const stillPresent = accumulated.filter((s) => s.present);
    const retractedNames = classifiedSymptoms.map((s) => s.term).join(', ');
    resetOffTopicStreak(sessionId);
    let reply;
    if (stillPresent.length) {
      setAwaitingFinalConfirmation(sessionId, true);
      incrementClarificationCount(sessionId);
      reply =
        `Got it, I've noted that you no longer have ${retractedNames}. So far I still have: ` +
        `${listMarker(formatClassifiedSymptoms(stillPresent))}. Would you like to add anything, remove anything else, ` +
        `or should I go ahead and recommend a specialist?`;
    } else {
      // Nothing left on file at all — do NOT route toward finalize
      // (there would be no actual evidence to send Infermedica).
      // Leave both pending-question flags unset so the next message is
      // treated as a genuinely fresh, open-ended turn.
      resetClarificationCount(sessionId);
      reply = `Got it, I've noted that you no longer have ${retractedNames}. That's everything you'd told me about — let me know if anything else comes up.`;
    }
    return envelope({
      kind: 'clarification',
      sessionId,
      needsClarification: stillPresent.length > 0,
      resolvedAge: age,
      resolvedSex: sex,
      reply: cleanReply(reply),
    });
  }

  // ================================================================
  // OFF-TOPIC STREAK BOOKKEEPING
  // Bounded safety valve for a pending-question turn STAGE 1 judged
  // genuinely off-topic (offTopicThisTurn) — separate, smaller budget
  // than MAX_CLARIFICATION_ROUNDS (see chatLog.js's offTopicStreak) so
  // noise doesn't cost a cooperative patient their real rounds, but a
  // patient who never engages with the question still can't stall the
  // conversation forever. Reset the moment a turn actually contributes
  // something real.
  // ================================================================
  if (wasAnsweringPendingQuestion) {
    const contributedSomethingReal =
      classifiedSymptoms.length > 0 ||
      turnMentionedConditions.length > 0 ||
      Boolean(
        finalConfirmationResolution &&
        (finalConfirmationResolution.no_change ||
          finalConfirmationResolution.additions.length > 0 ||
          finalConfirmationResolution.removals.length > 0)
      );
    if (offTopicThisTurn && !contributedSomethingReal) {
      incrementOffTopicStreak(sessionId);
    } else {
      resetOffTopicStreak(sessionId);
    }
  }

  if (offTopicThisTurn && getOffTopicStreak(sessionId) > MAX_OFF_TOPIC_STREAK) {
    resetOffTopicStreak(sessionId);
    if (accumulated.filter((s) => s.present).length === 0) {
      // Repeated off-topic replies and nothing usable gathered at all —
      // reset the round budget and ask plainly from scratch rather than
      // keep repeating a question that isn't landing.
      resetClarificationCount(sessionId);
      return envelope({
        kind: 'clarification',
        sessionId,
        needsClarification: true,
        resolvedAge: age,
        resolvedSex: sex,
        reply: cleanReply(
          "I'm having trouble following — let's start simple: what symptoms are you experiencing right now, and how long have you had them?"
        ),
      });
    }
    // Something usable IS already on file — stop re-asking a question
    // that keeps getting an unrelated reply, and offer to go ahead with
    // what we have (never straight to the engine without a yes).
    return askFinalConfirmation({
      sessionId, accumulated, age, sex,
      lead: "Let's not get stuck on that question.",
    });
  }

  // ================================================================
  // STAGE 7: READINESS STATE MACHINE
  //
  // This replaces the old per-turn Infermedica evidence/triage loop
  // entirely. The flow now is:
  //   1. Gather symptoms via Groq-only classification (STAGE 6) —
  //      no Infermedica involved.
  //   2. While there's still a useful TARGETED question to ask, ask it
  //      — assessIntake (clarificationCheck.js) judges both whether
  //      enough is known yet and, if not, what to ask next.
  //   3. Once assessIntake says enough is known (or the round budget
  //      runs out), ask ONE final gate: "is there anything else
  //      before I recommend a specialist?"
  //   4. If the patient's answer to that gate adds nothing new, that's
  //      read as confirmation — finalize now (the one and only point
  //      Infermedica gets called this round).
  //   5. If it adds something new, loop back to step 2 for the new
  //      material.
  //   6. If the patient adds symptoms again AFTER a recommendation has
  //      already been given, this whole loop naturally restarts for
  //      just the new material (see chatLog.js's `finalized` flag /
  //      resetClarificationCount), and Infermedica gets called again
  //      exactly once for that new round.
  // A single shared round counter (MAX_CLARIFICATION_ROUNDS) caps BOTH
  // question types combined, so a patient who never gives a clean
  // answer still gets forced to a recommendation eventually rather
  // than being questioned indefinitely.
  // ================================================================
  if (wasAnsweringFinalConfirmation) {
    const resolution = finalConfirmationResolution;

    if (!resolution.understood) {
      // Genuinely unclear — read the real list back instead of
      // guessing. Bounded by MAX_CLARIFICATION_ROUNDS + 1 (one extra
      // beyond the normal targeted-question budget, since this re-ask
      // doesn't consume a targeted-question round on its own path) so a
      // patient who never gives a parseable answer still gets forced to
      // a recommendation eventually, same reasoning as the targeted-
      // question cap below.
      // Past the round budget: keep asking (never finalize without an
      // explicit yes), but as plainly as possible.
      if (getClarificationCount(sessionId) >= MAX_CLARIFICATION_ROUNDS + 1) {
        return askFinalConfirmation({
          sessionId, accumulated, age, sex,
          lead: 'Sorry, I still could not tell. Please reply "yes" to get your recommendation, or tell me what to add or remove.',
        });
      }
      setAwaitingFinalConfirmation(sessionId, true);
      incrementClarificationCount(sessionId);
      const listText = formatClassifiedSymptoms(accumulated.filter((s) => s.present)) || 'nothing yet';
      const rereadReply =
        `I couldn't quite tell what you meant there. Just to confirm — so far I have: ${listMarker(listText)}. ` +
        `Would you like to add anything, remove anything, or should I go ahead and recommend a specialist?`;
      return envelope({
        kind: 'clarification',
        sessionId,
        needsClarification: true,
        resolvedAge: age,
        resolvedSex: sex,
        reply: cleanReply(rereadReply),
      });
    }

    const madeChanges = resolution.additions.length > 0 || resolution.removals.length > 0;
    if (!madeChanges) {
      // Confirmed no changes — go ahead and recommend.
      return await finalizeAndRecommend({ sessionId, patientId, message, age, sex, isDiagnosisRequest, subjectInfo });
    }
    // Patient added and/or removed something right when we thought we
    // were done. FIXED: this used to fall through into the round-cap
    // check below with the SAME clarificationCount that already covered
    // the symptom(s) just wrapped up — so a cooperative patient who
    // kept adding real symptoms (each one consuming a round via the
    // targeted-question-then-final-gate cycle) could hit
    // MAX_CLARIFICATION_ROUNDS right as they mentioned something new,
    // and get bounced straight into finalizeAndRecommend() without ever
    // being asked about the new symptom's duration/severity. New
    // material deserves its own fresh gathering budget, same as
    // symptoms added AFTER a recommendation already exists (see
    // finalizeAndRecommend's resetClarificationCount call) — the safety
    // valve is meant to stop a patient who won't give clean answers,
    // not to punish one who keeps giving new (or corrected) ones.
    resetClarificationCount(sessionId);

    // BUG (found live, two variants): a reply to "anything else?" that
    // ONLY removed something (no new additions) used to fall straight
    // through into the generic new-round-gathering logic below instead
    // of being acknowledged here — that logic only looks at UNFINALIZED
    // present symptoms (newRoundPresentSymptoms, further down), so a
    // removal that left only ALREADY-FINALIZED symptoms behind (e.g.
    // denying "amenorrhea" from an earlier recommendation round, with
    // "nausea"/"vomiting" from that same earlier round still present but
    // finalized:true) produced totalPresentCount === 0 down there and
    // triggered the unrelated "I couldn't tell what new symptoms you're
    // experiencing" question — even though a real removal HAD just been
    // understood and applied, just never acknowledged. The original fix
    // only covered the narrower case where NOTHING at all was left
    // present; this covers every additions.length === 0 case uniformly,
    // using ALL present symptoms (finalized or not) for the readback —
    // same acknowledgment shape pureRetractionTurn already uses for the
    // equivalent non-final-confirmation case.
    if (resolution.additions.length === 0) {
      resetOffTopicStreak(sessionId);
      const retractedNames = resolution.removals.join(', ');
      const stillPresent = accumulated.filter((s) => s.present);
      let reply;
      if (stillPresent.length) {
        setAwaitingFinalConfirmation(sessionId, true);
        reply =
          `Got it, I've noted that you no longer have ${retractedNames}. So far I still have: ` +
          `${listMarker(formatClassifiedSymptoms(stillPresent))}. Would you like to add anything, remove anything else, ` +
          `or should I go ahead and recommend a specialist?`;
      } else {
        reply = `Got it, I've noted that you no longer have ${retractedNames}. That's everything you'd told me about — let me know if anything else comes up.`;
      }
      return envelope({
        kind: 'clarification',
        sessionId,
        needsClarification: stillPresent.length > 0,
        resolvedAge: age,
        resolvedSex: sex,
        reply: cleanReply(reply),
      });
    }

    // SIBLING FIX to the removals-only branch above: an addition (a new
    // symptom OR — see resolveFinalConfirmation's rule 3b — a detail
    // CORRECTION to an already-known one, like "actually its not for 3
    // days, more like 5") used to fall straight through into the generic
    // targeted-question gathering logic below no matter what, even when
    // nothing was actually missing. Demonstrated live: correcting a
    // duration on a symptom whose severity was already known produced a
    // fresh, unrelated question ("is it constant or does it come and
    // go?") instead of just re-showing the same "anything else, or go
    // ahead?" gate — technically not wrong (assessIntake below CAN
    // legitimately ask for more), but a jarring, unnecessary detour for a
    // turn that only corrected one detail. Only take this shortcut when
    // EVERY addition this turn already has both duration and severity on
    // file after the merge above (a real, incomplete new symptom still
    // needs to fall through so assessIntake can ask what's missing).
    const allAdditionsFullySpecified = resolution.additions.every((a) => {
      const merged = accumulated.find((e) => e.term.toLowerCase().trim() === a.term.toLowerCase().trim());
      return merged && merged.present && merged.duration && merged.severity;
    });
    if (allAdditionsFullySpecified) {
      resetOffTopicStreak(sessionId);
      setAwaitingFinalConfirmation(sessionId, true);
      const stillPresent = accumulated.filter((s) => s.present);
      const removedNote = resolution.removals.length
        ? `I've also noted you no longer have ${resolution.removals.join(', ')}. `
        : '';
      const reply =
        `Got it, I've updated that. ${removedNote}So far I have: ${listMarker(formatClassifiedSymptoms(stillPresent))}. ` +
        `Would you like to add anything, remove anything, or should I go ahead and recommend a specialist?`;
      return envelope({
        kind: 'clarification',
        sessionId,
        needsClarification: true,
        resolvedAge: age,
        resolvedSex: sex,
        reply: cleanReply(reply),
      });
    }
    // Otherwise: at least one addition is still missing duration or
    // severity — fall through to the normal targeted-question gathering
    // logic below, which already knows how to ask for exactly what's
    // missing (assessIntake / getClarifyingQuestion).
  }

  if (accumulated.length === 0) {
    // ESCAPE HATCH (root-cause fix, demonstrated live): before this,
    // this branch had no memory of how many times it had already asked
    // the exact same question — a real, demonstrated dead end: an
    // early-exit gate (emergency, off-topic-as-concerning, diagnosis-
    // decline) discards its triggering message without ever
    // classifying it, so nothing accumulates and no pending-question
    // flag gets set either. Every later short reply then arrives fresh
    // with nothing to attach to, repeats this exact same generic
    // question, and the conversation is permanently stuck — none of
    // this app's other safety valves (offTopicStreak, clarificationCount)
    // ever engage, because none of them apply when NO question was ever
    // pending in the first place. After a couple of unproductive turns
    // in a row, stop repeating the same line and give a plainer,
    // example-based prompt instead — the kind of concrete nudge that
    // actually breaks a "the bot isn't understanding me" loop.
    incrementNoSymptomStreak(sessionId);
    const streak = getNoSymptomStreak(sessionId);
    // Same sibling fix as the round-cap fallback below: acknowledge a
    // mentioned condition ("I'm pregnant") instead of a fully generic
    // "I couldn't identify any symptoms" when that's the only real
    // content in the very first message(s) of a session.
    const reply = turnMentionedConditions.length
      ? `Thanks, I've noted that. What symptoms are you experiencing right now — where does it hurt, and how long has it been going on?`
      : streak > 2
      ? "I'm still not able to tell what's bothering you physically from your messages. Let's try once more, " +
        'plainly: describe the main thing you\'re feeling in one sentence — for example, "I have a headache ' +
        'that started two days ago." If you were worried this might be an emergency, please make sure you\'ve ' +
        'already gotten help rather than waiting on this chat.'
      : "I couldn't identify any symptoms in your message. Could you tell me what you're feeling physically — " +
        'where it hurts, and how long it has been going on?';
    return envelope({
      kind: 'clarification',
      sessionId,
      needsClarification: true,
      resolvedAge: age,
      resolvedSex: sex,
      reply: cleanReply(reply),
    });
  }
  resetNoSymptomStreak(sessionId);

  const roundsUsed = getClarificationCount(sessionId);

  // FIXED (demonstrated live): a fresh round that starts AFTER an
  // earlier recommendation this session (e.g. a new health event
  // mentioned later on, or the patient just adding something new) used
  // to be measured against ALL accumulated present symptoms here,
  // including ones a PRIOR recommendation already covered
  // (`finalized: true`). An old symptom often has no recorded
  // duration/severity of its own — nothing ever required it to, once
  // it was already finalized — so that blind count kept flagging
  // "needs clarification" and asking about the OLD, already-closed
  // symptom on a round that had nothing to do with it (a live example:
  // "How long has the headache been going on?" resurfacing after the
  // headache had already been recommended on, triggered by an
  // unrelated new allergy-exposure event). Only symptoms NEW this round
  // should count toward whether more targeted questions are needed.
  // BUG (found live): a patient re-reporting a symptom NAME that
  // happens to match one already `finalized` from an earlier
  // recommendation this session (e.g. "nausea" was part of an earlier
  // amenorrhea/nausea/vomiting recommendation, and the patient later
  // says "I am pregnant and have nausea" as a genuinely fresh, current
  // complaint) was invisible here — appendAccumulatedSymptoms
  // deliberately never resets `finalized` back to false on a term
  // match (see its own doc comment: restating more DETAIL about an
  // already-recommended symptom shouldn't inflate cross-domain risk
  // counting). But "the patient just told me about this again, right
  // now" and "don't double-count old evidence for risk scoring" are two
  // different questions, and conflating them meant a real, freshly
  // reported symptom got silently excluded from both this round-cap
  // check and assessIntake's input below — producing the same generic
  // "I couldn't tell what new symptoms you're experiencing" question on
  // repeat, even though the patient had just clearly answered it,
  // burning through the whole clarification-round budget. Recover the
  // set of terms classifySymptoms actually reported present THIS turn
  // (independent of the accumulated list's finalized flag) and count a
  // finalized entry as part of the new round too if it was just
  // restated.
  const thisTurnPresentTerms = new Set(
    classifiedSymptoms
      .filter((s) => s.present !== false)
      .map((s) => s.term.toLowerCase().trim())
  );
  // A finalized symptom restated now stays part of this round on LATER
  // turns too, not just this one — see chatLog.js's getReopenedTerms for
  // the live bug ("yes" to a follow-up about joint swelling dropped the
  // re-opened joint pain out of the round entirely).
  addReopenedTerms(
    sessionId,
    accumulated
      .filter((s) => s.finalized && thisTurnPresentTerms.has(s.term.toLowerCase().trim()))
      .map((s) => s.term)
  );
  const reopenedTerms = new Set(getReopenedTerms(sessionId));
  const newRoundPresentSymptoms = accumulated.filter((s) => {
    if (!s.present) return false;
    const key = s.term.toLowerCase().trim();
    return !s.finalized || thisTurnPresentTerms.has(key) || reopenedTerms.has(key);
  });
  const totalPresentCount = newRoundPresentSymptoms.length;

  // A new round has started (there's at least one finalized symptom
  // from an earlier recommendation) but nothing concrete has been
  // established for THIS round yet (e.g. a bare "yes" answering an
  // event follow-up, with no symptom name in it). Ask plainly instead
  // of falling into assessIntake below, which would otherwise be asked
  // to judge sufficiency with an empty symptoms array for this round
  // and have nothing new to reason about.
  if (totalPresentCount === 0 && accumulated.some((s) => s.finalized) && roundsUsed < MAX_CLARIFICATION_ROUNDS) {
    // If the ONLY real content this turn was a mentioned condition
    // ("I'm pregnant") with no physical symptom alongside it, say so —
    // the fully generic question otherwise reads as though the patient
    // wasn't heard at all, even though the condition WAS recorded (see
    // turnMentionedConditions/appendMentionedConditions above).
    const reply = turnMentionedConditions.length
      ? `Thanks, I've noted that. What symptoms are you experiencing right now?`
      : "I couldn't tell what new symptoms you're experiencing — could you describe what's happening physically right now?";
    setAwaitingClarificationAnswer(sessionId, true);
    setLastQuestionAsked(sessionId, reply);
    incrementClarificationCount(sessionId);
    return envelope({
      kind: 'clarification',
      sessionId,
      needsClarification: true,
      clarificationRound: getClarificationCount(sessionId),
      maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
      resolvedAge: age,
      resolvedSex: sex,
      reply: cleanReply(reply),
    });
  }

  // FIXED: this used to finalize IMMEDIATELY the moment roundsUsed hit
  // MAX_CLARIFICATION_ROUNDS, even if the patient had never once been
  // shown the final "anything else before I recommend?" gate — a
  // patient who used up the round budget on targeted duration/severity
  // questions could get bounced straight to Infermedica with no chance
  // to add or correct anything. The round cap should only trim further
  // TARGETED questions; the final gate always gets shown at least once
  // before a recommendation is produced. So: only ask another targeted
  // question if budget remains — if it doesn't, fall through to the
  // "ask the final gate" code below instead of finalizing directly.
  //
  // WHETHER to ask AND what to ask are now decided together, in one
  // judgment call — assessIntake (clarificationCheck.js), given the
  // real accumulated per-symptom duration/severity (not just names) and
  // the question this app most recently asked, instead of the old
  // checkNeedsClarification regex, which only ever looked at the raw
  // text of the CURRENT message and could re-ask something already
  // answered a turn earlier (see assessIntake's doc comment for the
  // demonstrated live bug this replaced). isOverrideRequested's
  // deterministic "just give me a recommendation" short-circuit still
  // runs — that's cheap and unambiguous, no reason to spend a call on
  // it — via the roundsUsed check plus assessIntake itself declining to
  // ask further once enough is known. The fixed template
  // (getClarifyingQuestion) is kept as a fallback for when the Groq
  // call fails outright, so a network hiccup never blocks the
  // conversation. Composing a QUESTION carries none of the risk that
  // composing a symptom classification did (see symptomClassifier.js's
  // "no" bug) — there's no medical assertion in a sentence that just
  // asks something, so this remains a safe place to let the model have
  // room.
  // BUG (found live): a duration/severity value blanket-applied to
  // multiple symptoms by the compound-answer rule (symptomClassifier.js
  // — "6 and few days" answering a question about nausea AND eye pain
  // together, neither named specifically) looks identical to a real,
  // confirmed value to assessIntake below, which only checks whether
  // duration/severity are non-null. Once the patient corrects ONE
  // symptom's guessed value, the OTHER's still-guessed, never actually
  // confirmed value silently satisfies "already has enough detail" and
  // the round skips straight to a recommendation without ever asking
  // about it — demonstrated live. This view — used ONLY for
  // assessIntake's sufficiency judgment — treats a still-guessed
  // duration/severity as unknown; the REAL accumulated entry (with the
  // guessed value intact, still better than nothing) is untouched and
  // is what actually reaches Infermedica if the round cap is hit first.
  // With only ONE symptom left there's nothing to have guessed between —
  // found live: "tingling and numbness for 2 weeks", then "the tingling
  // stopped", and the bot asked how long the numbness had lasted.
  const onlyOneLeft = newRoundPresentSymptoms.length === 1;
  const symptomsForIntakeAssessment = newRoundPresentSymptoms.map((s) => ({
    ...s,
    duration: s.durationGuessed && !onlyOneLeft ? null : s.duration,
    severity: s.severityGuessed && !onlyOneLeft ? null : s.severity,
  }));

  // BUG (found live): isOverrideRequested existed for exactly this case
  // — the patient explicitly signaling they want to stop answering
  // targeted questions and move on ("no just leave it, go ahead") — but
  // was never actually called anywhere in this pipeline (confirmed:
  // defined and exported, referenced only in a stale comment claiming
  // it "still runs"). Without it, this app has no way to hear "I'm
  // done" during the targeted-question loop at all — only classifySymptoms'
  // per-question relevance judgment runs, which correctly says the
  // reply doesn't answer the specific question asked, and re-asks it
  // ("that doesn't seem related to what we're discussing — ...") rather
  // than recognizing the patient's actual intent. Treated the same way
  // the round-cap already is: skip straight to "sufficient", moving on
  // to the final gate/wrap-up instead of asking anything further. Two
  // independent signals feed this: isOverrideRequested (a fixed phrase
  // list, cheap and exact) and skipAheadRequested (classifyPendingAnswerRelevance's
  // own meaning-based SKIP_AHEAD judgment, set above — catches phrasing
  // the fixed list can't anticipate).
  const roundsRemainingIncludingThis = Math.max(1, MAX_CLARIFICATION_ROUNDS - roundsUsed);
  const intakeAssessment = (roundsUsed >= MAX_CLARIFICATION_ROUNDS || isOverrideRequested(message) || skipAheadRequested)
    ? { sufficient: true, question: null }
    : await assessIntake({
        symptoms: symptomsForIntakeAssessment,
        lastQuestionAsked: getLastQuestionAsked(sessionId),
        currentMessage: message,
        roundsRemaining: roundsRemainingIncludingThis,
        maxRounds: MAX_CLARIFICATION_ROUNDS,
      });

  // BUG (found live): when assessIntake's AI call failed (provider quota),
  // the fixed fallback ALWAYS asked "how long, and how severe?" — even
  // right after the patient said "only today". The fallback now asks only
  // for what's actually missing, and moves on to the confirmation when
  // nothing is.
  let assessment = intakeAssessment;
  if (!assessment.sufficient && !assessment.question) {
    const gap = findMissingDetail(symptomsForIntakeAssessment);
    assessment = gap
      ? { sufficient: false, question: fallbackDetailQuestion(gap), referencedSymptom: gap.term, askedAboutSymptoms: [] }
      : { sufficient: true, question: null };
  }

  if (!assessment.sufficient) {
    const questionText = assessment.question;

    // FIXED (properly this time): an answer to our own targeted
    // question that contributes NOTHING relevant (e.g. "oh I love
    // apples") used to silently get the exact same question asked
    // again with no acknowledgment. The earlier fix for this was a
    // cheap deterministic guess (classifiedSymptoms.length === 0 with
    // no duration/severity regex match) — cheap because it was really
    // just re-deriving "is this off-topic" from secondhand signals
    // instead of asking the one thing already built to answer that
    // question. Now that STAGE 1's domain classifier runs on every
    // turn (context-aware — see classifyPendingAnswerRelevance), this
    // just reads its actual verdict for this turn instead of guessing.
    const contributedNothing = wasAnsweringClarification && offTopicThisTurn;
    const replyText = contributedNothing
      ? `That doesn't seem related to what we're discussing — ${questionText}`
      : questionText;

    setAwaitingClarificationAnswer(sessionId, true);
    setLastQuestionAsked(sessionId, questionText);
    setLastQuestionCandidates(sessionId, assessment.askedAboutSymptoms || []);
    setLastQuestionSymptom(sessionId, assessment.referencedSymptom || null);
    incrementClarificationCount(sessionId);
    return envelope({
      kind: 'clarification',
      sessionId,
      needsClarification: true,
      clarificationRound: getClarificationCount(sessionId),
      maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
      resolvedAge: age,
      resolvedSex: sex,
      // assessIntake's own question is already natural; the "doesn't seem
      // related" prefix or the template fallback still need composing.
      aiWritten: Boolean(intakeAssessment.question) && !contributedNothing,
      reply: cleanReply(replyText),
    });
  }

  // Reached when assessIntake says enough is known or the round budget is
  // spent. This used to finalize outright once past the +1 ceiling; now
  // it always shows the final gate, since the patient's explicit
  // go-ahead is required before the clinical engine is ever called.

  // Nothing targeted left to ask — ask the final gate once. Reads the
  // REAL accumulated list back (deterministically rendered via
  // formatClassifiedSymptoms — never left to Groq to recall from
  // memory, so it can't omit or invent a symptom here) so the patient
  // can explicitly add to or correct it, not just a generic "anything
  // else?" with no visibility into what's actually on file.
  return askFinalConfirmation({ sessionId, accumulated, age, sex });
}

/**
 * Shows the "So far I have: … should I go ahead?" gate. PRODUCT
 * REQUIREMENT: this is the ONLY road to finalizeAndRecommend() — the
 * clinical engine is never called until the patient has seen the exact
 * list being sent and said to go ahead. Every place that used to finalize
 * on its own (off-topic streak, unclear gate answer, round cap) now asks
 * this instead.
 *
 * @param {{sessionId: string, accumulated: Array, age: number, sex: string, lead?: string}} params
 *   lead: optional sentence shown before the list (e.g. after an unclear reply).
 */
function askFinalConfirmation({ sessionId, accumulated, age, sex, lead = '' }) {
  setAwaitingFinalConfirmation(sessionId, true);
  incrementClarificationCount(sessionId);
  const knownListText = formatClassifiedSymptoms(accumulated.filter((s) => s.present)) || 'nothing yet';
  const finalGateReply =
    `${lead ? `${lead} ` : ''}So far I have: ${listMarker(knownListText)}. Would you like to add anything, remove anything, ` +
    `or should I go ahead and recommend a specialist? Just say no (or "that's all") to go ahead.`;
  return envelope({
    kind: 'clarification',
    sessionId,
    needsClarification: true,
    awaitingConfirmation: true,
    clarificationRound: getClarificationCount(sessionId),
    maxClarificationRounds: MAX_CLARIFICATION_ROUNDS,
    resolvedAge: age,
    resolvedSex: sex,
    reply: cleanReply(finalGateReply),
  });
}

// ------------------------------------------------------------------
// FINALIZE & RECOMMEND
// The ONLY place in the symptom pipeline that calls Infermedica. Runs
// once per "round" — when the patient has confirmed there's nothing
// more to add (or the round cap forced it) — never on an ordinary
// gathering turn. Takes the session's accumulated symptom labels
// (Groq's own classification, never the patient's literal words —
// see symptomClassifier.js), maps them to real Infermedica evidence
// with a single /parse call, then runs the same triage/specialist/
// lab-test/recommendation logic the pipeline always has.
// ------------------------------------------------------------------
async function finalizeAndRecommend({ sessionId, patientId, message, age, sex, isDiagnosisRequest, subjectInfo }) {
  // isDependentSubject no longer plumbed through — a dependent (or
  // mixed self+dependent) message is declined outright back in STAGE 4
  // and never reaches this function at all now (see that stage's doc
  // comment). Anything reaching here is the patient's own care, so the
  // patient's own lab values / history / profile facts are always
  // fetched below.
  // SANITY GATE (root-cause fix), applied again here as defense in
  // depth — see symptomSanityGate.js's doc comment. The STAGE 6 call
  // site (above, in runPatientMessagePipeline) already filters
  // everything on its way INTO accumulatedSymptoms, so this is a
  // last-resort backstop, not the primary defense: it catches anything
  // that reached accumulatedSymptoms some other way (e.g. a session
  // hydrated from Supabase state written before this gate existed).
  // This is the actual choke point every accumulated symptom passes
  // through on its way to becoming Infermedica evidence, regardless of
  // which upstream path produced it.
  const accumulated = sanityFilterSymptoms(getAccumulatedSymptoms(sessionId), 'pre-infermedica');

  // Split into what's already been through an EARLIER recommendation
  // this session (finalized: true) vs. what's new this round
  // (finalized: false) — pure bookkeeping over OUR OWN classification,
  // never anything Infermedica returned. Used below for the
  // cross-domain check.
  // A finalized symptom the patient re-opened this round (see
  // getReopenedTerms) is part of THIS round's complaint, not the earlier
  // one — otherwise "joint pain is worse now, and there's swelling and
  // redness" compared joint pain against its own swelling/redness and
  // told the patient they "don't look related".
  const reopenedAtFinalize = new Set(getReopenedTerms(sessionId));
  const isReopened = (s) => reopenedAtFinalize.has(s.term.toLowerCase().trim());
  const priorRoundSymptoms = accumulated.filter((s) => s.finalized && !isReopened(s));
  const newRoundSymptoms = accumulated.filter((s) => !s.finalized || isReopened(s));

  // ================================================================
  // THE ONE INFERMEDICA CALL THAT MATTERS: map the accumulated,
  // already-classified symptom labels to real evidence.
  //
  // REPLACED: this used to be an LLM rewrite at temperature 0.4, so the
  // same symptoms reached /parse worded differently each time and came
  // back as different evidence (joint pain: Surgeon one run, Orthopedist
  // the next). Now one fixed sentence per symptom — see
  // describeSymptomsForParse (symptomClassifier.js) for what /parse was
  // measured to misread and why duration is left out.
  // ================================================================
  const fullText = describeSymptomsForParse(accumulated);
  // Always visible — a triage result often only makes sense in light of
  // the exact text Infermedica saw. Cheap: one line, every finalize call.
  console.log(`[processMessage] finalize: sending to Infermedica /parse: "${fullText}"`);
  let keptMentions = [];
  let rawMentionCount = 0;
  let droppedMentions = [];
  try {
    const rawMentions = await parsePatientMessage(fullText, age);
    rawMentionCount = rawMentions.length;
    const { kept, dropped } = filterGroundedMentions(rawMentions, fullText);
    keptMentions = kept;
    droppedMentions = dropped;
  } catch (err) {
    console.error('[processMessage] Infermedica /parse failed at finalize:', err.message);
    return envelope({
      kind: 'error',
      sessionId,
      resolvedAge: age,
      resolvedSex: sex,
      reply: "I'm having trouble connecting to the clinical engine. Please try again in a moment.",
    });
  }

  // Unaccounted complaints — a symptom Groq classified that
  // Infermedica's vocabulary still couldn't place. Normalized before
  // storage (symptomClassifier.js's normalizeComplaint) — never the
  // patient's own wording persisted anywhere.
  const newUnaccountedRaw = extractUnaccountedComplaints(fullText, keptMentions);
  const newUnaccounted = [];
  for (const clause of newUnaccountedRaw) {
    newUnaccounted.push(await normalizeComplaint(clause));
  }
  if (newUnaccounted.length) appendUnaccountedComplaints(sessionId, newUnaccounted);

  const dedup = new Map();
  for (const m of keptMentions) {
    dedup.set(`${m.id}:${m.choice_id || 'present'}`, { id: m.id, name: m.name, choice_id: m.choice_id || 'present' });
  }
  let mergedEvidence = Array.from(dedup.values());

  // CONTRADICTION BACKSTOP (root-cause fix, defense in depth): the dedup
  // key above includes choice_id, so if the SAME underlying Infermedica
  // finding id comes back with BOTH choice_id: 'present' AND choice_id:
  // 'absent' in this one /parse call, both survive deduplication and
  // would go to /triage as contradictory evidence about the same
  // finding. This shouldn't normally happen — accumulatedSymptoms is
  // supposed to hold at most one present/absent verdict per real-world
  // symptom (see chatLog.js's findLikelySameSymptom / clarificationCheck.js's
  // findLikelyKnownTerm for the term-matching that's meant to prevent
  // exactly this) — but it's the kind of thing that can still slip
  // through some path those checks don't cover (an unusual paraphrase
  // neither the model nor the curated synonym list resolves, a session
  // hydrated from before those fixes existed, etc). Rather than trust
  // that upstream matching is perfect, catch the actual contradiction
  // here, at the last point before Infermedica sees it: when both
  // verdicts exist for the same id, keep only 'absent'. An asserted
  // presence this app can't actually verify is uncontradicted is worse
  // to send than under-reporting one — a missed symptom can still surface
  // on a later turn or be raised with the doctor directly; a symptom
  // wrongly asserted present can inflate urgency or point to the wrong
  // specialist based on evidence that was never actually confirmed.
  const byId = new Map();
  for (const e of mergedEvidence) {
    if (!byId.has(e.id)) byId.set(e.id, []);
    byId.get(e.id).push(e);
  }
  const contradictedIds = [];
  for (const [id, entries] of byId) {
    const hasPresent = entries.some((e) => e.choice_id === 'present');
    const hasAbsent = entries.some((e) => e.choice_id === 'absent');
    if (hasPresent && hasAbsent) contradictedIds.push(id);
  }
  if (contradictedIds.length) {
    console.warn(
      `[processMessage] finalize: contradictory present+absent evidence for the same finding id(s) ` +
      `[${contradictedIds.join(', ')}] — keeping only 'absent' for each, dropping the unconfirmed 'present' entry.`
    );
    mergedEvidence = mergedEvidence.filter(
      (e) => !(contradictedIds.includes(e.id) && e.choice_id !== 'absent')
    );
  }

  if (mergedEvidence.length === 0) {
    // Diagnostic logging — this exact failure ("headache" and "eye
    // pain" both being extremely common Infermedica vocabulary terms
    // returning zero grounded matches) was previously an invisible dead
    // end. This makes it inspectable: what we sent, how many raw
    // mentions Infermedica returned at all, and what filterGroundedMentions
    // dropped and why (a dropped mention's orig_text not appearing as a
    // substring of fullText is the one thing that filter checks).
    console.warn(
      `[processMessage] finalize: 0 grounded mentions for fullText="${fullText}". ` +
      `Infermedica /parse returned ${rawMentionCount} raw mention(s) total. ` +
      `Dropped: ${JSON.stringify(droppedMentions.map((m) => ({ name: m.name, orig_text: m.orig_text })))}`
    );

    incrementUnmatchedFinalizeAttempts(sessionId);
    const attempts = getUnmatchedFinalizeAttempts(sessionId);

    if (attempts >= 2) {
      // FIXED: this used to be an unconditional dead end — ask to
      // rephrase, forever, with no escape hatch if Infermedica just
      // keeps failing to ground clearly real symptoms. Two consecutive
      // failures on a patient who has been specific and cooperative
      // means something in the matching pipeline isn't working right
      // now, not that nothing is wrong with them. A safe generic
      // fallback beats refusing to ever produce a recommendation.
      console.warn(`[processMessage] finalize: falling back to a generic recommendation after ${attempts} consecutive unmatched-evidence failures.`);
      resetUnmatchedFinalizeAttempts(sessionId);
      markAllSymptomsFinalized(sessionId);
      resetClarificationCount(sessionId);
      const fallbackReply = cleanReply(
        `I wasn't able to match what you've described to specific entries in the clinical database, but based on what ` +
        `you've told me, I'd recommend seeing a General Physician, who can evaluate your symptoms directly.` +
        (isDiagnosisRequest ? DECLINE_DIAGNOSIS_NOTE : '')
      );
      return envelope({
        kind: 'recommendation',
        sessionId,
        isDiagnosisRequest,
        urgency: 'routine',
        matchedSymptoms: [],
        matchSource: 'fallback',
        source: 'fallback',
        subject: subjectInfo,
        recommendation: {
          specialist_recommended: 'General Physician',
          rationale: 'Reported symptoms could not be matched to specific clinical database entries.',
          next_steps: 'See a General Physician for an in-person evaluation.',
          urgency: 'routine',
        },
        verification: null,
        resolvedAge: age,
        resolvedSex: sex,
        reply: fallbackReply,
      });
    }

    // First failure — give the patient one chance to rephrase. FIXED:
    // this used to leave BOTH awaitingClarificationAnswer and
    // awaitingFinalConfirmation false, so the next message fell through
    // to the normal off-topic/emergency AI gates as if it were an
    // unrelated fresh message instead of an answer to this question —
    // that's what produced the confusing "I can only help with health
    // symptoms" replies to "no I am finished" / "done" earlier.
    setAwaitingClarificationAnswer(sessionId, true);
    const reply =
      "I couldn't map what you've described to anything in the clinical database — could you describe " +
      'your main symptom a bit more plainly (what it is and roughly where)?';
    setLastQuestionAsked(sessionId, reply);
    return envelope({ kind: 'clarification', sessionId, needsClarification: true, resolvedAge: age, resolvedSex: sex, reply: cleanReply(reply) });
  }

  // Successful grounding this round — clear any earlier failure streak.
  resetUnmatchedFinalizeAttempts(sessionId);

  const evidenceForApi = mergedEvidence.map(({ id, choice_id }) => ({ id, choice_id, source: 'initial' }));
  const matchedSymptoms = mergedEvidence
    .filter((e) => e.choice_id !== 'absent')
    .map((e) => ({ id: e.id, name: e.name || e.id }));

  // Fetched EARLY (moved up from its old post-triage spot) so
  // pregnancy/chronic-condition risk factors can be turned into
  // Infermedica evidence and included in the SAME /triage,
  // /recommend_specialist, and /diagnosis calls that score urgency —
  // see getRiskFactorEvidence's doc comment below for why this matters:
  // those calls previously only ever saw symptom evidence, so the same
  // symptom (e.g. abdominal pain) was scored identically whether or not
  // the patient was pregnant or had a relevant chronic condition on
  // file, even though profileFacts had that information the whole time
  // — it just wasn't being sent anywhere Infermedica could use it.
  const storedProfileFacts = await getProfileFacts(patientId);
  const mentionedConditions = getMentionedConditions(sessionId);
  // Merged HERE, once, rather than separately at each downstream
  // consumer — profileFacts from this point on is what BOTH Infermedica
  // risk-factor evidence AND the recommendation prose/grounding
  // verification see, so a session-mentioned condition ("I have
  // diabetes") is treated identically to a stored one everywhere: it can
  // be cited in the reply and won't get wrongly blocked as an
  // "ungrounded_profile_fact_in_prose" violation (same failure class
  // already fixed once this session for a stored allergy) just because
  // it came from the conversation instead of Supabase.
  const profileFacts = [
    ...storedProfileFacts,
    ...mentionedConditions
      .filter((c) => !storedProfileFacts.some((f) => f.category === 'condition' && f.value.toLowerCase() === c.toLowerCase()))
      .map((c) => ({ value: c, category: 'condition' })),
  ];
  const riskFactorEvidence = await getRiskFactorEvidence(profileFacts, age);
  const evidenceForTriage = [...evidenceForApi, ...riskFactorEvidence];

  const triageResult = await getTriageForEvidence({ age, sex, evidence: evidenceForTriage });

  // FALLBACK WHEN INFERMEDICA ITSELF IS UNREACHABLE — see
  // infermedicaClient.js's getTriageForEvidence doc comment on
  // engineUnavailable. This app has no second clinically-validated
  // triage engine to fall back to, so the honest move on an outage is
  // to say so plainly and err toward caution, rather than let
  // generateRecommendation narrate a confident-sounding routine
  // recommendation built on evidence that was never actually assessed
  // (specialists would otherwise be an empty/fabricated placeholder,
  // not a real Infermedica answer). Short-circuits BEFORE generation —
  // nothing below this point should ever run on unassessed evidence.
  // Symptoms are deliberately NOT marked finalized here (unlike the
  // normal recommendation path) so the same accumulated evidence can be
  // re-attempted once the engine is reachable again, instead of being
  // silently closed out on a round that never actually got a real
  // answer.
  if (triageResult.engineUnavailable) {
    console.error('[processMessage] finalize: Infermedica /triage unavailable —', triageResult.error);
    // ESCALATED WORDING FOR A KNOWN RISK FACTOR: reuses profileFacts,
    // fetched above specifically so it's available here too. A patient
    // with a listed chronic condition or pregnancy has less margin for
    // a generic "see a doctor soon" — an outage means Infermedica can't
    // weigh that risk factor against the reported symptoms at all, so
    // this app should be MORE cautious here, not equally cautious. This
    // deliberately does NOT relabel the envelope urgency as 'emergency'
    // — that would claim certainty this app doesn't actually have (the
    // engine never ran); it strengthens the WORDING toward immediate
    // action instead, which is the honest amount of caution: "we don't
    // know, and given your history, don't wait to find out."
    const conditionsOnFile = (profileFacts || [])
      .filter((f) => f?.category === 'condition')
      .map((f) => f.value)
      .filter(Boolean);
    const hasRiskFactor = conditionsOnFile.length > 0;
    const cautionLine = hasRiskFactor
      ? `Since you have ${conditionsOnFile.join(', ')} on file, please don't wait this out — contact a doctor or ` +
        'urgent care now rather than waiting for this app to come back, since that combination needs a more ' +
        "careful read than I can safely give you without a working clinical engine."
      : "To be safe: if anything about how you're feeling is getting worse, or you're at all worried, please see " +
        'a doctor soon rather than waiting on me.';
    const reply = cleanReply(
      "I'm having trouble reaching the clinical engine that assesses urgency and specialist routing right now, " +
      `so I can't give you a confident recommendation for this. ${cautionLine} And if this ever feels like an ` +
      "emergency, don't wait for this app at all, go straight to emergency care." +
      (hasRiskFactor ? '' : ' Please try again in a little while, or describe your symptoms to a doctor directly in the meantime.')
    );
    return envelope({
      kind: 'error',
      sessionId,
      isDiagnosisRequest,
      urgency: 'urgent',
      subject: subjectInfo,
      resolvedAge: age,
      resolvedSex: sex,
      reply,
      source: 'infermedica_unavailable',
    });
  }

  const urgency = triageResult.urgency || 'routine';
  const labTests = triageResult.labTests || [];

  const specialists = enforcePediatricRouting(triageResult.specialists || ['General Physician'], age);

  if (urgency === 'emergency') {
    // FOUND LIVE: this was the one emergency path in the whole file that
    // never called markEmergencyAcknowledgeable — every other emergency
    // reply (keyword match, AI classifier, crisis) gets the "Notify
    // someone" / "Continue with this chat" buttons and the chat-stays-
    // locked-until-acknowledged behavior; this one, reached when
    // Infermedica's OWN /triage engine (not our classifiers) calls the
    // urgency level emergency, silently had neither — no way to resume
    // the conversation at all short of starting a new session.
    const triageLevel = triageResult.triageLevel;
    const templatedNote = TRIAGE_LEVEL_NOTES[triageLevel];
    const reply = templatedNote
      || `🚨 ${triageResult.triage?.triage_level_explanation || 'This may be a medical emergency. Please seek immediate medical attention.'}`;
    // BUG (found live): if the patient already continued past an
    // emergency in this conversation, offering Continue here again was an
    // endless loop — Continue replays the same confirmation, Infermedica
    // scores the same evidence as an emergency again, Continue again...
    // This IS the final answer for this round: give it without a Continue
    // lock and close the round, so the next message starts fresh.
    if (getEmergencyHistory(sessionId).length > 0) {
      markAllSymptomsFinalized(sessionId);
      resetClarificationCount(sessionId);
      return envelope({
        kind: 'emergency',
        sessionId,
        isEmergency: true,
        urgency: 'emergency',
        triageLevel,
        emergencyCategory: triageLevel === 'emergency_ambulance' ? 'ambulance' : 'emergency_room',
        resolvedAge: age,
        resolvedSex: sex,
        reply: cleanReply(`${reply} Based on everything you've described, please get emergency care now rather than booking a regular appointment.`),
        source: 'infermedica_triage',
      });
    }
    return envelope(markEmergencyAcknowledgeable({
      kind: 'emergency',
      sessionId,
      isEmergency: true,
      urgency: 'emergency',
      triageLevel,
      emergencyCategory: triageLevel === 'emergency_ambulance' ? 'ambulance' : 'emergency_room',
      resolvedAge: age,
      resolvedSex: sex,
      reply: cleanReply(reply),
      source: 'infermedica_triage',
    }, sessionId, message));
  }

  // ------------------------------------------------------------------
  // CROSS-DOMAIN CHECK — only meaningful on a SECOND-OR-LATER finalize
  // this session (priorRoundSymptoms non-empty): if what's NEW this
  // round points to a different specialist than what the PRIOR round
  // already settled on, flag it with a templated note rather than
  // silently folding it into one combined recommendation. This costs
  // two extra live /parse + /recommend_specialist calls, but only
  // here — once per round, never on an ordinary gathering turn.
  // ------------------------------------------------------------------
  let crossDomainNote = null;
  // Populated alongside crossDomainNote when a genuine cross-domain
  // finding fires — the actual specialist name, structured (not just
  // buried in reply prose), so the UI can render it as its own real
  // recommendation badge instead of the patient only seeing it as
  // freeform text.
  let secondarySpecialist = null;
  if (priorRoundSymptoms.length > 0 && newRoundSymptoms.length > 0) {
    try {
      const priorText = describeSymptomsForParse(priorRoundSymptoms);
      const newText = describeSymptomsForParse(newRoundSymptoms);

      const priorRaw = await parsePatientMessage(priorText, age);
      const { kept: priorKept } = filterGroundedMentions(priorRaw, priorText);
      const priorEvidenceForApi = priorKept.map((m) => ({ id: m.id, choice_id: m.choice_id || 'present', source: 'initial' }));
      const priorSpecialists = await getSpecialistNamesForEvidence({ age, sex, evidence: priorEvidenceForApi });

      const newRaw = await parsePatientMessage(newText, age);
      const { kept: newKept } = filterGroundedMentions(newRaw, newText);
      const newEvidenceForApi = newKept.map((m) => ({ id: m.id, choice_id: m.choice_id || 'present', source: 'initial' }));
      const newSpecialists = await getSpecialistNamesForEvidence({ age, sex, evidence: newEvidenceForApi });

      const distinct = pickDistinctSpecialist({ priorSpecialists, newSpecialists, mainSpecialists: specialists });
      if (distinct) {
        const newSymptomNames = newRoundSymptoms.filter((s) => s.present).map((s) => s.term);
        crossDomainNote = buildCrossDomainNote(newSymptomNames, distinct);
        secondarySpecialist = distinct;
      }
    } catch (err) {
      console.error('[processMessage] Cross-domain specialist check failed at finalize (non-fatal):', err.message);
    }
  }

  const realLabValues = await getRelevantLabValues(patientId, labTests);
  const patientHistory = await getPatientHistory(patientId, message);
  // profileFacts was already fetched earlier (before the /triage call,
  // for risk-factor evidence) — reused here for prose grounding as before.
  const unaccountedComplaints = getUnaccountedComplaints(sessionId);
  const conversationText = formatClassifiedSymptoms(accumulated);

  const generateInput = {
    matchedSymptoms,
    allowedSpecialists: specialists,
    labValues: realLabValues,
    patientHistory,
    profileFacts,
    conversationText,
    subjectInfo,
    unaccountedComplaints,
  };

  // FIXED (root cause, demonstrated live): matchedSymptomNames used to
  // be ONLY Infermedica's own canonical finding names (e.g.
  // "Paresthesia", "Decreased visual acuity"). Those ARE what
  // "reported_symptoms" is built from (see generaterecommendation.js),
  // so when the model reasonably translated them back into plain,
  // patient-facing language ("numbness and tingling", "blurred
  // vision") — the same everyday wording this app's own accumulated
  // symptom list already uses — the grounding verifier didn't
  // recognize that plain wording as anything on file and blocked the
  // whole recommendation as a hallucination, TWICE, forcing a fallback
  // to the generic safe-default text instead of the model's real
  // answer. Both the clinical name AND the plain patient-facing term
  // are equally legitimate, non-hallucinated ways to refer to the same
  // real, reported finding — so both are allowed here now, not just
  // whichever one happened to come from Infermedica.
  const groundedSymptomNames = [
    ...matchedSymptoms.map((s) => s.name),
    ...accumulated.filter((s) => s.present).map((s) => s.term),
  ];
  const reportedSeverities = accumulated
    .filter((s) => s.present && s.severity)
    .map((s) => s.severity);

  // FIXED (demonstrated live): this used to be just `message` — and the
  // turn that produces a recommendation is almost always a bare
  // confirmation ("that's all", "go ahead"), so the verifier's "the
  // patient said it themselves in this session" exception never saw
  // anything the patient had actually described. Their own recent
  // messages (never the bot's) plus this one.
  const patientStatedText = [
    ...getRecentMessages(sessionId).filter((m) => m.role === 'patient').map((m) => m.text),
    message,
  ].join('\n');

  let raw = await generateRecommendation(generateInput);
  let { ok, recommendation, violations } = await verifyRecommendation(raw, {
    allowedSpecialists: specialists,
    allowedLabTests: realLabValues.map((v) => v.testName),
    allowedProfileFacts: profileFacts,
    matchedSymptomNames: groundedSymptomNames,
    reportedSeverities,
    patientStatedText,
    graphUrgency: urgency,
    subjectInfo,
    checkKeywordMatch,
  });

  let retried = false;
  const actionable = violations.filter((v) => !v.repaired);
  if (!ok && actionable.length && raw?.source === 'generated') {
    console.warn('[verify] attempt 1 blocked:', JSON.stringify(actionable));
    const correctionNote = actionable.map((v) => `- ${v.code}: ${v.detail || ''}`).join('\n');
    const retryRaw = await generateRecommendation({ ...generateInput, correctionNote });
    const retryResult = await verifyRecommendation(retryRaw, {
      allowedSpecialists: specialists,
      allowedLabTests: realLabValues.map((v) => v.testName),
      allowedProfileFacts: profileFacts,
      matchedSymptomNames: groundedSymptomNames,
      reportedSeverities,
      patientStatedText,
      graphUrgency: urgency,
      subjectInfo,
      checkKeywordMatch,
    });
    retried = true;
    if (retryResult.ok) {
      raw = retryRaw;
      ({ ok, recommendation, violations } = retryResult);
      violations = [
        ...actionable.map((v) => ({ ...v, attempt: 1, corrected: true })),
        ...retryResult.violations.map((v) => ({ ...v, attempt: 2 })),
      ];
    } else {
      ({ ok, recommendation, violations } = retryResult);
      violations = [
        ...actionable.map((v) => ({ ...v, attempt: 1 })),
        ...retryResult.violations.map((v) => ({ ...v, attempt: 2 })),
      ];
    }
  }

  let reply = `${recommendation.rationale} ${recommendation.next_steps}`.trim();
  if (isDiagnosisRequest) reply += DECLINE_DIAGNOSIS_NOTE;
  if (newUnaccounted.length) {
    reply +=
      ` I should mention — I couldn't match the following to anything in my symptom database, ` +
      `so it wasn't factored into this recommendation: ${newUnaccounted.join(', ')}. ` +
      `Please mention ${newUnaccounted.length > 1 ? 'these' : 'this'} to the doctor directly.`;
  }
  if (crossDomainNote) reply += crossDomainNote;
  // The patient carried on past an emergency notice earlier in this
  // session — its symptoms are part of this assessment, and the warning
  // still stands.
  if (getEmergencyHistory(sessionId).length) reply += EARLIER_EMERGENCY_REMINDER;

  const triageLevelNote = TRIAGE_LEVEL_NOTES[triageResult.triageLevel];
  if (triageLevelNote) reply += ` ${triageLevelNote}`;
  reply += channelSentence(triageResult.recommendedChannel);

  // PREGNANCY SAFETY NET (MVP scope — explicit product decision): the
  // generation prompt (generaterecommendation.js, rule 10) already
  // instructs against a dismissive tone and asks for a "consult your
  // doctor" nudge whenever pregnancy is a profile fact, but that's a
  // probabilistic instruction, not a guarantee — this is the
  // deterministic backstop that makes the doctor-consult mention
  // unconditional, same "prompt instruction + cheap guaranteed check"
  // shape already used throughout this pipeline (symptomSanityGate.js,
  // the grounding verifier, etc.). Deliberately does NOT
  // attempt to deterministically detect/fix a dismissive TONE — that's
  // open-ended text-quality judgment, not something a keyword check can
  // reliably catch or repair; the prompt instruction is what carries
  // that half, this only guarantees the concrete, checkable half.
  const pregnancyOnFile = (profileFacts || []).some(
    (f) => f?.category === 'condition' && /pregnan|expecting/i.test(String(f.value || ''))
  );
  if (pregnancyOnFile && !/doctor (?:looking after|for) your pregnancy|pregnancy doctor/i.test(reply)) {
    reply += ' Since you\'re pregnant, please also let the doctor looking after your pregnancy know about this, rather than assuming it will settle on its own.';
  }

  // Mark this round's symptoms as finalized and reset the round
  // counter, so anything the patient adds AFTER this point gets its
  // own fresh gathering phase (and its own single Infermedica call
  // when THAT round finalizes) instead of hitting the round cap
  // immediately or being silently merged into this answer.
  markAllSymptomsFinalized(sessionId);
  resetClarificationCount(sessionId);

  return envelope({
    kind: 'recommendation',
    sessionId,
    isDiagnosisRequest,
    urgency: recommendation.urgency || urgency,
    matchedSymptoms,
    matchSource: 'infermedica',
    source: 'infermedica',
    subject: subjectInfo,
    recommendation,
    verification: { ok, violations, retried },
    unaccountedComplaints,
    crossDomainNote,
    secondarySpecialist,
    triageLevel: triageResult.triageLevel,
    recommendedChannel: triageResult.recommendedChannel,
    resolvedAge: age,
    resolvedSex: sex,
    reply: cleanReply(reply),
  });
}