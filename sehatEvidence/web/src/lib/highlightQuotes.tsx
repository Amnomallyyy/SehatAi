import type { ReactNode } from "react";

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Splits abstract text on each verbatim quote a claim actually cited from
 * it (agents/verifier.py's EntailmentVerdict.evidence_quote) and wraps each
 * match in a styled <mark>, React-safe (no dangerouslySetInnerHTML) --
 * mirrors markSids.tsx's split-and-wrap approach. Quotes are matched
 * case-insensitively and longest-first so an overlapping shorter quote
 * doesn't fragment a longer one. A quote that never appears verbatim (the
 * judge paraphrased instead of extracting) is silently skipped -- the
 * abstract still renders in full either way. */
export function highlightQuotes(abstract: string, quotes: string[]): ReactNode[] {
  const clean = Array.from(new Set(quotes.map((q) => q.trim()).filter((q) => q.length >= 4)));
  if (clean.length === 0) return [abstract];

  clean.sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`(${clean.map(escapeRegExp).join("|")})`, "gi");

  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  pattern.lastIndex = 0;
  while ((match = pattern.exec(abstract)) !== null) {
    if (match.index > lastIndex) parts.push(abstract.slice(lastIndex, match.index));
    parts.push(
      <mark key={`quote-${key++}`} className="rounded-sm bg-accent-soft px-0.5 text-ink">
        {match[0]}
      </mark>,
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < abstract.length) parts.push(abstract.slice(lastIndex));
  return parts;
}
