// ============================================
// HealthMate AI: Clarification Gate
// Decides whether to ask a follow-up question before generating a
// specialist recommendation.
//
// Both WHETHER to keep asking and WHAT to ask are a single AI judgment
// call, made together by assessIntake() (further down this file) — this
// replaced an earlier two-function split (a deterministic
// checkNeedsClarification + a separate generateClarifyingQuestion) that
// had a real, demonstrated bug: the deterministic gate only ever looked
// at the CURRENT message for duration/severity signals, ignoring that
// classifySymptoms already records duration/severity PER SYMPTOM on the
// accumulated list, so it could re-ask something already answered a
// turn earlier. Both of those functions have been removed — confirmed
// unused anywhere else in this codebase once assessIntake replaced
// them. getClarifyingQuestion() below is the one survivor: a fixed,
// non-AI fallback question used only when assessIntake fails outright
// (every provider down).
//
// Earlier still, before that split even existed, the multi-round
// interview in processMessage.js was driven by Infermedica's own
// /diagnosis `question` field directly — Infermedica's own generated
// question text (and the specific evidence item it chose to ask about)
// was relayed to the patient. Per a deliberate architecture decision,
// Infermedica is now used ONLY for its evidence vocabulary (/parse) and
// its actual triage/specialist output (/triage, /recommend_specialist,
// /diagnosis for lab-test suggestions only) — it no longer drives what
// question gets asked.
// ============================================

import { callAIStructured } from './callAi.js';
import { classifyDomain } from './offtopiccheck.js';
import { shareSynonymWord } from './symptomSynonyms.js';

const DURATION_PATTERNS = [
  /\b(day|days|week|weeks|hour|hours|month|months)\b/i,
  /\bsince\b/i,
  /\bago\b/i,
  /\b(this morning|last night|yesterday|today)\b/i,
  /\bstarted\b/i,
];

const SEVERITY_PATTERNS = [
  /\b(mild|moderate|severe|bad|terrible|unbearable|worst|awful|slight)\b/i,
  /\bout of (ten|10)\b/i,
  /\breally\b.*\b(bad|painful|hurts)\b/i,
];

const OVERRIDE_PHRASES = [
  "just tell me",
  "give me a recommendation",
  "skip the questions",
  "just recommend",
  "recommend now",
  "don't want to answer",
  "dont want to answer",
  "just give me",
  // ADDED (found live): this function existed but was never actually
  // called anywhere in the pipeline — confirmed dead code — and even
  // once wired back in, this list was too narrow to catch the phrasing
  // that actually surfaced the gap: "no just leave it, go ahead" kept
  // getting a targeted follow-up question re-asked instead of being
  // read as "stop asking, move on", because none of the phrases above
  // match ordinary "I'm done" language.
  "leave it",
  "go ahead",
  "that's enough",
  "thats enough",
  "just move on",
  "i'm done",
  "im done",
  "no more questions",
  "stop asking",
];

/**
 * True if the message contains a duration or severity signal — used by
 * processMessage.js to recognize clarification-answer messages ("severe",
 * "mild", "started 3 days ago") that legitimately don't match any known
 * symptom name and shouldn't be run through the off-topic check as if
 * they were unrelated chatter.
 *
 * @param {string} message
 * @returns {boolean}
 */
export function hasContextualDetail(message) {
  const hasDuration = DURATION_PATTERNS.some((pattern) => pattern.test(message));
  const hasSeverity = SEVERITY_PATTERNS.some((pattern) => pattern.test(message));
  return hasDuration || hasSeverity;
}

/**
 * @param {string} message - the patient's current raw message
 * @returns {boolean} - true if the patient explicitly asked to skip clarification
 */
export function isOverrideRequested(message) {
  const lower = message.toLowerCase();
  return OVERRIDE_PHRASES.some((phrase) => lower.includes(phrase));
}

