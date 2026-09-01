// ============================================
// SehatAI: Profile Facts
// The complete, authoritative set of facts the model is
// permitted to mention about the account holder.
//
// Each fact carries its CATEGORY. An earlier version of this file
// returned a flat list of bare strings — convenient for the verifier's
// membership check, and actively unsafe. The prompt received:
//
//     - Diabetes
//     - Penicillin
//     - Metformin
//     - Hypertension
//
// with no indication of what any of them were, so the model guessed and
// produced "current use of Metformin and Penicillin". Penicillin is an
// ALLERGY. Describing a drug a patient is allergic to as a drug they are
// taking is the single worst error this pipeline could make, and the
// grounding verifier passed it, because every term really was on file —
// only the relationship was invented.
//
// Categories are preserved for the prompt; factValues() flattens them
// back to bare strings for the verifier's membership check, so the two
// still cannot disagree about WHICH terms are on file.
// ============================================

import { supabase } from './supabaseClient.js';

/**
 * Human-readable labels used verbatim in the prompt. The model is
 * instructed to reuse this wording rather than invent a relationship.
 */
const CATEGORY_LABELS = {
  condition: 'Diagnosed condition',
  allergy: 'ALLERGY — patient must NOT receive this',
  medication: 'Currently taking',
  family_history: 'Family history (relative, not the patient)',
};

/**
 * @param {string} patientId
 * @returns {Promise<Array<{value: string, category: 'condition'|'allergy'|'medication'|'family_history'}>>}
 */
export async function getProfileFacts(patientId) {
  if (!patientId) return [];

  const [intake, meds] = await Promise.all([
    supabase
      .from('patient_intake_form')
      .select('existing_conditions, allergies, current_medications, family_history')
      .eq('patient_id', patientId)
      .maybeSingle(),
    supabase
      .from('medicines')
      .select('name')
      .eq('patient_id', patientId)
      .eq('active', true),
  ]);

  if (intake.error) console.error('[profileFact] intake lookup failed:', intake.error.message);
  if (meds.error) console.error('[profileFact] medicines lookup failed:', meds.error.message);

  const tagged = [
    ...(intake.data?.existing_conditions || []).map((v) => ({ value: v, category: 'condition' })),
    ...(intake.data?.allergies || []).map((v) => ({ value: v, category: 'allergy' })),
    ...(intake.data?.current_medications || []).map((v) => ({ value: v, category: 'medication' })),
    ...(intake.data?.family_history || []).map((v) => ({ value: v, category: 'family_history' })),
    ...((meds.data || []).map((m) => ({ value: m.name, category: 'medication' }))),
  ];

  // Deduplicate on value. If the same term arrives under two categories,
  // the FIRST wins by the order above — condition, then allergy, then
  // medication. Allergy deliberately outranks medication: if a term is
  // recorded as both, the safe reading is "do not give this".
  const seen = new Set();
  const facts = [];

  for (const fact of tagged) {
    const value = String(fact.value ?? '').trim();
    if (!value) continue;

    const key = value.toLowerCase();
    if (seen.has(key)) continue;

    seen.add(key);
    facts.push({ value, category: fact.category });
  }

  return facts;
}

/**
 * Flattens facts to bare values for the grounding verifier, which only
 * ever asks "is this exact term on file?".
 *
 * Accepts either shape so a caller still holding plain strings keeps
 * working.
 *
 * @param {Array<{value: string}|string>} facts
 * @returns {string[]}
 */
export function factValues(facts) {
  return (facts || [])
    .map((f) => (typeof f === 'string' ? f : f?.value))
    .filter(Boolean)
    .map((v) => String(v).trim());
}

/**
 * Renders facts as labeled lines for the prompt, grouped by category so
 * the model cannot conflate an allergy with a medication.
 *
 * @param {Array<{value: string, category: string}|string>} facts
 * @returns {string}
 */
export function formatProfileFacts(facts) {
  if (!facts || facts.length === 0) return '- none';

  const order = ['condition', 'allergy', 'medication', 'family_history'];
  const lines = [];

  for (const category of order) {
    const inCategory = (facts || []).filter(
      (f) => typeof f !== 'string' && f?.category === category
    );

    for (const fact of inCategory) {
      lines.push(`- ${CATEGORY_LABELS[category]}: ${fact.value}`);
    }
  }

  // Any bare strings (legacy callers) get listed without a claimed
  // relationship rather than being silently dropped.
  for (const fact of facts || []) {
    if (typeof fact === 'string' && fact.trim()) {
      lines.push(`- On file (relationship unspecified): ${fact.trim()}`);
    }
  }

  return lines.length ? lines.join('\n') : '- none';
}
