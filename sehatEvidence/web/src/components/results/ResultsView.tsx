import { useState } from "react";
import { SectionTitle } from "../shared/Card";
import type { Report } from "../../lib/types";
import { AbstainPanel } from "./AbstainPanel";
import { AnswerPanel } from "./AnswerPanel";
import { ClaimCard, DeletedClaimCard } from "./ClaimCard";
import { EvidencePool } from "./EvidencePool";
import { FunnelChart } from "./FunnelChart";
import { ParseDeletionsNote } from "./ParseDeletionsNote";
import { PlannedQueries } from "./PlannedQueries";

type ClaimTab = "kept" | "flagged" | "deleted";

/**
 * Shared results tree for both the live /ask flow and a replayed
 * /history/{id} run -- fed either the just-streamed report or one loaded
 * from GET /api/history/{id}. Reusing this one tree (rather than two
 * parallel render paths) is what the plan calls out explicitly under
 * "no duplicated rendering logic."
 */
export function ResultsView({ report }: { report: Report }) {
  const kept = report.claims.filter((c) => c.status === "kept");
  const flagged = report.claims.filter((c) => c.status === "flagged");
  const deleted = report.claims.filter((c) => c.status === "deleted");
  const [tab, setTab] = useState<ClaimTab>(flagged.length > 0 ? "flagged" : "kept");

  const tabs: { id: ClaimTab; label: string; items: typeof kept }[] = [
    { id: "kept", label: "Kept", items: kept },
    { id: "flagged", label: "Flagged", items: flagged },
    { id: "deleted", label: "Deleted", items: deleted },
  ];
  const active = tabs.find((t) => t.id === tab) ?? tabs[0];

  return (
    <div>
      <p className="mb-6 text-[19px] leading-snug font-semibold text-ink">
        <span className="mb-1.5 block text-[10px] font-bold tracking-[0.16em] text-ink-faint uppercase">Question</span>
        {report.question}
      </p>

      {report.abstained ? <AbstainPanel reasons={report.abstain_reasons} queries={report.queries} /> : <AnswerPanel answerText={report.answer_text} disclaimer={report.disclaimer} />}

      {!report.abstained && <PlannedQueries queries={report.queries} />}

      <FunnelChart funnel={report.funnel} />

      <ParseDeletionsNote deletions={report.synthesizer_parse_deletions} />

      {report.claims.length > 0 && (
        <section className="mb-8">
          <SectionTitle count={report.claims.length}>Claims</SectionTitle>
          <div className="mb-3 flex gap-1.5 border-b border-rule">
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`focus-ring border-b-2 px-3 py-2 text-[12.5px] font-semibold transition ${
                  active.id === t.id ? "border-accent text-ink" : "border-transparent text-ink-faint hover:text-ink-soft"
                }`}
              >
                {t.label} <span className="font-mono">{t.items.length}</span>
              </button>
            ))}
          </div>
          {active.items.length === 0 ? (
            <p className="text-[13px] text-ink-faint">No {active.label.toLowerCase()} claims.</p>
          ) : (
            active.items.map((c) => (c.status === "deleted" ? <DeletedClaimCard key={c.claim_id} claim={c} /> : <ClaimCard key={c.claim_id} claim={c} />))
          )}
        </section>
      )}

      <EvidencePool evidence={report.evidence} />
    </div>
  );
}