// A bare negative like "no" or "that's all" is ONLY ever meaningful in
// this pipeline as an answer to our own final-confirmation gate ("is
// there anything else? If not, just say no") — nothing else in the flow
// asks a yes/no question about a specific symptom. Matched as the
// ENTIRE message (trimmed, optional trailing punctuation) so it never
// fires on something like "no, but my eye also hurts", which genuinely
// does have more to add.
const BARE_NEGATIVE_CONFIRMATION_RE =
  /^(?:no|nope|nah|na|none|nothing(?: (?:else|more))?|that'?s? all|thats all|that'?s? it|thats it|i'?m done|im done|all done|go ahead|nothing to add)[.!\s]*$/i;

/**
 * True if the message is a bare "nothing more to add" answer — see
 * BARE_NEGATIVE_CONFIRMATION_RE above. Used ONLY when answering the
 * final-confirmation gate (processMessage.js's wasAnsweringFinalConfirmation),
 * to short-circuit the Groq symptom classifier entirely for this turn —
 * a real bug this fixed: a plain "no" was sometimes read by the
 * classifier as DENYING every symptom already on file (flipping them to
 * present: false) rather than as "no new symptoms", because the message
 * has no symptom name in it for a denial to attach to, yet the model
 * still attached one. Deterministic here removes that ambiguity
 * entirely rather than relying on the LLM to get it right every time.
 *
 * @param {string} message
 * @returns {boolean}
 */
export function isBareNegativeConfirmation(message) {
  return BARE_NEGATIVE_CONFIRMATION_RE.test(String(message || '').trim());
}

/**
 * Fixed-template fallback question — kept as a safety net for when
 * assessIntake() (below) fails outright (e.g. every AI provider down).
 * Not AI-generated, not Infermedica-generated — entirely this app's
 * own static text, so it carries zero compliance ambiguity either way.
 *
 * @param {'duration'|'severity'|'both'} missing
 * @returns {string}
 */
export function getClarifyingQuestion(missing) {
  if (missing === "duration") {
    return "Thanks for sharing that. How long have you been experiencing this?";
  }
  if (missing === "severity") {
    return "Thanks for sharing that. On a scale of mild to severe, how would you describe it?";
  }
  return "Thanks for sharing that. How long have you been experiencing this, and how severe would you say it is — mild, moderate, or severe?";
}

// ============================================
// CONVERSATIONAL INTAKE ASSESSMENT
//
// SUPERSEDES the old checkNeedsClarification + generateClarifyingQuestion
// pair as the primary path for the targeted-question loop (both were
// removed — confirmed unused anywhere else in this codebase; only
// getClarifyingQuestion is still used, as the network-failure fallback,
// same role it always had).
//
// The problem this replaces: checkNeedsClarification decided WHETHER to
// ask again by regex-matching duration/severity words against ONLY the
// current raw message, and totally ignored the fact that classifySymptoms
// already stores duration/severity PER SYMPTOM on the accumulated list.
// Demonstrated live bug this caused: patient says "I have a really bad
// headache" (severity present, duration missing) -> asked "how long?" ->
// patient replies "since this morning" -> the NEXT check only looks at
// THAT message, sees duration but no severity IN IT, and asks "how
// severe is it?" again, even though "really bad" is sitting right there
// in the accumulated symptom's own severity field one turn back. A
// regex scoped to the latest message can't see that; a judgment call
// that's shown the actual accumulated state can.
//
// assessIntake replaces BOTH the whether-to-ask decision and the
// what-to-ask composition with a single Groq call that's shown the real
// accumulated per-symptom state (term/duration/severity — not just
// names), the question this app most recently asked (if any), and the
// patient's latest reply — then decides in one shot whether enough is
// known to recommend well, and if not, composes the next question with
// that full picture in view instead of re-deriving "what's missing" from
// scratch each turn. It's also freed from the old fixed menu (duration /
// severity / one associated symptom only) — a real intake conversation
// also asks about triggers, what relieves or worsens it, whether it's
// constant or comes and goes, impact on daily activity, etc., so this
// can reach for any of those instead of mechanically cycling through the
// same three slots every round.
//
// Deliberately still NOT fully free-form: per the explicit product
// decision behind this change, "sufficient" defaults toward asking one
// more question when genuinely unsure, mirroring the same
// err-toward-caution posture checkAISeverity uses for emergencies ("when
// uncertain, err toward EMERGENCY") — the goal is better, less
// repetitive questions, not fewer of them. The hard MAX_CLARIFICATION_
// ROUNDS ceiling (checked by the caller) still applies regardless of
// what this returns, and isOverrideRequested's deterministic "just give
// me a recommendation" short-circuit still runs before this is even
// called — see processMessage.js's call site.
// ============================================

const INTAKE_SCHEMA = {
  type: "object",
  properties: {
    sufficient: { type: "boolean" },
    question: { type: ["string", "null"] },
    // ADDED (found live, safety-relevant): a real, demonstrated case had
    // this generate "how long have you been experiencing the eye pain?"
    // when the patient had only ever reported nausea — a hallucinated
    // symptom name in the question itself. Unlike generaterecommendation.js's
    // output, nothing verified this free-form question text actually
    // referred to something real. referencedSymptom makes the model
    // explicitly declare which recorded_symptoms entry (if any) the
    // question is about, in its EXACT given wording — the caller then
    // checks this against the real list before trusting the question at
    // all (see the verification below), the same citation-checking
    // pattern generaterecommendation.js's referenced_profile_facts
    // already uses.
    referencedSymptom: { type: ["string", "null"] },
  },
  required: ["sufficient", "question", "referencedSymptom"],
};

/**
 * @param {{
 *   symptoms: Array<{term: string, duration: string|null, severity: string|null}>,
 *   lastQuestionAsked?: string|null,
 *   currentMessage: string,
 *   roundsRemaining?: number|null,
 *   maxRounds?: number|null,
 * }} params
 * @returns {Promise<{sufficient: boolean, question: string|null}>}
 *   `sufficient: true` means the caller should stop asking targeted
 *   questions and move on (same meaning as checkNeedsClarification
 *   returning needsClarification: false). `question` is only ever
 *   populated when sufficient is false. On any failure (network error,
 *   invalid JSON), returns { sufficient: false, question: null } — the
 *   caller is expected to fall back to a fixed template question
 *   (getClarifyingQuestion) in that case, same as generateClarifyingQuestion's
 *   failure mode always worked, so an API hiccup never silently skips
 *   past a question that should have been asked.
 */
export async function assessIntake({ symptoms = [], lastQuestionAsked = null, currentMessage = "", roundsRemaining = null, maxRounds = null }) {
  const fallback = { sufficient: false, question: null };
  if (!symptoms.length) return fallback;

  const budgetNote =
    roundsRemaining != null && maxRounds != null
      ? `\n\nBUDGET: you have ${roundsRemaining} follow-up question${roundsRemaining === 1 ? '' : 's'} left, INCLUDING the one you're about to ask (out of ${maxRounds} total) — after that, this app moves on to a recommendation with whatever is known. ${
          roundsRemaining <= 1
            ? 'This is your LAST question — if duration or severity is still unknown for the main symptom, ask about that (it matters most for triage) rather than a lower-priority detail, and combine more than one missing thing into this one question if you need to.'
            : 'If duration or severity is still unknown, that usually matters more for triage than a lower-priority detail like a trigger or associated symptom.'
        }`
      : '';

  const system = `You are a careful, non-diagnostic medical intake assistant conducting a short, natural conversational interview before this app recommends which kind of specialist the patient should see.

You are given: the symptom(s) recorded for this patient so far, each with any duration/severity ALREADY known; the question you (the assistant) most recently asked, if any; and the patient's latest reply.

STEP 1 — decide whether you already have enough to make a good specialist recommendation, or something important is still missing.
- Treat it as SUFFICIENT once the main present symptom(s) have a known duration AND severity, or the symptom picture is already informative enough on its own that more questions wouldn't meaningfully change the recommendation.
- When genuinely unsure whether you have enough, lean toward NOT sufficient (ask one more) rather than stopping early — a rushed recommendation is worse than one more short, well-chosen question.
- NEVER decide something is still missing if it's already recorded — check each symptom's duration/severity fields below before deciding what to ask.

STEP 2 — if NOT sufficient, compose exactly ONE short follow-up question.
- You are not limited to duration/severity — also consider an associated symptom the patient hasn't mentioned, what triggers or relieves it, whether it's constant or comes and goes, or how it's affecting daily activity — whatever a real intake conversation would naturally reach for next given what's already known.
- Briefly and naturally acknowledge what the patient just said before asking, the way a person would, but keep the whole reply to one or two short sentences — this is a quick check-in question, not an essay.
- Never repeat a question about something already recorded.

HARD RULES:
1. Never name, suggest, or hint at a medical condition, disease, or diagnosis.
2. Ask at most ONE question.
3. If sufficient is true, "question" must be null.
4. If your question asks about a SPECIFIC symptom, set referencedSymptom to that symptom's EXACT term as given in recorded_symptoms above — copy it verbatim, never a different wording or a symptom not in that list. If your question is more general (e.g. asking about an associated symptom not yet recorded, or something that doesn't name any one specific recorded symptom), set referencedSymptom to null. NEVER invent, rename, or ask about a symptom that isn't actually in recorded_symptoms — every symptom name your question mentions must come from that list, exactly as written there.
5. PLAIN LANGUAGE, NOT MEDICAL JARGON: when you propose a new associated symptom to ask about, name it the way a patient would recognize, not a clinical label — "shortness of breath" not "dyspnea", "painful urination" not "dysuria", "no periods"/"missed periods" not "amenorrhea", "blood in urine" not "hematuria", "dizziness" not "vertigo". A patient asked about a term they never used will reasonably think you're describing something they didn't say.${budgetNote}

Respond with the JSON shape you were given.`;

  const message = JSON.stringify({
    recorded_symptoms: symptoms.map((s) => ({
      term: s.term,
      duration: s.duration || null,
      severity: s.severity || null,
    })),
    assistant_last_question: lastQuestionAsked || null,
    patient_latest_reply: currentMessage,
  });

  try {
    const parsed = await callAIStructured({ system, message, schema: INTAKE_SCHEMA });
    const sufficient = parsed?.sufficient === true;
    let question = !sufficient ? (String(parsed?.question || "").trim() || null) : null;

    // VERIFICATION (found live, root-cause fix): a real, demonstrated
    // case had this generate "how long have you been experiencing the
    // eye pain?" for a patient who had only ever reported nausea —
    // referencedSymptom is required so the model has to name which
    // recorded symptom it means, but a required field only helps if it's
    // actually checked. If a symptom was declared and it doesn't match
    // (case-insensitively) anything in the real recorded_symptoms list,
    // the question is discarded entirely rather than shown to the
    // patient — the caller falls back to its own fixed template
    // question, same safe path already used for a network/parse
    // failure. Never trust a generated question just because it parsed
    // as valid JSON.
    if (question) {
      const referencedSymptom = parsed?.referencedSymptom ? String(parsed.referencedSymptom).trim() : null;
      if (referencedSymptom) {
        const knownTerms = new Set(symptoms.map((s) => s.term.toLowerCase().trim()));
        if (!knownTerms.has(referencedSymptom.toLowerCase().trim())) {
          console.error(
            `[clarificationCheck] assessIntake generated a question referencing "${referencedSymptom}", which is not in ` +
            `recorded_symptoms (${symptoms.map((s) => s.term).join(', ')}) — discarding, caller falls back to a fixed template question.`
          );
          question = null;
        }
      }
    }

    return { sufficient, question };
  } catch (err) {
    console.error("[clarificationCheck] assessIntake failed (non-fatal, caller falls back to a fixed template question):", err.message);
    return fallback;
  }
}

// ============================================
// FINAL-CONFIRMATION GATE RESOLUTION
//
// SUPERSEDES the isBareNegativeConfirmation regex approach above for
// answering the "is there anything else before I recommend a
// specialist?" gate. That regex fixed a real bug (a bare "no" being
// misread by the general-purpose classifySymptoms as denying every
// accumulated symptom) by refusing to let Groq see the message at all
// in that one case — safe, but blunt: it could only recognize a small
// fixed set of "no" phrasings, couldn't understand "remove the neck
// pain, keep the headache", and gave up on anything genuinely
// ambiguous rather than asking for clarification.
//
// The actual root cause wasn't "Groq can't be trusted with this
// message" — it's that classifySymptoms had NO IDEA it was resolving a
// confirm/add/remove decision against a specific known list; it was
// just doing its normal "extract any symptom mentioned" job on a
// message with nothing to extract, and over-applied the "denial" rule.
// Giving the model that context directly — the exact known list, and
// an explicit three-way decision to make — lets it handle "no", "yes",
// "remove the headache", "actually add nausea too", and a genuinely
// unclear reply all correctly, INCLUDING telling the caller when it
// isn't confident so the app can read the list back and ask again
// instead of guessing. isBareNegativeConfirmation is left in place
// above (unused here) in case a purely-deterministic fallback is ever
// wanted again.
// ============================================

// Same fuzzy-match fallback as chatLog.js's findLikelySameSymptom, for
// the same underlying bug: rule 3 above tells the model to copy the
// EXACT known_symptoms string into removals, but a paraphrase slipping
// through ("blood in urine" when the tracked term is "red-colored
// urine") used to fail the strict exact-match filter below and get
// silently dropped — no error, no fallback, the patient's removal
// request just vanished with no trace. Matches a known term sharing a
// real, non-generic word (4+ letters) with the model's returned term
// instead of requiring byte-for-byte equality.
const GENERIC_SYMPTOM_WORDS = new Set(['pain', 'ache', 'aches', 'aching', 'feeling', 'sensation', 'problem', 'issue']);

function findLikelyKnownTerm(rawTerm, knownSymptoms) {
  const key = String(rawTerm || '').toLowerCase().trim();
  if (!key) return null;
  const exact = knownSymptoms.find((k) => k.toLowerCase().trim() === key);
  if (exact) return exact;
  const words = key.split(/\s+/).filter((w) => w.length >= 4 && !GENERIC_SYMPTOM_WORDS.has(w));
  if (!words.length) return null;
  return (
    knownSymptoms.find((k) => {
      const kWords = k.toLowerCase().trim().split(/\s+/);
      // Exact shared word first, then the curated synonym backstop
      // (see symptomSynonyms.js) for a pair like "stomach"/"abdominal"
      // sharing no word at all.
      return words.some((w) => kWords.includes(w)) || shareSynonymWord(words, kWords);
    }) || null
  );
}

const FINAL_CONFIRMATION_SCHEMA = {
  type: "object",
  properties: {
    understood: { type: "boolean" },
    no_change: { type: "boolean" },
    additions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          term: { type: "string" },
          present: { type: "boolean" },
          duration: { type: ["string", "null"] },
          severity: { type: ["string", "null"] },
          // See symptomClassifier.js's identical fields for why these
          // exist — a null duration/severity here normally just means
          // "not restated this turn" and safely leaves the stored value
          // alone; these flags are the explicit signal for "the patient
          // is actively correcting a wrong stored value away," which
          // this app must not otherwise be able to tell apart.
          durationCorrected: { type: ["boolean", "null"] },
          severityCorrected: { type: ["boolean", "null"] },
        },
        required: ["term", "present"],
      },
    },
    removals: { type: "array", items: { type: "string" } },
    // SIBLING FIX (same class of bug as symptomClassifier.js's
    // mentionedConditions): the AI classification path there extracts
    // a chronic condition mentioned in chat ("I'm pregnant"), but this
    // resolver — the OTHER place a patient's free-text reply gets read
    // — had no equivalent field, so a condition mentioned only while
    // answering "anything else before I recommend?" (e.g. "no changes,
    // but I should mention I'm pregnant") was silently lost: never
    // added to profileFacts/risk-factor evidence, never acknowledged.
    mentionedConditions: { type: "array", items: { type: "string" } },
  },
  required: ["understood", "no_change", "additions", "removals", "mentionedConditions"],
};

