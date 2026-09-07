// ============================================
// SehatAI: Symptom Sanity Gate
//
// Purpose: root-cause fix for a repeated failure class. The physical-
// symptom pipeline (classifySymptoms -> accumulatedSymptoms ->
// Infermedica) had NO independent check between "the classifier
// decided this is a symptom" and "send it to Infermedica". Four
// separate bugs in one debugging session reached Infermedica and came
// back `emergency` for reasons that had nothing to do with anything
// actually dangerous in what the patient typed:
//   - "depressed mood" extracted as a physical symptom
//   - "no thats not all" misread (only the leading word), triggering a
//     premature finalize with whatever bad evidence was on file
//   - "no pain" flagged as an ambiguous bare number, producing a
//     nonsense disambiguation question ("does 'no pain' mean no pain
//     days, or a severity of no pain out of 10?")
//   - "both" (answering that nonsense question) misread as confirming
//     two fake symptoms invented by the bot's own malformed question
//
// This mirrors the second-opinion pattern already used for the
// generated recommendation prose — generaterecommendation.js is
// constrained at generation time, then independently checked by
// groundingVerifier.js before it's shown to the patient. This module
// applies the same idea one stage earlier, to the EVIDENCE itself,
// before it is trusted anywhere downstream (stored, read back to the
// patient, or sent to Infermedica).
//
// Deliberately a cheap, deterministic, non-AI check: the failure mode
// this guards against is the AI classifier itself being wrong, so a
// second AI call grading the first would not be independent in any
// real sense, and would add another round-trip to every turn — latency
// has independently been a live concern in this app (see callAi.js's
// provider cascade). A deterministic net that runs in microseconds and
// never blocks anything a real patient would plausibly type is the
// right tradeoff for a gate whose only job is to catch obviously
// non-clinical entries before a real triage engine scores them.
// ============================================

// Mood/emotional language, as an INDEPENDENT backstop — not a
// duplicate of symptomClassifier.js's own exclusion rule inside the
// same prompt that produced the entry in the first place. The whole
// point of this gate is to not simply trust that upstream prompt
// again.
const MOOD_TERM_RE = /\b(depress(ed|ion)?|anxious|anxiety|stress(ed|ful)?|hopeless(ness)?|worthless(ness)?|lonely|loneliness|grief|grieving|low mood|mood swing|emotional(ly)?|unmotivated|no motivation)\b/i;

// Self-applied DIAGNOSIS LABELS, as an INDEPENDENT backstop for the
// same reason as MOOD_TERM_RE above — not a duplicate of
// symptomClassifier.js's own exclusion rule, which lives in the same
// prompt that can fail to follow it. Demonstrated live: "headache and
// eye pain and slight migraine" got tracked as THREE separate symptoms
// (headache, eye pain, migraine) instead of two — the diagnosis label
// inflated the apparent symptom count, which fed a false EMERGENCY
// verdict from the severity classifier on a later turn. A term that IS
// one of these labels, standing alone (not as part of a longer
// legitimate phrase like "migraine relief cream" — irrelevant here
// since these are symptom terms, not product names), is never
// something Infermedica should receive as independent evidence
// alongside the plain physical symptom it's redundant with.
const DIAGNOSIS_LABEL_WHOLE_RE = /^(migraine|migraines|sinusitis|bronchitis|pneumonia|flu|influenza|covid|covid-19|uti|appendicitis|tonsillitis|conjunctivitis|gastritis|arthritis)$/i;

// Fragments that only ever show up in THIS APP'S OWN auto-generated
// meta-questions (disambiguation re-asks, final-confirmation re-reads,
// off-topic re-prompts) — never in a real patient-described bodily
// complaint. A "symptom" term matching one of these means the
// classifier extracted a piece of the bot's own prior question, not
// something the patient actually reported — the exact shape of the
// "both" bug above (symptomClassifier.js's rule for extracting
// candidate symptoms named in a real clinical question, misfiring on
// the bot's own malformed one instead).
const META_ARTIFACT_RE = /\b(ambiguous|disambiguat|clarif(y|ication)|confirm(ation)?|recommend(ation)?|specialist|anything else|scale of|out of 10|day count|severity rating|go ahead|start simple|clinical database)\b/i;

// A term consisting ONLY of one of these (not as a substring inside a
// longer real term) is never a bodily complaint on its own — these are
// grammatical/meta/filler words, not clinical vocabulary. Catches the
// "no pain" / "both" family directly: even if one of those somehow
// arrived as a whole term instead of being caught by the rules above.
const BARE_NONSYMPTOM_WHOLE_RE = /^(both|either|neither|yes|yeah|yep|no|nope|none|nothing|not really|maybe|okay|ok|sure|fine|good|bad|more|less|same|it|that|this|thing|stuff|no pain|no change)$/i;

