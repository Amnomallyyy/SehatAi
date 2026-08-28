import { Gavel, Swords } from "lucide-react";
import type { FlagDetail } from "../../lib/types";

/**
 * Distinguishes Verifier flags from Red Team flags -- previously merged
 * into one indistinguishable list (pipeline.py's _apply_red_team appends
 * both into the same claim.flags array). README.md is explicit that this
 * separation matters: "The Red Team flags; only the Verifier deletes.
 * Keeping critique and deletion authority separate means a hostile
 * critic cannot silently rewrite the answer." This badge is where that
 * story finally becomes visible.
 */
export function FlagBadge({ detail }: { detail: FlagDetail }) {
  const isRedTeam = detail.source === "red_team";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 text-[11px] ${
        isRedTeam ? "border-flag/35 bg-flag-bg text-flag" : "border-info/35 bg-info-bg text-info"
      }`}
      title={isRedTeam ? "Raised by the Red Team's adversarial audit (flag-only, cannot delete)" : "Raised by the Verifier's own checks"}
    >
      {isRedTeam ? <Swords className="h-3 w-3" aria-hidden="true" /> : <Gavel className="h-3 w-3" aria-hidden="true" />}
      <span className="font-semibold">{detail.label}</span>
      {detail.note && <span className="text-ink-faint">— {detail.note}</span>}
    </span>
  );
}

/** Fallback for a plain flags[] string with no matching flag_details entry
 * (shouldn't happen once the backend change lands, but keeps the UI from
 * silently dropping a flag if the arrays ever get out of sync). */
export function PlainFlagBadge({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-sm border border-flag/35 bg-flag-bg px-2 py-0.5 text-[11px] font-semibold text-flag">
      {label}
    </span>
  );
}
