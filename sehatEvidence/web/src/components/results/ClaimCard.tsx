import { useId, useState } from "react";
import { Badge, CheckBadge } from "../shared/Badge";
import { Card } from "../shared/Card";
import type { Claim } from "../../lib/types";
import { markSids } from "../../lib/markSids";
import { CitationChip } from "./CitationChip";
import { ConfidenceGauge } from "./ConfidenceGauge";
import { FlagBadge, PlainFlagBadge } from "./FlagBadge";

/**
 * Shared body used by both a kept/flagged claim AND (via DeletedClaimCard)
 * a deleted one -- same verification detail either way. This is the
 * direct fix for the audit's biggest UI gap: deleted claims used to show
 * only struck-through text + a reason, even though the backend keeps the
 * full checks/verdict/confidence/citations for them (pipeline.py's own
 * docstring: this is "what makes an abstention auditable").
 */
export function ClaimBody({ claim }: { claim: Claim }) {
  return (
    <>
      <p id={`claim-text-${claim.claim_id}`} className="mb-3 text-[15px] leading-relaxed text-ink">
        {markSids(claim.text)}
      </p>
      <div className="mb-3 flex flex-wrap gap-1.5">
        <CheckBadge name="existence" outcome={claim.checks.existence} />
        <CheckBadge name="entailment" outcome={claim.checks.entailment} />
        <CheckBadge name="standing" outcome={claim.checks.standing} />
      </div>
      {claim.verdict && (
        <div className="mb-2.5 font-mono text-[11px] text-ink-soft">
          verdict: {claim.verdict}
          <ConfidenceGauge confidence={claim.confidence} />
        </div>
      )}
      {claim.evidence_quote && (
        <blockquote className="mb-3 border-l-2 border-rule bg-paper px-3.5 py-2.5 text-[13.5px] leading-relaxed text-ink-soft italic">
          {claim.evidence_quote}
        </blockquote>
      )}
      {claim.citations.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {claim.citations.map((c) => (
            <CitationChip key={c.sid} citation={c} />
          ))}
        </div>
      )}
      {(claim.flag_details.length > 0 || claim.flags.length > 0) && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {claim.flag_details.length > 0
            ? claim.flag_details.map((fd, i) => <FlagBadge key={i} detail={fd} />)
            : claim.flags.map((f, i) => <PlainFlagBadge key={i} label={f} />)}
        </div>
      )}
    </>
  );
}

export function ClaimCard({ claim }: { claim: Claim }) {
  const headingId = useId();
  return (
    <Card accent={claim.status === "flagged" ? "flag" : "pass"} className="mb-3 p-5">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <Badge kind={claim.status === "flagged" ? "flag" : "pass"}>{claim.status === "flagged" ? "flagged" : "kept"}</Badge>
        <span id={headingId} className="font-mono text-[11px] text-ink-faint">
          {claim.claim_id}
        </span>
      </div>
      <ClaimBody claim={claim} />
    </Card>
  );
}

/** Defaults collapsed, red-tinted, reuses ClaimBody's full verification
 * detail -- see the module docstring above. */
export function DeletedClaimCard({ claim }: { claim: Claim }) {
  const [open, setOpen] = useState(false);
  return (
    <Card accent="fail" className="mb-2.5 overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="focus-ring flex w-full items-start justify-between gap-3 px-5 py-3.5 text-left"
      >
        <div className="min-w-0">
          <p className={`text-[13.5px] text-ink-faint ${open ? "" : "truncate"}`} style={{ textDecoration: "line-through", textDecorationColor: "rgba(248,113,113,.45)" }}>
            {claim.text}
          </p>
          <p className="mt-1 font-mono text-[11.5px] text-fail">deleted — {claim.deletion_reason || "unspecified"}</p>
        </div>
        <span className="flex-none font-mono text-fail">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="border-t border-dashed border-rule px-5 py-4">
          <ClaimBody claim={claim} />
        </div>
      )}
    </Card>
  );
}
