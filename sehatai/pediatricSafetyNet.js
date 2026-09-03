// ============================================
// HealthMate AI: Pediatric Safety Net
// A CHECK, not a generator. Infermedica receives `age` on every call
// and very likely already weights /recommend_specialist toward
// Pediatrician for minors on its own — this does NOT try to duplicate
// that logic. It only catches the case where Infermedica's specialist
// list did NOT include a pediatrician for a patient under 18, and
// overrides in that case only.
//
// Deliberately a single hardcoded function, not a rule table — same
// reasoning as the old (now-discarded) applyPediatricOverride() in
// knowledgegraphlookup.js: this is a small, always-correct,
// no-clinical-judgment-needed case, cheaper and safer as plain code
// than as rows in a general-purpose override table.
//
// Every time this actually overrides something, it logs — that log is
// the signal for whether Infermedica needs this safety net at all, or
// whether it can eventually be removed.
// ============================================

const PEDIATRIC_AGE_CUTOFF = 18;

/**
 * @param {string[]} specialists - specialist names from Infermedica's
 *   getRecommendedSpecialist() response (see infermedicaClient.js)
 * @param {number|null} age - the account holder's own age. Always the
 *   account holder's: processMessage.js's STAGE 4 subject detection
 *   declines any dependent or mixed-subject message outright, before a
 *   recommendation is ever generated — see subjectDetection.js — so a
 *   dependent's age never reaches this far.
 * @returns {string[]} - possibly-overridden specialist list
 */
export function enforcePediatricRouting(specialists, age) {
  if (typeof age !== 'number' || Number.isNaN(age) || age >= PEDIATRIC_AGE_CUTOFF) {
    return specialists; // not a minor, no override needed
  }

  const list = specialists && specialists.length > 0 ? specialists : [];
  const hasPediatrician = list.some((s) => String(s).toLowerCase().includes('pediatric'));

  if (hasPediatrician) {
    return specialists; // Infermedica already got it right — nothing to do
  }

  console.warn(
    `[pediatricSafetyNet] Patient is a minor (age ${age}) but Infermedica's specialist ` +
    `list did not include a pediatrician: [${list.join(', ') || 'none'}]. Overriding to Pediatrician.`
  );

  return ['Pediatrician'];
}
