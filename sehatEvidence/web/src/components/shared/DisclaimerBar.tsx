import { DISCLAIMER_FALLBACK } from "../../lib/constants";

/** Rendered on every surface that shows an answer or the landing page --
 * previously shown only once near the top of the page (a gap the audit
 * flagged against config.py/contract.md's "must be displayed on every
 * surface" requirement). `text` should be report.disclaimer when a real
 * report is available. */
export function DisclaimerBar({ text = DISCLAIMER_FALLBACK }: { text?: string }) {
  return (
    <div className="rounded-[3px] border border-rule bg-rule-soft px-4 py-3 text-[11.5px] leading-relaxed text-ink-soft">
      <b className="mb-0.5 block text-[10px] font-bold tracking-[0.14em] text-ink-faint uppercase">Disclaimer</b>
      {text}
    </div>
  );
}
