// ============================================
// SehatAI: Symptom Text Normalizer
//
// Purpose: this app used to store the PATIENT's own literal sentences
// across turns (chatLog.js's messageHistory/unaccountedComplaints) so
// evidence and context could be re-derived turn-to-turn. Storing a
// patient's exact wording indefinitely (even just for the life of the
// running process) is more than this app needs to keep — the same
// continuity works just as well from a short, generic clinical label
// ("chest pain") as it does from the patient's exact sentence ("my
// chest has been absolutely killing me since last night"). This file
// is that normalization step: a Groq call (NOT Infermedica — this has
// nothing to do with Infermedica's vocabulary or its API Agreement)
// that reduces free text down to short, generic terms before anything
// gets held onto for the rest of the session.
//
// This is a deliberate LOSSY step: duration/severity are kept as short
// generic tags, but incidental narrative detail, and the patient's own
// phrasing, are not retained anywhere past the single request that
// produced them.
// ============================================

import { callAIStructured } from './callAi.js';

const SYMPTOM_SCHEMA = {
  type: 'object',
  properties: {
    symptoms: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          term: { type: 'string' },
          present: { type: 'boolean' },
          duration: { type: ['string', 'null'] },
          severity: { type: ['string', 'null'] },
          // ADDED (found live): appendAccumulatedSymptoms (chatLog.js)
          // deliberately never lets a null duration/severity overwrite
          // an existing value — a null here normally just means "this
          // turn didn't mention it," not "erase what's already known."
          // That's the wrong behavior for the ATTRIBUTE CORRECTION case
          // below, where the patient is explicitly saying a stored
          // duration/severity is WRONG (not just leaving it unstated) —
          // without an explicit signal, that correction silently gets
          // ignored and the wrong value stays. Set durationCorrected/
          // severityCorrected: true (alongside duration/severity: null)
          // ONLY when this turn is actively correcting away an existing
          // value, never for an ordinary turn that simply doesn't
          // mention duration/severity at all.
          // MANDATORY on every symptom entry (true or false, never
          // omitted) — see the CORRECTION vs GUESS rules further down
          // for exactly when each is true. Made required, not optional,
          // because leaving these as optional booleans meant the model
          // reliably skipped setting them even when clearly true —
          // confirmed live, 0/3 across repeated identical test calls —
          // silently defaulting to "not corrected/not guessed" every
          // time, which is the wrong default for the one case (a
          // blanket compound-answer guess) these fields exist to catch.
          durationCorrected: { type: 'boolean' },
          severityCorrected: { type: 'boolean' },
          durationGuessed: { type: 'boolean' },
          severityGuessed: { type: 'boolean' },
        },
        required: ['term', 'present', 'durationCorrected', 'severityCorrected', 'durationGuessed', 'severityGuessed'],
      },
    },
    // ADDED: for the one case this model should NOT guess its way through
    // — a bare, unitless number answering a question that asked for BOTH
    // duration AND severity together (e.g. question_asked = "how long
    // have you had this, and how severe is it on a scale of 1 to 10?",
    // reply = "7"). There is no way to tell from "7" alone whether that's
    // 7 days or 7/10 severity, and guessing wrong silently corrupts
    // clinical data. Rather than force a guess, the model reports the
    // ambiguity here and symptoms stays whatever it can determine
    // WITHOUT that one number (empty, if the number was the only content)
    // — processMessage.js reads this and asks the patient to disambiguate
    // instead of recording a wrong duration or severity.
    ambiguous: { type: ['boolean', 'null'] },
    ambiguousValue: { type: ['string', 'null'] },
    // ADDED: chronic conditions/risk factors mentioned in chat ("I have
    // diabetes", "I'm pregnant") used to be completely invisible to this
    // app unless already stored in the patient's profile — a patient
    // volunteering real, relevant context got silently ignored. This is
    // a SEPARATE extraction target from symptoms on purpose — a
    // condition is not a symptom, and should never be merged into
    // "symptoms" or treated as a bodily complaint. Deliberately just a
    // short list of terms, not full clinical judgment — the actual
    // recognition/verification happens downstream via Infermedica's own
    // /parse (same "second opinion" pattern used everywhere else in this
    // app), so a slightly imperfect extraction here is low-risk: nothing
    // gets trusted as a real risk factor unless Infermedica's own engine
    // independently confirms it.
    mentionedConditions: { type: 'array', items: { type: 'string' } },
  },
  required: ['symptoms'],
};

