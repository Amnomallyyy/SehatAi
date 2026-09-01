/**
 * Study-design -> evidence-hierarchy tier lookup, derived directly from
 * agents/appraiser.py's _BASE_SCORE table and its OCEBM 2011 / SORT /
 * GRADE comments -- not invented. Keep in sync if that table changes.
 * Base scores are included for reference; the Appraiser's real score
 * (relevance_score) already reflects recency/relevance modifiers on top
 * of this, so the tier badge is context, not a substitute for the score.
 */

export interface OcebmTier {
  tier: string;
  label: string;
  baseScore: number;
}

const OCEBM_TABLE: Record<string, OcebmTier> = {
  systematic_review: { tier: "OCEBM 1a", label: "Systematic review", baseScore: 95 },
  meta_analysis: { tier: "OCEBM 1a", label: "Meta-analysis", baseScore: 95 },
  rct: { tier: "OCEBM 1b", label: "Randomized controlled trial", baseScore: 85 },
  guideline: { tier: "AGREE II", label: "Clinical guideline", baseScore: 75 },
  cohort: { tier: "OCEBM 2b", label: "Cohort study", baseScore: 65 },
  case_control: { tier: "OCEBM 3b", label: "Case-control study", baseScore: 55 },
  review: { tier: "Narrative", label: "Narrative review", baseScore: 50 },
  clinical_trial_record: { tier: "Registry", label: "Trial registry record", baseScore: 40 },
  case_series: { tier: "OCEBM 4", label: "Case series", baseScore: 35 },
  case_report: { tier: "OCEBM 4", label: "Case report", baseScore: 25 },
  preprint: { tier: "Preprint", label: "Not peer-review certified", baseScore: 20 },
  unknown: { tier: "Unclassified", label: "Design not determined", baseScore: 15 },
};

export function ocebmFor(studyDesign: string | null): OcebmTier | null {
  if (!studyDesign) return null;
  return OCEBM_TABLE[studyDesign] ?? null;
}
