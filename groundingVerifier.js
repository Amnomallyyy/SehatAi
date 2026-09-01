// ============================================
// SehatAI: Grounding Verifier
// Every clinical entity in EVERY field, including free prose,
// must trace to retrieved context. Prose is not a loophole.
//
// FIXES vs the previous version:
//  1. Imports './safedefault.js' (real filename, lowercase d).
//  2. checkKeywordMatch() returns { isEmergency, ... } — NOT { matched }.
//     The old code read kw?.matched, so the emergency-language check
//     silently never fired. That was the single most important check in
//     this file and it was dead code.
//  3. Emergency language that merely repeats one of the patient's OWN
//     already-safety-checked matched symptoms is not a new alarm — it
//     used to nuke every turn for the rest of the session.
//  4. A condition/medication the PATIENT THEMSELVES stated in this
//     session is no longer a hard reject. It's recorded as a
//     non-blocking, repaired violation instead. Rejecting it made the
//     bot answer "I can't help with that" to anyone who mentioned their
//     own diabetes.
//  5. allowedSpecialists / allowedProfileFacts tolerate objects or
//     strings.
// ============================================

import { supabase } from './supabaseClient.js';
import { buildSafeDefault } from './safedefault.js';
import { factValues } from './profileFact.js';
import { callAIStructured } from './callAi.js';

// Phrases that assert the patient TAKES a drug. Used to catch the
// category-confusion failure: every term on file, but the relationship
// between them invented ("current use of Metformin and Penicillin" when
// Penicillin is an allergy). No membership check can catch this, because
// nothing was hallucinated — only the meaning.
const MEDICATION_CUE =
  /\b(?:taking|takes|taken|on|using|uses|use|prescribed|managed\s+with|treated\s+with|medicated\s+with|medications?|meds|drugs?|regimen|dose|doses)\b/i;

const PROSE_FIELDS = ['rationale', 'next_steps', 'explanation'];

const SPECIALIST_SHAPES = [
  /\b[a-z]+ologists?\b/gi,
  /\b[a-z]+iatricians?\b/gi,
  /\b[a-z]+iatrists?\b/gi,
];
const SPECIALIST_LEXICON = [
  'pediatrician', 'paediatrician', 'general physician', 'family physician', 'gp', 'internist',
  'surgeon', 'dentist', 'physiotherapist', 'physical therapist', 'dietitian', 'dietician',
  'nutritionist', 'chiropractor', 'optometrist', 'ent specialist', 'ent',
];
const LAB_LEXICON = [
  'cbc', 'complete blood count', 'hba1c', 'a1c', 'tsh', 't3', 't4', 'ldl', 'hdl', 'triglycerides',
  'lipid profile', 'troponin', 'esr', 'crp', 'creatinine', 'urea', 'egfr', 'hemoglobin', 'haemoglobin',
  'ana', 'rheumatoid factor', 'uric acid', 'vitamin d', 'vitamin b12', 'b12', 'ferritin', 'urinalysis',
  'blood sugar', 'fasting glucose', 'random glucose', 'liver function', 'lft', 'chest x-ray', 'x-ray',
  'ecg', 'ekg', 'echo', 'ultrasound', 'mri', 'ct scan', 'biopsy', 'spirometry', 'd-dimer',
];
const CONDITION_LEXICON = [
  'diabetes', 'diabetic', 'hypertension', 'high blood pressure', 'asthma', 'copd', 'tuberculosis', 'tb',
  'hypothyroidism', 'hyperthyroidism', 'thyroid', 'anemia', 'anaemia', 'arthritis', 'rheumatoid arthritis',
  'lupus', 'ckd', 'kidney disease', 'heart disease', 'high cholesterol', 'obesity', 'epilepsy',
  'pregnancy', 'pregnant', 'depression', 'anxiety', 'migraine', 'psoriasis', 'eczema', 'gout',
];
const MED_LEXICON = [
  'metformin', 'insulin', 'aspirin', 'paracetamol', 'ibuprofen', 'penicillin', 'amoxicillin',
  'atorvastatin', 'losartan', 'amlodipine', 'levothyroxine', 'omeprazole', 'prednisolone',
  'salbutamol', 'warfarin', 'clopidogrel',
];
const EMERGENCY_PHRASES = [
  /\bcall\s+(?:911|1122|15|an?\s+ambulance|emergency)/i,
  /\bemergency\s+room\b/i,
  /\bgo\s+to\s+(?:the\s+)?er\b/i,
  /\bimmediate(?:ly)?\s+medical\s+attention\b/i,
  /\blife[-\s]threatening\b/i,
];
const DIAGNOSIS_PHRASES = [
  /\byou\s+(?:have|are\s+suffering\s+from|likely\s+have|probably\s+have)\b/i,
  /\bthis\s+is\s+(?:likely\s+)?(?:a\s+)?(?:case\s+of|diagnosis)\b/i,
  /\bdiagnos(?:is|ed|e)\b/i,
];