/**
 * Resolves the patient's answer to the final "anything else before I
 * recommend a specialist?" gate, against the EXACT list of symptoms
 * already on file this session (never invented, never assumed).
 *
 * @param {{message: string, knownSymptoms: string[]}} params
 * @returns {Promise<{understood: boolean, no_change: boolean,
 *   additions: Array<{term:string, present:boolean, duration:string|null, severity:string|null}>,
 *   removals: string[], mentionedConditions: string[]}>}
 *   `removals` are copied verbatim from knownSymptoms — never a term the
 *   model invented. If `understood` is false, the caller should read
 *   the known list back to the patient and ask again rather than
 *   guessing what they meant. `mentionedConditions` is populated
 *   independently of `understood` — a chronic condition mention should
 *   still be recorded even on an otherwise-unclear reply.
 */
export async function resolveFinalConfirmation({ message, knownSymptoms = [] }) {
  const fallback = { understood: false, no_change: false, additions: [], removals: [], mentionedConditions: [] };
  if (!message || !message.trim()) return fallback;

  const system = `You are resolving ONE confirmation step in a symptom-intake conversation, right after the patient was asked: "Before I recommend a specialist — is there anything else you'd like to add? If not, just say no and I'll go ahead." You will be given the EXACT list of symptoms already recorded for this patient (known_symptoms) and the patient's reply. Decide what the reply means:

1. If the reply clearly means "no changes, go ahead" (e.g. "no", "nope", "that's all", "go ahead", "looks good", "correct"), return { understood: true, no_change: true, additions: [], removals: [] }. Read the WHOLE reply's meaning, not just its first word — "no" is only this case when the reply as a whole confirms nothing more to add. A reply like "no that's not all" or "no wait, I also have..." uses the word "no" but means the OPPOSITE (there IS more to add) — that is case 2 below (or case 5 if what they want to add isn't clear), never case 1. When a leading "no"/"nope" is immediately followed by a contradiction like "not all", "wait", "actually", or a new complaint, trust the contradiction, not the leading word. The SAME thing applies in reverse to an affirmative leading word: "yes, I have more to add", "yes actually one more thing", "yeah wait, also..." use "yes"/"yeah" but mean the OPPOSITE of "go ahead" — there IS more to add, so this is case 2 (or case 5 if what they want to add isn't named yet), never case 1. A bare "yes"/"yeah"/"sure" with NO qualifier after it is genuinely ambiguous here (unlike "no" alone, which this question's phrasing makes unambiguous) — the question asked was "is there anything else... if not, just say no", so a bare affirmative with nothing else stated doesn't actually confirm "go ahead" OR name what to add; treat a bare, qualifier-free "yes"/"yeah"/"sure" as case 5 (understood: false) rather than guessing it means "go ahead" just because it sounds affirmative.
2. If the reply mentions a symptom or complaint NOT already in known_symptoms, add it to "additions" using a SHORT GENERIC term (never quote the patient's own sentence, never include names/dates) — same rules as normal symptom extraction: present (false only if explicitly denied), duration, severity if stated. Use PLAIN LANGUAGE, not medical jargon — this term gets read back to the patient, so it must stay recognizable as what they described, not a clinical label they never used (e.g. "no periods"/"missed periods", never "amenorrhea"; "shortness of breath", never "dyspnea"; "blood in urine", never "hematuria").
3. If the reply says the patient no longer HAS a symptom that IS in known_symptoms — its presence, not just a detail about it — copy that EXACT string from known_symptoms into "removals". Do not paraphrase or invent a term not in the list.
3b. ATTRIBUTE CORRECTION, NOT A REMOVAL — FOUND LIVE, a real, demonstrated bug: "actually its not for 3 days, more like 5" (correcting a wrong duration this app itself recorded) got put into BOTH "additions" (with the corrected duration) AND "removals" (as if the symptom itself was being denied) — those two are contradictory, and applying both left a symptom the patient still clearly has marked as gone. "Was wrong about" is ambiguous between "wrong that I have it at all" (case 3, a real removal) and "wrong about a DETAIL — duration, severity" (this case, NOT a removal): if the correction is about a detail, put ONLY an entry in "additions" with that EXACT known_symptoms term, present: true, the corrected duration/severity, and durationCorrected/severityCorrected: true for whichever attribute is being corrected away (see the schema) — do NOT also add it to "removals". Only use "removals" (case 3) when the patient is denying having the symptom at all, never for a same-symptom detail correction.
4. A reply can both add and remove in the same message — but never the SAME symptom in both (see 3b): if a term appears in "additions", it must not also appear in "removals", and vice versa.
5. If you genuinely cannot tell what the patient means (off-topic, unrelated, too vague, contradicts itself), set understood: false — do not guess. Leave no_change: false and additions/removals as whatever you're confident about (often empty). mentionedConditions is independent of this — extract it whenever the reply names one, even if the rest of the reply is unclear.
6. NEVER name, suggest, or hint at a medical condition or diagnosis. This does NOT mean ignoring one the PATIENT brings up about themselves: if the reply mentions a chronic condition, diagnosis, or health-relevant status about themselves (e.g. "I'm pregnant", "I have diabetes", "I'm asthmatic") — SEPARATE from any symptom — list each as a short generic term in mentionedConditions, even alongside "no changes, go ahead" (case 1) or an addition/removal. This is a different, non-symptom field — do not also add it to additions/removals.

Return ONLY the JSON — no commentary.`;

  const userMessage = JSON.stringify({ known_symptoms: knownSymptoms, patient_reply: message });

  try {
    const parsed = await callAIStructured({ system, message: userMessage, schema: FINAL_CONFIRMATION_SCHEMA });
    const additions = Array.isArray(parsed?.additions)
      ? parsed.additions
          .filter((s) => s && typeof s.term === "string" && s.term.trim())
          .map((s) => ({
            term: s.term.trim(),
            present: s.present !== false,
            duration: s.duration && String(s.duration).trim() ? String(s.duration).trim() : null,
            severity: s.severity && String(s.severity).trim() ? String(s.severity).trim() : null,
            // ADDED (found live, alongside the removals de-dup fix
            // below): these flags were part of the schema but this
            // mapping silently dropped them, so even a correctly-
            // classified attribute correction never actually reached
            // chatLog.js's merge — the flags just vanished here.
            durationCorrected: s.durationCorrected === true,
            severityCorrected: s.severityCorrected === true,
          }))
      : [];
    // Removals are resolved against the known list (exact match first,
    // then the fuzzy shared-word fallback above for a paraphrase) rather
    // than a model-invented term straight through — see
    // findLikelyKnownTerm's doc comment for the real, demonstrated bug
    // this fixes (a paraphrased removal silently vanishing).
    const additionTermsLower = new Set(additions.map((a) => a.term.toLowerCase().trim()));
    const removals = Array.isArray(parsed?.removals)
      ? [...new Set(
          parsed.removals
            .filter((t) => typeof t === "string" && t.trim())
            .map((t) => findLikelyKnownTerm(t, knownSymptoms))
            .filter(Boolean)
            // DETERMINISTIC BACKSTOP (found live): a real, demonstrated
            // case had the model put the SAME term in both "additions"
            // (a corrected duration, present: true) and "removals" (a
            // full removal) for one pure attribute correction — despite
            // the prompt rule above now explicitly forbidding this.
            // Contradictory data always loses to the deterministic
            // check here rather than trusting the model got rule 4
            // right every time: if a term is also being added back with
            // present: true this same turn, it's not actually being
            // removed — drop it from removals.
            .filter((t) => !additionTermsLower.has(t.toLowerCase().trim()))
        )]
      : [];
    const mentionedConditions = Array.isArray(parsed?.mentionedConditions)
      ? parsed.mentionedConditions.filter((c) => typeof c === "string" && c.trim()).map((c) => c.trim())
      : [];
    return {
      understood: parsed?.understood === true,
      no_change: parsed?.no_change === true,
      additions,
      removals,
      mentionedConditions,
    };
  } catch (err) {
    console.error("[clarificationCheck] resolveFinalConfirmation failed (non-fatal, caller re-asks by reading the list back):", err.message);
    return fallback;
  }
}

