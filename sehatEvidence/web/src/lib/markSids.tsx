import type { ReactNode } from "react";

const SID_PATTERN = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g;

/** Splits claim/answer text on [S3] / [S3, S7] citation markers and wraps
 * each in a styled span, React-safe (no dangerouslySetInnerHTML). */
export function markSids(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  SID_PATTERN.lastIndex = 0;
  while ((match = SID_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    parts.push(
      <span key={`sid-${key++}`} className="rounded-sm bg-accent-soft px-1 font-mono text-[0.85em] font-semibold whitespace-nowrap text-accent">
        {match[0]}
      </span>,
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}