const SYMPTOM_SYSTEM = `You are a clinical text normalizer. Given a patient's free-text message, extract each symptom or physical complaint mentioned and reduce it to a SHORT, GENERIC term — e.g. "chest pain", "shortness of breath", "headache", "nausea". NEVER quote or closely paraphrase the patient's own sentence. NEVER include names, locations, exact dates, or any other identifying or narrative detail.

PLAIN LANGUAGE, NOT MEDICAL JARGON: this term gets read back to the patient later ("So far I have: X") — it must stay recognizable as what they actually described, not a clinical label they never used. Prefer the everyday equivalent whenever one exists and is just as short/generic, even if it's less "official"-sounding: "no periods" or "missed periods" (NOT "amenorrhea"), "shortness of breath" (NOT "dyspnea"), "painful urination" (NOT "dysuria"), "blood in urine" (NOT "hematuria"), "itching" (NOT "pruritus"), "dizziness" (NOT "vertigo"), "fainting" (NOT "syncope"), "nosebleed" (NOT "epistaxis"), "ringing in ears" (NOT "tinnitus"), "light sensitivity" (NOT "photophobia"). This is about the WORD CHOICE only, not about being vague — still be specific and generic the same way "chest pain" or "nausea" already are; just say it the way the patient would recognize, not the way a chart would.

Extract EVERY distinct physical/bodily complaint the patient names, even a vague, non-specific, or
single-word one whose exact nature or location isn't yet clear (e.g. "irritation", "discomfort",
"itching", "burning", "swelling") — do not silently drop a plausible complaint just because it's
underspecified. This app's own follow-up questions are what narrow a vague complaint down; that is
not this extraction step's job, so do not pre-filter for specificity here. A real, demonstrated case:
"nausea, irritation" extracted only "nausea", silently losing "irritation" even though it's a
perfectly plausible bodily complaint on its own — that's the failure mode this instruction exists to
prevent. Only leave a term out entirely if it's one of the specific excluded categories below (mood/
emotional, self-applied diagnosis label) or genuinely isn't a bodily complaint at all (e.g. it's part
of a greeting, a question back to you, or off-topic content).

NEVER extract an emotional, mood, or mental-health term as a symptom — this includes things like
"depressed mood", "low mood", "anxiety", "stress", "feeling down/low", "no motivation" when it is
describing mood rather than a physical symptom (physical fatigue described as bodily tiredness —
e.g. "I'm physically exhausted", "low energy", "no energy", "I feel drained" — is fine to extract as
"fatigue"; low/no ENERGY on its own is a common, legitimate physical-fatigue complaint and should be
extracted as "fatigue" even without an extra word like "physically" — it is MOTIVATION, not energy,
that leans mood-only). This app routes emotional content through a
completely separate, dedicated flow (see safetyCheck.js's checkEmotionalConcern /
composeEmotionalFollowUp) — it must never be recorded here or handed to the medical triage engine
downstream, which is tuned for physical evidence only and can score mental-health-flavored evidence
as an emergency in a way that has nothing to do with the patient's actual physical symptoms. If the
ONLY thing in the message is mood/emotional content with no physical complaint at all, return an
empty symptoms array — do not invent a "depressed mood"-style entry, and do not attach mood language
as the duration/severity detail of an unrelated already_known_symptoms entry either.

NEVER extract a self-applied DIAGNOSIS LABEL (e.g. "migraine", "sinusitis", "bronchitis",
"pneumonia", "flu", "UTI", "appendicitis", "arthritis") as its OWN separate symptom entry when it is
describing the SAME underlying complaint as a plain physical symptom already in the message — e.g.
"headache and slight migraine" is ONE complaint (a headache), not two; do not create both a
"headache" entry and a separate "migraine" entry for it. A patient labeling their own symptom with a
diagnosis-sounding name is still just describing what they're feeling, not adding a second, distinct
finding — extract only the plain physical term ("headache"), never the diagnosis label, and never
count it as additional evidence. This matters because each separate tracked symptom independently
feeds this app's emergency-severity check — inflating one real complaint into two or three distinct-
looking entries makes an ordinary presentation look more alarming than it is. If a diagnosis label
appears with NO plain physical symptom alongside it at all (rare), extract the plain physical
complaint it most obviously implies (e.g. "migraine" alone -> "headache") rather than the label itself.

For each symptom also report:
- durationCorrected, severityCorrected, durationGuessed, severityGuessed: EVERY symptom entry MUST include all four as explicit true/false — never omit them. Default false for all four; the rules below (ATTRIBUTE CORRECTION and the COMPOUND ANSWER case further down) say exactly when one should be true instead.
- present: false ONLY if the patient explicitly denied a SPECIFICALLY NAMED symptom (e.g. "no chest pain", "I don't have a fever", "the headache is gone now"). A bare, contentless "no"/"nope"/"not really" with no symptom named in it is NEVER a denial of anything in already_known_symptoms — it names nothing to deny, so treat it as containing no symptom information at all (return an empty array; do not mark any already_known_symptoms entry as present: false).
- CRITICAL for a denial that refers to something in already_known_symptoms, even if the patient phrases it differently than how it's listed there (e.g. already_known_symptoms has "red-colored urine" and the patient says "I don't really have blood in urine" — same thing, different words; or already_known_symptoms has "shortness of breath" and the patient says "I can breathe fine now"): set the term field to the EXACT text of the matching already_known_symptoms entry, not a new paraphrase of the patient's own wording. This app tracks each symptom by matching term strings, so a denial recorded under different wording than the original entry silently fails to cancel it — always reuse the already_known_symptoms entry's own exact text when a denial is clearly about it.
- ATTRIBUTE CORRECTION, NOT A DENIAL: "not" (or "isn't"/"wasn't") right before a DURATION, SEVERITY, or other descriptive qualifier — never right before the symptom noun itself — negates only that qualifier, not the symptom's presence. E.g. "my eye pain is not for 6 days" (correcting a WRONG duration this app itself applied — the patient still has eye pain, they're only saying the 6-day figure is wrong), "the headache isn't that bad", "it wasn't constant, more on and off" are all attribute corrections: present stays true (or unchanged if already true in already_known_symptoms), never present: false for these. For the specific attribute being corrected (duration and/or severity), set it to null AND set the matching durationCorrected/severityCorrected flag to true (a correction that doesn't also state what the RIGHT value is means that detail is now genuinely unknown again, not that the symptom disappeared — the flag is what tells this app to actually clear the old wrong value instead of quietly keeping it). Contrast with an actual denial, where "not"/"don't" sits directly on the symptom itself: "I don't have eye pain (anymore)", "no eye pain", "eye pain is gone" — THOSE are present: false, and durationCorrected/severityCorrected don't apply. When genuinely unsure which one a message means, prefer the attribute-correction reading over presence denial — wrongly discarding a symptom the patient still has is a worse failure than leaving one attribute blank for one more turn.
- PRONOUN DENIAL: a denial can also refer to a symptom with a bare pronoun instead of naming it — "actually I don't have that anymore", "it's gone now", "that's better now". Judge by MEANING whether this is really a denial (walking back a specific already-reported symptom) versus something else that merely contains the same words — "I don't have that severe of a headache" is NOT a denial, "that" there modifies severity, not presence; "not that bad" similarly isn't a denial. When it genuinely IS a pronoun denial: if already_known_symptoms has EXACTLY ONE entry, resolve the pronoun to that entry's exact term and mark it present: false, same as any other denial. If already_known_symptoms has MORE than one entry, a bare pronoun denial is genuinely ambiguous about which one it means — do NOT guess which one; return an empty symptoms array instead (the app will ask a follow-up naturally). If already_known_symptoms is empty, there is nothing for the pronoun to refer to — also return an empty array.
- duration: a short GENERIC duration if the patient stated one (e.g. "3 days", "2 weeks", "since this morning") — null if not stated. Never include an exact calendar date.
- severity: a short generic severity word if stated (e.g. "mild", "moderate", "severe") — null if not stated.

You will also be given already_known_symptoms — symptom terms already recorded earlier in this same conversation — and, when available, question_asked — the EXACT question this app just asked the patient. USE question_asked to correctly attribute the reply instead of guessing from already_known_symptoms alone. In particular:
- If question_asked was about something OTHER than an already_known_symptoms entry (e.g. it asked about a possible associated symptom like nausea or light sensitivity, not about the headache itself), a "no" in the reply answers THAT question — it is NOT a denial of anything in already_known_symptoms, even though the reply appears right after a known symptom was mentioned earlier in the conversation. Only mark an already_known_symptoms entry present: false if the patient's words are actually about that entry.
- If the reply both answers question_asked negatively AND mentions a new symptom (e.g. question_asked = "any nausea or light sensitivity?", reply = "no but I have eye pain"), extract ONLY the new symptom ("eye pain") — the "no" itself carries no symptom information to record, since what it's declining isn't a tracked symptom.
- If question_asked was itself about a specific already_known_symptoms entry (e.g. "how long have you had the headache?", "how severe is the eye pain?"), then a bare duration/severity/no-change answer DOES apply to that entry — same as the already_known_symptoms rule below.
- If question_asked itself NAMES one or more CANDIDATE symptoms that are NOT already in already_known_symptoms (e.g. "Are you experiencing any symptoms right now, such as trouble breathing, swelling, or a rash?") and the reply is a plain AFFIRMATION — the patient confirming that yes, they are experiencing (one or more of) what was asked about, without specifying which one or adding further detail. Judge this by MEANING, not by matching against a fixed list of words — "yes", "yeah", a typo/elongation of either, "I do", "I do have that", "definitely", "for sure", or any other clearly affirmative reply with no further specifics all count the same way. Extract ALL of the candidate symptoms named in question_asked, each present: true, with no duration/severity (the patient only confirmed having them, not any detail about them). A vague affirmation to a multi-option question is safer recorded as every possibility it named than silently dropped entirely. If the reply instead specifies WHICH ONE applies (e.g. "yes, trouble breathing"), extract only that one, not all of them. This is a genuine confirmation of new symptom(s), not a bare duration/severity phrase — it does not need already_known_symptoms to be empty or non-empty either way.
- If question_asked is not given (this is not answering a targeted follow-up), fall back to judging the message on its own.

- If question_asked explicitly asks for BOTH a duration AND a severity rating in the same question (e.g. "how long have you had this, and how would you rate the severity on a scale of 1 to 10?"), the reply needs to be read carefully, in this priority order:
  1. NAMED, PER-SYMPTOM ANSWER: the reply names two or more symptoms and gives each its OWN value (e.g. "headache for two days and eye pain for three", "headache 2 days, eye pain 3 days"). Match each value to the symptom it's actually attached to — do NOT apply one symptom's number to another, and do NOT apply either number to every already_known_symptoms entry just because more than one exists. Each named symptom gets only the value stated right next to it.
  2. COMPOUND ANSWER (both duration AND severity actually stated): the reply gives both pieces of information, even briefly (e.g. "7 days and severity is 7", "2 weeks, moderate", "3 days and it's a 6/10"). Extract BOTH: duration from the duration-shaped part, severity from the severity-shaped part (a number out of 10, or a word like mild/moderate/severe). If no symptom is named, apply both values to every already_known_symptoms entry the question was about (same reasoning as the already_known_symptoms rule below) — this is different from case 3 below because here the patient actually supplied both values, so there's nothing ambiguous to preserve.

MANDATORY WHENEVER CASE 2 (COMPOUND ANSWER) APPLIES TO MORE THAN ONE already_known_symptoms ENTRY AT ONCE, WITH NO SYMPTOM NAMED: on EVERY entry you apply it to, you MUST set durationGuessed: true AND severityGuessed: true. This is not optional. The patient gave real numbers, but never said which symptom(s) they actually describe — treating that as confirmed for every symptom is a guess, and this app needs to know it's a guess so it keeps asking rather than silently trusting it. Get this wrong and a patient's real, still-undetermined symptom detail gets treated as settled.
  3. AMBIGUOUS BARE NUMBER: the ENTIRE reply is just one bare NUMBER (digits, or a spelled-out
  number word like "seven") with no unit and no second value (e.g. "7", "seven" — not "7 days",
  not "7 and 8", not "7/10"). A lone number like this could mean either a day count or a point on
  the 1-10 severity scale, and guessing wrong records the wrong thing. Do NOT guess. For this case
  only: leave that number out of any symptom's duration/severity entirely (other genuinely stated
  information, if any, can still be extracted normally), and instead set the top-level
  ambiguous: true and ambiguousValue: the exact number/word as given (e.g. "7"), so the app can ask
  the patient which one they meant. This case is ONLY for an actual number — a denial like "no
  pain", "none", "not really", "no" is NEVER ambiguous under this rule (it has no number in it at
  all); handle it with the present:false denial rule above instead, and never set ambiguous: true
  for it.
  If question_asked asked about ONLY duration OR ONLY severity (not both), a bare number is never ambiguous — attribute it to whichever one was actually asked.

Separately from question_asked, already_known_symptoms is also used for this case: if the patient's message states ONLY a duration, severity, or timing detail (e.g. "3 days", "moderate", "it started yesterday") and does NOT name any new symptom or complaint, and question_asked (when given) doesn't indicate otherwise:
- If already_known_symptoms has exactly one entry, return that exact term (present: true) with the stated duration/severity filled in — do NOT return an empty array just because the patient didn't repeat the symptom's name.
- If already_known_symptoms has more than one entry, do the same for every one of them — applying the stated duration/severity to all of them is a safer default than silently dropping the information.
- If already_known_symptoms is empty, a bare duration/severity phrase has nothing to attach to — return an empty array as usual.

If the message contains no symptom or complaint information at all and does not fall into the cases above (e.g. it is just "yes", "no", a greeting, or something off-topic), return an empty symptoms array. Only set ambiguous: true in the specific bare-number case described above — otherwise omit it or set it false/null.

SEPARATE FROM SYMPTOMS — mentionedConditions: if the patient mentions a chronic condition, diagnosis, or health-relevant status about THEMSELVES (e.g. "I have diabetes", "I'm pregnant", "I have hypertension", "I'm asthmatic"), list each one as a short generic term in mentionedConditions, e.g. ["diabetes"], ["pregnant"]. This is completely independent of the symptoms array — NEVER put a condition into symptoms (a condition is not a bodily complaint), and NEVER put a symptom into mentionedConditions. If nothing conditionlike is mentioned, return an empty array. Do NOT include something ABOUT someone else (e.g. "my mom has diabetes") — only the account holder's own stated conditions. Do NOT include a self-applied diagnosis label for the CURRENT complaint (e.g. "I think this is a migraine" describing today's headache) — that is already handled by the symptoms rules above; mentionedConditions is for pre-existing, ongoing conditions, not a guess about the current complaint.

Return ONLY the JSON — no commentary.`;

