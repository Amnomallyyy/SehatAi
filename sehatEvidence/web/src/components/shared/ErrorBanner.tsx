import { AlertTriangle, PlugZap, ServerCrash, Wifi } from "lucide-react";
import { ApiError, NetworkError, StreamError } from "../../lib/api";

/** Distinct messaging per failure class (previously one generic string
 * for every kind of failure). */
export function ErrorBanner({ error, onDismiss }: { error: unknown; onDismiss?: () => void }) {
  let title = "Something went wrong";
  let detail = error instanceof Error ? error.message : "Request failed.";
  let Icon = AlertTriangle;

  if (error instanceof NetworkError) {
    title = "Can't reach the server";
    Icon = Wifi;
  } else if (error instanceof ApiError) {
    Icon = error.status >= 500 ? ServerCrash : AlertTriangle;
    title =
      error.status === 503
        ? "Pipeline unavailable"
        : error.status >= 500
          ? "Server error"
          : error.status === 404
            ? "Not found"
            : "Request rejected";
  } else if (error instanceof StreamError) {
    title = "Stream interrupted";
    Icon = PlugZap;
    detail = detail || "The live connection to the pipeline was interrupted.";
  }

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="flex items-start gap-3 rounded-[3px] border border-fail/30 border-l-[3px] border-l-fail bg-fail-bg px-4 py-3.5"
    >
      <Icon className="mt-0.5 h-4 w-4 flex-none text-fail" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-fail">{title}</p>
        <p className="mt-0.5 text-[13px] leading-snug text-ink-soft">{detail}</p>
      </div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="focus-ring flex-none text-[11px] font-semibold text-ink-faint hover:text-ink"
        >
          Dismiss
        </button>
      )}
    </div>
  );
}
