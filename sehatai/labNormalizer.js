// ============================================
// SehatAI: Lab Test Name Normalizer
// Maps Infermedica lab test names to your
// database test names.
// ============================================

const LAB_NAME_MAP = {
  // Blood tests
  'blood count': 'CBC',
  'complete blood count': 'CBC',
  'cbc': 'CBC',
  'full blood count': 'CBC',
  'fbc': 'CBC',
  
  // Cardiac
  'troponin': 'Troponin',
  'troponin i': 'Troponin',
  'troponin t': 'Troponin',
  'ck-mb': 'CK-MB',
  'ckmb': 'CK-MB',
  'bnp': 'BNP',
  'nt-probnp': 'BNP',
  
  // Metabolic
  'glucose': 'Fasting glucose',
  'blood glucose': 'Fasting glucose',
  'fasting glucose': 'Fasting glucose',
  'random glucose': 'Random glucose',
  'hba1c': 'HbA1c',
  'a1c': 'HbA1c',
  'glycated hemoglobin': 'HbA1c',
  
  // Lipids
  'lipid profile': 'Lipid profile',
  'cholesterol': 'Lipid profile',
  'ldl': 'LDL',
  'hdl': 'HDL',
  'triglycerides': 'Triglycerides',
  
  // Renal
  'creatinine': 'Creatinine',
  'bun': 'BUN',
  'urea': 'BUN',
  'egfr': 'eGFR',
  
  // Liver
  'alt': 'ALT',
  'ast': 'AST',
  'liver function': 'LFT',
  'lft': 'LFT',
  'bilirubin': 'Bilirubin',
  
  // Electrolytes
  'sodium': 'Sodium',
  'potassium': 'Potassium',
  'chloride': 'Chloride',
  'electrolytes': 'Electrolytes',
  
  // Hematology
  'hemoglobin': 'Hemoglobin',
  'haemoglobin': 'Hemoglobin',
  'hgb': 'Hemoglobin',
  'hematocrit': 'Hematocrit',
  'platelet': 'Platelet count',
  'platelet count': 'Platelet count',
  
  // Inflammatory
  'crp': 'CRP',
  'c-reactive protein': 'CRP',
  'esr': 'ESR',
  'erythrocyte sedimentation rate': 'ESR',
  
  // Thyroid
  'tsh': 'TSH',
  't3': 'T3',
  't4': 'T4',
  'free t4': 'T4',
  'thyroid panel': 'Thyroid panel',
  
  // Urinalysis
  'urinalysis': 'Urinalysis',
  'urine dipstick': 'Urinalysis',
  'urine analysis': 'Urinalysis',
  
  // Coagulation
  'pt': 'PT',
  'inr': 'INR',
  'aptt': 'APTT',
  'coagulation': 'Coagulation profile',
  'coagulation profile': 'Coagulation profile',

  // Added with LAB_STORED_VARIANTS (below): other ways a suggested test may be worded
  'wbc': 'WBC',
  'white blood cell count': 'WBC',
  'white blood cells': 'WBC',
  'leukocyte count': 'WBC',
  'thyroid stimulating hormone': 'TSH',
  'thyroid-stimulating hormone': 'TSH',
  'serum creatinine': 'Creatinine',
  'fasting blood sugar': 'Fasting glucose',
  'fasting blood glucose': 'Fasting glucose',
  'random blood sugar': 'Random glucose',
};

/**
 * Normalize a lab test name from Infermedica to your database name
 */
export function normalizeLabName(infermedicaName) {
  if (!infermedicaName) return null;
  
  const lower = infermedicaName.toLowerCase().trim();
  
  // Direct match
  if (LAB_NAME_MAP[lower]) {
    return LAB_NAME_MAP[lower];
  }
  
  // Partial match (e.g., "Troponin I" → "Troponin")
  for (const [key, value] of Object.entries(LAB_NAME_MAP)) {
    if (lower.includes(key) || key.includes(lower)) {
      return value;
    }
  }
  
  // If no match, return the original name (may still work)
  return infermedicaName;
}

