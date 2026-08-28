import { Check, Copy } from "lucide-react";
import { useState } from "react";

/** A plain-text identifier (PMID/NCT/DOI) with a copy button -- these
 * were previously only ever used internally to build outbound link URLs,
 * never shown as copyable text on their own (audit gap). */
export function CopyableId({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API can be denied/unavailable; fail silently, the text
      // is still selectable manually right next to the button.
    }
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      className="focus-ring inline-flex items-center gap-1.5 rounded-sm border border-rule bg-paper px-2 py-0.5 font-mono text-[11px] text-ink-soft transition hover:border-accent hover:text-accent"
      title={`Copy ${label}`}
    >
      <span className="text-ink-faint">{label}</span>
      {value}
      {copied ? <Check className="h-3 w-3 text-pass" aria-hidden="true" /> : <Copy className="h-3 w-3" aria-hidden="true" />}
      <span className="sr-only">{copied ? "Copied" : `Copy ${label} ${value}`}</span>
    </button>
  );
}
