// ============================================
// HealthMate AI: Safety Gate Module
// ============================================

// ---- CRISIS PATTERNS (self-harm, harm to others) ----
export const CRISIS_PATTERNS = [
  /\b(?:i\s+)?(?:don'?t|do\s+not|dont)\s+want\s+to\s+(?:live|be\s+alive|exist)\b/i,
  /\bsuicid(?:e|al)\b/i,
  /\bno\s+(?:reason|point)\s+(?:to|in)\s+liv(?:e|ing)\b/i,
  /\bhurt(?:ing)?\s+myself\b/i,
  /\bself[-\s]harm\b/i,
  /\bhurt(?:ing)?\s+(?:someone|somebody|people|him|her|them|others?)\b/i,
  /\bkill(?:ing)?\s+(?:someone|somebody|him|her|them|people|others?)\b/i,
  /\bwant\s+to\s+(?:hurt|harm|kill)\b/i,
  /\bbetter\s+off\s+dead\b/i,
  /\bkhud\s*kushi\b/i,
  /\bmarna\s+chahta\b/i,
  /\bjeena\s+nahi\s+chahta\b/i,
];

// ---- DIAGNOSIS REQUEST PATTERNS ----
export const DIAGNOSIS_PATTERNS = [
  /\bdiagnos(?:e|is|ing)\s+(?:me|my|this|it)\b/i,
  /\bwhat(?:'?s|\s+is|\s+are)\s+wrong\s+with\s+(?:me|my)\b/i,
  /\bwrong\s+with\s+(?:me|my)\b/i,
  /\bwhat\s+(?:could|might|do\s+you\s+think)\s+(?:it|this)\s+(?:be|is)\b/i,
  /\b(?:do|can)\s+you\s+think\s+i\s+have\b/i,
  /\bwhat\s+(?:do|might|could)\s+i\s+have\b/i,
  // FOUND live: "what do you think i could have" — same "what ... i
  // have" shape as the pattern right above, but with "do you think"
  // inserted between "what" and "i", which the adjacent-word pattern
  // above doesn't span. This exact phrasing was demonstrated live
  // reaching this app riding along with a real clarifying-question
  // answer ("a couple of days. What do you think i could have").
  /\bwhat\s+do\s+you\s+think\s+i\s+(?:could|might|do)\s+have\b/i,
  // FOUND live: "what disease do I have" — same "what ... i have" shape
  // as the pattern above, but with a NOUN naming the very thing being
  // asked about ("disease"/"condition"/etc.) inserted between "what"
  // and "do", which neither adjacent-word pattern spans. Demonstrated
  // live riding along with real content ("fever for 2 days, what
  // disease do I have") — the diagnosis half went completely
  // unrecognized, silently skipping the decline note entirely instead
  // of just losing the fever/duration content (a separate bug fixed
  // alongside this one).
  /\bwhat\s+(?:disease|condition|illness|sickness|infection|problem)\s+(?:do|might|could)\s+i\s+have\b/i,
  /\btell\s+me\s+what\s+i\s+have\b/i,
  /\bwhat\s+is\s+(?:my\s+)?diagnosis\b/i,
  // FOUND live: "can I be diagnosed" — a PASSIVE construction, the
  // patient is now the grammatical subject instead of the object
  // ("diagnose ME/THIS/IT"), which pattern 1 above doesn't span at all.
  // Demonstrated live at the final-confirmation gate ("ok but can I be
  // diagnosed"), where it went completely unrecognized as a diagnosis
  // request.
  /\bcan\s+(?:i|this|it)\s+be\s+diagnosed\b/i,
  /\bmujhe\s+kya\s+(?:hua|hai)\b/i,
];

// ---- EMERGENCY TERMS (keyword + aliases) ----
export const EMERGENCY_TERMS = [
  {
    canonical: "chest pain",
    aliases: [
      "chest tightness", "chest pressure", "heart hurts", "chest feels heavy",
      "elephant on my chest", "chest feels tight", "pain in my chest", "pain in the chest",
    ],
    category: "cardiac",
  },
  {
    canonical: "difficulty breathing",
    aliases: ["can't breathe", "cant breathe", "cant catch my breath", "gasping for air", "suffocating", "short of breath"],
    category: "respiratory",
  },
  {
    canonical: "severe bleeding",
    aliases: ["won't stop bleeding", "wont stop bleeding", "bleeding a lot", "blood everywhere", "heavy bleeding"],
    category: "trauma",
  },
  {
    canonical: "loss of consciousness",
    aliases: ["passed out", "fainted", "blacked out", "unresponsive"],
    category: "neurological",
  },
  {
    canonical: "stroke signs",
    aliases: ["face drooping", "slurred speech", "can't move one side", "sudden confusion", "sudden numbness"],
    category: "neurological",
  },
  {
    canonical: "severe allergic reaction",
    aliases: ["throat closing", "swelling face", "anaphylaxis", "cant swallow"],
    category: "allergic",
  },
  {
    canonical: "suicidal ideation",
    aliases: ["want to die", "kill myself", "end my life", "dont want to live"],
    category: "mental_health",
  },
];

// ---- Layer 1: Deterministic keyword match ----
export function checkKeywordMatch(message) {
  const lowerMsg = message.toLowerCase();
  for (const term of EMERGENCY_TERMS) {
    const allPhrases = [term.canonical, ...term.aliases];
    for (const phrase of allPhrases) {
      if (lowerMsg.includes(phrase)) {
        return {
          isEmergency: true,
          matchedTerm: term.canonical,
          matchedPhrase: phrase,
          category: term.category,
          source: "keyword",
        };
      }
    }
  }
  return { isEmergency: false };
}

// ---- Layer 2: AI fallback ----
const SEVERITY_FEWSHOT_EXAMPLES = `
Examples:
Message: "chest pain that started suddenly and radiates to my arm, I feel dizzy"
Classification: EMERGENCY (cardiac warning signs — sudden onset, radiation, associated dizziness)

Message: "mild chest tightness after eating a big spicy meal, nothing else"
Classification: NOT_EMERGENCY (classic reflux/heartburn pattern, no other warning signs — but if this
message ALSO mentioned shortness of breath, sweating, arm/jaw pain, or the patient sounding unsure,
it would be EMERGENCY)

Message: "I've had a dull headache since this morning"
Classification: NOT_EMERGENCY (no stroke signs, no sudden severe onset)

Message: "worst headache of my life, came on suddenly, can't see straight"
Classification: EMERGENCY (thunderclap headache pattern — possible stroke/hemorrhage warning sign)

Message: "can't catch my breath after climbing stairs, happens sometimes"
Classification: EMERGENCY (breathing difficulty — even described as intermittent, err toward flagging)

Message: "I feel a bit anxious and my heart is beating fast before my exam"
Classification: NOT_EMERGENCY (situational anxiety with a clear non-cardiac trigger, no chest pain,
breathing difficulty, or other red flags)

Message: "I feel really depressed and also have a headache and eye pain"
Classification: NOT_EMERGENCY (ordinary mood language — "depressed", "sad", "stressed", "anxious" —
appearing ALONGSIDE a physical symptom is not itself a red flag, and neither symptom here has any actual
warning-sign descriptor: no sudden onset, no severe pain, no neurological signs. Judge the PHYSICAL
symptoms on their own clinical merits, exactly as if the mood language weren't there — an emotional
word sitting next to an unremarkable symptom does not make the symptom more dangerous. This is a
different case from a genuine self-harm/crisis statement, which a SEPARATE check handles — this
classifier's only job is physical warning signs.)
`;

/**
 * @param {string} message
 * @param {Function} callAI
 * @param {string|null} [context] - OPTIONAL. A short, plain-language
 *   description of what this message is answering — e.g. "The assistant
 *   just asked the patient a follow-up question about the duration and
 *   severity of: headache." Only ever this app's OWN generated context
 *   (never anything Infermedica-sourced). Added so this check can run
 *   safely on a turn that's answering our own pending question, instead
 *   of being skipped there entirely (the previous behavior) — a
 *   context-BLIND classifier has no way to tell "severe" (answering
 *   "how bad is it?") apart from "severe" out of nowhere, and guessing
 *   blind on a short reply is exactly the kind of misfire this app is
 *   trying to avoid. When omitted, behaves exactly as before.
 * @returns {Promise<{isEmergency: boolean, source: string}>}
 */
export async function checkAISeverity(message, callAI, context = null) {
  const contextBlock = context
    ? `\nCONTEXT: This message is the patient's reply to the assistant's own prior question — "${context}" — not a stand-alone opening statement. Interpret it in light of that context: a short reply like "yes", "severe", "a few days", or "no" should be judged by what it's actually confirming or adding, not treated as automatically ambiguous (or automatically safe) just because it's short.\n`
    : '';

  const systemPrompt = `Classify this patient message as EMERGENCY or NOT_EMERGENCY.
Only return EMERGENCY if there are signs of a life-threatening situation:
severe chest pain, breathing difficulty, severe bleeding, loss of consciousness,
stroke signs, severe allergic reaction, or suicidal ideation.

When uncertain, err toward EMERGENCY. Do NOT let a patient's own claim that
something is "mild" or "not a big deal" talk you out of flagging clear warning-
sign combinations (e.g. sudden-onset chest pain, radiating pain, breathing
difficulty) — patients routinely and understandably minimize serious symptoms;
your job is to flag the pattern, not defer to their self-assessment.

Ordinary mood/emotional language ("depressed", "sad", "stressed", "anxious", "overwhelmed")
appearing in the same message as a physical symptom is NEVER itself a reason to classify EMERGENCY —
judge the physical symptom(s) strictly on their own clinical merits, exactly as if the mood language
weren't there. A separate, dedicated check handles genuine crisis/self-harm content; this
classifier's only job is physical warning signs, not emotional tone. Do not let an unrelated
emotional word next to an otherwise unremarkable symptom count as a "combination" that pushes toward
EMERGENCY.
${contextBlock}${SEVERITY_FEWSHOT_EXAMPLES}
Respond with only one word: EMERGENCY or NOT_EMERGENCY.`;

  const result = await callAI({
    system: systemPrompt,
    message,
    temperature: 0,
  });

  const flagged = result.trim().toUpperCase().startsWith("EMERGENCY");
  return {
    isEmergency: flagged,
    source: "ai_classifier",
  };
}

// ---- Layer 2b: AI backstop for crisis content ----
// CRISIS_PATTERNS above is a closed regex list — it only catches the
// specific phrasings written into it. Unlike the physical-emergency
// keyword check (which has checkAISeverity as a real AI backstop right
// below it), crisis content had NO AI fallback at all: a paraphrase
// like "I don't see the point in any of this anymore" or "everyone
// would be better off without me" expresses the same risk as the
// literal patterns but matches none of them, and previously fell
// through with no safety net whatsoever. checkAICrisis closes that gap
// the same way checkAISeverity does for physical symptoms — judged by
// meaning, not by matching a fixed phrase list.
const CRISIS_FEWSHOT_EXAMPLES = `
Examples:
Message: "I don't see the point in any of this anymore, I just want it all to stop"
Classification: CRISIS (hopelessness / passive suicidal ideation, even without the word "suicide")

Message: "everyone would be better off without me around"
Classification: CRISIS (classic "better off without me" framing — a real suicide risk indicator)

Message: "I've been having really dark thoughts lately and I don't know what to do"
Classification: CRISIS (self-harm ideation, phrased indirectly)

Message: "I'm so angry I could just hurt him, I swear"
Classification: CRISIS (expressed intent/desire to harm another person)

Message: "I'm just really stressed about my exam tomorrow"
Classification: NOT_CRISIS (situational stress, no self-harm or violence content)

Message: "I feel like giving up on my diet plan, I keep failing"
Classification: NOT_CRISIS (frustration about a routine goal — do not over-flag ordinary language
that merely contains words like "giving up")

Message: "this headache is killing me"
Classification: NOT_CRISIS (a common figure of speech about a physical symptom, not an expression
of wanting to die)

Message: "I have chest pain"
Classification: NOT_CRISIS (a plain physical symptom report — a bodily complaint, however painful or
frightening, is NEVER by itself suicidal ideation, hopelessness, self-harm, or intent to harm someone
else. Do not read distress, urgency, or physical pain words as crisis content on their own — only
classify CRISIS when the message itself expresses a wish to die/not exist, self-harm, or harm to
others. This exact message has been misclassified as CRISIS before — it is unambiguously NOT_CRISIS.)

Message: "I have chest tightness, it really hurts"
Classification: NOT_CRISIS (same reasoning — a painful physical sensation described in plain terms,
with no self-harm or death-related content of any kind)

Message: "feeling low"
Classification: NOT_CRISIS (ordinary low mood — sadness, being down, having a rough day — is NOT
the same as hopelessness framed as wanting it all to end. Only escalate to CRISIS when the message
itself expresses a wish to die/not exist, self-harm, or harm to others — a mild mood statement with
no such content, even a somber one, should route to ordinary supportive/emotional handling instead,
not an emergency.)
`;

/**
 * @param {string} message
 * @param {Function} callAI
 * @param {string|null} [context] - same purpose as checkAISeverity's
 *   `context` param: what pending question (if any) this message is
 *   answering, so a short reply isn't judged in isolation.
 * @returns {Promise<boolean>}
 */
export async function checkAICrisis(message, callAI, context = null) {
  const contextBlock = context
    ? `\nCONTEXT: This message is the patient's reply to the assistant's own prior question — "${context}" — not a stand-alone opening statement. Judge it by what it's actually answering, not in isolation.\n`
    : '';

  const systemPrompt = `Classify this message as CRISIS or NOT_CRISIS.
Return CRISIS if it expresses, even indirectly or without an explicit keyword like "suicide" or
"self-harm", any of: suicidal ideation, a wish to die or not exist, hopelessness framed as wanting
it all to end, self-harm, or an intent or desire to hurt or kill another person. Judge by MEANING —
paraphrases and indirect language count exactly as much as explicit statements; do not require an
exact phrase or keyword match.
Do NOT flag ordinary frustration, situational stress, or common figures of speech that don't
actually express a wish for death or violence. This includes ordinary low/sad mood on its own
("feeling low", "having a rough day", "I'm sad") — sadness by itself is NOT hopelessness or
suicidal ideation; only classify CRISIS when the message itself contains an actual wish to die or
not exist, self-harm, or intent to harm someone else, not merely a negative emotion.
${contextBlock}${CRISIS_FEWSHOT_EXAMPLES}
Respond with only one word: CRISIS or NOT_CRISIS.`;

  // DELIBERATELY no try/catch here — this mirrors checkAISeverity above,
  // which also lets a callAI failure propagate rather than swallowing it.
  // An earlier version of this function caught the error and returned
  // `false` ("fail open"), which sounds harmless but is actually the
  // single worst failure mode this check could have: a real, indirectly-
  // phrased crisis message ("everyone would be better off without me")
  // arriving during a transient provider outage would have been silently
  // classified NOT_CRISIS and sailed through to ordinary symptom
  // gathering, with nothing in the reply or logs distinguishing that from
  // a genuine "checked, and it's fine." A thrown error at least surfaces
  // as a hard failure the caller/operator can see, instead of a false
  // negative dressed up as a normal answer — for a life-safety check,
  // failing loud beats failing safe-looking.
  const result = await callAI({ system: systemPrompt, message, temperature: 0 });
  return result.trim().toUpperCase().startsWith('CRISIS');
}

// ---- Emotional-support-only detection (regex pre-filter + AI backstop) ----
// Previously a closed regex list with NO AI backstop at all, and
// skipped entirely whenever the message was answering a pending
// question (processMessage.js's old `!wasAnsweringPendingQuestion`
// gate) — so a paraphrase the regex missed ("I just feel like nobody
// gets what I'm going through") was invisible everywhere, and even a
// literal keyword hit went uninspected mid-clarification. Same fix
// shape as checkAISeverity/checkAICrisis: keep the regex as a free,
// instant pre-filter, back it with an AI classifier that judges intent
// from context, and never skip based on conversation stage.
export const EMOTIONAL_KEYWORDS = [
  /\b(?:lonely|loneliness|alone|isolated|isolation)\b/i,
  /\b(?:sad|sadness|depression|depressed)\b/i,
  /\b(?:anxious|anxiety|nervous|worried|stress|stressed)\b/i,
  /\b(?:need.*support|emotional.*support|need.*help|need.*talk)\b/i,
  /\b(?:feel.*alone|feel.*isolated|feel.*sad)\b/i,
];

export function checkEmotionalKeywords(message) {
  return EMOTIONAL_KEYWORDS.some((re) => re.test(message));
}

const EMOTIONAL_FEWSHOT_EXAMPLES = `
Examples:
Message: "I feel like nobody understands me and I just want to be left alone"
Classification: EMOTIONAL_ONLY (isolation/distress, no physical symptom, no answer to a pending question)

Message: "I've been so overwhelmed lately, everything feels like too much"
Classification: EMOTIONAL_ONLY (emotional overwhelm, no physical complaint)

Message: "my chest has been tight and I've also been really anxious about it"
Classification: NOT_EMOTIONAL_ONLY (a physical symptom is present alongside the anxiety — route on
the physical symptom; the anxiety can be acknowledged but shouldn't replace triage)

Message: "3 days"
Classification: NOT_EMOTIONAL_ONLY (a plain answer to a pending follow-up question, not emotional content)

Message: "no, just really tired of dealing with this pain every day"
Classification: NOT_EMOTIONAL_ONLY (frustration about an ongoing physical symptom, not a standalone
emotional/mental-health concern)
`;

/**
 * @param {string} message
 * @param {Function} callAI
 * @param {string|null} [context] - what pending question (if any) this
 *   message is answering; same convention as checkAISeverity/checkAICrisis.
 * @returns {Promise<boolean>}
 */
export async function checkEmotionalConcern(message, callAI, context = null) {
  const contextBlock = context
    ? `\nCONTEXT: This message is the patient's reply to the assistant's own prior question — "${context}" — not a stand-alone opening statement. Judge it by what it's actually answering, not in isolation.\n`
    : '';

  const systemPrompt = `Classify this patient message as EMOTIONAL_ONLY or NOT_EMOTIONAL_ONLY.
Return EMOTIONAL_ONLY only if the message is PRIMARILY expressing an emotional or mental-health
concern — loneliness, sadness, anxiety, stress, feeling overwhelmed, needing someone to talk to —
with NO physical symptom described, and it is not simply a plain answer to a pending follow-up
question about a physical symptom (a duration, a severity word, yes/no, or an added detail).
If a physical symptom is ALSO present, or this is a plain answer to a pending question, classify as
NOT_EMOTIONAL_ONLY instead — a physical symptom should still be triaged even if the patient also
expresses distress about it.
${contextBlock}${EMOTIONAL_FEWSHOT_EXAMPLES}
Respond with only one word: EMOTIONAL_ONLY or NOT_EMOTIONAL_ONLY.`;

  try {
    const result = await callAI({ system: systemPrompt, message, temperature: 0 });
    return result.trim().toUpperCase().startsWith('EMOTIONAL_ONLY');
  } catch (err) {
    console.error('[safetyCheck] AI emotional-concern check failed:', err.message);
    // Fail open to the regular symptom pipeline rather than block.
    return false;
  }
}

// ---- Conversational emotional check-in (ask, then listen, then adapt) ----
// EXPLICIT PRODUCT DECISION: a first standalone emotional message ("I am
// feeling really low") used to get an immediate, fixed mental-health-
// referral reply — the same block every time, no matter what the patient
// says next. That's the same "jump straight to the canned answer instead
// of asking" shape of bug already fixed once for physical symptoms (see
// assessIntake in clarificationCheck.js, and the bare-number ambiguity fix
// in symptomClassifier.js) — here applied to emotional content instead.
// The fix is a one-round conversational check-in: ask a warm, natural
// follow-up ("how long has this been going on, and is anything physical
// bothering you too?") instead of reciting the referral immediately, then
// read the patient's actual answer with interpretEmotionalFollowUpAnswer
// below to decide what happens next — hand off to the physical-symptom
// pipeline if one was mentioned, give the referral only once it's actually
// still warranted, or simply acknowledge a retraction ("I'm not really
// feeling that low actually") instead of repeating the referral or
// ignoring the correction outright.

/**
 * Composes ONE short, warm, natural follow-up question in response to a
 * first standalone emotional statement — never the referral itself. Kept
 * general-purpose (not from a fixed template) so it reads like a real
 * reply to what the patient actually said, not a form question.
 *
 * @param {string} message - the patient's original emotional statement
 * @param {Function} callAI
 * @returns {Promise<string>}
 */
export async function composeEmotionalFollowUp(message, callAI) {
  const FALLBACK =
    "I'm sorry to hear that — how long have you been feeling this way, and is anything physical bothering you too?";
  const systemPrompt = `The patient just shared something emotional (feeling low, anxious, overwhelmed,
lonely, etc.) with no physical symptom mentioned. Write ONE short, warm, natural sentence that:
- briefly acknowledges what they said (not clinical, not a form letter),
- then asks ONE gentle follow-up question that invites them to say more AND checks whether
  anything physical is going on too (e.g. "how long has this been going on, and is anything
  physical bothering you as well?").
Do NOT recommend a therapist/counselor yet, do NOT diagnose, do NOT mention emergency services.
This is a check-in question, not the final answer. Keep it to 1-2 sentences, plain text, no lists.`;

  try {
    const result = await callAI({ system: systemPrompt, message, temperature: 0.4 });
    const text = (result || '').trim();
    return text || FALLBACK;
  } catch (err) {
    console.error('[safetyCheck] composeEmotionalFollowUp failed, using fallback:', err.message);
    return FALLBACK;
  }
}

/**
 * Interprets the patient's answer to our own emotional check-in question
 * (composeEmotionalFollowUp above). Distinct from checkEmotionalConcern's
 * plain binary classification because this needs to tell apart THREE
 * outcomes at once: a physical symptom was mentioned (hand off to the
 * normal pipeline), the patient is walking back the earlier statement
 * (retraction — acknowledge, don't repeat the referral), or they're
 * confirming/restating the distress (the referral is actually warranted
 * now, having asked first instead of assuming).
 *
 * @param {string} message - the patient's reply to our follow-up
 * @param {Function} callAIStructured
 * @param {string} priorQuestion - the exact follow-up text we asked (context)
 * @returns {Promise<{hasPhysicalSymptom:boolean, retracted:boolean, stillStruggling:boolean}>}
 */
export async function interpretEmotionalFollowUpAnswer(message, callAIStructured, priorQuestion) {
  const systemPrompt = `The assistant just asked the patient this emotional check-in question:
"${priorQuestion}"
Classify the patient's reply below into three independent yes/no judgments:
- hasPhysicalSymptom: true if the reply mentions ANY physical/bodily symptom anywhere
  (pain, blood, fever, nausea, a body part hurting, etc.), even alongside emotional content.
- retracted: true if the patient is walking back, softening, or correcting their earlier
  emotional statement — e.g. "I'm not really feeling that low actually", "it's not that bad",
  "I'm okay now", "I overreacted". This is about them saying it's LESS true than before.
- stillStruggling: true if they confirm, restate, or continue describing ongoing emotional
  distress (sadness, low mood, anxiety, loneliness, overwhelm) with no retraction.
retracted and stillStruggling are mutually exclusive — never both true. If the reply is a
plain answer with neither signal (e.g. just states a duration), leave both false.`;

  try {
    const result = await callAIStructured({
      system: systemPrompt,
      message,
      schema: {
        hasPhysicalSymptom: 'boolean',
        retracted: 'boolean',
        stillStruggling: 'boolean',
      },
      temperature: 0,
    });
    return {
      hasPhysicalSymptom: Boolean(result?.hasPhysicalSymptom),
      retracted: Boolean(result?.retracted),
      stillStruggling: Boolean(result?.stillStruggling),
    };
  } catch (err) {
    console.error('[safetyCheck] interpretEmotionalFollowUpAnswer failed, defaulting to still-struggling:', err.message);
    // Fail toward giving the supportive referral rather than silently
    // dropping a genuine emotional concern.
    return { hasPhysicalSymptom: false, retracted: false, stillStruggling: true };
  }
}

// ---- Immediately dangerous events -> emergency, full stop ----
// Explicit product decision, started from allergen exposure and then
// broadened: a patient describing an event that is INHERENTLY
// emergency-level on its own — regardless of whether they've stated
// any current symptom yet — is escalated straight to emergency
// guidance, no follow-up question asked first. This is deliberately
// different from every other HEALTH_EVENT (a missed medication dose, a
// vaccine, general food poisoning with no known allergy), which still
// gets a follow-up question before anything escalates: a live run
// showed the follow-up-first flow letting a real allergen-exposure
// case ("I ate something I'm allergic to" -> "are you having a
// reaction?" -> "yess") slip all the way through to an ordinary
// clarification round instead of emergency guidance, because
// checkAISeverity judges the raw MESSAGE text turn by turn and neither
// half of that exchange reads as urgent in isolation. The same
// reasoning obviously extends past allergens — "I think I've been
// stabbed" describes an event that is a medical emergency by its
// nature, and waiting to ask "are you bleeding?" and getting an answer
// back costs real time exactly like waiting on the allergic-reaction
// follow-up did. So this covers penetrating/severe trauma (stabbing,
// gunshot, being struck by a vehicle), and other categories of event
// that are inherently dangerous the moment they're described
// (drowning, choking, severe burns, electrocution, poisoning/overdose)
// alongside allergen exposure, all judged by MEANING via few-shot
// examples — never a fixed word list — since the whole point is
// catching however the patient actually phrases it, not a specific
// vocabulary.
//
// Returns a CATEGORY (or null) rather than a bare boolean so the reply
// can give guidance appropriate to what actually happened, reusing the
// same EMERGENCY_GUIDANCE categories checkKeywordMatch already uses:
// 'allergic', 'trauma' (bleeding/penetrating injury guidance fits
// stabbing/gunshot/being struck), 'respiratory' (choking/drowning are
// fundamentally airway emergencies, and so is a patient stating right
// now that they can't breathe / can't catch their breath — that's a
// present-tense symptom, not a past event, but it's exactly as
// inherently dangerous and shouldn't wait on a follow-up question
// either), 'panic_attack' (a patient-reported panic attack — panic
// attack symptoms can be indistinguishable from a cardiac or
// respiratory emergency without being examined, so this escalates
// rather than assuming it's "just" anxiety), or 'default' (poisoning/
// overdose/electrocution/severe burns — general "this may be a medical
// emergency, call now" guidance rather than a specific first-aid step
// this app has no business prescribing).
const DANGEROUS_EVENT_FEWSHOT_EXAMPLES = `
Examples:
Message: "I ate something I'm allergic to"
Classification: ALLERGEN_EXPOSURE (stated ingestion of a self-identified allergen)

Message: "I think I just took a pill I'm allergic to by accident"
Classification: ALLERGEN_EXPOSURE (stated exposure to a self-identified allergen — medication this time, same risk)

Message: "a bee stung me and I'm allergic to bee stings"
Classification: ALLERGEN_EXPOSURE (stated exposure to a self-identified allergen — an insect sting, same category)

Message: "I have a peanut allergy"
Classification: NOT_DANGEROUS_EVENT (stating an allergy exists, historically/generally — no exposure just happened)

Message: "I think I've been stabbed"
Classification: TRAUMA_EVENT (penetrating injury — inherently a medical emergency regardless of stated symptoms)

Message: "I got shot"
Classification: TRAUMA_EVENT (gunshot wound — same category)

Message: "a car just hit me"
Classification: TRAUMA_EVENT (being struck by a vehicle — severe trauma risk regardless of how the patient currently feels)

Message: "I fell down the stairs"
Classification: NOT_DANGEROUS_EVENT (a fall alone isn't inherently an emergency the way a stabbing/gunshot is — ordinary HEALTH_EVENT handling, i.e. a follow-up question, is appropriate here)

Message: "I think I swallowed something poisonous" / "I may have overdosed on my medication"
Classification: OTHER_DANGEROUS_EVENT (poisoning/overdose — inherently dangerous, general emergency guidance rather than a specific unproven first-aid step)

Message: "I was choking on food a minute ago" / "someone just pulled me out of the pool, I almost drowned"
Classification: RESPIRATORY_EVENT (airway/breathing emergency by nature)

Message: "I can't breathe" / "I can't catch my breath" / "I'm struggling to breathe right now"
Classification: RESPIRATORY_EVENT (a stated present-tense inability or severe difficulty breathing is an airway/breathing emergency by nature, exactly like choking — this is not "an ordinary symptom description" even though no event just happened)

Message: "I'm a little short of breath after climbing the stairs"
Classification: NOT_DANGEROUS_EVENT (mild, exertion-related breathlessness with an obvious benign cause — not a stated inability to breathe)

Message: "I think I'm having a panic attack" / "I'm having a panic attack right now, my chest feels tight and I can't breathe"
Classification: PANIC_ATTACK_EVENT (a self-reported panic attack — its symptoms can be indistinguishable from a cardiac or respiratory emergency without being examined)

Message: "I've been feeling anxious about work lately"
Classification: NOT_DANGEROUS_EVENT (general anxiety, not a stated panic attack happening now)

Message: "I forgot to take my blood pressure medication today"
Classification: NOT_DANGEROUS_EVENT (a missed dose is a different, non-emergency kind of health event)

Message: "I got a vaccine yesterday"
Classification: NOT_DANGEROUS_EVENT (routine event, not inherently dangerous)
`;

/**
 * @param {string} message
 * @param {Function} callAI
 * @returns {Promise<'allergic'|'trauma'|'respiratory'|'panic_attack'|'default'|null>}
 *   The emergency-guidance category to use, or null if this message
 *   doesn't describe an inherently dangerous event.
 */
export async function checkDangerousEvent(message, callAI) {
  const systemPrompt = `Classify this message into exactly one category:

ALLERGEN_EXPOSURE - the patient describes having JUST eaten, taken, touched, been stung/bitten by,
  or otherwise been exposed to something THEY identify (even loosely — "something I'm allergic to"
  counts, it doesn't need to name the specific allergen) as an allergen.

TRAUMA_EVENT - the patient describes a penetrating or severe physical injury event that just
  happened — being stabbed, shot, struck by a vehicle, or a comparably severe impact/injury —
  regardless of whether they've said anything about current symptoms or bleeding yet.

RESPIRATORY_EVENT - the patient describes a choking or near-drowning event that just happened, OR
  states RIGHT NOW that they can't breathe / can't catch their breath / are struggling to breathe —
  an airway/breathing emergency by its nature either way. Ordinary mild breathlessness with an
  obvious benign cause (e.g. after exertion) does NOT count.

PANIC_ATTACK_EVENT - the patient states they are having, or think they are having, a panic attack
  right now. Treat this as an emergency even if no other severe symptom is stated — panic attack
  symptoms can be hard to distinguish from a cardiac or respiratory emergency without being
  examined, so this should not wait on a follow-up question. General/background anxiety with no
  stated panic attack happening now does NOT count.

OTHER_DANGEROUS_EVENT - the patient describes some other event that is inherently dangerous the
  moment it's described, even with no symptom stated yet: poisoning, a drug/medication overdose,
  electrocution, a severe burn.

NOT_DANGEROUS_EVENT - anything else, including: stating an allergy exists in general/historical
  terms with no fresh exposure just described, a plain fall with no stated severe injury, a missed
  medication dose, a routine vaccine, food poisoning with no stated allergy, mild exertion-related
  breathlessness, general/background anxiety, or any other ordinary symptom description — none of
  these are inherently emergencies on their own the way the categories above are, and are better
  handled by this app's normal follow-up-question flow instead of skipping straight to emergency
  guidance.

Judge by MEANING, not by matching specific words — the event being described is what matters, not
the vocabulary used to describe it.
${DANGEROUS_EVENT_FEWSHOT_EXAMPLES}
Respond with only one word: ALLERGEN_EXPOSURE, TRAUMA_EVENT, RESPIRATORY_EVENT, PANIC_ATTACK_EVENT, OTHER_DANGEROUS_EVENT, or NOT_DANGEROUS_EVENT.`;

  const result = (await callAI({ system: systemPrompt, message, temperature: 0 })).trim().toUpperCase();
  if (result.startsWith('ALLERGEN_EXPOSURE')) return 'allergic';
  if (result.startsWith('TRAUMA_EVENT')) return 'trauma';
  if (result.startsWith('RESPIRATORY_EVENT')) return 'respiratory';
  if (result.startsWith('PANIC_ATTACK_EVENT')) return 'panic_attack';
  if (result.startsWith('OTHER_DANGEROUS_EVENT')) return 'default';
  return null;
}

// ---- Combined safety check ----
export async function runSafetyCheck(message, callAI) {
  const keywordResult = checkKeywordMatch(message);
  if (keywordResult.isEmergency) {
    return keywordResult;
  }
  const aiResult = await checkAISeverity(message, callAI);
  if (aiResult.isEmergency) {
    return aiResult;
  }
  return { isEmergency: false };
}

// ---- Category-specific guidance ----
const EMERGENCY_GUIDANCE = {
  cardiac: {
    headline: "This could be a cardiac emergency.",
    action: "Call your local emergency number now, or have someone take you to the nearest emergency room. Do not drive yourself.",
  },
  respiratory: {
    headline: "This could be a breathing emergency.",
    action: "Call your local emergency number now. If you have a rescue inhaler and were prescribed one, use it while help is on the way.",
  },
  trauma: {
    headline: "This could be a bleeding emergency.",
    action: "Apply firm, direct pressure to the area and call your local emergency number now.",
  },
  neurological: {
    headline: "This could be a stroke or a serious neurological emergency.",
    action: "Call your local emergency number now. Note the time symptoms started — it matters for treatment.",
  },
  allergic: {
    headline: "This could be a severe allergic reaction.",
    action: "If you have an epinephrine auto-injector, use it now and call your local emergency number immediately.",
  },
  panic_attack: {
    headline: "This could be a panic attack — but it could also be a cardiac or breathing emergency, and it's hard to tell apart without being checked.",
    action: "Try to sit or lie down and slow your breathing if you can. If your symptoms don't ease within a few minutes, or you have chest pain, get worse, or you're unsure, call your local emergency number now.",
  },
  mental_health: {
    headline: "It sounds like you're going through something very difficult right now.",
    action: "Please reach out to a crisis line or emergency services right now — you don't have to go through this alone.",
  },
  default: {
    headline: "This may be a medical emergency.",
    action: "Please call your local emergency number or go to the nearest emergency room now.",
  },
};

export function getEmergencyGuidance(category) {
  return EMERGENCY_GUIDANCE[category] || EMERGENCY_GUIDANCE.default;
}

// ---- Separate: misuse/diagnosis-request check ----
const DIAGNOSIS_REQUEST_PATTERNS = [
  /what'?s?\s*(is\s*)?wrong with me/i,
  /what do i have\b/i,
  /what (could|might) (i have|this be)\b/i,
  /what condition (do i have|is this)\b/i,
  /what'?s? (causing|the diagnosis)/i,
  /am i sick with\b/i,
  /diagnose me\b/i,
  /\bdo (i|you think i) have\b.*\?/i,
  /\bcould (this|it) be\b/i,
  /tell me what disease/i,
  /what is (this|it)\b/i,
];

async function aiAssistedMisuseCheck(message, callAI, context = null) {
  // `context` lets this run safely even when the message is answering
  // this app's own pending question — previously this whole AI fallback
  // was skipped on those turns (processMessage.js's old
  // `!wasAnsweringPendingQuestion` gate), so a diagnosis request phrased
  // as a follow-up answer ("so is it something serious then?") had no
  // check running on it at all. Same context convention as
  // checkAISeverity/checkAICrisis/checkEmotionalConcern.
  const contextBlock = context
    ? `\nCONTEXT: This message is the patient's reply to the assistant's own prior question — "${context}" — not a stand-alone opening statement. A short reply that plausibly just answers that question (a duration, a severity word, "yes"/"no", a symptom detail) is NOT a diagnosis request even if brief — judge it by what it's actually responding to.\n`
    : '';

  const systemPrompt = `Is the patient asking you to diagnose them or tell them what
condition/disease they have — rather than just describing symptoms or answering a
follow-up question about their symptoms?
This includes indirect phrasings like "do you think I have X", "could this be X",
"is it possible I have X", not just "what's wrong with me".
${contextBlock}
Respond with only one word: YES or NO.`;

  const result = await callAI({ system: systemPrompt, message, temperature: 0 });
  return result.trim().toUpperCase().startsWith("YES");
}

export async function checkMisuseRequest(message, callAI, context = null) {
  // The regex fast-path below is context-BLIND — several of its patterns
  // are broad, everyday phrasings ("could this be...", "what is this")
  // that a legitimate answer to a pending follow-up question can easily
  // contain (e.g. "could it be from the spicy food I ate yesterday?"
  // answering a duration/cause question). Trusting it on a pending-
  // question turn would silently reintroduce the exact bug this
  // `context` param exists to fix, just one layer further down than
  // processMessage.js's own DIAGNOSIS_PATTERNS check. So: only take the
  // context-blind fast path on a FRESH message (context == null); once
  // there's a pending question to weigh the reply against, skip straight
  // to the context-aware AI classifier instead of trusting a keyword hit
  // blind.
  if (!context) {
    const regexMatch = DIAGNOSIS_REQUEST_PATTERNS.some((pattern) => pattern.test(message));
    if (regexMatch) {
      return { isDiagnosisRequest: true, source: "keyword" };
    }
  }
  const aiMatch = await aiAssistedMisuseCheck(message, callAI, context);
  return { isDiagnosisRequest: aiMatch, source: "ai_classifier" };
}