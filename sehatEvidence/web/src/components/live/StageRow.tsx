import { motion } from "framer-motion";
import { Check } from "lucide-react";
import type { StageEvent } from "../../lib/types";
import type { StageId } from "../../hooks/useAskStream";

export interface StageMeta {
  label: string;
  placeholder: string;
}

export const STAGE_ORDER: Exclude<StageId, "complete">[] = [
  "strategist",
  "retrieval",
  "appraiser",
  "synthesizer",
  "verifier",
  "red_team",
];

export const STAGE_META: Record<Exclude<StageId, "complete">, StageMeta> = {
  strategist: { label: "Strategist", placeholder: "Planning as many targeted search queries as the question needs" },
  retrieval: { label: "Retrieval", placeholder: "Querying PubMed, Europe PMC, ClinicalTrials.gov · checking retractions" },
  appraiser: { label: "Appraiser", placeholder: "Scoring evidence by design, recency & relevance" },
  synthesizer: { label: "Synthesizer", placeholder: "Drafting a fully-cited answer" },
  verifier: { label: "Verifier", placeholder: "Checking existence, entailment & standing of every claim" },
  red_team: { label: "Red Team", placeholder: "Adversarial audit for weak or risky claims" },
};

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function joinTrunc(list: string[] | undefined, max: number): string {
  const items = list ?? [];
  const shown = items.slice(0, max).map((q) => `"${q}"`);
  const extra = items.length - shown.length;
  return shown.join("; ") + (extra > 0 ? ` +${extra} more` : "");
}

/** Builds the human-readable detail line from the REAL fields the
 * matching pipeline.py _emit() call sends -- mirrors the previous
 * embedded-UI's formatDetail(), now typed against StageEvent. */
export function formatStageDetail(stageId: Exclude<StageId, "complete">, event: StageEvent | undefined): string {
  if (!event || event.status === "start") return STAGE_META[stageId].placeholder;
  switch (stageId) {
    case "strategist": {
      if (event.status === "progress") {
        const e = event as Extract<StageEvent, { stage: "strategist"; status: "progress" }>;
        const n = num(e.query_count);
        return `round ${num(e.round)}: ${n} quer${n === 1 ? "y" : "ies"} drafted, under review`;
      }
      const e = event as Extract<StageEvent, { stage: "strategist"; status: "done" }>;
      const n = e.queries?.length ?? 0;
      return `${n} quer${n === 1 ? "y" : "ies"} planned: ${joinTrunc(e.queries, 2)}`;
    }
    case "retrieval": {
      const e = event as Extract<StageEvent, { stage: "retrieval"; status: "done" }>;
      const n = num(e.pool_size);
      return `${n} record${n === 1 ? "" : "s"} retrieved${e.retracted ? ` · ${e.retracted} retracted excluded` : ""}`;
    }
    case "appraiser": {
      if (event.status === "progress") {
        const e = event as Extract<StageEvent, { stage: "appraiser"; status: "progress" }>;
        return `scoring batch ${num(e.batch)}/${num(e.batch_count)}`;
      }
      const e = event as Extract<StageEvent, { stage: "appraiser"; status: "done" }>;
      const n = num(e.appraised);
      return `${n} record${n === 1 ? "" : "s"} scored${typeof e.top_score === "number" ? ` · top score ${e.top_score}` : ""}`;
    }
    case "synthesizer": {
      const e = event as Extract<StageEvent, { stage: "synthesizer"; status: "done" }>;
      if (e.abstained) return "Judged the evidence insufficient — abstaining";
      const n = num(e.sentences);
      return `${n} cited sentence${n === 1 ? "" : "s"} drafted`;
    }
    case "verifier": {
      if (event.status === "progress") {
        const e = event as Extract<StageEvent, { stage: "verifier"; status: "progress" }>;
        return `checking claim ${num(e.claim)}/${num(e.claim_count)}`;
      }
      const e = event as Extract<StageEvent, { stage: "verifier"; status: "done" }>;
      const f = e.funnel ?? { claims_generated: 0, claims_deleted: 0, claims_kept: 0, by_reason: {} };
      return `${num(f.claims_generated)} generated → ${num(f.claims_deleted)} deleted → ${num(f.claims_kept)} kept`;
    }
    case "red_team": {
      const e = event as Extract<StageEvent, { stage: "red_team"; status: "done" }>;
      const n = num(e.flagged_claims);
      return `${n} claim${n === 1 ? "" : "s"} flagged`;
    }
    default:
      return "";
  }
}

function llmCallsOf(event: StageEvent | undefined): number | null {
  if (!event || event.status !== "done") return null;
  const withCalls = event as { llm_calls?: number | null };
  return typeof withCalls.llm_calls === "number" ? withCalls.llm_calls : null;
}

export function StageRow({ stageId, event }: { stageId: Exclude<StageId, "complete">; event: StageEvent | undefined }) {
  const status =
    event?.status === "done" ? "done" : event?.status === "start" || event?.status === "progress" ? "active" : "pending";
  const index = STAGE_ORDER.indexOf(stageId);
  const meta = STAGE_META[stageId];
  const llmCalls = llmCallsOf(event);

  return (
    <div className={`flex items-center gap-3 rounded-sm px-1 py-2 transition-opacity ${status === "pending" ? "opacity-40" : "opacity-100"}`}>
      <div
        className={`flex h-[22px] w-[22px] flex-none items-center justify-center rounded-full border font-mono text-[10.5px] transition-colors ${
          status === "done"
            ? "border-pass bg-pass-bg text-pass"
            : status === "active"
              ? "border-accent bg-accent-soft shadow-[0_0_0_4px_var(--eb-accent-soft)]"
              : "border-rule bg-paper text-ink-faint"
        }`}
      >
        {status === "done" ? (
          <Check className="h-3 w-3" aria-hidden="true" />
        ) : status === "active" ? (
          <motion.span
            className="h-2 w-2 rounded-full bg-gradient-to-br from-accent to-accent-2"
            animate={{ opacity: [1, 0.4, 1] }}
            transition={{ duration: 1, repeat: Infinity, ease: "easeInOut" }}
          />
        ) : (
          String(index + 1).padStart(2, "0")
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className={`flex items-center gap-1.5 text-[12.5px] font-semibold ${status === "active" ? "text-accent-2" : status === "done" ? "text-ink" : "text-ink-soft"}`}>
          {meta.label}
          {status === "done" && llmCalls !== null && (
            <span
              className={`rounded-full border px-1.5 py-px font-mono text-[9.5px] font-bold ${
                llmCalls === 0 ? "border-rule bg-rule-soft text-ink-faint" : "border-accent/35 bg-accent-soft text-accent-2"
              }`}
            >
              {llmCalls} LLM call{llmCalls === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <div className="mt-0.5 font-mono text-[11px] leading-snug text-ink-faint">{formatStageDetail(stageId, event)}</div>
      </div>
    </div>
  );
}
