/**
 * Ported from demo/seed_queries.json (static reference content, not user
 * data, so it's embedded here rather than served from a new endpoint).
 * The old UI hardcoded 3 unrelated seed questions instead of using this
 * file at all -- an audit gap. Keep in sync by hand if that file changes.
 */
export interface SeedQuestion {
  question: string;
  category: string;
  showcaseFeature: string;
  showcaseLabel: string;
}

export const SEED_QUESTIONS: SeedQuestion[] = [
  {
    question:
      "Does SGLT2 inhibitor therapy reduce heart failure hospitalization in patients with type 2 diabetes?",
    category: "therapy",
    showcaseFeature: "verification_pass",
    showcaseLabel: "Strong, multi-RCT answer",
  },
  {
    question:
      "In hospitalized adults with community-acquired pneumonia, does adjunctive corticosteroid therapy reduce 30-day mortality?",
    category: "therapy",
    showcaseFeature: "deletion_funnel",
    showcaseLabel: "Visible deletion funnel",
  },
  {
    question:
      "What is the optimal intrathecal nusinersen dosing interval for adults with SMA type 4 who have already failed risdiplam?",
    category: "therapy",
    showcaseFeature: "abstention",
    showcaseLabel: "Honest abstention",
  },
  {
    question: "Does ivermectin reduce mortality in hospitalized patients with COVID-19?",
    category: "therapy",
    showcaseFeature: "standing_check",
    showcaseLabel: "Retraction standing check",
  },
  {
    question:
      "How accurate are plasma p-tau217 assays for diagnosing Alzheimer disease pathology in a primary care population?",
    category: "diagnosis",
    showcaseFeature: "clinical_trial_integration",
    showcaseLabel: "Trial-registry evidence",
  },
];
