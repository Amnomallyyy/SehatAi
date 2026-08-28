import type { SynthesizerParseDeletion } from "../../lib/types";

/** Sentences the Synthesizer's own deterministic parser dropped before
 * the Verifier ever saw them (uncited sentences, citations to unknown
 * evidence). Distinct from the Verifier's deletion funnel -- these never
 * reached verification at all. Previously computed but never surfaced
 * past a stdout log line (a genuine backend gap, not just a UI one). */
export function ParseDeletionsNote({ deletions }: { deletions: SynthesizerParseDeletion[] }) {
  if (deletions.length === 0) return null;
  return (
    <details className="mb-6 rounded-[3px] border border-rule bg-card">
      <summary className="focus-ring cursor-pointer list-none px-4 py-3 text-[12.5px] font-semibold text-ink-soft [&::-webkit-details-marker]:hidden">
        {deletions.length} draft sentence{deletions.length === 1 ? "" : "s"} dropped before verification (uncited or unresolved citation)
      </summary>
      <div className="border-t border-dashed border-rule px-4 py-3">
        {deletions.map((d, i) => (
          <div key={i} className="py-1.5 text-[12.5px]">
            <p className="m-0 text-ink-faint line-through decoration-fail/45">{d.text}</p>
            <p className="m-0 font-mono text-[11px] text-fail">{d.reason}</p>
          </div>
        ))}
      </div>
    </details>
  );
}
