import { ocebmFor } from "../../lib/ocebm";

export function StudyDesignBadge({ studyDesign }: { studyDesign: string | null }) {
  const tier = ocebmFor(studyDesign);
  if (!tier) return null;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-sm border border-info/35 bg-info-bg px-2 py-0.5 text-[10.5px] font-semibold text-info"
      title={`${tier.label} — evidence-hierarchy base score ${tier.baseScore}/100 before recency/relevance modifiers`}
    >
      <span className="font-mono">{tier.tier}</span>
      {tier.label}
    </span>
  );
}