// A bare "pain"/"ache" with NO body location named isn't an actionable
// clinical complaint on its own — real, demonstrated case: the patient
// wrote "headache and pain", and "pain" alone got extracted as its own
// tracked symptom, distinct from "headache", which Infermedica then
// couldn't ground to anything specific ("I couldn't match the
// following... pain"). This is different from BARE_NONSYMPTOM_WHOLE_RE
// above (which catches conversational filler like "no pain" as an
// artifact) — this instead catches a genuinely-meant but underspecified
// complaint. A located pain term ("chest pain", "eye pain", "abdominal
// pain") is unaffected — only a pain/ache word with nothing else next
// to it is rejected.
const LOCATIONLESS_PAIN_RE = /^(pain|ache|aches|aching|hurt|hurts|hurting|sore|soreness)$/i;

// Same "no pain" artifact, but as a SUBSTRING check rather than a
// whole-term match — a real, demonstrated case had it survive as "no
// pain days" (extracted from this app's own disambiguation question
// "does 'no pain' mean no pain days, or a severity of no pain out of
// 10?"), which BARE_NONSYMPTOM_WHOLE_RE's anchored match does not
// catch since the term isn't EXACTLY "no pain". A genuine denial of
// pain is already recorded correctly via present:false on the real
// symptom term elsewhere in this pipeline (see symptomClassifier.js's
// denial rule) — nothing legitimate is ever named "no pain ___" as its
// own term, so this is safe to reject unconditionally.
const NO_PAIN_ARTIFACT_RE = /\bno pain\b/i;

const NUMBER_ONLY_RE = /^\d+(\.\d+)?$/;

/**
 * @param {{term:string, present:boolean, duration:?string, severity:?string}} entry
 * @returns {string|null} a short reason the entry should be dropped, or
 *   null if it passes.
 */
function reasonToReject(entry) {
  const term = String(entry?.term || '').trim();
  if (!term) return 'empty term';
  if (NUMBER_ONLY_RE.test(term)) return 'bare number, not a bodily complaint';
  if (BARE_NONSYMPTOM_WHOLE_RE.test(term)) return 'grammatical/meta/filler word, not a bodily complaint';
  if (NO_PAIN_ARTIFACT_RE.test(term)) return "\"no pain\" denial artifact leaked into a term — not a real symptom name";
  if (LOCATIONLESS_PAIN_RE.test(term)) return 'bare pain/ache with no body location — not an actionable complaint on its own';
  if (MOOD_TERM_RE.test(term)) return 'mood/emotional term — belongs to the emotional-support flow, never to Infermedica';
  if (DIAGNOSIS_LABEL_WHOLE_RE.test(term)) return 'self-applied diagnosis label tracked as its own symptom — redundant with (and inflates) the plain physical complaint it describes';
  if (META_ARTIFACT_RE.test(term)) return "matches this app's own meta-question phrasing, not something the patient reported";
  if (term.length > 60) return 'implausibly long for a normalized clinical term';
  // A real clinical term has at least one recognizable word (letters
  // only, length >= 2) — catches leftover punctuation/number soup
  // without rejecting genuinely short real terms like "flu", "gas".
  const hasRealWord = term.split(/\s+/).some((w) => /^[a-z][a-z'-]*$/i.test(w) && w.length >= 2);
  if (!hasRealWord) return 'no recognizable clinical wording';
  return null;
}

/**
 * Independent sanity check run AFTER classifySymptoms/
 * resolveFinalConfirmation decide something is a symptom, and BEFORE
 * it is trusted anywhere downstream. This is the single choke point
 * every one of the four bugs described above would have been caught
 * at, regardless of which upstream prompt or rule produced the bad
 * entry — see processMessage.js's two call sites: right where a
 * turn's classified symptoms are merged into accumulatedSymptoms, and
 * again right before the accumulated list is turned into evidence for
 * Infermedica (defense in depth — catches anything that reached
 * accumulatedSymptoms some other way, e.g. a session hydrated from
 * before this gate existed).
 *
 * @param {Array<{term:string, present:boolean, duration:?string, severity:?string}>} symptoms
 * @param {string} [logLabel] - where this call happens, for the
 *   console.warn below (e.g. 'accumulate', 'pre-infermedica') — makes a
 *   dropped entry traceable to which stage caught it.
 * @returns {Array} the entries that passed
 */
export function sanityFilterSymptoms(symptoms, logLabel = 'sanity-gate') {
  if (!Array.isArray(symptoms) || symptoms.length === 0) return [];
  const kept = [];
  for (const entry of symptoms) {
    const reason = reasonToReject(entry);
    if (reason) {
      console.warn(`[symptomSanityGate:${logLabel}] dropped "${entry?.term}" — ${reason}`);
    } else {
      kept.push(entry);
    }
  }
  return kept;
}
