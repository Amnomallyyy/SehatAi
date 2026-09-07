import { Database, RefreshCw, Trash2, Zap } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ResultsView } from "../components/results/ResultsView";
import { Button } from "../components/shared/Button";
import { ErrorBanner } from "../components/shared/ErrorBanner";
import { HealthStatusDot } from "../components/shared/HealthStatusDot";
import { ThemeToggle } from "../components/shared/ThemeToggle";
import { useClearHistory, useDeleteHistoryItem, useHistoryItem, useHistoryList } from "../hooks/useHistory";
import type { RunSource } from "../lib/types";

const SOURCE_BADGE: Record<RunSource, { label: string; icon: typeof Zap }> = {
  live: { label: "live run", icon: Zap },
  cache_hit: { label: "from cache", icon: Database },
  mock: { label: "mock demo", icon: RefreshCw },
};

function ListView() {
  const { data, isLoading, error } = useHistoryList({ limit: 100 });
  const del = useDeleteHistoryItem();
  const clearAll = useClearHistory();
  const [confirmClear, setConfirmClear] = useState(false);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <p className="text-[13px] text-ink-faint">{data ? `${data.total} run${data.total === 1 ? "" : "s"}` : " "}</p>
        {data && data.total > 0 && (
          <div className="flex items-center gap-2">
            {confirmClear ? (
              <>
                <span className="text-[12px] text-ink-faint">Delete all history?</span>
                <Button variant="danger" onClick={() => clearAll.mutate(undefined, { onSettled: () => setConfirmClear(false) })}>
                  Confirm
                </Button>
                <Button variant="ghost" onClick={() => setConfirmClear(false)}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button variant="ghost" onClick={() => setConfirmClear(true)}>
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" /> Clear all
              </Button>
            )}
          </div>
        )}
      </div>

      {isLoading && <p className="text-[13px] text-ink-faint">Loading…</p>}
      {error != null && <ErrorBanner error={error} />}
      {data && data.runs.length === 0 && (
        <div className="rounded-[3px] border border-dashed border-rule bg-card p-6 text-center text-[13px] text-ink-faint">
          No questions asked yet.{" "}
          <Link to="/ask" className="text-accent-2 underline decoration-accent-2/60 underline-offset-2 hover:text-accent">
            Ask one
          </Link>
          .
        </div>
      )}
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {data?.runs.map((run) => {
          const badge = SOURCE_BADGE[run.source] ?? SOURCE_BADGE.live;
          const Icon = badge.icon;
          return (
            <li key={run.id} className="flex items-center gap-3 rounded-[3px] border border-rule bg-card p-4 shadow-[var(--eb-shadow)]">
              <Link to={`/history/${run.id}`} className="focus-ring min-w-0 flex-1">
                <p className="truncate text-[13.5px] text-ink hover:text-accent">{run.question}</p>
                <p className="mt-1 flex items-center gap-2 font-mono text-[11px] text-ink-faint">
                  <span className={run.abstained ? "text-flag" : "text-pass"}>{run.abstained ? "abstained" : "answered"}</span>
                  <span>{new Date(run.created_at).toLocaleString()}</span>
                  <span className="inline-flex items-center gap-1">
                    <Icon className="h-3 w-3" aria-hidden="true" /> {badge.label}
                  </span>
                </p>
              </Link>
              <button
                type="button"
                onClick={() => del.mutate(run.id)}
                aria-label={`Delete "${run.question}"`}
                className="focus-ring flex-none rounded p-2 text-ink-faint hover:text-fail"
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function DetailView({ id }: { id: string }) {
  const { data, isLoading, error } = useHistoryItem(id);
  const navigate = useNavigate();
  return (
    <div>
      <button type="button" onClick={() => navigate("/history")} className="focus-ring mb-4 text-[12px] text-ink-faint hover:text-ink">
        &larr; Back to history
      </button>
      {isLoading && <p className="text-[13px] text-ink-faint">Loading…</p>}
      {error != null && <ErrorBanner error={error} />}
      {data && data.source === "mock" && data.question !== data.report.question && (
        <div className="mb-6 rounded-[3px] border border-info/30 bg-info-bg px-4 py-3 text-[12px] text-info">
          You asked <b>&ldquo;{data.question}&rdquo;</b>, but this was answered in mock demo mode, which always
          replays the same canned example regardless of the question asked — the content below is that example,
          not a real answer to your question.
        </div>
      )}
      {data && <ResultsView report={data.report} />}
    </div>
  );
}

export default function History() {
  const { id } = useParams<{ id: string }>();

  return (
    <div className="mx-auto max-w-[1080px] px-6 pb-24">
      <header className="flex flex-wrap items-end justify-between gap-6 border-b border-rule py-11">
        <div>
          <Link to="/" className="focus-ring">
            <h1 className="m-0 text-[27px] font-bold tracking-tight text-ink">
              EvidenceBoard<span className="bg-gradient-to-br from-accent to-accent-2 bg-clip-text text-transparent">.</span>
            </h1>
          </Link>
          <p className="mt-1.5 text-[12px] font-semibold tracking-[0.16em] text-ink-faint uppercase">History</p>
        </div>
        <div className="flex items-center gap-3">
          <HealthStatusDot />
          <Link to="/ask" className="focus-ring rounded-[3px] border border-rule bg-card px-3 py-1.5 text-[12px] font-semibold text-ink-soft transition hover:border-ink-faint">
            Ask a question
          </Link>
          <ThemeToggle />
        </div>
      </header>
      <div className="mt-8">{id ? <DetailView id={id} /> : <ListView />}</div>
    </div>
  );
}