// ============================================
// DISAMBIGUATION-ANSWER RESOLUTION
//
// Dedicated resolver for the ONE OTHER meta-question this app asks,
// besides the final-confirmation gate above: "does 'X' mean X days, or
// a severity of X out of 10?" (see symptomClassifier.js's ambiguous
// bare-number rule for why this question ever gets asked). Before this
// existed, a reply to it was run back through the general-purpose
// classifySymptoms, with the disambiguation question's own text passed
// in as context — the exact same architectural mismatch
// resolveFinalConfirmation above was built to fix for the OTHER
// meta-question: a classifier tuned to interpret real clinical
// questions, applying its own rules to the bot's artificial phrasing
// about its own uncertainty. A real, demonstrated case: "both"
// answering this exact question matched classifySymptoms' "affirmation
// to named candidates" rule (written for a real clinical question like
// "are you experiencing nausea or light sensitivity?"), fabricating two
// new symptoms out of the bot's own question text and producing a false
// emergency.
//
// This function's ONLY job is the narrow one this specific question
// needs: does the reply mean the ambiguous value is a DURATION, a
// SEVERITY, BOTH, or is it simply unclear. It has no access to (and no
// use for) the general symptom-extraction rules that caused the
// original misfire — there is no "extract a new symptom" path here at
// all.
// ============================================

