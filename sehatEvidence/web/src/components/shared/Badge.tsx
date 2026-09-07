import type { ReactNode } from "react";

export type BadgeKind = "pass" | "fail" | "flag" | "skip" | "info";

const KIND_CLASSES: Record<BadgeKind, string> = {
  pass: "bg-pass-bg text-pass border-pass/35",
  fail: "bg-fail-bg text-fail border-fail/35",
  flag: "bg-flag-bg text-flag border-flag/35",
  skip: "bg-rule-soft text-ink-faint border-rule",
  info: "bg-info-bg text-info border-info/35",
};

const KIND_MARK: Record<BadgeKind, string> = {
  pass: "✓",
  fail: "✗",
  flag: "!",
  skip: "–",
  info: "●",
};

export function Badge({
  kind,
  children,
  mark,
}: {
  kind: BadgeKind;
  children: ReactNode;
  /** Override the default glyph; pass "" to omit it entirely. */
  mark?: string;
}) {
  const glyph = mark ?? KIND_MARK[kind];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-[11px] font-semibold ${KIND_CLASSES[kind]}`}
    >
      {glyph && <span className="font-mono">{glyph}</span>}
      {children}
    </span>
  );
}

/** Maps the three verification-check vocabularies (pass/fail/skipped,
 * supports/refutes/nei, pass/fail/flag/skipped) onto one Badge, exactly
 * mirroring the old checkBadge() logic in api/server.py's embedded JS. */
export function CheckBadge({ name, outcome }: { name: string; outcome: string | null | undefined }) {
  const value = (outcome || "skipped").toLowerCase();
  let kind: BadgeKind = "skip";
  if (value === "pass" || value === "supports") kind = "pass";
  else if (value === "fail" || value === "refutes") kind = "fail";
  else if (value === "flag" || value === "nei") kind = "flag";
  return (
    <Badge kind={kind}>
      {name} &middot; {value}
    </Badge>
  );
}
