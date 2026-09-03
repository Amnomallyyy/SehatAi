// ============================================
// HealthMate AI: Lab Value Lookup
// Takes the relatedTestNames from knowledgeGraphLookup.js and pulls
// the ACTUAL numbers from extracted_data for one patient — ground
// truth, straight from the relational DB, never paraphrased through
// an AI summary. Only returns the most recent result per test name,
// so an old resolved value doesn't get presented as current.
// ============================================

import { normalizeLabNames } from "./labNormalizer.js";
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

  // FOUND: labNormalizer.js already existed with a real, curated
  // Infermedica-name -> DB-name alias map (e.g. "A1C"/"glycated
  // hemoglobin" -> "HbA1c") — built specifically for the case
  // normalizeTestName's loose lowercase/strip-punctuation matching can't
  // catch (a genuine synonym, not just a formatting difference), but it
  // was never actually imported anywhere. Apply it first so Infermedica
  // suggesting "A1C" correctly matches a DB row stored as "HbA1c",
  // THEN still run normalizeTestName on top for casing/punctuation
  // variance in whatever the alias map (or an unmapped name) returns.
  const aliasedTestNames = normalizeLabNames(testNames);
  const requestedNormalized = new Set(aliasedTestNames.map(normalizeTestName));

  const matchingRows = data.filter((row) =>
    requestedNormalized.has(normalizeTestName(row.test_name))
  );

  // Keep only the most recent row per test_name — a patient may have
  // multiple historical results for the same test, and only the
  // latest one should inform a current recommendation.
  const mostRecentByTest = new Map();
  for (const row of matchingRows) {
    if (!mostRecentByTest.has(row.test_name)) {
      mostRecentByTest.set(row.test_name, row);
    }
  }

  // Surface anything Infermedica suggested that genuinely has no match
  // on file, so it's visible instead of silently absent.
  const matchedNormalized = new Set(
    Array.from(mostRecentByTest.keys()).map(normalizeTestName)
  );
  for (let i = 0; i < testNames.length; i++) {
    // Check against the ALIASED name (what was actually looked up), not
    // the raw one — otherwise a genuine match found via labNormalizer
    // (e.g. Infermedica said "A1C", matched a DB row stored as "HbA1c")
    // would still get logged as unmatched here, since "a1c" and "hba1c"
    // normalize to different strings.
    if (!matchedNormalized.has(normalizeTestName(aliasedTestNames[i]))) {
      logUnmatchedLabTest(testNames[i], patientId);
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