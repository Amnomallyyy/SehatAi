// ============================================
// SehatAI: Off-Topic / Domain Classification Gate
// Uses AI classifier (Groq) as the primary detector,
// with deterministic regex as a fast pre-filter.
//
// UPDATED (2nd time): this used to be a binary off-topic gate, then a
// 3-way domain classifier (HEALTH_RELATED / DIET_RELATED / off-topic).
// It's now 5-way — HEALTH_RELATED, DIET_RELATED, HEALTH_EVENT (new),
// NOT_HEALTH_RELATED, or CONCERNING — and can optionally be told what
// question it's answering (the `context` param below). Both changes
// exist to close the same real gap: this classifier used to be
// skipped ENTIRELY on any turn that was answering one of this app's
// own pending questions (a targeted clarifying question, or the final
// "anything else?" gate), because a context-blind classifier has no
// way to tell a short legitimate answer ("severe", "no", "since
// yesterday") apart from genuinely off-topic chatter, and would
// misfire constantly if it ran blind on those turns. Rather than keep
// it skipped there (leaving those turns with no relevance/safety net
// at all — including the CONCERNING category, i.e. this was ALSO the
// path violent/hateful content detection ran through) or bolt on a
// separate bespoke "is this relevant" heuristic in processMessage.js,
// the fix is to give this SAME classifier the context it's missing —
// see classifyDomain's `context` param — so it can be trusted on
// every turn, not just the first one.
//
// HEALTH_EVENT is the other real gap this closes: a message like "I
// ate something I'm allergic to" or "I forgot to take my medication"
// is neither a symptom (HEALTH_RELATED) nor a diet question
// (DIET_RELATED) nor irrelevant (NOT_HEALTH_RELATED) — it's a
// clinically relevant EVENT that deserves its own specific follow-up
// ("are you having any reaction right now?"), not a generic "that
// doesn't seem related" or a silent drop. See
// clarificationCheck.js's classifyPendingAnswerRelevance, which is
// what actually acts on this category.
//
// checkOffTopic() is kept as a backward-compatible wrapper in case
// anything else still imports the old shape.
// ============================================

// ------------------------------------------------------------------
// FAST PRE-FILTERS (Regex - Free, No AI)
// ------------------------------------------------------------------

const GREETING_ONLY_PATTERNS = [
  /^(hi+|hello+|hey+|yo+|sup)[\s!.]*$/i,
  /^good (morning|afternoon|evening|night)[\s!.]*$/i,
];

const PROFANITY_TERMS = ["fuck", "shit", "bitch", "asshole", "bastard"];

// ------------------------------------------------------------------
// EXPORT: Fast pre-filter for greetings
// ------------------------------------------------------------------
export function checkObviouslyOffTopic(message) {
  const trimmed = message.trim();
  if (GREETING_ONLY_PATTERNS.some((pattern) => pattern.test(trimmed))) {
    return { offTopic: true, reason: "greeting" };
  }
  return { offTopic: false };
}

// ------------------------------------------------------------------
// EXPORT: Profanity check (regex, deterministic — no AI).
// Exported (was module-private) so processMessage.js can run it
// unconditionally, including on a pending-question answer.
// ------------------------------------------------------------------
export function containsProfanity(message) {
  const lower = message.toLowerCase();
  return PROFANITY_TERMS.some((term) => lower.includes(term));
}