let vocabCache = null;

export async function loadVerifierVocabulary({ force = false } = {}) {
  if (vocabCache && !force) return vocabCache;

  const [specialists, relatedTests, meds] = await Promise.all([
    supabase.from('specialists').select('name'),
    supabase.from('symptom_related_tests').select('test_name'),
    supabase.from('medicines').select('name'),
  ]);

  vocabCache = {
    specialists: dedupe([...SPECIALIST_LEXICON, ...(specialists.data || []).map((r) => r.name)]),
    labs:        dedupe([...LAB_LEXICON,        ...(relatedTests.data || []).map((r) => r.test_name)]),
    facts:       dedupe([...CONDITION_LEXICON, ...MED_LEXICON, ...(meds.data || []).map((r) => r.name)]),
  };
  return vocabCache;
}

/**
 * @param {object|null} recommendation - raw model output from generateRecommendation()
 * @param {{
 *   allowedSpecialists?: Array<string|{name:string}>,
 *   allowedLabTests?: string[],
 *   allowedProfileFacts?: string[],
 *   matchedSymptomNames?: string[],
 *   patientStatedText?: string,
 *   graphUrgency?: string,
 *   subjectInfo?: {subject:string, relation?:string|null},
 *   checkKeywordMatch?: Function
 * }} ctx
 */
