// ============================================
// SehatAI: Subject Detection
// Determines WHOSE body a message is about, before any symptom
// is attributed to the authenticated patient.
//
// FIXED: callAI in this codebase takes an OBJECT
// ({ system, message, temperature }) and returns a string — not a
// single prompt string. The previous version called callAI(prompt),
// which sent `undefined` as the system message and threw, so the AI
// layer silently never worked and every unmatched message fell
// through to the 'default' -> 'self' branch.
// ============================================

const DEPENDENT_PATTERNS = [
  { re: /\bmy\s+(?:baby|infant|newborn|new\s?born)\b/i,              relation: 'child',  ageGroup: 'infant' },
  { re: /\bmer[ae]\s+(?:bacch?[ae]|bachi|beta|beti)\b/i,             relation: 'child',  ageGroup: 'child' },
  { re: /\bmy\s+toddler\b/i,                                         relation: 'child',  ageGroup: 'toddler' },
  { re: /\bmy\s+(?:son|daughter|child|kid)\b/i,                      relation: 'child',  ageGroup: 'child' },
  { re: /\bmy\s+(?:teen|teenager|teenaged?\s+(?:son|daughter))\b/i,   relation: 'child',  ageGroup: 'adolescent' },
  { re: /\bmy\s+(?:mother|mom|mum|ammi|father|dad|abu|grand(?:mother|father|ma|pa))\b/i,
                                                                     relation: 'parent', ageGroup: 'older_adult' },
  { re: /\bmy\s+(?:husband|wife|spouse|partner|brother|sister|friend|cousin|aunt|uncle|nephew|niece)\b/i,
                                                                     relation: 'other',  ageGroup: 'adult' },
];

const SELF_PATTERNS = [
  /\bI\s+(?:have|am|ve|feel|felt|got|had|been|m)\b/i,
  /\bI'?(?:m|ve)\b/i,
  /\bmy\s+(?:own\s+)?(?:head|chest|stomach|belly|tummy|back|legs?|arms?|hands?|feet|throat|skin|joints?|eyes?|ears?|neck|knees?)\b/i,
  /\bmyself\b/i,
  /\b(?:hurts?|aches?|pains?)\s+me\b/i,
];

const THIRD_PERSON_PRONOUNS = /\b(?:he|she|him|her|his|hers|they|them|their|its)\b/i;

function ageGroupFromPhrase(message) {
  const months = message.match(/\b(\d{1,3})\s*(?:month|months|mo|mos)[\s-]*old\b/i);
  if (months) {
    const m = Number(months[1]);
    if (m <= 12) return 'infant';
    if (m <= 36) return 'toddler';
    return 'child';
  }
  const years = message.match(/\b(\d{1,3})\s*(?:year|years|yr|yrs)[\s-]*old\b/i);
  if (years) {
    const y = Number(years[1]);
    if (y < 1)  return 'infant';
    if (y < 3)  return 'toddler';
    if (y < 13) return 'child';
    if (y < 18) return 'adolescent';
    if (y < 65) return 'adult';
    return 'older_adult';
  }
  return null;
}

const AI_SUBJECT_SYSTEM =
`You classify WHO a health message is about. Reply with ONLY a JSON object. No prose, no markdown, no reasoning.
Schema: {"subject":"self"|"dependent"|"mixed","relation":"child"|"parent"|"other"|null,"ageGroup":"infant"|"toddler"|"child"|"adolescent"|"adult"|"older_adult"|null}
"self" = the sender's own body. "dependent" = another person's body. "mixed" = both in one message.`;

/**
 * Does this message contain any signal about WHOSE body is being discussed?
 *
 * Deliberately generous: any personal pronoun, possessive, or relation word
 * counts. If this returns false the message is something like "few days and
 * very severe" or "since monday" — there is no person in it to classify, so
 * no classifier can add information.
 *
 * Returning false too often is the safe direction: it means we inherit the
 * established subject rather than re-ask. Returning true too often just
 * costs an AI call, which is the behaviour we had before.
 */
