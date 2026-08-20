// ============================================
// HealthMate AI: Safety Gate Module
// Runs FIRST on every incoming chatbot message, before intent routing.
// Deliberately simple and auditable — this is the highest-stakes code in the app.
// ============================================

// ------------------------------------------
// Layer 1: Deterministic keyword/alias match
// (fast, predictable, no AI — the first line of defense)
// ------------------------------------------

export const EMERGENCY_TERMS = [
  {
    canonical: "chest pain",
    aliases: ["chest tightness", "chest pressure", "heart hurts", "chest feels heavy", "elephant on my chest", "chest feels tight"],
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

/**
 * Fast, literal substring match against known emergency terms + aliases.
 * No AI involved — deterministic and instant.
 */
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

// ------------------------------------------
// Layer 2: AI fallback for phrasing the keyword list misses
// Only runs if Layer 1 found nothing. Always biased toward
// over-flagging — a false alarm costs a message, a missed
// emergency could cost a life.
// ------------------------------------------

/**
 * @param {string} message - the patient's raw message
 * @param {(args: {system: string, message: string, temperature: number}) => Promise<string>} callAI
 *        - inject your actual AI client call here (Anthropic/OpenAI SDK wrapper)
 */
export async function checkAISeverity(message, callAI) {
  const systemPrompt = `Classify this patient message as EMERGENCY or NOT_EMERGENCY.
Only return EMERGENCY if there are signs of a life-threatening situation:
severe chest pain, breathing difficulty, severe bleeding, loss of consciousness,
stroke signs, severe allergic reaction, or suicidal ideation.
When uncertain, err toward EMERGENCY.
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

// ------------------------------------------
// Combined entry point — call this one function from your router.
// ------------------------------------------

/**
 * Runs the full safety gate: keyword check first, AI fallback second.
 * This is the ONLY function the rest of the app should call.
 *
 * @param {string} message
 * @param {Function} callAI - your AI client wrapper (see checkAISeverity above)
 * @returns {Promise<{isEmergency: boolean, source?: string, matchedTerm?: string, category?: string}>}
 */
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

// ------------------------------------------
// Separate: misuse/diagnosis-request check
// Kept independent from emergency detection — a message can be
// BOTH alarming AND asking for a diagnosis, and each needs its
// own response (emergency escalation vs. "I can't diagnose").
// ------------------------------------------

const DIAGNOSIS_REQUEST_PATTERNS = [
  "what disease do i have",
  "what's wrong with me",
  "whats wrong with me",
  "do i have cancer",
  "diagnose me",
  "what condition is this",
  "tell me what disease",
];

export function checkMisuseRequest(message) {
  const lowerMsg = message.toLowerCase();
  const matched = DIAGNOSIS_REQUEST_PATTERNS.some((pattern) => lowerMsg.includes(pattern));
  return { isDiagnosisRequest: matched };
}