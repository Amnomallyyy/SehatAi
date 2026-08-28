import { SectionTitle } from "../shared/Card";
import type { Funnel } from "../../lib/types";

export function FunnelChart({ funnel }: { funnel: Funnel }) {
  const generated = funnel.claims_generated || 0;
  const deleted = funnel.claims_deleted || 0;
  const kept = funnel.claims_kept || 0;
  const total = generated > 0 ? generated : deleted + kept;
  const keptPct = total ? (kept / total) * 100 : 0;
  const delPct = total ? (deleted / total) * 100 : 0;
  const reasons = Object.entries(funnel.by_reason ?? {});

  return (
    <section className="mb-8">
      <SectionTitle>Verification funnel</SectionTitle>
      <div className="rounded-[4px] border border-rule bg-card p-5 shadow-[var(--eb-shadow)]">
        <div className="font-mono text-[13px] text-ink-soft">
          <b className="font-semibold text-ink">{generated}</b> claims generated
          <span className="mx-1.5 text-ink-faint">→</span>
          <b className="font-semibold text-ink">{deleted}</b> deleted
          <span className="mx-1.5 text-ink-faint">→</span>
          <b className="font-semibold text-ink">{kept}</b> shown
        </div>
        <div className="mt-3 flex h-2.5 overflow-hidden rounded-sm bg-rule-soft">
          <div className="h-full bg-pass transition-[width] duration-500" style={{ width: `${keptPct}%` }} />
          <div className="h-full bg-fail transition-[width] duration-500" style={{ width: `${delPct}%` }} />
        </div>
        <div className="mt-2.5 flex flex-wrap gap-4 text-[11px] text-ink-faint">
          <span>
            <i className="mr-1.5 inline-block h-2 w-2 rounded-[2px] bg-info align-middle" /> generated <em className="font-mono text-ink-soft not-italic">{generated}</em>
          </span>
          <span>
            <i className="mr-1.5 inline-block h-2 w-2 rounded-[2px] bg-fail align-middle" /> deleted <em className="font-mono text-ink-soft not-italic">{deleted}</em>
          </span>
          <span>
            <i className="mr-1.5 inline-block h-2 w-2 rounded-[2px] bg-pass align-middle" /> shown <em className="font-mono text-ink-soft not-italic">{kept}</em>
          </span>
        </div>
        {reasons.length > 0 && (
          <div className="mt-3 border-t border-dashed border-rule pt-3 text-[12px] text-ink-soft">
            {reasons.map(([reason, count]) => (
              <div key={reason} className="flex justify-between gap-3 py-0.5">
                <span>{reason}</span>
                <span className="font-mono text-fail">×{count}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