export async function verifyRecommendation(recommendation, ctx = {}) {
  const violations = [];

  if (!recommendation || typeof recommendation !== 'object') {
    violations.push({ code: 'malformed_output', detail: 'Recommendation was not an object' });
    return blocked(violations, ctx);
  }

  const vocab = await loadVerifierVocabulary();
  const rec = { ...recommendation };

  const allowedSpecialistNames = asArray(ctx.allowedSpecialists)
    .map((s) => (typeof s === 'string' ? s : s?.name))
    .filter(Boolean);

  const allowedSpecialists = normSet(allowedSpecialistNames);
  const allowedLabs        = normSet(ctx.allowedLabTests || []);
  // Accepts labeled facts ({value, category}) or bare strings.
  const profileFactObjects = asArray(ctx.allowedProfileFacts).filter(
    (f) => f && typeof f === 'object' && f.value
  );
  const allowedFacts       = normSet(factValues(ctx.allowedProfileFacts || []));
  const knownSymptoms      = normSet(ctx.matchedSymptomNames || []);
  const isSelf             = ctx.subjectInfo?.subject === 'self';
  const prose              = PROSE_FIELDS.map((f) => rec[f] || '').join('\n');
  const patientSaid        = norm(ctx.patientStatedText || '');

  const patientMentioned = (term) => {
    const t = norm(term);
    if (!t || !patientSaid) return false;
    return new RegExp(`(?<![a-z0-9])${escapeRe(t)}(?![a-z0-9])`, 'i').test(` ${patientSaid} `);
  };

  // FOUND live, same root cause already fixed once for symptom matching
  // (matchedSymptomNames/knownSymptoms — see the isConjunctionOfKnown
  // comment below): allowedFacts.has(n) is a pure EXACT-STRING match
  // against the fact's full normalized value. A stored fact with any
  // extra detail in it — "Pregnant (28 weeks)" normalizes to "pregnant
  // 28 weeks" — never exactly equals how a model naturally refers to it
  // in prose ("pregnancy", "pregnant"), so a completely real, on-file
  // condition got blocked as ungrounded. Demonstrated live: a pregnant
  // patient's real recommendation was discarded TWICE (both attempts
  // blocked) and replaced with the generic safe-default fallback because
  // of this — first tried a whole-word SUBSTRING match (mention within
  // fact / fact within mention), which fixed "pregnant" (a literal
  // prefix of "pregnant 28 weeks") but NOT "pregnancy" — different word
  // FORM, not a substring of each other at all ("pregnancy" vs
  // "pregnant" diverge after "pregnan"). English condition names
  // routinely have this adjective/noun split with a shared stem
  // (pregnant/pregnancy, diabetic/diabetes, asthmatic/asthma), so this
  // checks a shared PREFIX per word (>=6 chars, long enough that
  // unrelated short words don't accidentally collide) between the
  // mention and each individual word of each allowed fact — not the
  // fact's whole multi-word string, since "pregnant 28 weeks" as one
  // string won't prefix-match a single-word mention like "pregnancy" at
  // all.
  const FACT_PREFIX_MIN_LEN = 6;
  const sharesLongPrefix = (a, b) => {
    if (a.length < FACT_PREFIX_MIN_LEN || b.length < FACT_PREFIX_MIN_LEN) return false;
    // Actual longest-common-prefix length, not an arbitrary guessed
    // slice length — "diabetes" vs "diabetic" only share "diabet" (6
    // chars), diverging at the 7th ('e' vs 'i'); a guessed length here
    // previously compared past the real divergence point and wrongly
    // said they didn't match.
    let i = 0;
    const maxLen = Math.min(a.length, b.length);
    while (i < maxLen && a[i] === b[i]) i++;
    return i >= FACT_PREFIX_MIN_LEN;
  };
  const factAllowsMention = (n) => {
    if (!n) return false;
    if (allowedFacts.has(n)) return true;
    const mentionRe = new RegExp(`(?<![a-z0-9])${escapeRe(n)}(?![a-z0-9])`, 'i');
    const mentionWords = n.split(/\s+/).filter(Boolean);
    for (const fact of allowedFacts) {
      if (mentionRe.test(` ${fact} `)) return true;
      const factRe = new RegExp(`(?<![a-z0-9])${escapeRe(fact)}(?![a-z0-9])`, 'i');
      if (factRe.test(` ${n} `)) return true;
      const factWords = fact.split(/\s+/).filter(Boolean);
      if (mentionWords.some((mw) => factWords.some((fw) => sharesLongPrefix(mw, fw)))) return true;
    }
    return false;
  };

  // AI extraction runs once per verify pass, alongside (not instead of)
  // the lexicon scan — see extractClinicalEntitiesAI's doc comment.
  // `null` means the AI call itself failed; in that case every mention
  // list below just falls back to lexicon-only, exactly like before
  // this fix existed, rather than the whole check silently skipping.
  const aiEntities = await extractClinicalEntitiesAI(prose);
  const mergeMentions = (lexMentions, aiList) =>
    dedupe([...lexMentions, ...(aiList || [])]);

  // 1. Structured specialist must be a graph candidate.
  if (!allowedSpecialists.has(norm(rec.specialist_recommended))) {
    violations.push({
      code: 'specialist_not_in_graph',
      detail: `"${rec.specialist_recommended}" not in [${allowedSpecialistNames.join(', ')}]`,
    });
  }

  // 2. No specialist may be named in prose unless it is a graph candidate.
  //    Checked against BOTH the lexicon scan and the AI extraction, so a
  //    specialist name outside the ~15-word SPECIALIST_LEXICON (e.g.
  //    "Cardiologist", "Neurologist" — not literally hardcoded above)
  //    still gets caught if it isn't actually an allowed candidate.
  const specialistMentions = mergeMentions(
    findMentions(prose, vocab.specialists, SPECIALIST_SHAPES),
    aiEntities?.specialists
  );
  for (const mention of specialistMentions) {
    if (!allowedSpecialists.has(norm(mention)) && !isSpecialistAlias(mention, allowedSpecialists)) {
      violations.push({ code: 'ungrounded_specialist_in_prose', detail: mention });
    }
  }

  // 3. Lab tests must be retrieved AND declared.
  const declaredLabs = normSet(asArray(rec.referenced_lab_tests));
  for (const d of declaredLabs) {
    if (!allowedLabs.has(d)) {
      violations.push({ code: 'hallucinated_lab_test', detail: `declared "${d}" was never retrieved` });
    }
  }
  const labMentions = mergeMentions(findMentions(prose, vocab.labs), aiEntities?.labs);
  for (const mention of labMentions) {
    const n = norm(mention);
    if (!allowedLabs.has(n)) {
      violations.push({ code: 'ungrounded_lab_in_prose', detail: mention });
    } else if (!declaredLabs.has(n)) {
      rec.referenced_lab_tests = [...asArray(rec.referenced_lab_tests), mention];
      violations.push({ code: 'undeclared_lab_repaired', detail: mention, repaired: true });
    }
  }

  // 4. Profile facts. Not-in-record is a hard reject, including negated
  //    mentions ("no history of asthma") — the model has no basis to
  //    assert absence either. EXCEPTION: if the patient said it
  //    themselves in this session, echoing it back is not a
  //    hallucination; record it, don't block on it.
  const declaredFacts = normSet(asArray(rec.referenced_profile_facts));
  for (const d of declaredFacts) {
    if (!factAllowsMention(d) && !patientMentioned(d)) {
      violations.push({ code: 'hallucinated_profile_fact', detail: `declared "${d}" not in record` });
    }
  }
  const factMentions = mergeMentions(findMentions(prose, vocab.facts), aiEntities?.facts);
  for (const mention of factMentions) {
    const n = norm(mention);
    // FIXED (root cause, demonstrated live): a mention that matches one
    // of THIS ROUND's actual matched/reported symptom findings is not a
    // hallucinated condition — it's legitimately sourced evidence, even
    // when it's phrased in Infermedica's own clinical nomenclature
    // rather than everyday language (e.g. "Amenorrhea" is Infermedica's
    // canonical name for a late/absent period finding, not something
    // the model invented). `knownSymptoms` was already being computed
    // above but never actually consulted by this check — only by the
    // unrelated emergency-language echo check further down — so a
    // genuinely grounded finding with a clinical-sounding canonical name
    // was blocked identically to a fabricated diagnosis, 8 times in a
    // row on a real recommendation, discarding an otherwise-correct
    // answer for no reason connected to actual hallucination risk.
    // Also grounded if the mention is a plain CONJUNCTION of two or
    // more individually-known symptoms ("numbness and tingling" when
    // "numbness" and "tingling" are both separately tracked) — real,
    // demonstrated case: the AI entity extraction combined two known
    // symptoms into one phrase, which failed the exact-match check
    // above even though every individual piece of it was genuinely
    // grounded. This does NOT loosen what counts as known — every part
    // still has to be independently in knownSymptoms; it only stops a
    // model naturally saying "X and Y" instead of "X. Y." from reading
    // as a hallucination.
    const isConjunctionOfKnown = (() => {
      const parts = mention.split(/\s*(?:,|&|\band\b|\bor\b)\s*/i).map(norm).filter(Boolean);
      return parts.length > 1 && parts.every((p) => knownSymptoms.has(p));
    })();
    if (knownSymptoms.has(n) || isConjunctionOfKnown) continue;
    if (!isSelf) {
      // An answer about someone else's body must not cite the account
      // holder's record at all — unless the sender said it themselves.
      if (patientMentioned(mention)) {
        violations.push({ code: 'patient_stated_fact', detail: mention, repaired: true });
      } else {
        violations.push({
          code: 'patient_data_leak_into_dependent_answer',
          detail: `answer is about a ${ctx.subjectInfo?.relation || 'dependent'} but cites "${mention}"`,
        });
      }
    } else if (!factAllowsMention(n)) {
      if (patientMentioned(mention)) {
        violations.push({ code: 'patient_stated_fact', detail: mention, repaired: true });
      } else {
        violations.push({ code: 'ungrounded_profile_fact_in_prose', detail: mention });
      }
    } else if (!declaredFacts.has(n)) {
      rec.referenced_profile_facts = [...asArray(rec.referenced_profile_facts), mention];
      violations.push({ code: 'undeclared_profile_fact_repaired', detail: mention, repaired: true });
    }
  }

  // 4b. Category confusion. The terms are all on file, so every check
  //     above passes — but the relationship asserted between them can
  //     still be fabricated, and for an allergy that is dangerous.
  const categoryOf = (category) =>
    profileFactObjects.filter((f) => f.category === category).map((f) => f.value);

  for (const term of categoryOf('allergy')) {
    const finder = new RegExp(`(?<![a-z0-9])${escapeRe(term)}(?![a-z0-9])`, 'gi');
    let hit;

    while ((hit = finder.exec(prose)) !== null) {
      const before = prose.slice(Math.max(0, hit.index - 70), hit.index);
      const after = prose.slice(hit.index + term.length, hit.index + term.length + 20);

      // Correctly labeled in the prose — "Penicillin allergy",
      // "allergic to Penicillin". Leave these alone.
      if (/allerg/i.test(after)) continue;
      if (/allerg\w*\s+(?:to\s*)?$/i.test(before)) continue;

      if (MEDICATION_CUE.test(before)) {
        violations.push({
          code: 'allergy_described_as_medication',
          detail: `"${term}" is a recorded ALLERGY but the answer describes it as a drug the patient takes`,
        });
      }
    }
  }

  for (const term of categoryOf('family_history')) {
    const finder = new RegExp(`(?<![a-z0-9])${escapeRe(term)}(?![a-z0-9])`, 'gi');
    let hit;

    while ((hit = finder.exec(prose)) !== null) {
      const before = prose.slice(Math.max(0, hit.index - 70), hit.index);

      // "family history of X" / "his mother had X" is the correct framing.
      if (/\b(?:family|mother|father|parent|sibling|brother|sister|relative)\b/i.test(before)) continue;

      if (/\b(?:history of|patient(?:'s)?|account holder(?:'s)?|has|with|diagnosed with|suffers from)\b/i.test(before)) {
        violations.push({
          code: 'family_history_stated_as_patient_condition',
          detail: `"${term}" is recorded as FAMILY history but the answer attributes it to the patient`,
        });
      }
    }
  }

  // 5. Emergency language may never appear on a non-emergency path.
  //    checkKeywordMatch returns { isEmergency, matchedTerm, matchedPhrase, category }.
  const kw = typeof ctx.checkKeywordMatch === 'function' ? ctx.checkKeywordMatch(prose) : null;
  const echoesKnownSymptom =
    kw?.isEmergency &&
    (knownSymptoms.has(norm(kw.matchedTerm)) || knownSymptoms.has(norm(kw.matchedPhrase)));

  if (kw?.isEmergency && !echoesKnownSymptom) {
    violations.push({
      code: 'emergency_language_on_routine_path',
      detail: `${kw.matchedPhrase || kw.matchedTerm} (${kw.category || 'unknown'})`,
    });
  }
  const phraseHit = EMERGENCY_PHRASES.find((re) => re.test(prose));
  if (phraseHit) {
    violations.push({ code: 'emergency_language_on_routine_path', detail: String(phraseHit) });
  }

  // 6. No diagnosis.
  for (const re of DIAGNOSIS_PHRASES) {
    if (re.test(prose)) { violations.push({ code: 'diagnostic_language', detail: String(re) }); break; }
  }

  // 7. Urgency belongs to the graph, not the model. Repair rather than reject.
  if (ctx.graphUrgency && rec.urgency !== ctx.graphUrgency) {
    violations.push({
      code: 'urgency_overridden',
      detail: `model "${rec.urgency}" -> graph "${ctx.graphUrgency}"`,
      repaired: true,
    });
    rec.urgency = ctx.graphUrgency;
  }

  // 8. The recommended specialist must actually be NAMED somewhere in
  //    the prose — checks 1-2 above only guard against naming the WRONG
  //    one; nothing guarded against naming NONE at all. Demonstrated
  //    live: specialist_recommended came back correctly as "Neurologist"
  //    (so check 1 passed) but next_steps only ever said "the
  //    recommended specialist" — the model apparently read rule 2
  //    ("don't name any OTHER specialist") over-cautiously and avoided
  //    naming ITS OWN pick too, leaving the patient with no actual name
  //    to act on. Not a hallucination and not worth rejecting/
  //    regenerating over — the correct name is already fully trusted at
  //    this point (check 1 passed), so it's simply appended to
  //    next_steps instead.
  if (rec.specialist_recommended) {
    const specialistNamedInProse = new RegExp(
      `(?<![a-z0-9])${escapeRe(norm(rec.specialist_recommended))}(?![a-z0-9])`,
      'i'
    ).test(norm(prose));
    if (!specialistNamedInProse) {
      violations.push({
        code: 'specialist_name_missing_from_prose',
        detail: `"${rec.specialist_recommended}" was never actually named in rationale/next_steps`,
        repaired: true,
      });
      rec.next_steps = rec.next_steps
        ? `${rec.next_steps} Schedule an appointment with a ${rec.specialist_recommended}.`
        : `Schedule an appointment with a ${rec.specialist_recommended}.`;
    }
  }

  const blocking = violations.filter((v) => !v.repaired);
  if (blocking.length > 0) return blocked(violations, ctx);
  return { ok: true, recommendation: rec, violations };
}

