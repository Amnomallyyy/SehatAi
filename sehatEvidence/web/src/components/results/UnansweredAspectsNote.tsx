/** Facets of the question the Synthesizer itself flagged as not addressed
 * by the evidence set ([GAP]-tagged sentences, see agents/synthesizer.py).
 * These are self-reported by the model, not independently verified --
 * unlike ParseDeletionsNote (which shows content the deterministic parser
 * rejected), this is deliberately styled as neutral/informational rather
 * than as a failure, and is never rendered inline with the answer text so
 * it can never be mistaken for a verified claim. */
export function UnansweredAspectsNote({ aspects }: { aspects?: string[] }) {
  // Defensive: this app caches reports forever with no TTL (core/store.py),
  // so a report served from history/cache can predate this field entirely
  // -- treat a missing value as "none", never crash the results tree.
  if (!aspects || aspects.length === 0) return null;
  return (
    <details className="mb-6 rounded-[3px] border border-info/35 bg-info-bg">
      <summary className="focus-ring cursor-pointer list-none px-4 py-3 text-[12.5px] font-semibold text-info [&::-webkit-details-marker]:hidden">
        {aspects.length} part{aspects.length === 1 ? "" : "s"} of the question not addressed by the retrieved evidence
      </summary>
      <div className="border-t border-dashed border-info/25 px-4 py-3">
        {aspects.map((aspect, i) => (
          <p key={i} className="m-0 py-1.5 text-[12.5px] text-ink-soft">
            {aspect}
          </p>
        ))}
      </div>
    </details>
  );
}