/**
 * @param {string} message - the patient's raw message for THIS turn
 * @param {string[]} knownSymptomTerms - symptom terms already accumulated
 *   this session (present ones), so a bare "3 days" / "moderate" answer to
 *   our own follow-up question can be attached to the right symptom
 *   instead of being silently dropped for not naming it again.
 * @param {string|null} [questionAsked] - the EXACT text of the targeted
 *   clarifying question this app just asked (see chatLog.js's
 *   getLastQuestionAsked), when this message is answering one. Fixes a
 *   real, demonstrated failure: without this, the model had no way to
 *   tell "no" answering a question about an UNRELATED possible symptom
 *   (e.g. "any nausea or light sensitivity?") apart from "no" denying
 *   an already-known one (e.g. "headache") — it would sometimes deny
 *   the known symptom anyway, having nothing else to attach a leading
 *   "no" to. Passing the actual question fixes this at the source
 *   (the model now has what it needs to reason about it correctly)
 *   rather than only catching it after the fact.
 * @returns {Promise<{symptoms: Array<{term:string, present:boolean, duration:string|null, severity:string|null}>, ambiguous: boolean, ambiguousValue: string|null}>}
 *   UPDATED: was a bare array. Now also carries `ambiguous`/`ambiguousValue`
 *   for the one case this function deliberately refuses to guess at — a
 *   lone unitless number answering a question that asked for BOTH
 *   duration and severity together (see SYMPTOM_SYSTEM's case 3 above).
 *   Every existing caller reading `.symptoms` off the old bare array
 *   needs updating to read `.symptoms` off this object instead — see
 *   processMessage.js's call site, which now checks `.ambiguous` and
 *   asks the patient to disambiguate instead of silently mis-recording
 *   a duration as a severity or vice versa.
 */