const DISAMBIGUATION_SCHEMA = {
  type: "object",
  properties: {
    understood: { type: "boolean" },
    appliesTo: { type: ["string", "null"] }, // 'duration' | 'severity' | 'both' | null
  },
  required: ["understood", "appliesTo"],
};

/**
 * @param {{message: string, ambiguousValue: string, knownSymptoms: string[]}} params
 * @returns {Promise<{understood: boolean, appliesTo: 'duration'|'severity'|'both'|null}>}
 *   `appliesTo` is only meaningful when understood is true. If
 *   understood is false, the caller should re-ask the disambiguation
 *   question rather than guess — same "don't guess, ask again" posture
 *   as resolveFinalConfirmation's understood: false case.
 */
export async function resolveDisambiguationAnswer({ message, ambiguousValue, knownSymptoms = [] }) {
  const fallback = { understood: false, appliesTo: null };
  if (!message || !message.trim() || !ambiguousValue) return fallback;

  const system = `You are resolving ONE narrow disambiguation question in a symptom-intake conversation. The assistant just asked the patient: "Just to double check — does \"${ambiguousValue}\" mean ${ambiguousValue} days, or a severity of ${ambiguousValue} out of 10?" — a plain clarification about a NUMBER the patient already gave, for these already-recorded symptom(s): ${knownSymptoms.join(", ") || "their reported symptom(s)"}.

Decide what the patient's reply means, from these outcomes only:
- "duration": the reply says it's a number of days/time (e.g. "days", "the first one", "duration", "just days").
- "severity": the reply says it's a severity rating (e.g. "severity", "out of 10", "the second one", "pain level").
- "both": the reply clearly means BOTH at once — ${ambiguousValue} days AND a severity of ${ambiguousValue} (e.g. "both", "yes both", "it's both").
- null with understood: false: the reply does NOT actually answer this specific question — it's unrelated, states a totally different duration/severity value instead, denies having the symptom(s), or is otherwise unclear. Do not guess in this case.

Return ONLY the JSON — no commentary.`;

  try {
    const parsed = await callAIStructured({ system, message, schema: DISAMBIGUATION_SCHEMA });
    const appliesTo = ["duration", "severity", "both"].includes(parsed?.appliesTo) ? parsed.appliesTo : null;
    return {
      understood: parsed?.understood === true && appliesTo !== null,
      appliesTo,
    };
  } catch (err) {
    console.error("[clarificationCheck] resolveDisambiguationAnswer failed (non-fatal, caller re-asks the disambiguation question):", err.message);
    return fallback;
  }
}