function hasAnyPersonSignal(text) {
  return /\b(?:i|i'?m|im|me|my|mine|myself|we|our|he|him|his|she|her|hers|they|them|their|it|its|you|your|baby|infant|newborn|child|kid|son|daughter|toddler|mother|mom|mum|father|dad|wife|husband|brother|sister|parent|grand\w*|nephew|niece|cousin|aunt|uncle|friend|patient)\b/i.test(
    text
  );
}

/**
 * @param {string} message
 * @param {Function} callAIStructured - callAi.js's callAIStructured({ system, message, schema, temperature }).
 *   UPDATED: was callAI (plain text) with hand-rolled JSON.parse — switched
 *   to callAIStructured so a malformed/unparseable reply from one provider
 *   fails over to the next provider instead of silently giving up (see the
 *   note further down, at the call site, for the live bug this fixed).
 * @param {{previousSubject?:string, previousRelation?:string, previousAgeGroup?:string}} [opts]
 */
export async function detectSubject(message, callAIStructured, opts = {}) {
  const text = String(message || '');

  let dependentHit = null;
  for (const p of DEPENDENT_PATTERNS) {
    if (p.re.test(text)) { dependentHit = p; break; }
  }
  const selfHit = SELF_PATTERNS.some((re) => re.test(text));
  const agePhrase = ageGroupFromPhrase(text);

  if (dependentHit && selfHit) {
    return { subject: 'mixed', relation: dependentHit.relation,
             ageGroup: agePhrase || dependentHit.ageGroup, source: 'pattern', confidence: 0.9 };
  }
  if (dependentHit) {
    return { subject: 'dependent', relation: dependentHit.relation,
             ageGroup: agePhrase || dependentHit.ageGroup,
             source: agePhrase ? 'age_phrase' : 'pattern', confidence: 0.9 };
  }
  if (selfHit) {
    // NOTE: uses agePhrase now — see the doc comment above ageGroup's
    // definition for why this used to be hardcoded null even when the
    // patient stated their own age in the same message (e.g. "I'm 25
    // and I have a headache"). ageGroup is still NOT what determines a
    // self-subject's age for triage (that's always the real profile
    // age from getPatientProfile — see processMessage.js STAGE 5); this
    // is purely so a stated self age isn't silently discarded here,
    // e.g. for future features or debugging, and so self and dependent
    // detection are handled consistently.
    return { subject: 'self', relation: null, ageGroup: agePhrase || null, source: 'pattern', confidence: 0.9 };
  }

  // Pronoun-only follow-up inherits the previous turn's subject.
  if (opts.previousSubject === 'dependent' && THIRD_PERSON_PRONOUNS.test(text)) {
    return { subject: 'dependent', relation: opts.previousRelation || 'other',
             ageGroup: agePhrase || opts.previousAgeGroup || null,
             source: 'carryover', confidence: 0.6 };
  }

  // No person-signal at all, and the session already established one.
  //
  // This is the common case and it used to burn an AI call every time. In a
  // live 6-turn session, "few days and very severe" and "what do you think
  // is wrong with me" both went to the model, which returned `self` — the
  // subject the session had already established two turns earlier at higher
  // confidence. The result was a `pattern 0.9 → ai 0.7` flip-flop across a
  // session where the subject never actually changed.
  //
  // A message with no first-person, no third-person and no relation word
  // carries no subject information. There is nothing for the model to
  // classify, so asking it is pure cost and the answer it invents is less
  // reliable than what we already knew.
  if (!hasAnyPersonSignal(text) && opts.previousSubject) {
    return {
      subject: opts.previousSubject,
      relation: opts.previousRelation || null,
      ageGroup: agePhrase || opts.previousAgeGroup || null,
      // Reuses 'carryover' rather than a new 'inherited' label ON PURPOSE.
      //
      // `chat_messages.subject_source` has a check constraint that permits
      // only pattern | age_phrase | ai | carryover | default. A new value
      // would fail the INSERT, and a failed insert on this table is a
      // SILENT TOTAL LOSS of the audit row — that is the exact bug that
      // meant no record was written for a crisis message. Not worth a
      // schema migration for a nicer label.
      //
      // Distinguish the two carryover kinds by confidence: 0.6 is the
      // pronoun-follow-up above, 0.85 is this no-signal inheritance.
      source: 'carryover',
      confidence: 0.85,
    };
  }

  if (typeof callAIStructured === 'function') {
    try {
      // UPDATED: this used to call plain callAI (text) and hand-roll its
      // own JSON.parse(stripToJsonObject(raw)) — the ONLY call site in
      // this codebase that didn't go through callAIStructured. That
      // meant it missed two things every other AI-driven decision here
      // gets for free: (1) callAIStructured's schema-instruction appended
      // to the prompt (explicit "respond ONLY with valid JSON matching
      // this schema, no commentary" — AI_SUBJECT_SYSTEM's own "no prose"
      // line wasn't always enough on its own), and (2) provider fallback
      // ON A PARSE FAILURE specifically — callAI's cascade only retries
      // the next provider on a thrown error, but a hand-parsed JSON.parse
      // failure here was caught LOCALLY (the try/catch below), so it
      // never propagated back up for callAI to retry with Groq — it just
      // silently gave up and fell through to the low-confidence default,
      // even when Groq was configured and would have parsed fine. Live
      // logs showed exactly this: "[subjectDetection] AI classification
      // failed: Expected ',' or '}' after property value..." from a
      // malformed NVIDIA reply, with no fallback attempt at all.
      const json = await callAIStructured({
        system: AI_SUBJECT_SYSTEM,
        message: text,
        schema: { subject: 'self|dependent|mixed', relation: 'child|parent|other|null', ageGroup: 'infant|toddler|child|adolescent|adult|older_adult|null' },
        temperature: 0,
      });
      const subject = ['self', 'dependent', 'mixed'].includes(json.subject) ? json.subject : null;
      if (subject) {
        return {
          subject,
          relation: ['child', 'parent', 'other'].includes(json.relation) ? json.relation : null,
          ageGroup: agePhrase ||
            (['infant', 'toddler', 'child', 'adolescent', 'adult', 'older_adult'].includes(json.ageGroup)
              ? json.ageGroup : null),
          source: 'ai', confidence: 0.7,
        };
      }
    } catch (err) {
      console.error('[subjectDetection] AI classification failed:', err.message);
      // fall through to the safe default below
    }
  }

  return { subject: 'self', relation: null, ageGroup: null, source: 'default', confidence: 0.4 };
}