// Deterministic backstop for a PRONOUN denial ("I don't have that
// anymore", "no I don't have that", "that's gone") the prompt-level
// rule above sometimes misses — see classifySymptoms' use of this
// below. Deliberately broad enough to catch how people actually talk
// (a bare "don't have that" with no explicit "anymore"/"gone" marker is
// common and real), while excluding the one confirmed false-positive
// shape: "that" used as a DEGREE word before an adjective ("that bad",
// "that severe", "that much") — there "that" is intensifying, not
// denying presence. Kept intentionally recall-leaning over precision-
// leaning: this only ever fires when there's exactly ONE known symptom
// (bounded blast radius) and nothing here is unrecoverable — a
// wrongly-cancelled symptom can always be re-added before finalize, the
// same way any other correction can.
export const PRONOUN_DENIAL_RE =
  /\b(?:don'?t|do\s+not|dont|no(?:pe)?|not)\b[^.!?]{0,25}\b(?:have|got|feel(?:ing)?|experiencing)\s+(?:that|it|this)\b(?!\s+(?:bad|severe|much|intense|strong|painful|big|serious|worse|great|good))|\b(?:that|it|this)\s*(?:'s|\s+is)\s+(?:gone|resolved|better|over|not\s+(?:there|happening))\b|\bno\s+longer\s+(?:have|got|experiencing|feeling)\s+(?:that|it|this)\b|\bnot\s+(?:that|it|this)\b(?!\s+(?:bad|severe|much|intense|strong|painful|big|serious|worse|great|good))/i;

