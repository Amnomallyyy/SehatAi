// ============================================
// SehatAI: Fake Demo Patient Data Seeder
//
// Populates Supabase with a handful of realistic, entirely-synthetic
// patients so `npm run chat` / `npm run web` / `npm run test:edge` have
// something real to pull from — profile facts, active medications, lab
// results (for the lab-value grounding path), a doctor's note, and diet
// preferences (for the DietBot side, same Supabase project).
//
// Idempotent by design: every row uses a FIXED, hardcoded UUID and is
// written with .upsert() keyed on that id — re-running this script
// after tweaking a value updates the existing rows in place instead of
// creating duplicates. Safe to run as many times as you want before
// the demo.
//
// This does NOT touch the knowledge-graph tables (symptoms, specialists,
// symptom_specialist_map, symptom_related_tests, symptom_combination_rules)
// — that's seedsymptomknowledgegraph.js's job, a separate concern. This
// script is purely patient-side data.
//
// Usage:
//   npm run seed:data
//   (or: node seedfakedata.js)
// ============================================

import { supabase } from './supabaseClient.js';

// ------------------------------------------------------------------
// Fixed IDs — hardcoded (not crypto.randomUUID()) so every rerun
// upserts the SAME rows instead of creating new ones each time.
// ------------------------------------------------------------------
const PATIENT_1 = 'a1000000-0000-4000-8000-000000000001'; // Ayesha Khan — diabetes + hypertension, good lab/med/diet coverage
const PATIENT_2 = 'a2000000-0000-4000-8000-000000000002'; // Bilal Ahmed — asthma, different specialist path
const PATIENT_3 = 'a3000000-0000-4000-8000-000000000003'; // Zara Sheikh — deliberately EMPTY (no meds/labs/diet prefs)

const DOC_1 = 'b1000000-0000-4000-8000-000000000001';
const DOC_2 = 'b2000000-0000-4000-8000-000000000002';

const MED_1A = 'c1000000-0000-4000-8000-000000000001';
const MED_1B = 'c1000000-0000-4000-8000-000000000002';
const MED_2A = 'c2000000-0000-4000-8000-000000000001';

const LAB_1A = 'd1000000-0000-4000-8000-000000000001';
const LAB_1B = 'd1000000-0000-4000-8000-000000000002';
const LAB_1C = 'd1000000-0000-4000-8000-000000000003';
const LAB_2A = 'd2000000-0000-4000-8000-000000000001';

const ADVICE_1 = 'e1000000-0000-4000-8000-000000000001';
const ADVICE_2 = 'e2000000-0000-4000-8000-000000000002';

const SUMMARY_1 = 'f1000000-0000-4000-8000-000000000001';
const SUMMARY_2 = 'f2000000-0000-4000-8000-000000000002';

const today = new Date();
const daysAgo = (n) => new Date(today.getTime() - n * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);

async function upsert(table, rows, label) {
  const { error } = await supabase.from(table).upsert(rows);
  if (error) {
    console.error(`✗ ${label} failed:`, error.message);
    return false;
  }
  console.log(`✓ ${label} (${rows.length} row${rows.length === 1 ? '' : 's'})`);
  return true;
}

async function seedClinicalAdviceEmbedding(id, patientId, content) {
  // Best-effort: populates summaries_vectors so getPatientHistory()'s
  // RAG search has something to actually retrieve for this patient.
  // Skipped gracefully (not fatal to the rest of the seed) if the
  // embedding provider isn't configured — see embeddingProvider.js.
  try {
    const { embedText, isFakeMode } = await import('./embeddingProvider.js');
    const embedding = await embedText(content, 1024);
    if (isFakeMode()) {
      console.warn('  (embeddingProvider is in FAKE mode — summaries_vectors row will not be semantically searchable, but is safe to insert)');
    }
    await upsert(
      'summaries_vectors',
      [{ id, patient_id: patientId, source_id: id, source_type: 'clinical_advice', content, embedding }],
      '  summaries_vectors row for clinical_advice'
    );
  } catch (err) {
    console.warn(`  (skipped summaries_vectors embedding — ${err.message})`);
  }
}

