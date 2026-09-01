export interface RetrievalPrefs {
  prioritizeRcts: boolean;
  includePreprints: boolean;
  includeTrialRecords: boolean;
}

export const DEFAULT_PREFS: RetrievalPrefs = {
  prioritizeRcts: false,
  includePreprints: false,
  includeTrialRecords: false,
};

/**
 * There's no dedicated backend parameter for retrieval preferences (the
 * Strategist plans queries from the raw question text, and that's the
 * pipeline's only input at this stage) -- rather than add a fake control
 * that does nothing, these toggles append a short natural-language clause
 * to the submitted question, which the Strategist's own query-planning
 * LLM call already reads and incorporates. See buildQuestionWithPrefs().
 */
export function buildQuestionWithPrefs(question: string, prefs: RetrievalPrefs): string {
  const clauses: string[] = [];
  if (prefs.prioritizeRcts) clauses.push("prioritize randomized controlled trials and systematic reviews");
  if (prefs.includePreprints) clauses.push("include preprints if relevant");
  if (prefs.includeTrialRecords) clauses.push("include ongoing trial registry records if relevant");
  if (clauses.length === 0) return question;
  return `${question} (${clauses.join("; ")})`;
}

const OPTIONS: { key: keyof RetrievalPrefs; label: string }[] = [
  { key: "prioritizeRcts", label: "Prioritize RCTs & systematic reviews" },
  { key: "includePreprints", label: "Include preprints" },
  { key: "includeTrialRecords", label: "Include trial registry records" },
];

export function RetrievalPreferences({
  value,
  onChange,
}: {
  value: RetrievalPrefs;
  onChange: (next: RetrievalPrefs) => void;
}) {
  return (
    <fieldset className="mt-3 flex flex-wrap gap-x-5 gap-y-2 border-0 p-0">
      <legend className="mb-1.5 w-full text-[10px] font-bold tracking-[0.14em] text-ink-faint uppercase">
        Retrieval preferences (optional)
      </legend>
      {OPTIONS.map((opt) => (
        <label key={opt.key} className="focus-within:outline-accent flex cursor-pointer items-center gap-2 text-[12.5px] text-ink-soft">
          <input
            type="checkbox"
            checked={value[opt.key]}
            onChange={(e) => onChange({ ...value, [opt.key]: e.target.checked })}
            className="h-3.5 w-3.5 accent-accent"
          />
          {opt.label}
        </label>
      ))}
    </fieldset>
  );
}
