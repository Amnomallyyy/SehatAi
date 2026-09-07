import { History as HistoryIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AgentActivityPanel } from "../components/live/AgentActivityPanel";
import { AskForm, type AskSubmitOptions } from "../components/ask/AskForm";
import { SeedQuestions } from "../components/ask/SeedQuestions";
import { buildQuestionWithPrefs } from "../components/ask/RetrievalPreferences";
import { ResultsView } from "../components/results/ResultsView";
import { DisclaimerBar } from "../components/shared/DisclaimerBar";
import { ErrorBanner } from "../components/shared/ErrorBanner";
import { HealthStatusDot } from "../components/shared/HealthStatusDot";
import { ThemeToggle } from "../components/shared/ThemeToggle";
import { useAskStream } from "../hooks/useAskStream";

export default function Ask() {
  const { busy, stages, log, cacheHit, report, error, elapsedMs, ask } = useAskStream();
  const [panelOpen, setPanelOpen] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);

  function handleSubmit(question: string, opts: AskSubmitOptions) {
    const finalQuestion = buildQuestionWithPrefs(question, opts.prefs);
    setPendingQuestion(question);
    setPanelOpen(true);
    void ask(finalQuestion, { forceRefresh: opts.forceRefresh });
  }

  // Auto-close once the report lands -- a brief pause first so the
  // completed stage tracker (all checkmarks, or the cache-hit state) is
  // actually visible for a moment rather than vanishing the instant
  // results are ready. The user can still reopen it via "view agent
  // activity" or by asking again.
  useEffect(() => {
    if (!report || busy) return;
    const timer = setTimeout(() => setPanelOpen(false), 450);
    return () => clearTimeout(timer);
  }, [report, busy]);

  return (
    <div className="mx-auto max-w-[1080px] px-6 pb-24">
      <header className="flex flex-wrap items-end justify-between gap-6 border-b border-rule py-11">
        <div>
          <Link to="/" className="focus-ring">
            <h1 className="m-0 text-[27px] font-bold tracking-tight text-ink">
              EvidenceBoard<span className="bg-gradient-to-br from-accent to-accent-2 bg-clip-text text-transparent">.</span>
            </h1>
          </Link>
          <p className="mt-1.5 text-[12px] font-semibold tracking-[0.16em] text-ink-faint uppercase">Verification-first clinical evidence</p>
        </div>
        <div className="flex items-center gap-3">
          <HealthStatusDot />
          <Link
            to="/history"
            className="focus-ring inline-flex items-center gap-1.5 rounded-[3px] border border-rule bg-card px-3 py-1.5 text-[12px] font-semibold text-ink-soft transition hover:border-ink-faint"
          >
            <HistoryIcon className="h-3.5 w-3.5" aria-hidden="true" /> History
          </Link>
          <ThemeToggle />
        </div>
      </header>

      <div className="mt-6">
        <DisclaimerBar />
      </div>

      <div className="mt-8">
        <AskForm busy={busy} onSubmit={handleSubmit} />
        <SeedQuestions disabled={busy} onPick={(q) => handleSubmit(q, { forceRefresh: false, prefs: { prioritizeRcts: false, includePreprints: false, includeTrialRecords: false } })} />
      </div>

      {busy && !report && (
        <div className="mt-5 flex items-center gap-3 font-mono text-[12px] text-ink-faint">
          <b className="text-ink-soft">Pipeline running</b>
          <span>{(elapsedMs / 1000).toFixed(1)}s</span>
          <span className="text-rule">&middot;</span>
          <button type="button" onClick={() => setPanelOpen(true)} className="focus-ring text-accent-2 underline decoration-dotted underline-offset-2 hover:text-accent">
            view agent activity
          </button>
        </div>
      )}

      {error != null && (
        <div className="mt-6">
          <ErrorBanner error={error} />
        </div>
      )}

      {report && (
        <div className="mt-9">
          <ResultsView report={report} />
        </div>
      )}

      <AgentActivityPanel open={panelOpen} onClose={() => setPanelOpen(false)} stages={stages} log={log} cacheHit={cacheHit} elapsedMs={elapsedMs} />

      <footer className="mt-14 flex flex-wrap justify-between gap-2 border-t border-rule pt-4 font-mono text-[11px] text-ink-faint">
        <span>EvidenceBoard</span>
        {pendingQuestion && !busy && !report && !error && <span>Ready.</span>}
      </footer>
    </div>
  );
}
