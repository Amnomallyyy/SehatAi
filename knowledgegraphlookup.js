// ============================================
// HealthMate AI: Knowledge Graph Lookup
// Takes matched symptom_id(s) from matchSymptoms.js and queries the
// two knowledge graph edge tables:
//   - symptom_specialist_map  -> candidate specialists + urgency
//   - symptom_related_tests   -> which extracted_data.test_name values matter
//
// This is a pure lookup layer — no AI involved, nothing invented.
// If a matched symptom has no row in either table, that's a real gap
// in the graph (a doctor hasn't mapped it yet), not something this
// code should paper over by guessing.
// ============================================

import { createClient } from "@supabase/supabase-js";
import "dotenv/config";

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);

const URGENCY_RANK = { routine: 1, urgent: 2, emergency: 3 };

/**
 * Given matched symptom rows, returns every specialist the knowledge
 * graph connects them to, plus the single highest urgency level found
 * across all of them (mirrors the ranking logic in matchSymptoms.js's
 * getHighestUrgency, kept independent since this file has its own
 * distinct job: building the candidate list for generation, not
 * deciding emergency status).
 *
 * @param {Array<{id: string, name: string}>} matchedSymptoms
 * @returns {Promise<{
 *   candidateSpecialists: Array<{id: string, name: string, description: string}>,
 *   urgency: string|null
 * }>}
 */
export async function getSpecialistsForSymptoms(matchedSymptoms) {
  if (!matchedSymptoms || matchedSymptoms.length === 0) {
    return { candidateSpecialists: [], urgency: null };
  }

  const symptomIds = matchedSymptoms.map((s) => s.id);

  const { data, error } = await supabase
    .from("symptom_specialist_map")
    .select(`
      urgency_level,
      specialists ( id, name, description )
    `)
    .in("symptom_id", symptomIds);

  if (error) {
    throw new Error(`Specialist lookup failed: ${error.message}`);
  }

  if (!data || data.length === 0) {
    // No mapping exists for any matched symptom — the caller should
    // treat this the same as "no match" and fall back to the safe
    // default (General Physician), not invent a specialist here.
    return { candidateSpecialists: [], urgency: null };
  }

  // Dedupe specialists (a symptom could map to the same specialist
  // via more than one row, or two matched symptoms could share one).
  const seen = new Map();
  for (const row of data) {
    if (row.specialists && !seen.has(row.specialists.id)) {
      seen.set(row.specialists.id, row.specialists);
    }
  }

  const highestUrgencyRow = data.reduce((max, row) =>
    URGENCY_RANK[row.urgency_level] > URGENCY_RANK[max.urgency_level] ? row : max
  );

  return {
    candidateSpecialists: Array.from(seen.values()),
    urgency: highestUrgencyRow.urgency_level,
  };
}

/**
 * Given matched symptom rows, returns the deduped list of lab test
 * names relevant to them — used to filter extracted_data down to
 * only what's actually relevant, rather than pulling a patient's
 * entire lab history into context.
 *
 * @param {Array<{id: string, name: string}>} matchedSymptoms
 * @returns {Promise<string[]>}
 */
export async function getRelatedTestNames(matchedSymptoms) {
  if (!matchedSymptoms || matchedSymptoms.length === 0) {
    return [];
  }

  const symptomIds = matchedSymptoms.map((s) => s.id);

  const { data, error } = await supabase
    .from("symptom_related_tests")
    .select("test_name")
    .in("symptom_id", symptomIds);

  if (error) {
    throw new Error(`Related test lookup failed: ${error.message}`);
  }

  if (!data || data.length === 0) {
    return [];
  }

  return [...new Set(data.map((row) => row.test_name))];
}

/**
 * Convenience wrapper — runs both lookups together, since they're
 * always needed at the same point in the pipeline (right after
 * matchSymptoms resolves).
 *
 * @param {Array<{id: string, name: string}>} matchedSymptoms
 */
export async function getKnowledgeGraphContext(matchedSymptoms) {
  const [specialistResult, relatedTestNames] = await Promise.all([
    getSpecialistsForSymptoms(matchedSymptoms),
    getRelatedTestNames(matchedSymptoms),
  ]);

  return {
    candidateSpecialists: specialistResult.candidateSpecialists,
    urgency: specialistResult.urgency,
    relatedTestNames,
  };
}