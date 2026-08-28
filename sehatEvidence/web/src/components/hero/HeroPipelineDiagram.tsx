import { ArrowRight, FileSearch, Gavel, PenLine, ShieldCheck, Swords, Target } from "lucide-react";

interface Stage {
  icon: typeof Target;
  label: string;
  caption: string;
}

const STAGES: Stage[] = [
  { icon: Target, label: "Strategist", caption: "3–5 targeted queries" },
  { icon: FileSearch, label: "Retrieval", caption: "PubMed · Europe PMC · CT.gov" },
  { icon: Gavel, label: "Appraiser", caption: "OCEBM / GRADE scoring" },
  { icon: PenLine, label: "Synthesizer", caption: "Forced [S#] citations" },
  { icon: ShieldCheck, label: "Verifier", caption: "3-check gate — deletes" },
  { icon: Swords, label: "Red Team", caption: "Flags only, never deletes" },
];

/** Hand-rolled, no charting/diagram library -- six labeled nodes in the
 * pipeline's real run order (see pipeline.py's _run_stages), matching
 * README.md's architecture section. */
export function HeroPipelineDiagram() {
  return (
    <div
      className="flex flex-wrap items-stretch justify-center gap-2 sm:flex-nowrap sm:gap-1"
      role="img"
      aria-label="Pipeline: Strategist, then Retrieval across PubMed, Europe PMC and ClinicalTrials.gov, then Appraiser, then Synthesizer, then Verifier which deletes unsupported claims, then Red Team which only flags."
    >
      {STAGES.map((stage, i) => (
        <div key={stage.label} className="flex items-stretch gap-1">
          <div className="flex w-[128px] flex-col items-center gap-2 rounded-[4px] border border-rule bg-card px-3 py-4 text-center shadow-[var(--eb-shadow)]">
            <div className="flex h-9 w-9 items-center justify-center rounded-full border border-accent-soft bg-accent-soft text-accent-2">
              <stage.icon className="h-4 w-4" aria-hidden="true" />
            </div>
            <div className="text-[13px] font-semibold text-ink">{stage.label}</div>
            <div className="font-mono text-[10px] leading-tight text-ink-faint">{stage.caption}</div>
          </div>
          {i < STAGES.length - 1 && (
            <div className="flex items-center text-rule">
              <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
