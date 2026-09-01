import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANT_CLASSES: Record<Variant, string> = {
  primary:
    "bg-gradient-to-br from-accent to-accent-2 text-[#04101f] font-semibold shadow-[0_8px_20px_-8px_rgba(79,141,253,.55)] hover:brightness-110 disabled:opacity-55 disabled:shadow-none",
  secondary: "bg-card text-ink border border-rule hover:border-ink-faint",
  ghost: "bg-transparent text-ink-soft hover:text-ink hover:bg-card",
  danger: "bg-fail-bg text-fail border border-fail/35 hover:brightness-110",
};

export function Button({
  variant = "primary",
  children,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; children: ReactNode }) {
  return (
    <button
      className={`focus-ring inline-flex items-center justify-center gap-2 rounded-[3px] px-4 py-2.5 text-sm transition disabled:cursor-not-allowed ${VARIANT_CLASSES[variant]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3 w-3 animate-spin rounded-full border-2 border-[#04101f]/30 border-t-[#04101f] ${className}`}
      aria-hidden="true"
    />
  );
}
