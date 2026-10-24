// ============================================
// HealthMate AI: Lab Value Lookup
// Takes the relatedTestNames from knowledgeGraphLookup.js and pulls
// the ACTUAL numbers from extracted_data for one patient — ground
// truth, straight from the relational DB, never paraphrased through
// an AI summary. Only returns the most recent result per test name,
// so an old resolved value doesn't get presented as current.
// ============================================

import { labNameVariants } from "./labNormalizer.js";
import { supabase } from "./supabaseClient.js";

/**
 * Normalizes a test name for comparison: lowercase, strip everything
 * that isn't a letter or digit. This is deliberately loose — "Blood
 * Pressure", "blood-pressure", and "BLOOD PRESSURE " all collapse to
 * the same key. It will NOT catch a genuine synonym ("CBC" vs.
 * "Complete Blood Count") — that needs an alias, not normalization,
 * and is a separate, deliberate non-goal here (see logUnmatchedLabTest
 * below for how those get surfaced instead of silently guessed at).
 *
 * @param {string} name
 * @returns {string}
 */
function normalizeTestName(name) {
  return String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

/**
 * Logs a test name Infermedica suggested that didn't match anything in
 * this patient's extracted_data, even after normalization. Console-only
 * for now — same review-and-fix-by-hand posture as matchSymptoms.js's
 * unmatched_queries table, just without a dedicated DB table yet. If
 * this starts showing up a lot, that's the signal to add one and start
 * building a real alias list instead of guessing.
 *
 * @param {string} testName
 * @param {string} patientId
 */
function logUnmatchedLabTest(testName, patientId) {
  console.warn(
    `[getlabvalues] No recorded result found for suggested test "${testName}" ` +
    `(patient ${patientId}) — either the patient has no result for it, or the ` +
    `name doesn't match what's stored in extracted_data even after normalization.`
  );
}

/**
 * Pure matching step (no database): given a patient's stored lab rows (newest first) and the test names Infermedica
 * suggested, returns the most recent stored row per test that counts as a match, plus the suggested names with no
 * match at all.
 *
 * A suggested name matches a stored row if the row's name equals, after loose normalisation, the name's canonical
 * form OR any known stored variant of it (labNormalizer.js: "Creatinine" also finds "Serum Creatinine",
 * "Hemoglobin" finds "Hb", ...). labNormalizer.js already existed with a curated Infermedica-name -> DB-name alias
 * map built for exactly the case loose matching cannot catch (a real synonym); it was never imported anywhere
 * until it was wired in here, and it only knew one stored spelling per test, hence the variants.
 *
 * @param {Array<{test_name:string}>} rows - newest first
 * @param {string[]} testNames
 * @returns {{rows: Array, unmatched: string[]}}
 */
export function selectRelevantLabRows(rows, testNames) {
  const variantsPerSuggestion = (testNames || []).map((n) => labNameVariants(n).map(normalizeTestName));
  const wanted = new Set(variantsPerSuggestion.flat());
  const mostRecentByTest = new Map();
  for (const row of rows || []) {
    // Keep only the most recent row per test_name: a patient may have several historical results for the same test.
    if (wanted.has(normalizeTestName(row.test_name)) && !mostRecentByTest.has(row.test_name)) {
      mostRecentByTest.set(row.test_name, row);
    }
  }
  const matchedNormalized = new Set(Array.from(mostRecentByTest.keys()).map(normalizeTestName));
  const unmatched = (testNames || []).filter((_, i) => !variantsPerSuggestion[i].some((v) => matchedNormalized.has(v)));
  return { rows: Array.from(mostRecentByTest.values()), unmatched };
}

/**
 * @param {string} patientId
 * @param {string[]} testNames - suggested test names, e.g. from Infermedica's
 *   /diagnosis response (see infermedicaClient.js's extractLabTests)
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
    // Nothing was suggested — return empty, don't fall back to pulling
    // the patient's whole lab history.
    return [];
  }

  // No .in("test_name", testNames) here on purpose — that's an exact
  // SQL match, and Infermedica's phrasing won't reliably line up with
  // whatever string is stored in extracted_data.test_name. Pull the
  // patient's lab rows and match normalized names in JS instead.
  const { data, error } = await supabase
    .from("extracted_data")
    .select("test_name, value, value_numeric, unit, normal_range, flag, recorded_at")
    .eq("patient_id", patientId)
    .order("recorded_at", { ascending: false });

  if (error) {
    throw new Error(`Lab value lookup failed: ${error.message}`);
  }

  if (!data || data.length === 0) {
    return [];
  }

  const { rows: matchedRows, unmatched } = selectRelevantLabRows(data, testNames);
  // Surface anything Infermedica suggested that genuinely has no match on file, so it is visible instead of
  // silently absent.
  for (const name of unmatched) logUnmatchedLabTest(name, patientId);

  return matchedRows.map((row) => ({
    testName: row.test_name,
    value: row.value,
    valueNumeric: row.value_numeric,
    unit: row.unit,
    normalRange: row.normal_range,
    flag: row.flag,
    recordedAt: row.recorded_at,
  }));
}