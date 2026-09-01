import { SEED_QUESTIONS } from "../../lib/seedQuestions";

/** Ported from demo/seed_queries.json -- the old UI used 3 hardcoded
 * questions unrelated to this file; these are curated to each showcase a
 * specific pipeline behavior (funnel deletion, abstention, a retraction
 * standing check, trial-registry integration). */
export function SeedQuestions({ onPick, disabled }: { onPick: (question: string) => void; disabled?: boolean }) {
  return (
    <div className="mt-4">
      <p className="mb-2 text-[10px] font-bold tracking-[0.14em] text-ink-faint uppercase">Try an example</p>
      <div className="flex flex-wrap gap-2">
        {SEED_QUESTIONS.map((seed) => (
          <button
            key={seed.question}
            type="button"
            disabled={disabled}
            onClick={() => onPick(seed.question)}
            title={seed.question}
            className="focus-ring rounded-[3px] border border-rule bg-card px-3 py-1.5 text-left text-[11.5px] text-ink-soft transition hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
          >
            <span className="mr-1.5 font-mono text-[9.5px] text-accent-2 uppercase">{seed.showcaseLabel}</span>
            <span className="block max-w-[260px] truncate">{seed.question}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
