

// ============================================
// HealthMate AI: Patient Profile Lookup
// Fetches structured facts used as grounded AI context (see
// specialistrecommendation.js) and the pediatric override (see
// applyPediatricOverride in knowledgegraphlookup.js).
//
// Reads from:
//   - patients (date_of_birth -> age computed on read, sex)
//   - medicines (active = true only) — structured/prescribed meds
//   - patient_intake_form — ONE row per patient, holding the
//     patient's self-reported existing_conditions, allergies,
//     current_medications, and family_history as text[] columns.
//     There are no separate patient_conditions / patient_allergies /
//     patient_family_history tables in this schema — don't recreate
//     them; everything self-reported lives on this one row.
//
// This module ONLY reads facts — it does no reasoning and makes no
// specialist decisions. This is DIFFERENT from getPatientdata.js,
// which does semantic RAG search over unstructured summaries_vectors
// content — this file reads flat, structured, already-true-or-false
// fields.
// ============================================

import { createClient } from "@supabase/supabase-js";
import "dotenv/config";

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);

/**
 * Computes age in whole years from a date_of_birth string. Deliberately
 * NOT read from patients.age — that column goes stale the moment a
 * birthday passes; computing it from date_of_birth on read never does.
 *
 * @param {string|null} dateOfBirth - ISO date string, e.g. "1994-03-12"
 * @returns {number|null}
 */
function computeAge(dateOfBirth) {
  if (!dateOfBirth) return null;

  const dob = new Date(dateOfBirth);
  if (isNaN(dob.getTime())) return null;

  const today = new Date();
  let age = today.getFullYear() - dob.getFullYear();
  const hasHadBirthdayThisYear =
    today.getMonth() > dob.getMonth() ||
    (today.getMonth() === dob.getMonth() && today.getDate() >= dob.getDate());
  if (!hasHadBirthdayThisYear) age -= 1;

  return age;
}

/**
 * @param {string} patientId
 * @returns {Promise<{
 *   age: number|null,
 *   sex: string|null,
 *   existingConditions: string[],
 *   allergies: string[],
 *   currentMedications: string[],
 *   familyHistory: string[]
 * }>}
 */
export async function getPatientProfile(patientId) {
  const [patientResult, medsResult, intakeResult] = await Promise.all([
    supabase.from("patients").select("date_of_birth, sex").eq("id", patientId).single(),
    supabase.from("medicines").select("name").eq("patient_id", patientId).eq("active", true),
    supabase
      .from("patient_intake_form")
      .select("existing_conditions, allergies, current_medications, family_history")
      .eq("patient_id", patientId)
      .single(),
  ]);

  if (patientResult.error) {
    console.error(`Patient profile lookup failed: ${patientResult.error.message}`);
  }
  if (medsResult.error) {
    console.error(`Active medications lookup failed: ${medsResult.error.message}`);
  }
  if (intakeResult.error) {
    // PGRST116 = "no rows found" from .single() — a patient who simply
    // hasn't filled out an intake form yet is normal, not an error.
    // Only log genuine failures (bad connection, RLS, etc.).
    if (intakeResult.error.code !== "PGRST116") {
      console.error(`Patient intake form lookup failed: ${intakeResult.error.message}`);
    }
  }

  const intake = intakeResult.data || {};

  // currentMedications merges BOTH sources: the structured `medicines`
  // table (prescribed/document-extracted, authoritative) and whatever
  // the patient self-reported on their intake form — deduped so
  // neither source silently overrides the other.
  const medsFromTable = (medsResult.data || []).map((m) => m.name);
  const medsFromIntake = intake.current_medications || [];
  const currentMedications = [...new Set([...medsFromTable, ...medsFromIntake])];

  return {
    age: computeAge(patientResult.data?.date_of_birth ?? null),
    sex: patientResult.data?.sex ?? null,
    currentMedications,
    existingConditions: intake.existing_conditions || [],
    allergies: intake.allergies || [],
    familyHistory: intake.family_history || [],
  };
}