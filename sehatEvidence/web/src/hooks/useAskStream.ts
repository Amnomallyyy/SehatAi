import { useCallback, useRef, useState } from "react";
import { type AskOptions, askStream } from "../lib/api";
import type { CacheHitMessage, Report, StageEvent } from "../lib/types";

export type StageId = StageEvent["stage"];
export type StageStatusMap = Partial<Record<Exclude<StageId, "complete">, StageEvent>>;

export interface LogLine {
  id: number;
  elapsedMs: number;
  stageId: StageId | "cache";
  status: string;
  event: StageEvent | CacheHitMessage;
}

interface AskStreamState {
  busy: boolean;
  stages: StageStatusMap;
  log: LogLine[];
  cacheHit: CacheHitMessage | null;
  report: Report | null;
  error: unknown;
  elapsedMs: number;
}

const INITIAL_STATE: AskStreamState = {
  busy: false,
  stages: {},
  log: [],
  cacheHit: null,
  report: null,
  error: null,
  elapsedMs: 0,
};

/**
 * Drives one /api/ask/stream request. Every stage/log entry comes
 * straight from the real NDJSON events (see pipeline.py's on_event /
 * api/server.py's _handle_ask_stream) -- nothing here is timed or
 * simulated. A {"type":"cache_hit"} message short-circuits the stage
 * tracker entirely rather than faking six stage events that didn't run.
 */
export function useAskStream() {
  const [state, setState] = useState<AskStreamState>(INITIAL_STATE);
  const abortRef = useRef<AbortController | null>(null);
  const startRef = useRef<number>(0);
  const logIdRef = useRef(0);
  const elapsedTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    if (elapsedTimerRef.current) {
      clearInterval(elapsedTimerRef.current);
      elapsedTimerRef.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    stop();
    setState(INITIAL_STATE);
  }, [stop]);

  const ask = useCallback(
    async (question: string, opts: AskOptions = {}) => {
      stop();
      const controller = new AbortController();
      abortRef.current = controller;
      startRef.current = Date.now();
      logIdRef.current = 0;
      setState({ ...INITIAL_STATE, busy: true });

      elapsedTimerRef.current = setInterval(() => {
        setState((s) => (s.busy ? { ...s, elapsedMs: Date.now() - startRef.current } : s));
      }, 100);

      const pushLog = (stageId: StageId | "cache", status: string, event: StageEvent | CacheHitMessage) => {
        logIdRef.current += 1;
        setState((s) => ({
          ...s,
          log: [...s.log, { id: logIdRef.current, elapsedMs: Date.now() - startRef.current, stageId, status, event }],
        }));
      };

      try {
        for await (const msg of askStream(question, opts, controller.signal)) {
          if (msg.type === "stage") {
            const event = msg as unknown as StageEvent;
            if (event.stage === "complete") {
              pushLog("complete" as StageId, event.status, event);
              continue;
            }
            setState((s) => ({ ...s, stages: { ...s.stages, [event.stage]: event } }));
            pushLog(event.stage, event.status, event);
          } else if (msg.type === "cache_hit") {
            setState((s) => ({ ...s, cacheHit: msg }));
            pushLog("cache", "cache_hit", msg);
          } else if (msg.type === "result") {
            setState((s) => ({ ...s, report: msg.report }));
          }
        }
        setState((s) => ({ ...s, busy: false }));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setState((s) => ({ ...s, busy: false, error: err }));
      } finally {
        if (elapsedTimerRef.current) {
          clearInterval(elapsedTimerRef.current);
          elapsedTimerRef.current = null;
        }
      }
    },
    [stop],
  );

  return { ...state, ask, stop, reset };
}