// ============================================
// PENDING-ANSWER RELEVANCE GATE
//
// This is the piece that used to not exist at all: on a turn answering
// one of this app's own pending questions (a targeted clarifying
// question OR the final "anything else?" gate), the off-topic AI
// classifier and the CONCERNING-content check it also does were both
// skipped ENTIRELY — not because relevance/safety stopped mattering on
// those turns, but because both checks were context-blind and would
// misread a short legitimate answer ("severe", "no", "since
// yesterday") as off-topic chatter. Per the explicit decision to reuse
// the SAME classifier rather than build a new bespoke "is this
// relevant" heuristic, this function is a thin wrapper around
// offtopiccheck.js's classifyDomain — now context-aware — plus the
// one bit of NEW behavior a pending-question turn needs: recognizing a
// HEALTH_EVENT (an exposure/trigger that isn't itself a symptom, e.g.
// "I ate something I'm allergic to") and turning it into a specific
// follow-up question instead of either silently ignoring it or lumping
// it into a generic "that doesn't seem related" reply.
//
// @param {{message: string, callAI: Function, context: string|null}} params
// @returns {Promise<{
//   classification: 'CONCERNING'|'OFF_TOPIC'|'HEALTH_EVENT'|'ON_TOPIC',
//   followUpQuestion: string|null
// }>}
// ============================================
export async function classifyPendingAnswerRelevance({ message, callAI, context = null, recentHistory = null }) {
  const domain = await classifyDomain(message, callAI, context, recentHistory);

  if (domain === 'CONCERNING') {
    return { classification: 'CONCERNING', followUpQuestion: null };
  }
  // ADDED (found live): "no just leave it, go ahead" — a reply that
  // doesn't answer the specific targeted question, but clearly MEANS
  // "stop asking, move on" — used to have no way to be heard at all:
  // this classifier only ever said ON_TOPIC or OFF_TOPIC, and neither
  // is right for "I understand you'd like to skip that, but..." is
  // what OFF_TOPIC produces, re-asking the same question instead of
  // recognizing the patient's actual intent. isOverrideRequested (a
  // fixed phrase list, wired in separately) catches only the exact
  // phrasings on that list; this is the meaning-based version, judged
  // by the SAME classifier already running on every targeted-question
  // reply rather than a hardcoded list that can't generalize.
  if (domain === 'SKIP_AHEAD') {
    return { classification: 'SKIP_AHEAD', followUpQuestion: null };
  }
  if (domain === 'NOT_HEALTH_RELATED') {
    return { classification: 'OFF_TOPIC', followUpQuestion: null };
  }
  if (domain === 'HEALTH_EVENT') {
    const followUpQuestion = await generateEventFollowUp({ eventMessage: message });
    return { classification: 'HEALTH_EVENT', followUpQuestion };
  }
  // HEALTH_RELATED or DIET_RELATED
  return { classification: 'ON_TOPIC', followUpQuestion: null };
}

