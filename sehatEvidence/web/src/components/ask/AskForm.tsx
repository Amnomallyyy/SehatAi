import { RefreshCw } from "lucide-react";
import { useId, useState } from "react";
import { Button, Spinner } from "../shared/Button";
import { DEFAULT_PREFS, RetrievalPreferences, type RetrievalPrefs } from "./RetrievalPreferences";

export interface AskSubmitOptions {
  forceRefresh: boolean;
  prefs: RetrievalPrefs;
}

export function AskForm({
  busy,
  onSubmit,
}: {
  busy: boolean;
  onSubmit: (question: string, opts: AskSubmitOptions) => void;
}) {
  const [question, setQuestion] = useState("");
  const [forceRefresh, setForceRefresh] = useState(false);
  const [prefs, setPrefs] = useState<RetrievalPrefs>(DEFAULT_PREFS);
  const inputId = useId();

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || busy) return;
    onSubmit(trimmed, { forceRefresh, prefs });
  }

  return (
    <form onSubmit={handleSubmit} autoComplete="off">
      <div className="flex flex-wrap gap-2.5">
        <div className="min-w-[300px] flex-1">
          <label htmlFor={inputId} className="mb-1.5 block text-[10px] font-bold tracking-[0.16em] text-ink-faint uppercase">
            Clinical question
          </label>
          <input
            id={inputId}
            type="text"
            required
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. Does metformin reduce all-cause mortality in type 2 diabetes?"
            className="focus-ring w-full rounded-[3px] border border-rule bg-card px-4 py-3.5 text-[15px] text-ink shadow-[var(--eb-shadow)] placeholder:text-ink-faint focus:border-accent"
          />
        </div>
        <Button type="submit" disabled={busy} className="min-w-[132px] self-end">
          {busy ? (
            <>
              <Spinner /> Working
            </>
          ) : (
            "Ask"
          )}
        </Button>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <RetrievalPreferences value={prefs} onChange={setPrefs} />
        <label className="flex cursor-pointer items-center gap-1.5 text-[11.5px] text-ink-faint">
          <input
            type="checkbox"
            checked={forceRefresh}
            onChange={(e) => setForceRefresh(e.target.checked)}
            className="h-3.5 w-3.5 accent-accent"
          />
          <RefreshCw className="h-3 w-3" aria-hidden="true" />
          Force a fresh run (skip cache)
        </label>
      </div>
    </form>
  );
}
