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
 * Normalize an array of lab test names
 */
export function normalizeLabNames(names) {
  if (!names || !Array.isArray(names)) return [];
  return names.map(n => normalizeLabName(n)).filter(Boolean);
}