const EVENT_FOLLOWUP_SCHEMA = {
  type: 'object',
  properties: { question: { type: 'string' } },
  required: ['question'],
};

/**
 * Composes ONE short, specific follow-up question about a health-
 * relevant EVENT the patient just mentioned (an exposure, injury, or
 * medication lapse — not itself a symptom; see classifyDomain's
 * HEALTH_EVENT category). e.g. for "I ate something I'm allergic to",
 * a good follow-up is "Are you having any reaction right now — itching,
 * swelling, trouble breathing, anything like that?" — something a real
 * intake conversation would actually ask, instead of the generic
 * "anything else?" re-read this app would otherwise fall back to for
 * content it has no symptom to attach to.
 *
 * Falls back to a safe generic question on any failure.
 *
 * @param {{eventMessage: string}} params
 * @returns {Promise<string>}
 */
export async function generateEventFollowUp({ eventMessage }) {
  const fallback = "Thanks for letting me know — are you noticing any symptoms because of that right now?";
  if (!eventMessage || !eventMessage.trim()) return fallback;

  const system = `You are a careful, non-diagnostic medical intake assistant. The patient just described a health-relevant EVENT — an exposure, injury, medication lapse, or similar — that is not itself a symptom. Ask exactly ONE short, specific follow-up question to find out if it's causing them any symptoms right now, or any other detail a doctor would want to know (e.g. what they were exposed to, when it happened, whether they've had a reaction like this before).

HARD RULES:
1. Never name, suggest, or hint at a medical condition or diagnosis.
2. Ask exactly one question, one or two short sentences, warm and plain-spoken. Use plain language a patient would recognize, never a clinical term (e.g. "trouble breathing" not "dyspnea", "itching" not "pruritus") — this text is shown to the patient directly.
3. Do not repeat the event back verbatim as a diagnosis-sounding statement — just ask about its effects.`;

  try {
    const parsed = await callAIStructured({
      system,
      message: JSON.stringify({ event: eventMessage }),
      schema: EVENT_FOLLOWUP_SCHEMA,
    });
    const question = String(parsed?.question || '').trim();
    return question || fallback;
  } catch (err) {
    console.error('[clarificationCheck] generateEventFollowUp failed (non-fatal, using a generic fallback question):', err.message);
    return fallback;
  }
}