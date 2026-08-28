import { Search } from "lucide-react";
import { useState } from "react";

/** Strategist's planned search queries, shown for a successful (non-
 * abstained) answer too -- AbstainPanel shows its own copy inline for
 * the abstain case. Previously vanished entirely once a run finished. */
export function PlannedQueries({ queries }: { queries: string[] }) {
  const [open, setOpen] = useState(false);
  if (queries.length === 0) return null;
  return (
    <section className="mb-6">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="focus-ring flex items-center gap-2 font-mono text-[11.5px] text-ink-faint hover:text-ink-soft"
      >
        <Search className="h-3.5 w-3.5" aria-hidden="true" />
        {queries.length} search{queries.length === 1 ? "" : "es"} run
        <span>{open ? "−" : "+"}</span>
      </button>
      {open && (
        <ul className="mt-2 flex flex-wrap gap-1.5 p-0">
          {queries.map((q, i) => (
            <li key={i} className="list-none rounded-sm border border-rule bg-card px-2 py-1 font-mono text-[11px] text-ink-soft">
              {q}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