/**
 * Kept exported — generaterecommendation.js no longer needs it (it uses
 * callAIStructured now), but it's still useful anywhere a reasoning
 * model's raw text has to be coerced to JSON.
 */
export function stripToJsonObject(raw) {
  let s = String(raw || '');
  s = s.replace(/<think>[\s\S]*?<\/think>/gi, '');
  s = s.replace(/<think>[\s\S]*$/i, '');
  s = s.replace(/```(?:json)?/gi, '');
  const start = s.indexOf('{');
  const end = s.lastIndexOf('}');
  if (start === -1 || end === -1 || end <= start) throw new Error('No complete JSON object in response');
  return s.slice(start, end + 1);
}

// ------------------------------------------------------------------
// REMOVED: escalateForSubject / isPediatricSubject / candidatesForSubject
// / shouldUsePatientRecord / estimateAgeForGroup used to live here, for
// an architecture where a dependent's message was routed through this
// same pipeline using an ESTIMATED age from a rough ageGroup bucket.
// That architecture was explicitly abandoned (see processMessage.js's
// STAGE 4 comment) — a dependent or mixed self+dependent message is now
// declined outright, with no estimated age, no dependent-specific
// specialist pool, and no urgency escalation ever computed for one.
// Pediatric safety for the ONLY subject that still reaches
// recommendation (the authenticated patient, using their own real
// profile age) is handled by pediatricSafetyNet.js's
// enforcePediatricRouting instead. These functions had zero remaining
// call sites anywhere in the app — left in place they were actively
// misleading (implying dependent/pediatric routing logic lives here,
// when it doesn't), so they were deleted rather than kept as unused
// exports.
// ------------------------------------------------------------------