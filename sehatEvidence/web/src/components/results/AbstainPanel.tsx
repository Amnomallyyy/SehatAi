import { Search } from "lucide-react";

/** Now shows `queries` -- the Strategist's planned searches -- which
 * previously only appeared transiently in the live stream and vanished
 * once the run finished, leaving an abstention with no record of what
 * was actually searched for. */
export function AbstainPanel({ reasons, queries }: { reasons: string[]; queries: string[] }) {
  return (
    <section className="mb-8">
      <div className="rounded-[4px] border border-flag/30 border-l-[3px] border-l-flag bg-flag-bg p-5">
        <h3 className="m-0 mb-2 text-[15px] font-semibold text-[#fcd34d]">No answer given — the evidence did not support one</h3>
        <p className="m-0 mb-2.5 text-[13.5px] text-[#fde68a]">
          EvidenceBoard abstains rather than answering from thin or unverifiable evidence. The pool it did find is
          listed below.
        </p>
        <ul className="m-0 list-disc pl-5 text-[13.5px] text-[#fde68a]">
          {(reasons.length ? reasons : ["no reason recorded"]).map((r, i) => (
            <li key={i} className="my-0.5">
              {r}
            </li>
          ))}
        </ul>
        {queries.length > 0 && (
          <div className="mt-3 border-t border-dashed border-flag/30 pt-3">
            <p className="mb-1.5 flex items-center gap-1.5 text-[11px] font-bold tracking-wide text-[#fde68a] uppercase">
              <Search className="h-3 w-3" aria-hidden="true" /> Searches that were run
            </p>
            <ul className="m-0 flex flex-wrap gap-1.5 p-0">
              {queries.map((q, i) => (
                <li key={i} className="list-none rounded-sm border border-flag/30 bg-paper px-2 py-1 font-mono text-[11px] text-[#fde68a]">
                  {q}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
