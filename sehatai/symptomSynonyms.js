// ============================================
// SehatAI: Symptom Wording Synonyms
//
// Small, curated list of common colloquial/clinical body-area synonym
// pairs, used ONLY as a deterministic backstop when matching a DENIAL
// or removal against an already-tracked symptom term whose wording
// shares no word in common with it — e.g. "I don't have stomach pain"
// trying to cancel a tracked "abdominal pain" entry. Demonstrated live:
// that exact denial silently failed to cancel the tracked symptom,
// because "stomach" and "abdominal" share zero letters/words, defeating
// the plain shared-significant-word heuristic in chatLog.js's
// findLikelySameSymptom and clarificationCheck.js's findLikelyKnownTerm.
//
// Deliberately small and curated, not an exhaustive medical thesaurus —
// both call sites above already try the model's own judgment first (the
// extraction/resolution prompts explicitly instruct reusing the known
// term's exact wording); this only covers the handful of everyday
// synonym pairs a patient is actually likely to type, as a safety net
// for when that instruction isn't followed precisely.
// ============================================

const SYNONYM_GROUPS = [
  ['stomach', 'abdomen', 'abdominal', 'belly', 'tummy'],
  ['urine', 'urinary', 'pee', 'wee'],
  ['throat', 'pharynx'],
  ['nose', 'nasal'],
  ['breathing', 'breath', 'breathless', 'respiratory'],
  ['tired', 'tiredness', 'fatigue', 'fatigued', 'exhausted', 'energy'],
  ['dizzy', 'dizziness', 'lightheaded', 'lightheadedness'],
  ['vomit', 'vomiting', 'puking', 'throwing'],
  ['poop', 'pooping', 'stool', 'bowel'],
  ['skin', 'dermal', 'cutaneous'],
  ['heart', 'cardiac'],
];

const WORD_TO_GROUP = new Map();
SYNONYM_GROUPS.forEach((group, i) => {
  for (const word of group) WORD_TO_GROUP.set(word, i);
});

/**
 * True if two (already-lowercased) words are the same word, or belong
 * to the same curated synonym group above.
 *
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
export function areSynonymWords(a, b) {
  if (a === b) return true;
  const ga = WORD_TO_GROUP.get(a);
  if (ga === undefined) return false;
  return ga === WORD_TO_GROUP.get(b);
}

/**
 * True if any word in `wordsA` is the same as, or a curated synonym of,
 * any word in `wordsB`.
 *
 * @param {string[]} wordsA
 * @param {string[]} wordsB
 * @returns {boolean}
 */
export function shareSynonymWord(wordsA, wordsB) {
  return wordsA.some((a) => wordsB.some((b) => areSynonymWords(a, b)));
}