// ------------------------------------------------------------------
// PRIVATE: AI domain classifier
// ------------------------------------------------------------------
async function aiAssistedDomainCheck(message, callAI, context, recentHistory) {
  const contextBlock = context
    ? `\nCONTEXT: This message is the patient's reply to the assistant's own prior question — "${context}" — not a stand-alone opening statement. A short reply that plausibly answers that question (a duration, a severity word, "yes"/"no", or an added detail) should be classified as HEALTH_RELATED even if it contains no explicit symptom word of its own — judge it by what it's responding to, not in isolation.\n`
    : '';
  // ADDED (found live): this classifier used to see ONLY the current
  // message plus a one-line description of the immediately-prior
  // question — no memory of the actual conversation. A reply like "no
  // just leave it, go ahead" was judged purely against the single
  // pending question, with no way to read it in light of what had
  // actually been discussed. RECENT CONVERSATION gives it that — a
  // short, bounded window (see chatLog.js's appendRecentMessage), never
  // permanently stored, used only to inform THIS judgment.
  const historyBlock = recentHistory && recentHistory.length
    ? `\nRECENT CONVERSATION (oldest first, for context only — judge the CURRENT message below, not these):\n${recentHistory.map((h) => `${h.role === 'patient' ? 'Patient' : 'Assistant'}: ${h.text}`).join('\n')}\n`
    : '';

  const systemPrompt = `Classify this message into exactly one category:

HEALTH_RELATED - describes a physical or mental health symptom or concern, OR is a plausible direct answer (duration, severity, yes/no, an added detail) to a follow-up question the assistant just asked about a symptom.
  Examples: "I have a headache", "my chest hurts", "I feel anxious", "my stomach hurts", "I have a cough", "I can't sleep", "I feel depressed", "3 days", "pretty severe", "yes", "no".

DIET_RELATED - asks about food, nutrition, meals, or diet — with NO physical symptom described.
  Examples: "what should I eat with diabetes", "is banana bad for my kidneys", "can I have coffee on my medication", "give me a meal plan", "what foods should I avoid", "is this food healthy for me".

HEALTH_EVENT - describes a health-relevant exposure, event, or action that is NOT itself a symptom, but that a doctor would want to know about. Only use this when NO symptom and NO diet question is ALSO present in the same message (if a symptom is present too, classify as HEALTH_RELATED instead — the symptom takes priority).
  Examples: "I ate something I'm allergic to", "I think I took the wrong medication", "I forgot to take my medication today", "I fell down the stairs", "I was in a car accident", "I got a vaccine yesterday", "I stopped my blood pressure medication last week".

NOT_HEALTH_RELATED - anything that is NOT a health or diet concern, and does not plausibly answer a pending follow-up question per the CONTEXT above.
  Examples: "1+1=2", "what's the weather", "tell me a joke", "hello", "how are you", "pizza recipe for a party", "what time is it", "random", "nonsense", "I love apples".

CONCERNING - threats of violence, hate speech, or abusive content.
  Examples: "I want to hurt someone", "kill them".

SKIP_AHEAD - ONLY valid when CONTEXT above shows this is answering the assistant's own follow-up question. The reply doesn't answer that specific question, but its MEANING is clearly "I don't want to answer more questions, just move on / give me the recommendation now" — judge this by what the patient actually means, not by matching fixed words. This is different from NOT_HEALTH_RELATED, which is for a reply that's simply unrelated chatter with no such intent behind it.
  Examples (when CONTEXT is present): "no just leave it, go ahead", "that's enough, move on", "I'm done answering questions", "just go with what I've told you", "can we skip this part".
${contextBlock}${historyBlock}
CRITICAL RULES:
- If the message describes BOTH a physical symptom AND a diet question or event (e.g. "I have a stomach ache, is it something I ate", "I'm having chest pain, what should I eat"), classify as HEALTH_RELATED — the symptom takes priority.
- If the message does not describe a health OR diet concern OR event, and does not plausibly answer the CONTEXT question (when given), and does not carry a "stop asking, move on" intent either, classify as NOT_HEALTH_RELATED.

Respond with ONLY one word: HEALTH_RELATED, DIET_RELATED, HEALTH_EVENT, NOT_HEALTH_RELATED, CONCERNING, or SKIP_AHEAD.`;

  try {
    const result = await callAI({ system: systemPrompt, message, temperature: 0 });
    return result.trim().toUpperCase();
  } catch (err) {
    console.error('[offtopiccheck] AI failed:', err.message);
    // Safe fallback: treat as health-related (better to ask than to block)
    return 'HEALTH_RELATED';
  }
}

// ------------------------------------------------------------------
// EXPORT: Domain classifier — SYMPTOM vs. DIET vs. EVENT vs. off-topic.
// Does NOT itself check profanity (see containsProfanity above) —
// callers run that deterministic check separately, unconditionally,
// before ever reaching this AI call.
//
// @param {string} message
// @param {Function} callAI
// @param {string|null} [context] - OPTIONAL plain-language description
//   of the pending question this message may be answering (this app's
//   own text, never Infermedica-sourced). Pass this whenever the
//   message is answering a targeted clarifying question or the final
//   "anything else?" gate, so a short legitimate answer isn't
//   misjudged as off-topic. Omit for a fresh, stand-alone message.
// @param {Array<{role:'patient'|'bot', text:string}>} [recentHistory] -
//   OPTIONAL short window of recent turns (see chatLog.js's
//   getRecentMessages) so this classifier can judge an ambiguous reply
//   in light of the actual conversation, not just the single pending
//   question. Omit for a fresh conversation with no prior turns.
// @returns {Promise<'HEALTH_RELATED'|'DIET_RELATED'|'HEALTH_EVENT'|'NOT_HEALTH_RELATED'|'CONCERNING'|'SKIP_AHEAD'>}
// ------------------------------------------------------------------
export async function classifyDomain(message, callAI, context = null, recentHistory = null) {
  if (typeof callAI !== 'function') {
    console.warn('[offtopiccheck] No AI available, treating as health-related');
    return 'HEALTH_RELATED';
  }
  return aiAssistedDomainCheck(message, callAI, context, recentHistory);
}

// ------------------------------------------------------------------
// EXPORT: Main off-topic check — kept for backward compatibility with
// the old binary shape ({offTopic, reason}). Treats HEALTH_EVENT as
// "not off-topic" (domain: 'HEALTH_EVENT') since a caller using this
// simpler wrapper has no way to act on the richer category anyway;
// processMessage.js uses classifyDomain directly so it CAN act on it
// (see clarificationCheck.js's classifyPendingAnswerRelevance).
// ------------------------------------------------------------------
export async function checkOffTopic(message, callAI, context = null) {
  // 1. FAST: Profanity check (no AI)
  if (containsProfanity(message)) {
    return { offTopic: true, reason: "profanity" };
  }

  // 2. AI: Classifier (primary detection)
  const classification = await classifyDomain(message, callAI, context);

  if (classification === 'CONCERNING') {
    return { offTopic: true, reason: "concerning_content" };
  }
  if (classification === 'NOT_HEALTH_RELATED') {
    return { offTopic: true, reason: "ai_classifier" };
  }
  // HEALTH_RELATED, DIET_RELATED, or HEALTH_EVENT → not off-topic
  return { offTopic: false, domain: classification };
}