async function main() {
  console.log('Seeding fake demo patient data into Supabase...\n');

  // ================================================================
  // PATIENT 1 — Ayesha Khan: diabetes + hypertension. Full coverage:
  // active meds, intake form, a lab report with abnormal flags, a
  // doctor's note, diet preferences. This is the patient to use for
  // demoing the lab-value grounding path and the diet bot together.
  // ================================================================
  await upsert('patients', [{
    id: PATIENT_1,
    name: 'Ayesha Khan',
    date_of_birth: '1991-03-14',
    age: 34,
    sex: 'female',
    consented_at: new Date().toISOString(),
  }], 'patients: Ayesha Khan');

  await upsert('patient_intake_form', [{
    patient_id: PATIENT_1,
    existing_conditions: ['Type 2 Diabetes', 'Hypertension'],
    allergies: ['Penicillin'],
    current_medications: ['Metformin', 'Atorvastatin'],
    family_history: ['Heart disease (father)'],
    updated_at: new Date().toISOString(),
  }], 'patient_intake_form: Ayesha Khan');

  await upsert('medicines', [
    { id: MED_1A, patient_id: PATIENT_1, name: 'Metformin', dosage: '500mg twice daily', start_date: daysAgo(400), active: true, recorded_at: new Date().toISOString() },
    { id: MED_1B, patient_id: PATIENT_1, name: 'Atorvastatin', dosage: '10mg once daily', start_date: daysAgo(200), active: true, recorded_at: new Date().toISOString() },
  ], 'medicines: Ayesha Khan');

  await upsert('documents', [{
    id: DOC_1,
    patient_id: PATIENT_1,
    category: 'lab_report',
    status: 'structured',
    doctor_reviewed: true,
    original_filename: 'ayesha_khan_labs_2026.pdf',
    document_date: daysAgo(10),
    uploaded_at: new Date().toISOString(),
  }], 'documents: Ayesha Khan lab report');

  await upsert('extracted_data', [
    { id: LAB_1A, document_id: DOC_1, patient_id: PATIENT_1, test_name: 'HbA1c', value: '7.2', value_numeric: 7.2, unit: '%', normal_range: '4.0-5.6', flag: 'high', document_date: daysAgo(10), recorded_at: daysAgo(10) },
    { id: LAB_1B, document_id: DOC_1, patient_id: PATIENT_1, test_name: 'Fasting Glucose', value: '130', value_numeric: 130, unit: 'mg/dL', normal_range: '70-99', flag: 'high', document_date: daysAgo(10), recorded_at: daysAgo(10) },
    { id: LAB_1C, document_id: DOC_1, patient_id: PATIENT_1, test_name: 'LDL Cholesterol', value: '145', value_numeric: 145, unit: 'mg/dL', normal_range: '<100', flag: 'high', document_date: daysAgo(10), recorded_at: daysAgo(10) },
  ], 'extracted_data: Ayesha Khan lab values');

  const advice1 = 'Continue Metformin and Atorvastatin as prescribed. Monitor fasting glucose weekly. ' +
    'Advised a low-carb, low-sodium diet given hypertension. Re-check HbA1c in 3 months.';
  await upsert('clinical_advice', [{
    id: ADVICE_1, patient_id: PATIENT_1, content: advice1, origin: 'entered', document_date: daysAgo(10), recorded_at: daysAgo(10),
  }], 'clinical_advice: Ayesha Khan');
  await seedClinicalAdviceEmbedding(SUMMARY_1, PATIENT_1, advice1);

  await upsert('diet_patient_preferences', [{
    patient_id: PATIENT_1,
    dietary_restrictions: ['low-carb', 'low-sodium'],
    food_allergies: [],
    disliked_foods: ['broccoli'],
    favorite_foods: ['chicken', 'rice', 'lentils'],
    updated_at: new Date().toISOString(),
  }], 'diet_patient_preferences: Ayesha Khan');

  // ================================================================
  // PATIENT 2 — Bilal Ahmed: asthma. Different specialist path than
  // Patient 1, useful for showing the pipeline isn't hardcoded to one
  // condition. Lighter data set (one med, one lab-ish result, one
  // note, vegetarian diet preferences).
  // ================================================================
  await upsert('patients', [{
    id: PATIENT_2,
    name: 'Bilal Ahmed',
    date_of_birth: '1980-07-22',
    age: 45,
    sex: 'male',
    consented_at: new Date().toISOString(),
  }], 'patients: Bilal Ahmed');

  await upsert('patient_intake_form', [{
    patient_id: PATIENT_2,
    existing_conditions: ['Asthma'],
    allergies: ['Dust', 'Pollen'],
    current_medications: ['Salbutamol inhaler'],
    family_history: [],
    updated_at: new Date().toISOString(),
  }], 'patient_intake_form: Bilal Ahmed');

  await upsert('medicines', [
    { id: MED_2A, patient_id: PATIENT_2, name: 'Salbutamol', dosage: 'inhaler, as needed', start_date: daysAgo(600), active: true, recorded_at: new Date().toISOString() },
  ], 'medicines: Bilal Ahmed');

  await upsert('documents', [{
    id: DOC_2,
    patient_id: PATIENT_2,
    category: 'lab_report',
    status: 'structured',
    doctor_reviewed: true,
    original_filename: 'bilal_ahmed_spirometry_2026.pdf',
    document_date: daysAgo(30),
    uploaded_at: new Date().toISOString(),
  }], 'documents: Bilal Ahmed spirometry report');

  await upsert('extracted_data', [
    { id: LAB_2A, document_id: DOC_2, patient_id: PATIENT_2, test_name: 'FEV1', value: '78', value_numeric: 78, unit: '% predicted', normal_range: '>80', flag: 'low', document_date: daysAgo(30), recorded_at: daysAgo(30) },
  ], 'extracted_data: Bilal Ahmed spirometry');

  const advice2 = 'Continue Salbutamol inhaler as needed for symptom flare-ups. Avoid known triggers (dust, pollen). ' +
    'Follow up if rescue inhaler use exceeds twice a week.';
  await upsert('clinical_advice', [{
    id: ADVICE_2, patient_id: PATIENT_2, content: advice2, origin: 'entered', document_date: daysAgo(30), recorded_at: daysAgo(30),
  }], 'clinical_advice: Bilal Ahmed');
  await seedClinicalAdviceEmbedding(SUMMARY_2, PATIENT_2, advice2);

  await upsert('diet_patient_preferences', [{
    patient_id: PATIENT_2,
    dietary_restrictions: ['vegetarian'],
    food_allergies: [],
    disliked_foods: ['dairy'],
    favorite_foods: ['lentils', 'salad', 'grilled vegetables'],
    updated_at: new Date().toISOString(),
  }], 'diet_patient_preferences: Bilal Ahmed');

  // ================================================================
  // PATIENT 3 — Zara Sheikh: deliberately EMPTY beyond the bare
  // patients row. Useful for demoing/confirming the "no data on file"
  // fallback path (generic recommendation, no lab values, DietBot
  // returns broader/less personalized advice per retrieval.py) rather
  // than only ever showing off the fully-populated case.
  // ================================================================
  await upsert('patients', [{
    id: PATIENT_3,
    name: 'Zara Sheikh',
    date_of_birth: '1998-11-02',
    age: 27,
    sex: 'female',
    consented_at: new Date().toISOString(),
  }], 'patients: Zara Sheikh (intentionally no other data)');

  console.log('\nDone. Demo patient IDs:\n');
  console.log(`  Ayesha Khan (diabetes + hypertension, full data): ${PATIENT_1}`);
  console.log(`  Bilal Ahmed (asthma, lighter data):               ${PATIENT_2}`);
  console.log(`  Zara Sheikh (empty — tests the no-data fallback): ${PATIENT_3}`);
  console.log('\nPaste one of these into testChat.js\'s DEFAULT_PATIENT_ID, webServer.js\'s');
  console.log('DEFAULT_PATIENT_ID, or testEdgeCases.js\'s TEST_PATIENT_ID — or pass Ayesha\'s');
  console.log('or Bilal\'s ID as an argument: `node testChat.js ' + PATIENT_1 + '`');
  console.log('or type it into the "Patient ID" field in the browser demo UI (`npm run web`).');
}

main().catch((err) => {
  console.error('Fatal error while seeding:', err);
  process.exit(1);
});