// Deterministic backstop for a BLANKET WELLNESS statement — "I'm fine
// now", "I feel fine", "I'm okay", "all better", "nothing's wrong
// anymore" — with NO specific symptom named at all. Real, demonstrated
// gap: unlike PRONOUN_DENIAL_RE above (which resolves "that"/"it" to
// ONE specific known symptom, and deliberately stays silent when there's
// more than one candidate to avoid guessing which one), "I'm fine now"
// isn't ambiguous about WHICH symptom it means — it's a global
// statement, so it applies to every currently-tracked present symptom,
// not just one. classifySymptoms' own prompt has no rule for this at
// all (it only ever tries to deny a SPECIFICALLY NAMED symptom), so a
// patient saying this got no response whatsoever — the symptom list
// just sat there unchanged, which is exactly the frustrating "it
// doesn't remove anything" behavior this fixes.
export const BLANKET_WELLNESS_RE =
  /\b(?:i'?m|i\s+am|i\s+feel|feeling)\s+(?:fine|okay|ok|better|all\s+better|good)\s*(?:now|today)?\s*[.!]?$|\ball\s+better\b|\bnothing'?s?\s+wrong\b|\bno\s+(?:more\s+)?symptoms?\b|\bi\s+don'?t\s+have\s+any\s+symptoms?\b|\beverything'?s?\s+(?:fine|okay|ok|better)\b/i;

// Live-demonstrated case: "actually forget all that, let's start fresh"
// sent right at the "anything else before I recommend a specialist?"
// prompt got forced through resolveFinalConfirmation's narrow confirm/
// add/remove vocabulary — it read "forget all that" as a REMOVAL of the
// one tracked symptom, then separately re-asked a fresh clarifying
// question about that same now-denied symptom, an internally
// contradictory result. There was no dedicated way to recognize "start
// over" intent anywhere in the pipeline — this is that deterministic
// backstop, checked early (right after the mandatory safety gates, in
// processMessage.js) so it can never be swallowed by whichever
// pending-question resolver happens to be active. Deliberately anchored
// to explicit restart/reset language, not just "forget" on its own —
// "I forgot to mention I also have a fever" must never match this (past
// tense "forgot", not an instruction to reset), and "let's start with my
// headache" must not either (a different sense of "start").
export const RESTART_INTENT_RE =
  /\b(?:let'?s\s+start\s+(?:fresh|over)|start\s+(?:fresh|over)|start\s+(?:this|everything)\s+over|start\s+again|begin\s+again)\b|\bforget\s+(?:all\s+that|everything|what\s+i\s+(?:said|told\s+you))\b|\b(?:can\s+we\s+|please\s+)?(?:restart|reset)(?:\s+(?:this|the\s+conversation|everything))?\b|\bscratch\s+(?:that|all\s+that)\b/i;

export async function classifySymptoms(message, knownSymptomTerms = [], questionAsked = null) {
  if (!message || !message.trim()) return { symptoms: [], ambiguous: false, ambiguousValue: null, mentionedConditions: [] };
  try {
    const parsed = await callAIStructured({
      system: SYMPTOM_SYSTEM,
      message: JSON.stringify({
        patient_message: message,
        already_known_symptoms: knownSymptomTerms,
        question_asked: questionAsked || null,
      }),
      schema: SYMPTOM_SCHEMA,
    });
    const symptoms = Array.isArray(parsed?.symptoms) ? parsed.symptoms : [];
    const lowerMessage = message.toLowerCase();
    const cleanedSymptoms = symptoms
      .filter((s) => s && typeof s.term === 'string' && s.term.trim())
      .map((s) => ({
        term: s.term.trim(),
        present: s.present !== false,
        duration: s.duration && String(s.duration).trim() ? String(s.duration).trim() : null,
        severity: s.severity && String(s.severity).trim() ? String(s.severity).trim() : null,
      }))
      // FIXED (demonstrated live): the prompt above is explicit that
      // present:false requires the symptom to be SPECIFICALLY NAMED in
      // the message, but the model was observed doing it anyway for a
      // message like "no but i have eye pain" answering a follow-up
      // question about an UNRELATED symptom (nausea/light sensitivity)
      // — it flipped the already-known "headache" to denied even
      // though "headache" appears nowhere in that message, apparently
      // over-applying the leading "no". This is a hard, checkable
      // constraint the model itself already promised to follow, so
      // it's enforced here rather than trusted blindly: a denial whose
      // term isn't textually present in the patient's own message this
      // turn is dropped (treated as "no information," the safer
      // default) instead of accepted. A real denial ("no chest pain",
      // "the headache is gone") always contains the term's own words,
      // so this never blocks a genuine one.
      .filter((s) => {
        if (s.present) return true;
        const words = s.term.toLowerCase().split(/\s+/).filter((w) => w.length > 2);
        return words.length === 0 || words.some((w) => lowerMessage.includes(w));
      });
    // FIXED (demonstrated live): the model flagged ambiguous: true with
    // ambiguousValue: "no pain" — a plain denial, not a number — despite
    // the prompt explicitly restricting this case to an actual bare
    // number. That bogus "ambiguous value" went on to generate a
    // nonsense clarifying question ("does 'no pain' mean no pain days,
    // or a severity of no pain out of 10?"), which then confused the
    // NEXT turn's classification too. Rather than trust the model's own
    // ambiguous flag blindly, verify ambiguousValue is actually a number
    // (digits, optionally with a decimal, or a spelled-out number word)
    // before honoring it — a non-numeric "ambiguous" value is treated as
    // not ambiguous at all, falling back to whatever `symptoms` already
    // extracted (e.g. a genuine denial goes through the present:false
    // path above instead).
    const NUMBER_WORD_RE = /^(zero|one|two|three|four|five|six|seven|eight|nine|ten)$/i;
    const rawAmbiguousValue = parsed?.ambiguousValue ? String(parsed.ambiguousValue).trim() : '';
    const looksNumeric = /^\d+(\.\d+)?$/.test(rawAmbiguousValue) || NUMBER_WORD_RE.test(rawAmbiguousValue);
    const ambiguous = Boolean(parsed?.ambiguous) && Boolean(rawAmbiguousValue) && looksNumeric;

    // DETERMINISTIC BACKSTOP (demonstrated live): the PRONOUN DENIAL
    // prompt rule above isn't reliably followed — a real, observed case
    // ("hey actually I dont have that anymore", one known symptom
    // "headache") came back with an empty symptoms array instead of a
    // denial. Only fires when the model found NOTHING at all this turn
    // (never overrides something it DID extract) and there's exactly
    // ONE known symptom to resolve the pronoun to — see
    // PRONOUN_DENIAL_RE's own comment for why it's scoped the way it
    // is. Deliberately bypasses the "term must appear in the message"
    // filter above (cleanedSymptoms' own denial guard) — that filter
    // exists to catch the MODEL over-applying a denial to a term it
    // didn't actually mean; this is the opposite case, a deliberate
    // pronoun resolution where the term is KNOWN not to appear
    // verbatim, that's the whole point of resolving "that" to it.
    const mentionedConditions = Array.isArray(parsed?.mentionedConditions)
      ? [...new Set(
          parsed.mentionedConditions
            .filter((c) => typeof c === 'string' && c.trim())
            .map((c) => c.trim())
        )]
      : [];

    if (cleanedSymptoms.length === 0 && knownSymptomTerms.length === 1 && PRONOUN_DENIAL_RE.test(message)) {
      return {
        symptoms: [{ term: knownSymptomTerms[0], present: false, duration: null, severity: null }],
        ambiguous: false,
        ambiguousValue: null,
        mentionedConditions,
      };
    }

    // DETERMINISTIC BACKSTOP (demonstrated live): a BLANKET WELLNESS
    // statement ("I am fine now") names no specific symptom at all, so
    // neither the model's own denial rule nor PRONOUN_DENIAL_RE above
    // (which requires exactly one candidate to resolve a pronoun to)
    // ever fires — the symptom list just sits there unchanged, which is
    // exactly the frustrating "saying I'm fine doesn't remove anything"
    // behavior this fixes. Unlike a pronoun denial, "I'm fine" isn't
    // ambiguous about WHICH symptom it means — it's a global statement,
    // so every currently-present known symptom is denied, not just one.
    if (cleanedSymptoms.length === 0 && knownSymptomTerms.length >= 1 && BLANKET_WELLNESS_RE.test(message)) {
      return {
        symptoms: knownSymptomTerms.map((term) => ({ term, present: false, duration: null, severity: null })),
        ambiguous: false,
        ambiguousValue: null,
        mentionedConditions,
      };
    }

    return {
      symptoms: cleanedSymptoms,
      ambiguous,
      ambiguousValue: ambiguous ? rawAmbiguousValue : null,
      mentionedConditions,
    };
  } catch (err) {
    console.error('[symptomClassifier] classifySymptoms failed:', err.message);
    // FOUND via adversarial testing: this used to return exactly the
    // same shape as "the patient's message genuinely contained no
    // symptom info" — which processMessage.js then turned into "I
    // couldn't identify any symptoms in your message," a reply that
    // blames the PATIENT's message for a failure that was actually ours
    // (every AI provider failed this call — observed live during a
    // period of heavy rate-limiting). _classificationFailed lets the
    // caller give an honest "having trouble, try again" reply instead of
    // a misleading one — same "fail loud, don't dress up a failure as a
    // normal answer" principle already applied to checkAICrisis above.
    return { symptoms: [], ambiguous: false, ambiguousValue: null, mentionedConditions: [], _classificationFailed: true };
  }
}

/**
 * Renders a classifySymptoms() result into one compact string — this
 * is what actually gets handed to chatLog.js's appendSessionMessage
 * for storage, NEVER the patient's raw message. e.g.
 *   [{ term: "chest pain", present: true, severity: "severe", duration: "3 days" }]
 *   -> "chest pain (severe, for 3 days)"
 *
 * @param {Array} symptoms - classifySymptoms() result
 * @returns {string} - empty string if there's nothing worth storing
 */
export function formatClassifiedSymptoms(symptoms) {
  if (!symptoms || symptoms.length === 0) return '';
  return symptoms
    .map((s) => {
      const parts = [];
      if (!s.present) parts.push('denied');
      if (s.severity) parts.push(s.severity);
      if (s.duration) parts.push(`for ${s.duration}`);
      return parts.length ? `${s.term} (${parts.join(', ')})` : s.term;
    })
    .join('; ');
}

// ============================================
// NATURAL-LANGUAGE COMPOSITION FOR INFERMEDICA'S /parse INPUT
//
// finalizeAndRecommend() (processMessage.js) used to build the text it
// sends to Infermedica's /parse by mechanically joining formatted tags
// — "headache (mild, for 2 days); eye pain" — which reads nothing like
// real patient language. That format is what a real conversation
// diagnosed as the likely cause of a stuck-loop bug: /parse returned a
// correct match but without its `orig_text` field populated, and
// filterGroundedMentions (processMessage.js) — which exists to reject
// mentions Infermedica invents with no basis in what was actually
// sent — had no substring to verify, so it dropped a real finding.
//
// Per the explicit decision that produced this function: rather than
// patch that failure by regrounding against a NEW hand-maintained list
// of known terms/synonyms, give Groq a LITTLE more room (temperature)
// to turn the already-decided structured symptom list into one natural
// sentence — the kind of phrasing Infermedica's own NLP is tuned to
// parse in the first place, since that's the kind of text real patients
// type. This is composing PROSE from data we already fully trust (the
// structured list itself, produced by classifySymptoms/
// resolveFinalConfirmation above), not making a new clinical judgment —
// there's no new fact being asserted here that classifySymptoms didn't
// already decide, so a little temperature is safe in a way it would not
// be for the classification step itself (which stays temperature 0).
// The caller still runs a deterministic sanity check on the output
// (see processMessage.js) and falls back to the old mechanical format
// if the check fails, so a bad composition can never silently drop or
// invent a symptom.
// ============================================

const NATURAL_TEXT_SCHEMA = {
  type: 'object',
  properties: { sentence: { type: 'string' } },
  required: ['sentence'],
};

const NATURAL_TEXT_SYSTEM = `You are turning a structured list of a patient's already-recorded symptoms into ONE short, natural-sounding description, the way a patient might actually say it to a doctor — not a mechanical list of tags.

Rules:
- Mention EVERY symptom term in the list. Use plain everyday phrasing where there's an obvious one, but keep each symptom clearly recognizable — do not merge or drop any of them.
- If a symptom is marked present: false, phrase it as a clear, explicit denial ("no chest pain", "hasn't had a fever", "no nausea") — never omit it, and never phrase it so it could be misread as present.
- Naturally weave in duration and severity ONLY where given for that symptom — do not invent or mechanically restate one that isn't there.
- Do not add any symptom, cause, detail, or narrative that isn't in the list. Do not diagnose or speculate about a cause.
- Keep it to 1-3 short sentences of plain English.

Return ONLY the JSON — no commentary.`;

/**
 * @param {Array<{term:string, present:boolean, duration:string|null, severity:string|null}>} symptoms
 * @returns {Promise<string>} - empty string on failure or empty input;
 *   caller falls back to the mechanical template in that case.
 */
export async function composeNaturalDescription(symptoms) {
  if (!symptoms || !symptoms.length) return '';
  try {
    const parsed = await callAIStructured({
      system: NATURAL_TEXT_SYSTEM,
      message: JSON.stringify({ symptoms }),
      schema: NATURAL_TEXT_SCHEMA,
      // A bit more room than the classification calls (which stay at 0)
      // — see the doc comment above this function for why that's safe
      // here specifically: this call composes phrasing, not a new
      // clinical fact.
      temperature: 0.4,
    });
    return String(parsed?.sentence || '').trim();
  } catch (err) {
    console.error('[symptomClassifier] composeNaturalDescription failed (non-fatal, caller falls back to templated text):', err.message);
    return '';
  }
}

const COMPLAINT_SCHEMA = {
  type: 'object',
  properties: { normalized: { type: 'string' } },
  required: ['normalized'],
};

const COMPLAINT_SYSTEM = `Rewrite the patient's complaint clause as the shortest possible GENERIC clinical phrase (2-5 words) — strip every bit of narrative detail, names, dates, causes, and any other specific or identifying information, keeping only the bare nature/location of the complaint. Example: "my toe has been throbbing since I stubbed it on the stairs yesterday" -> "toe pain". If you truly cannot tell what body part or complaint is being described, return "unspecified complaint". Return ONLY the JSON — no commentary.`;

/**
 * Same idea as classifySymptoms, but for a single clause /parse
 * couldn't match to any Infermedica finding (see complaintDiff.js /
 * processMessage.js's "unaccounted complaints" handling). These used
 * to be stored verbatim in chatLog.js's unaccountedComplaints — the
 * patient's own clause text, kept for the rest of the session so the
 * LLM keeps being told not to treat it as evidence. Normalizing it
 * here first means that store never holds the patient's own wording
 * either.
 *
 * @param {string} clauseText
 * @returns {Promise<string>}
 */
export async function normalizeComplaint(clauseText) {
  if (!clauseText || !clauseText.trim()) return '';
  try {
    const parsed = await callAIStructured({
      system: COMPLAINT_SYSTEM,
      message: JSON.stringify({ clause: clauseText }),
      schema: COMPLAINT_SCHEMA,
    });
    const normalized = String(parsed?.normalized || '').trim();
    return normalized || 'unspecified complaint';
  } catch (err) {
    console.error('[symptomClassifier] normalizeComplaint failed (non-fatal, using a generic placeholder):', err.message);
    return 'unspecified complaint';
  }
}