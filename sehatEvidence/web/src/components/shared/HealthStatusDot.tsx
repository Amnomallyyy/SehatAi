import { useHealth } from "../../hooks/useHealth";

export function HealthStatusDot() {
  const { data, isError, isLoading } = useHealth();

  let dotClass = "bg-ink-faint shadow-[0_0_0_3px_var(--eb-rule-soft)]";
  let label = "checking service…";
  if (!isLoading) {
    if (isError || !data) {
      dotClass = "bg-fail shadow-[0_0_0_3px_var(--eb-fail-bg)]";
      label = "service unreachable";
    } else {
      dotClass = "bg-pass shadow-[0_0_0_3px_var(--eb-pass-bg)] animate-pulse";
      label = `${data.llm_model} · ${data.keys_count} key${data.keys_count === 1 ? "" : "s"}`;
    }
  }

  return (
    <div className="inline-flex items-center gap-2 pb-1.5 font-mono text-[11px] tracking-wide text-ink-faint" role="status">
      <i className={`h-[7px] w-[7px] rounded-full ${dotClass}`} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
