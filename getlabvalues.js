// ============================================
// HealthMate AI: Lab Value Lookup
// Takes the relatedTestNames from knowledgeGraphLookup.js and pulls
// the ACTUAL numbers from extracted_data for one patient — ground
// truth, straight from the relational DB, never paraphrased through
// an AI summary. Only returns the most recent result per test name,
// so an old resolved value doesn't get presented as current.
// ============================================

import { createClient } from "@supabase/supabase-js";
import "dotenv/config";

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);

/**
 * @param {string} patientId
 * @param {string[]} testNames - from getRelatedTestNames() / getKnowledgeGraphContext()
 * @returns {Promise<Array<{
 *   testName: string,
 *   value: string,
 *   valueNumeric: number|null,
 *   unit: string|null,
 *   normalRange: string|null,
 *   flag: string|null,
 *   recordedAt: string
 * }>>}
 */
export async function getRelevantLabValues(patientId, testNames) {
  if (!testNames || testNames.length === 0) {
    // Knowledge graph had nothing relevant to look up — return empty,
    // don't fall back to pulling the patient's whole lab history.
    return [];
  }

  const { data, error } = await supabase
    .from("extracted_data")
    .select("test_name, value, value_numeric, unit, normal_range, flag, recorded_at")
    .eq("patient_id", patientId)
    .in("test_name", testNames)
    .order("recorded_at", { ascending: false });

  if (error) {
    throw new Error(`Lab value lookup failed: ${error.message}`);
  }

  if (!data || data.length === 0) {
    return [];
  }

  // Keep only the most recent row per test_name — a patient may have
  // multiple historical results for the same test, and only the
  // latest one should inform a current recommendation.
  const mostRecentByTest = new Map();
  for (const row of data) {
    if (!mostRecentByTest.has(row.test_name)) {
      mostRecentByTest.set(row.test_name, row);
    }
  }

  return Array.from(mostRecentByTest.values()).map((row) => ({
    testName: row.test_name,
    value: row.value,
    valueNumeric: row.value_numeric,
    unit: row.unit,
    normalRange: row.normal_range,
    flag: row.flag,
    recordedAt: row.recorded_at,
  }));
}