/**
 * Other names a STORED result may carry for the same test. Labs print "Serum Creatinine", "Hb", "WBC Count",
 * "SGPT" ... while Infermedica (and the canonical names above) say "Creatinine", "Hemoglobin", "WBC", "ALT".
 * Matching only the canonical name silently skipped those results (found live: "Creatinine" never found a stored
 * "Serum Creatinine"). Keys are the canonical names from LAB_NAME_MAP; values are extra stored names that mean the
 * SAME test. Compared after loose normalisation (lowercase, letters and digits only), so spacing/case don't matter.
 * Deliberately conservative: only genuine synonyms. Different tests stay different (Direct bilirubin is NOT
 * Total bilirubin; Urine creatinine is NOT Serum creatinine).
 */
const LAB_STORED_VARIANTS = {
  'Creatinine': ['Serum Creatinine', 'Creatinine, Serum', 'S. Creatinine', 'Creatinine (Serum)'],
  'Hemoglobin': ['Haemoglobin', 'Hb', 'Hgb', 'Hemoglobin (Hb)', 'Haemoglobin (Hb)'],
  'Fasting glucose': ['Fasting Blood Sugar', 'FBS', 'Fasting Blood Glucose', 'Blood Sugar Fasting', 'Blood Glucose Fasting', 'Glucose (Fasting)', 'Glucose, Fasting'],
  'Random glucose': ['Random Blood Sugar', 'RBS', 'Random Blood Glucose', 'Blood Sugar Random', 'Glucose (Random)'],
  'HbA1c': ['Glycated Hemoglobin', 'Glycosylated Hemoglobin', 'Hemoglobin A1c', 'A1c'],
  'Platelet count': ['Platelets', 'Platelet Count', 'PLT'],
  'WBC': ['WBC Count', 'White Blood Cell Count', 'White Blood Cells', 'Total WBC Count', 'Total Leukocyte Count', 'TLC'],
  'BUN': ['Blood Urea Nitrogen', 'Blood Urea', 'Urea'],
  'ALT': ['SGPT', 'SGPT (ALT)', 'ALT (SGPT)', 'Alanine Aminotransferase'],
  'AST': ['SGOT', 'SGOT (AST)', 'AST (SGOT)', 'Aspartate Aminotransferase'],
  'TSH': ['Thyroid Stimulating Hormone', 'Thyroid-Stimulating Hormone'],
  'CRP': ['C-Reactive Protein', 'C Reactive Protein'],
  'ESR': ['Erythrocyte Sedimentation Rate'],
  'eGFR': ['Estimated GFR', 'GFR'],
  'Bilirubin': ['Total Bilirubin'],
  'Sodium': ['Sodium (Na)', 'Serum Sodium', 'Na'],
  'Potassium': ['Potassium (K)', 'Serum Potassium', 'K'],
  // a panel stands for its components: asking for a lipid profile should find the patient's cholesterol results
  'Lipid profile': ['Total Cholesterol', 'Cholesterol', 'LDL Cholesterol', 'HDL Cholesterol', 'Triglycerides', 'LDL', 'HDL'],
};

/**
 * Every stored test name that counts as a match for one name Infermedica suggested: the canonical name plus its
 * known stored variants. Always includes the canonical name itself, so nothing that matched before stops matching.
 * @param {string} infermedicaName
 * @returns {string[]}
 */
export function labNameVariants(infermedicaName) {
  const canonical = normalizeLabName(infermedicaName);
  if (!canonical) return [];
  return [canonical, ...(LAB_STORED_VARIANTS[canonical] || [])];
}

/**
 * Normalize an array of lab test names
 */
export function normalizeLabNames(names) {
  if (!names || !Array.isArray(names)) return [];
  return names.map(n => normalizeLabName(n)).filter(Boolean);
}