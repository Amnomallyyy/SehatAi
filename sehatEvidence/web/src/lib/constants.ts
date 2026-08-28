/**
 * Verbatim copy of config.py's DISCLAIMER constant, for use on pages
 * (Landing) that render before any report has come back from the API.
 * Everywhere a real report is available, prefer report.disclaimer --
 * this is a display-only fallback, never the source of truth.
 */
export const DISCLAIMER_FALLBACK =
  "EvidenceBoard is a literature search and evidence-summarization aid for " +
  "healthcare professionals. It is not a medical device and does not provide " +
  "medical advice, diagnosis, or treatment recommendations. Every claim must be " +
  "verified against the primary source (links provided) before clinical use. " +
  "Do not enter patient-identifiable information. Automated verification checks " +
  "can err; the treating clinician remains responsible for clinical decisions.";
