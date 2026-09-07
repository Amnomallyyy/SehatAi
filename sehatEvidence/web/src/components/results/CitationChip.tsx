import { ExternalLink } from "lucide-react";
import type { Citation } from "../../lib/types";
import { linkFor } from "../../lib/links";

/** Split chip: the sid half jumps to the matching row in the Evidence
 * pool (in-page anchor, id="ev-{sid}" set on EvidenceRow); the label
 * half opens the external source. The old UI only ever linked externally
 * — no way to jump to the shared sid namespace's other half. */
export function CitationChip({ citation }: { citation: Citation }) {
  const url = linkFor(citation);
  const label = citation.citation_key || citation.sid || "source";

  function jumpToEvidence(e: React.MouseEvent) {
    e.preventDefault();
    const el = document.getElementById(`ev-${citation.sid}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("ring-2", "ring-accent");
    setTimeout(() => el.classList.remove("ring-2", "ring-accent"), 1400);
  }

  return (
    <span className="inline-flex items-stretch overflow-hidden rounded-sm border border-rule text-[11.5px]">
      <a
        href={`#ev-${citation.sid}`}
        onClick={jumpToEvidence}
        className="focus-ring border-r border-rule bg-accent-soft px-1.5 py-0.5 font-mono font-semibold text-accent hover:bg-accent/20"
        title={`Jump to ${citation.sid} in the evidence pool`}
      >
        {citation.sid}
      </a>
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="focus-ring flex items-center gap-1 bg-paper px-1.5 py-0.5 text-ink-soft hover:text-accent"
          title={citation.title ?? label}
        >
          {label} <ExternalLink className="h-2.5 w-2.5" aria-hidden="true" />
        </a>
      ) : (
        <span className="flex items-center bg-paper px-1.5 py-0.5 text-ink-faint">{label}</span>
      )}
    </span>
  );
}
