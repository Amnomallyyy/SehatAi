import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Database, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import type { LogLine } from "../../hooks/useAskStream";
import type { CacheHitMessage, StageEvent } from "../../lib/types";
import { formatStageDetail, STAGE_META, STAGE_ORDER } from "./StageRow";
import { StageRow } from "./StageRow";

function formatElapsed(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

function logLineText(line: LogLine): { stageLabel: string; message: string } {
  if (line.stageId === "cache") {
    const e = line.event as CacheHitMessage;
    return { stageLabel: "Cache", message: `Answered instantly from a previous run (${new Date(e.cached_at).toLocaleString()})` };
  }
  if (line.stageId === "complete") {
    const e = line.event as Extract<StageEvent, { stage: "complete" }>;
    return {
      stageLabel: "Pipeline",
      message: e.status === "answered" ? "Answer ready." : `Abstained${e.reason ? `: ${e.reason}` : "."}`,
    };
  }
  const stageId = line.stageId as Exclude<StageEvent["stage"], "complete">;
  const meta = STAGE_META[stageId];
  const event = line.event as StageEvent;
  if (line.status === "start") return { stageLabel: meta.label, message: "started" };
  let message = formatStageDetail(stageId, event);
  const withCalls = event as { llm_calls?: number | null };
  if (typeof withCalls.llm_calls === "number") {
    message +=
      withCalls.llm_calls > 0
        ? ` (${withCalls.llm_calls} real LLM call${withCalls.llm_calls === 1 ? "" : "s"})`
        : " (deterministic — no LLM call)";
  }
  return { stageLabel: meta.label, message };
}

export function AgentActivityPanel({
  open,
  onClose,
  stages,
  log,
  cacheHit,
  elapsedMs,
}: {
  open: boolean;
  onClose: () => void;
  stages: Partial<Record<Exclude<StageEvent["stage"], "complete">, StageEvent>>;
  log: LogLine[];
  cacheHit: CacheHitMessage | null;
  elapsedMs: number;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();
  useFocusTrap(panelRef, open, onClose);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log.length]);

  const doneCount = STAGE_ORDER.filter((id) => stages[id]?.status === "done").length;
  const progressPct = cacheHit ? 100 : Math.max(4, (doneCount / STAGE_ORDER.length) * 100);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="fixed inset-0 z-40 bg-[rgba(3,6,12,.55)] backdrop-blur-[1px]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduceMotion ? 0 : 0.22 }}
            onClick={onClose}
            aria-hidden="true"
          />
          <motion.div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="agent-panel-heading"
            tabIndex={-1}
            className="fixed top-0 right-0 z-41 flex h-screen w-full flex-col border-l border-rule bg-card shadow-[-16px_0_40px_-16px_rgba(0,0,0,.6)] sm:w-[400px]"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ duration: reduceMotion ? 0 : 0.22, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="flex flex-none items-start justify-between gap-3 border-b border-rule px-5 py-4">
              <div>
                <b id="agent-panel-heading" className="block text-sm font-bold text-ink">
                  Agent activity
                </b>
                <span className="mt-0.5 block font-mono text-[11px] text-ink-faint">{formatElapsed(elapsedMs)} elapsed</span>
              </div>
              <button
                type="button"
                onClick={onClose}
                aria-label="Collapse panel"
                className="focus-ring flex h-[26px] w-[26px] flex-none items-center justify-center rounded border border-rule text-ink-faint hover:text-ink"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>

            {cacheHit ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
                <Database className="h-8 w-8 text-accent-2" aria-hidden="true" />
                <p className="text-sm font-semibold text-ink">Served from a previous run</p>
                <p className="text-[12px] text-ink-faint">
                  This exact question was already answered on {new Date(cacheHit.cached_at).toLocaleString()}. No agents ran
                  this time — check &quot;force a fresh run&quot; to bypass the cache.
                </p>
              </div>
            ) : (
              <>
                <div className="flex-none px-4 pt-3.5 pb-1">
                  {STAGE_ORDER.map((id) => (
                    <StageRow key={id} stageId={id} event={stages[id]} />
                  ))}
                </div>
                <div className="mx-4 mb-4 h-[3px] flex-none overflow-hidden rounded-full bg-rule-soft">
                  <motion.div
                    className="h-full bg-gradient-to-r from-accent to-accent-2"
                    animate={{ width: `${progressPct}%` }}
                    transition={{ duration: reduceMotion ? 0 : 0.4, ease: "easeInOut" }}
                  />
                </div>
              </>
            )}

            <div className="flex flex-none items-center justify-between border-t border-rule px-4 py-2.5 text-[10.5px] font-bold tracking-[0.1em] text-ink-faint uppercase">
              <span>Live log</span>
            </div>
            <div ref={logRef} className="flex-1 overflow-y-auto px-4 pb-5" aria-live="polite" aria-relevant="additions">
              {log.map((line) => {
                const { stageLabel, message } = logLineText(line);
                return (
                  <div key={line.id} className="flex gap-2 border-b border-dashed border-rule-soft py-1.5 text-[11.5px] last:border-none">
                    <span className="flex-none pt-px font-mono text-[10px] text-ink-faint">+{formatElapsed(line.elapsedMs)}</span>
                    <div>
                      <span className="font-mono text-[10.5px] font-bold tracking-wide text-accent-2 uppercase">{stageLabel}</span>{" "}
                      <span className="leading-snug text-ink-soft">{message}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
