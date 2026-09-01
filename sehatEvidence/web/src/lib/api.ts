import type {
  ApiErrorBody,
  HealthResponse,
  HistoryDetailResponse,
  HistoryListResponse,
  Report,
  StreamMessage,
} from "./types";

/** Thrown for a well-formed HTTP error response (`{"error": "..."}`), the
 * shape every endpoint in api/server.py returns for 400/404/500/503. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Thrown when the request never reached the server at all (offline,
 * connection refused, DNS failure, CORS block) -- distinct from ApiError
 * so the UI can say "can't reach the server" instead of a generic message. */
export class NetworkError extends Error {
  constructor(message = "Could not reach the EvidenceBoard server.") {
    super(message);
    this.name = "NetworkError";
  }
}

/** Thrown for a failure specific to the NDJSON streaming protocol itself
 * (a malformed line, an explicit {"type":"error"} frame, or the stream
 * ending without ever sending a result) -- distinct from ApiError because
 * by the time this fires, the HTTP response itself already succeeded. */
export class StreamError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StreamError";
  }
}

async function parseErrorBody(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as ApiErrorBody;
    if (body && typeof body.error === "string") return body.error;
  } catch {
    // fall through to the generic message below
  }
  return `Request failed with HTTP ${response.status}.`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new NetworkError();
  }
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorBody(response));
  }
  // 204 (DELETE /api/history/{id}) and any other empty body have nothing to
  // parse -- calling .json() on them throws a SyntaxError that would
  // otherwise silently break every caller expecting T to be void here.
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/api/health");
}

export interface AskOptions {
  forceRefresh?: boolean;
}

/** Synchronous /api/ask -- returns the full report in one response, no
 * progress events. Used by History (replaying a stored run needs no
 * streaming) and as a fallback if the browser can't stream a POST body. */
export function askSync(question: string, opts: AskOptions = {}): Promise<Report> {
  return request<Report>("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, force_refresh: opts.forceRefresh ?? false }),
  });
}

/** Streams /api/ask/stream as an async generator of parsed NDJSON
 * messages. The caller (useAskStream) drives UI state off each yielded
 * message; this function only handles the transport (chunk buffering,
 * line splitting, JSON parsing) and throws NetworkError/ApiError/
 * StreamError for the failure classes described above. */
export async function* askStream(
  question: string,
  opts: AskOptions = {},
  signal?: AbortSignal,
): AsyncGenerator<StreamMessage, void, unknown> {
  let response: Response;
  try {
    response = await fetch("/api/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, force_refresh: opts.forceRefresh ?? false }),
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new NetworkError();
  }

  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorBody(response));
  }
  if (!response.body || !response.body.getReader) {
    throw new StreamError("This browser does not support streamed responses.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawResult = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        let msg: StreamMessage;
        try {
          msg = JSON.parse(trimmed) as StreamMessage;
        } catch {
          continue; // a malformed line is skipped, not fatal -- matches the previous UI's tolerance
        }
        if (msg.type === "error") {
          throw new StreamError(msg.error || "Pipeline error.");
        }
        if (msg.type === "result") sawResult = true;
        yield msg;
      }
    }
  } finally {
    reader.releaseLock();
  }

  if (!sawResult) {
    throw new StreamError("Stream ended without a result.");
  }
}

export interface HistoryListParams {
  limit?: number;
  offset?: number;
  abstained?: boolean;
}

export function getHistory(params: HistoryListParams = {}): Promise<HistoryListResponse> {
  const search = new URLSearchParams();
  if (params.limit != null) search.set("limit", String(params.limit));
  if (params.offset != null) search.set("offset", String(params.offset));
  if (params.abstained != null) search.set("abstained", String(params.abstained));
  const qs = search.toString();
  return request<HistoryListResponse>(`/api/history${qs ? `?${qs}` : ""}`);
}

export function getHistoryItem(id: string): Promise<HistoryDetailResponse> {
  return request<HistoryDetailResponse>(`/api/history/${encodeURIComponent(id)}`);
}

export async function deleteHistoryItem(id: string): Promise<void> {
  await request<void>(`/api/history/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function clearHistory(): Promise<{ deleted: number }> {
  return request<{ deleted: number }>("/api/history", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
}
