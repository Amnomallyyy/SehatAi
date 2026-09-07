import { useMemo, useState } from "react";
import { SectionTitle } from "../shared/Card";
import type { Evidence, EvidenceSource } from "../../lib/types";
import { EvidenceRow } from "./EvidenceRow";

type SortKey = "score" | "date";

const SOURCE_LABELS: Record<EvidenceSource, string> = {
  pubmed: "PubMed",
  europepmc: "Europe PMC",
  clinicaltrials: "ClinicalTrials.gov",
};

/** Container for the ranked evidence list -- previously always shown in
 * one fixed best-first order with no way to filter/sort by source,
 * preprint status, retraction, or score (audit gap). */
export function EvidencePool({
  evidence,
  quotesBySid = {},
}: {
  evidence: Evidence[];
  quotesBySid?: Record<string, string[]>;
}) {
  const [sourceFilter, setSourceFilter] = useState<EvidenceSource | "all">("all");
  const [retractedOnly, setRetractedOnly] = useState(false);
  const [sort, setSort] = useState<SortKey>("score");

  const sources = useMemo(() => Array.from(new Set(evidence.map((e) => e.source))), [evidence]);

  const visible = useMemo(() => {
    let items = evidence;
    if (sourceFilter !== "all") items = items.filter((e) => e.source === sourceFilter);
    if (retractedOnly) items = items.filter((e) => e.is_retracted);
    items = [...items];
    if (sort === "score") {
      items.sort((a, b) => (b.relevance_score ?? 0) - (a.relevance_score ?? 0));
    } else {
      items.sort((a, b) => (b.publication_date ?? "").localeCompare(a.publication_date ?? ""));
    }
    return items;
  }, [evidence, sourceFilter, retractedOnly, sort]);

  if (evidence.length === 0) {
    return (
      <section className="mb-8">
        <SectionTitle>Evidence pool</SectionTitle>
        <div className="rounded-[3px] border border-dashed border-rule bg-card p-4 text-[13px] text-ink-faint">
          No records were retrieved for this question.
        </div>
      </section>
    );
  }

  return (
    <section className="mb-8">
      <SectionTitle count={`${visible.length} of ${evidence.length} ranked`}>Evidence pool</SectionTitle>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[11.5px]">
        <select
          aria-label="Filter evidence by source"
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value as EvidenceSource | "all")}
          className="focus-ring rounded-sm border border-rule bg-card px-2 py-1 text-ink-soft"
        >
          <option value="all">All sources</option>
          {sources.map((s) => (
            <option key={s} value={s}>
              {SOURCE_LABELS[s] ?? s}
            </option>
          ))}
        </select>
        <select
          aria-label="Sort evidence"
          value={sort}
          onChange={(e) => setSort(e.target.value as SortKey)}
          className="focus-ring rounded-sm border border-rule bg-card px-2 py-1 text-ink-soft"
        >
          <option value="score">Sort: relevance score</option>
          <option value="date">Sort: publication date</option>
        </select>
        <label className="flex cursor-pointer items-center gap-1.5 text-ink-faint">
          <input type="checkbox" checked={retractedOnly} onChange={(e) => setRetractedOnly(e.target.checked)} className="h-3.5 w-3.5 accent-fail" />
          Retracted only
        </label>
      </div>
      <ol className="m-0 flex list-none flex-col gap-2 p-0">
        {visible.map((e) => (
          <EvidenceRow key={e.sid} evidence={e} quotes={quotesBySid[e.sid] ?? []} />
        ))}
      </ol>
    </section>
  );
}
