import { ChevronDown, ExternalLink } from "lucide-react";
import { useState } from "react";
import { Badge } from "../shared/Badge";
import { CopyableId } from "../shared/CopyableId";
import type { Evidence } from "../../lib/types";
import { linkFor } from "../../lib/links";
import { StudyDesignBadge } from "./StudyDesignBadge";

function scoreClass(score: number): string {
  if (score >= 60) return "border-accent/30 bg-accent-soft text-accent";
  if (score >= 30) return "border-flag/35 bg-flag-bg text-flag";
  return "border-rule bg-rule-soft text-ink-faint";
}

/** One row in the evidence pool. Previously showed only title/meta/score
 * with is_retracted/is_preprint/trial_status tags -- rationale and
 * abstract were computed by the backend but never surfaced anywhere;
 * retraction_source was silently dropped before it even reached the API
 * (see the pipeline.py fix). All four are shown here now. */
export function EvidenceRow({ evidence }: { evidence: Evidence }) {
  const [expanded, setExpanded] = useState(false);
  const url = linkFor(evidence);
  const title = evidence.title || "(untitled record)";
  const score = evidence.relevance_score ?? 0;

  const idChip = evidence.native_id
    ? { label: evidence.source === "clinicaltrials" ? "NCT" : evidence.source === "pubmed" ? "PMID" : "ID", value: evidence.native_id }
    : null;

  return (
    <li id={`ev-${evidence.sid}`} className={`scroll-mt-24 rounded-[3px] border bg-card p-4 shadow-[var(--eb-shadow)] transition-shadow ${evidence.is_retracted ? "border-l-[3px] border-l-fail bg-fail-bg/40" : "border-rule"}`}>
      <div className="grid grid-cols-[auto_1fr_auto] items-start gap-3.5">
        <span className="pt-0.5 font-mono text-[12px] font-semibold text-accent">{evidence.sid}</span>
        <div className="min-w-0">
          {url ? (
            <a href={url} target="_blank" rel="noopener noreferrer" className="focus-ring inline-flex items-center gap-1.5 text-[14.5px] leading-snug text-ink underline decoration-rule underline-offset-2 hover:text-accent hover:decoration-accent">
              {title} <ExternalLink className="h-3 w-3 flex-none" aria-hidden="true" />
            </a>
          ) : (
            <span className="text-[14.5px] leading-snug text-ink">{title}</span>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 font-mono text-[11.5px] text-ink-faint">
            {evidence.journal && <span>{evidence.journal}</span>}
            {evidence.publication_date && <span>{evidence.publication_date}</span>}
            {evidence.citation_key && <span>{evidence.citation_key}</span>}
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {evidence.is_retracted && <Badge kind="fail">retracted{evidence.retraction_source ? ` · ${evidence.retraction_source}` : ""}</Badge>}
            {evidence.is_preprint && <Badge kind="flag">preprint · not peer reviewed</Badge>}
            {evidence.trial_status && <Badge kind="info">trial: {evidence.trial_status}</Badge>}
            <StudyDesignBadge studyDesign={evidence.study_design} />
          </div>
        </div>
        <span className={`rounded-sm border px-2 py-0.5 font-mono text-[12px] font-semibold whitespace-nowrap ${scoreClass(score)}`}>{score}</span>
      </div>

      {(evidence.rationale || evidence.abstract || idChip || evidence.doi) && (
        <div className="mt-3 border-t border-dashed border-rule-soft pt-3">
          {evidence.rationale && <p className="text-[12.5px] leading-relaxed text-ink-soft">{evidence.rationale}</p>}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {idChip && <CopyableId label={idChip.label} value={idChip.value} />}
            {evidence.doi && <CopyableId label="DOI" value={evidence.doi} />}
            {evidence.abstract && (
              <button
                type="button"
                onClick={() => setExpanded((e) => !e)}
                aria-expanded={expanded}
                className="focus-ring inline-flex items-center gap-1 font-mono text-[11px] text-accent-2 hover:text-accent"
              >
                <ChevronDown className={`h-3 w-3 transition-transform ${expanded ? "rotate-180" : ""}`} aria-hidden="true" />
                {expanded ? "hide abstract" : "show abstract"}
              </button>
            )}
          </div>
          {expanded && evidence.abstract && (
            <p className="mt-2 rounded-sm bg-paper p-3 text-[12.5px] leading-relaxed text-ink-soft">{evidence.abstract}</p>
          )}
        </div>
      )}
    </li>
  );
}
