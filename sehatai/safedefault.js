// ============================================
// SehatAI: Safe Default Response
// ============================================

export const SAFE_DEFAULT_RESPONSE = {
  specialist_recommended: 'General Physician',
  rationale:
    "I can't give you a specific specialist recommendation for this with confidence. " +
    'A General Physician is the right first step — they can assess the symptoms directly ' +
    'and refer you onward if needed.',
  next_steps:
    'Book an appointment with a General Physician. Bring any recent test reports with you.',
  referenced_lab_tests: [],
  referenced_profile_facts: [],
  urgency: 'routine',
  source: 'safe_default',
};

/**
 * Builds a safe fallback recommendation that's actually grounded
 * against whatever specialist candidates ARE available this round,
 * instead of blindly hardcoding "General Physician" — real, demonstrated
 * bug this fixes: when Infermedica's own candidate list narrows to
 * something that does NOT include General Physician (e.g. just
 * ["Ophthalmologist"] for eye pain), the plain SAFE_DEFAULT_RESPONSE
 * above named a specialist that isn't even a valid candidate for this
 * round — which then failed this app's OWN grounding verifier a SECOND
 * time (specialist_not_in_graph + ungrounded_specialist_in_prose), with
 * no recovery path, since a fallback-of-a-fallback never existed. The
 * "safe" default wasn't actually safe: ungrounded, and delivered anyway
 * because nothing re-verified it. Picking the first real candidate here
 * means the safe default is always genuinely grounded — it just isn't
 * confident enough to have generated a full write-up via the model, not
 * that it has nothing valid to point to.
 *
 * @param {Array<string|{name:string}>} [allowedSpecialists]
 * @returns {typeof SAFE_DEFAULT_RESPONSE}
 */
export function buildSafeDefault(allowedSpecialists = []) {
  const names = (allowedSpecialists || [])
    .map((s) => (typeof s === 'string' ? s : s?.name))
    .filter(Boolean);
  if (names.length === 0) return SAFE_DEFAULT_RESPONSE;

  const pick = names[0];
  const article = /^[aeiou]/i.test(pick) ? 'an' : 'a';
  return {
    specialist_recommended: pick,
    rationale:
      "I can't give you a specific recommendation for this with full confidence, but based on what's " +
      `been reported, ${article} ${pick} is the most relevant type of specialist to start with.`,
    next_steps: `Schedule an appointment with ${article} ${pick}. Bring any recent test reports with you.`,
    referenced_lab_tests: [],
    referenced_profile_facts: [],
    urgency: 'routine',
    source: 'safe_default',
  };
}