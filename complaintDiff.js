// ============================================
// SehatAI: Unaccounted Complaint Detection
// Infermedica's /parse only returns mentions it recognized. Anything
// the patient said that didn't map to a finding just silently
// disappears from `evidence` — the model never sees it, and the
// patient is never told it wasn't understood. This is the same shape
// of bug flagged against the old local symptom-graph matcher
// (unaccountedComplaints not reaching the envelope), just moved to a
// new source now that /parse is the thing doing the matching.
//
// This is deliberately a coarse heuristic, not NLP: split the raw
// message into clause-sized chunks, and treat a chunk as "accounted
// for" if any of /parse's mentions' orig_text overlaps with it.
// Anything left over is reported as unaccounted — surfaced to the
// patient via generaterecommendation.js's unrecognised_complaints
// framing, and available for manual review the same way
// getlabvalues.js's logUnmatchedLabTest() surfaces unmatched test
// names: console-only for now, promote to a real table only if this
// turns out to fire a lot.
// ============================================

// Duration/severity language and pure connective filler isn't a
// complaint on its own — don't flag "since yesterday" or "and" as an
// unaccounted complaint just because /parse didn't tag it with a
// finding id.
const FILLER_ONLY_PATTERNS = [
  /^\s*(and|also|plus|too|as well)\s*$/i,
  /\b(day|days|week|weeks|hour|hours|month|months|since|ago|started|this morning|last night|yesterday|today|few|couple|several)\b/i,
  /\b(mild|moderate|severe|bad|terrible|unbearable|worst|awful|slight|really|very)\b/i,
  // Bare answers to a yes/no clarifying question ("Yes", "No", "Yeah") —
  // not a complaint, just confirming/denying a suggested finding. See
  // interpretPendingAnswer() in infermedicaClient.js, which is what
  // actually turns a real "yes" into evidence; this just stops the
  // SAME word from also being misfiled as an unrecognized complaint.
  /^\s*(yes|yeah|yep|yup|no|nope|nah|sure|correct|right|exactly|ok|okay|definitely|absolutely|not really)\s*\.?\s*$/i,
  // Generic self-report framing ("I have been experiencing this",
  // "it has been going on") that restates THAT something is happening
  // without naming what — the actual symptom name lives in a
  // different clause (or was already accounted for), so this framing
  // alone isn't a new, separate complaint.
  /\b(i\s*(?:'ve|have|had)?\s*been\s+(?:experiencing|feeling|having|noticing)|i\s*(?:'ve|have|had)|this\s+(?:has\s+been|is|has)|it\s+(?:has\s+been|is)|going\s+on)\b/i,
  /\b(this|that|it)\b/i,
  // Generic connector/stopwords left behind once the specific patterns
  // above have removed the actual filler content — e.g. "for a" after
  // "few days" is stripped, or "with" after a duration phrase. These
  // only matter for deciding emptiness: a clause with real symptom
  // words in it won't become empty just because its stopwords are
  // also stripped.
  /\b(for|a|an|the|of|to|in|on|at|with|about|been|be|is|are|was|were|has|have|had|i|my|me)\b/i,
];

function normalize(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function splitIntoClauses(message) {
  return String(message || '')
    .split(/,|;|\band\b|\&/i)
    .map((c) => c.trim())
    .filter(Boolean);
}

function isFillerOnly(clause) {
  const n = normalize(clause);
  if (!n) return true;
  // A clause that's ONLY duration/severity language (no other content
  // words left after stripping those patterns) isn't a complaint.
  let stripped = n;
  for (const pattern of FILLER_ONLY_PATTERNS) {
    stripped = stripped.replace(new RegExp(pattern.source, 'gi'), ' ');
  }
  // Bare numbers left behind by stripping "3 days ago" -> "3" aren't a
  // complaint on their own — strip those too before deciding.
  stripped = stripped.replace(/\b\d+\b/g, ' ').replace(/\s+/g, ' ').trim();
  return stripped.length === 0;
}

function isCovered(clause, mentionTexts) {
  const n = normalize(clause);
  if (!n) return true;
  return mentionTexts.some((mentionText) => {
    const m = normalize(mentionText);
    if (!m) return false;
    // Either direction: a short mention inside a longer clause, or a
    // clause that IS basically the mention text.
    return n.includes(m) || m.includes(n);
  });
}

/**
 * @param {string} message - the patient's raw message for this turn
 * @param {Array<{name?:string, orig_text?:string, text?:string}>} mentions
 *   - Infermedica /parse's mentions array for THIS turn only (not
 *     accumulated evidence — orig_text only exists on the fresh parse
 *     response, not on merged evidence items)
 * @returns {string[]} - clause-level complaints that didn't match any
 *   recognized mention, trimmed and deduped
 */
export function extractUnaccountedComplaints(message, mentions = []) {
  const mentionTexts = mentions
    .map((m) => m.orig_text || m.text || m.name)
    .filter(Boolean);

  const clauses = splitIntoClauses(message);
  const unaccounted = [];
  const seen = new Set();

  for (const clause of clauses) {
    if (isFillerOnly(clause)) continue;
    if (isCovered(clause, mentionTexts)) continue;

    const key = normalize(clause);
    if (seen.has(key)) continue;
    seen.add(key);
    unaccounted.push(clause);
  }

  return unaccounted;
}