function blocked(violations, ctx) {
  // buildSafeDefault, not the plain SAFE_DEFAULT_RESPONSE — real,
  // demonstrated bug: when ctx.allowedSpecialists narrows to something
  // that does NOT include General Physician (e.g. just
  // ["Ophthalmologist"] for eye pain), the plain hardcoded fallback
  // named a specialist that isn't even a valid candidate for this round
  // — the "safe" default failed this exact verifier's own checks a
  // second time (specialist_not_in_graph, ungrounded_specialist_in_prose)
  // and got delivered anyway, since nothing re-verifies blocked()'s own
  // return value. Grounding the fallback in the real candidate list
  // here closes that gap at its actual source, not just for the
  // specific case that surfaced it.
  return {
    ok: false,
    recommendation: {
      ...buildSafeDefault(ctx?.allowedSpecialists),
      urgency: ctx?.graphUrgency || 'routine',
      source: 'safe_default',
    },
    violations,
  };
}

function norm(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9\s-]/g, '').replace(/\s+/g, ' ').trim();
}
function normSet(arr) { return new Set(asArray(arr).map(norm).filter(Boolean)); }
function asArray(v)   { return Array.isArray(v) ? v : v ? [v] : []; }
function dedupe(arr)  { return [...new Set(arr.filter(Boolean).map((s) => String(s).trim()))]; }
function escapeRe(s)  { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

function findMentions(text, lexicon, shapeRegexes = []) {
  const hay = ' ' + norm(text) + ' ';
  const found = new Set();
  for (const term of lexicon) {
    const t = norm(term);
    if (!t) continue;
    if (new RegExp(`(?<![a-z0-9])${escapeRe(t)}(?![a-z0-9])`, 'i').test(hay)) found.add(term);
  }
  for (const shape of shapeRegexes) {
    for (const m of hay.matchAll(new RegExp(shape.source, 'gi'))) found.add(m[0].trim());
  }
  return [...found];
}

// ------------------------------------------------------------------
// AI-BASED entity extraction — closes the real gap in findMentions()
// above. findMentions() can only ever "see" an entity that happens to
// already be spelled out in SPECIALIST_LEXICON / LAB_LEXICON /
// CONDITION_LEXICON / MED_LEXICON (or match one of the few -ologist/
// -iatrician/-iatrist regex shapes). That means a hallucinated entity
// the model invents — a condition, drug, test, or specialist name that
// simply isn't one of the ~120 hardcoded words above — is INVISIBLE to
// this verifier: it never becomes a "mention" in the first place, so
// it never gets checked against what was actually retrieved. The one
// check whose entire job is "catch hallucination" was itself blind to
// anything outside a fixed vocabulary — the opposite of what it's for.
//
// Fix: ask the model itself (a separate, narrowly-scoped extraction
// call, not the generation call) to read the prose and name every
// specific clinical entity it contains, in the same three buckets the
// lexicons cover. This is exactly the "let AI judge open-ended
// language, keep grounding deterministic" split used everywhere else
// in this app — the EXTRACTION is AI (open-ended, natural language),
// but what happens to each extracted name (is it in the allowed set?)
// is still a plain, deterministic set-membership check, unchanged.
// Lexicon-based findMentions() is kept running alongside this, not
// replaced — it's a free, instant pre-filter and a backstop for when
// the extraction call itself fails or times out (see the catch below,
// which fails safe by returning null so callers fall back to
// lexicon-only rather than silently skipping the check).
// ------------------------------------------------------------------
async function extractClinicalEntitiesAI(prose) {
  const trimmed = String(prose || '').trim();
  if (!trimmed) return { specialists: [], labs: [], facts: [] };

  const schema = {
    type: 'object',
    properties: {
      specialists: { type: 'array', items: { type: 'string' } },
      lab_tests: { type: 'array', items: { type: 'string' } },
      conditions_and_medications: { type: 'array', items: { type: 'string' } },
    },
    required: ['specialists', 'lab_tests', 'conditions_and_medications'],
  };

  const system = `You are a strict extraction tool, not a medical assistant — you never
give advice, only list what is literally written.

Read the TEXT and list every SPECIFIC, NAMED clinical entity it contains, sorted
into three buckets:
- specialists: any named type of doctor, specialist, or clinic role (e.g.
  "Neurologist", "ENT", "physiotherapist", "cardiologist").
- lab_tests: any named lab test, imaging study, or diagnostic procedure (e.g.
  "CBC", "chest x-ray", "HbA1c", "MRI").
- conditions_and_medications: any named medical condition, disease, diagnosis,
  allergy, or medication/drug (e.g. "diabetes", "Penicillin", "asthma",
  "migraine").

Extract the name exactly as it appears in the text — do not rename, correct, or
normalize it, and do not invent an entity that isn't actually named. Do NOT
extract generic, non-specific words on their own, such as "specialist",
"doctor", "a medication", "the condition", "a test" — only extract when an
ACTUAL specific name is given. If a bucket has nothing, return an empty array
for it.`;

  try {
    const result = await callAIStructured({ system, message: trimmed, schema });
    return {
      specialists: Array.isArray(result?.specialists) ? result.specialists.filter(Boolean) : [],
      labs: Array.isArray(result?.lab_tests) ? result.lab_tests.filter(Boolean) : [],
      facts: Array.isArray(result?.conditions_and_medications)
        ? result.conditions_and_medications.filter(Boolean)
        : [],
    };
  } catch (err) {
    console.error('[groundingVerifier] AI entity extraction failed — falling back to lexicon-only detection for this turn:', err.message);
    return null;
  }
}

// Generic role words that don't distinguish one specialty from another on
// their own — stripped out before the fuzzy fallback below compares two
// phrasings of what might be the same role.
const GENERIC_ROLE_WORDS = new Set([
  'specialist', 'doctor', 'physician', 'practitioner', 'consultant', 'expert', 'clinician',
]);

function coreRoleWords(s) {
  return norm(s)
    .split(' ')
    .filter((w) => w && !GENERIC_ROLE_WORDS.has(w));
}

function isSpecialistAlias(mention, allowedSet) {
  const aliases = {
    'gp': 'general physician',
    'family physician': 'general physician',
    'ent': 'ent specialist',
    'paediatrician': 'pediatrician',
    // FIXED (demonstrated live): "eye specialist" for "Ophthalmologist"
    // was blocked as an ungrounded specialist mention — a real,
    // everyday colloquial term for a real allowed specialist, not a
    // hallucination. coreRoleWords' fuzzy fallback below only catches
    // pairs sharing an actual word (e.g. "General" in both "General
    // Physician" and "General Practitioner") — it can't help here,
    // since "eye specialist" and "ophthalmologist" share no words at
    // all despite meaning the same thing. Extended with a small set of
    // other common colloquial names for the same reason, spanning
    // different body systems so the pattern is covered broadly rather
    // than only for the one case that happened to surface first.
    'eye specialist': 'ophthalmologist',
    'eye doctor': 'ophthalmologist',
    'skin specialist': 'dermatologist',
    'skin doctor': 'dermatologist',
    'heart specialist': 'cardiologist',
    'heart doctor': 'cardiologist',
    'bone specialist': 'orthopedist',
    'bone doctor': 'orthopedist',
    'kidney specialist': 'nephrologist',
    'kidney doctor': 'nephrologist',
    'lung specialist': 'pulmonologist',
    'lung doctor': 'pulmonologist',
    'brain specialist': 'neurologist',
    'nerve specialist': 'neurologist',
    'hormone specialist': 'endocrinologist',
    // norm() strips apostrophes, so only the no-apostrophe form below
    // is ever actually matched — "children's doctor" normalizes to
    // "childrens doctor" before this lookup runs.
    'childrens doctor': 'pediatrician',
    'stomach specialist': 'gastroenterologist',
    'gut specialist': 'gastroenterologist',
  };
  const target = aliases[norm(mention)];
  if (target && allowedSet.has(target)) return true;

  // Fuzzy fallback: a mention naming the same core role in different
  // generic wording than an allowed specialist ("General Practitioner"
  // for the allowed "General Physician", "ENT doctor" for "ENT
  // Specialist", "family doctor" for "Family Physician") shouldn't hard-
  // block an otherwise-correct recommendation just because it phrased
  // the role differently. Added alongside extractClinicalEntitiesAI (see
  // above) — that AI extraction step surfaces real paraphrases the old
  // lexicon-only detection never even saw, so without this the newly
  // widened detection would start over-blocking valid recommendations on
  // ordinary synonyms. This does NOT loosen what counts as "allowed" —
  // it only recognizes a different wording of a name that's ALREADY in
  // allowedSet; an actually-wrong specialist still won't share a core
  // word with anything on the list and still gets flagged.
  const mentionCore = coreRoleWords(mention).join(' ');
  if (!mentionCore) return false;
  for (const allowed of allowedSet) {
    if (coreRoleWords(allowed).join(' ') === mentionCore) return true;
  }
  return false;
}