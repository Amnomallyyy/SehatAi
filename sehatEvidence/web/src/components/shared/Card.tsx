import type { HTMLAttributes, ReactNode } from "react";

export function Card({
  children,
  className = "",
  accent,
  ...rest
}: HTMLAttributes<HTMLDivElement> & { children: ReactNode; accent?: "pass" | "fail" | "flag" | "accent" }) {
  const accentBorder = accent
    ? {
        pass: "border-l-[3px] border-l-pass",
        fail: "border-l-[3px] border-l-fail",
        flag: "border-l-[3px] border-l-flag",
        accent: "border-l-[3px] border-l-accent",
      }[accent]
    : "";
  return (
    <div
      className={`rounded-[4px] border border-rule bg-card shadow-[var(--eb-shadow)] ${accentBorder} ${className}`}
      {...rest}
    >
      {children}
    </div>
  );
}

export function SectionTitle({ children, count }: { children: ReactNode; count?: number | string }) {
  return (
    <h2 className="mb-3.5 flex items-baseline gap-2.5 text-[11px] font-bold tracking-[0.18em] text-ink-faint uppercase">
      {children}
      {count != null && <span className="font-mono text-ink-soft normal-case tracking-normal">{count}</span>}
      <span className="h-px flex-1 bg-rule" />
    </h2>
  );
}
