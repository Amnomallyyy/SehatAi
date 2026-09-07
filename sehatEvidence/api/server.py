"""
api/server.py -- EvidenceBoard HTTP surface.

A dependency-free Python backend: stdlib ``ThreadingHTTPServer`` plus the
project's own modules (no flask/fastapi/uvicorn). The frontend (see
../web/) is a separate Vite/React app with its own build step -- this
module is a pure JSON API in dev, and additionally serves that app's
built static files (``../web/dist``) in production so ``python -m
api.server`` alone is still enough to run the whole product after one
``npm run build``.

Endpoints
---------
GET    /                       built frontend (web/dist/index.html), if built
GET    /api/health              liveness + which models/keys this process is configured with
POST   /api/ask                 {"question", "force_refresh"?} -> the pipeline's full report dict
POST   /api/ask/stream          same, but NDJSON progress events then the report
GET    /api/history              list past runs (summaries only)
GET    /api/history/{id}         one past run's full report
DELETE /api/history/{id}         delete one past run
DELETE /api/history              wipe all history (body: {"confirm": true})

Design notes
------------
The pipeline is built ONCE in :func:`main` and shared by every request
thread (the agents are stateless; the FailoverLLMClient's key rotation is
process-wide on purpose -- a key that dies stays retired for everyone).
:meth:`EvidencePipeline.run` never raises and reports abstention as a
successful outcome, so ``/api/ask`` returns 200 with ``abstained=true``
rather than an error status; 500 is reserved for genuine server faults.

History/cache (core/store.py): both ask endpoints check for a prior run
of the same (normalized) question before calling the pipeline, and
record every run afterward. A cache hit on the streaming endpoint sends
exactly one ``{"type":"cache_hit",...}`` line, never faked stage events
-- see _handle_ask_stream. ``force_refresh: true`` bypasses the cache.

``--mock`` makes every ask replay demo/mock_response.json instead of
calling the network -- the offline demo path. Mock runs are recorded
with source="mock", never conflated with "live" in history.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# Allow both `python -m api.server` (from sehatEvidence/) and
# `python api/server.py` (any working directory) to import the project.
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Settings, get_settings  # noqa: E402
from core import store  # noqa: E402
from pipeline import (  # noqa: E402
    ABSTAIN_LLM_DEAD,
    ABSTAIN_LLM_SYNTHESIS,
    EvidencePipeline,
    build_default_pipeline,
)

__all__ = ["EvidenceHandler", "main", "run_server"]

#: Refuse absurd request bodies outright (a clinical question is a sentence).
MAX_BODY_BYTES = 64 * 1024

#: The built frontend, produced by `npm run build` in web/. Resolved once
#: at import time; do_GET re-checks .exists() per request so a build that
#: appears while the server is already running (or a fresh checkout with
#: no build yet) is handled without a restart.
WEB_DIST = (Path(__file__).parent.parent / "web" / "dist").resolve()

#: The ONLY cross-origin callers this API ever needs to trust: this
#: project's own Vite dev server (see web/vite.config.ts's proxy comment
#: -- in production the built frontend is same-origin and needs no CORS
#: allowance at all), plus CareLink's frontend (architecture doc §01/§07 --
#: EvidenceBoard is embedded there as the doctor-side "Clinical Evidence"
#: tab, a genuinely different origin/port, calling this API directly with
#: no auth layer of its own). See _cors()'s docstring for why this must
#: never become a wildcard.
_DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3002", "http://127.0.0.1:3002"}


def _no_build_message() -> bytes:
    return (
        b"<!doctype html><html><body style=\"font-family:monospace;padding:2rem\">"
        b"<h1>EvidenceBoard API is running, but the frontend isn't built yet.</h1>"
        b"<p>Run <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>, "
        b"then reload -- or run <code>npm run dev</code> in web/ for local development "
        b"(it proxies /api/* to this server).</p>"
        b"<p>The API itself is live: <a href=\"/api/health\">/api/health</a></p>"
        b"</body></html>"
    )


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class EvidenceHandler(BaseHTTPRequestHandler):
    """Serves the built frontend (if present) and the JSON API over one
    shared pipeline instance + one shared history/cache database.

    ``pipeline``, ``use_mock``, ``settings`` and ``db`` are class
    attributes set once by :func:`run_server`: every request thread reads
    the same objects, which is safe because the agents hold no
    per-question state and ``db`` writes are internally serialized (see
    core/store.py's ``_write_lock``).
    """

    #: Shared, set by run_server() before the server starts accepting.
    pipeline: Optional[EvidencePipeline] = None
    use_mock: bool = False
    settings: Optional[Settings] = None
    db: Optional[sqlite3.Connection] = None

    server_version = "EvidenceBoard/1.0"
    sys_version = ""  # do not advertise the Python version
    protocol_version = "HTTP/1.1"  # required for keep-alive + Content-Length

    # --- response plumbing -------------------------------------------------

    def _cors(self) -> None:
        """CORS headers (identical on every response, incl. errors).

        Origin-allowlisted, NOT wildcard -- see the security-review note on
        _DEV_ORIGINS. A wildcard `Access-Control-Allow-Origin: *` combined
        with DELETE would let ANY website the user has open in another tab
        silently issue `DELETE /api/history` (bulk-wipe, needs only the
        static `{"confirm":true}` body) or read `GET /api/history` (which
        can contain sensitive clinical questions) via a background
        cross-origin fetch -- the browser's CORS preflight would approve
        it, no user interaction beyond having this server running. Two
        origins never need this header at all regardless of what it says:
        same-origin requests (the built frontend in production) and non-
        browser clients (curl, contract.md's examples) -- CORS is a
        browser-only, response-reading restriction. So this only actually
        restricts an unrecognized third-party origin, which is the point.
        """
        origin = self.headers.get("Origin")
        if origin in _DEV_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # else: no Origin header (same-origin/non-browser -- unaffected, see
        # above) or an origin we don't recognize -- send no CORS headers at
        # all, so a browser blocks every cross-origin use, read or write,
        # from anywhere else. Allow-Methods/-Headers would be meaningless
        # without a matching Allow-Origin anyway.

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        """Send one complete response (headers + body)."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _write_chunk(self, data: bytes) -> None:
        """Write one HTTP/1.1 chunked-transfer frame and flush immediately.

        Used only by the streaming endpoint, where the total body length
        isn't known up front (events arrive live), so plain Content-Length
        framing (``_respond``) doesn't apply.
        """
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunks(self) -> None:
        """Terminate a chunked response (the zero-length final chunk)."""
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _send_json(self, status: int, payload: dict) -> None:
        """Serialize ``payload`` as UTF-8 JSON."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        """Uniform machine-readable error shape: ``{"error": "..."}``."""
        self._send_json(status, {"error": message})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """Route access logs through the project's ``[server]`` prefix."""
        print(f"[server] {self.address_string()} {fmt % args}")

    # --- HTTP verbs ---------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS preflight: headers only, no body."""
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/health":
            self._send_json(200, self._health())
            return
        if path == "/api/history":
            self._handle_history_list()
            return
        if path.startswith("/api/history/"):
            self._handle_history_detail(path[len("/api/history/"):])
            return
        if path.startswith("/api/"):
            self._send_error_json(404, f"no such endpoint: {path}")
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        """Same routing as GET; _respond() suppresses the body."""
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/ask":
            self._handle_ask()
            return
        if path == "/api/ask/stream":
            self._handle_ask_stream()
            return
        self._send_error_json(404, f"no such endpoint: {path}")

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/history":
            self._handle_history_clear()
            return
        if path.startswith("/api/history/"):
            self._handle_history_delete(path[len("/api/history/"):])
            return
        self._send_error_json(404, f"no such endpoint: {path}")

    # --- static file serving (production: the built frontend) ---------------

    def _serve_static(self, path: str) -> None:
        """Serve ../web/dist -- the built frontend uses HashRouter (routes
        are '/#/ask', '/#/history', never real server paths), so there is
        deliberately NO SPA-fallback/catch-all route here: every real GET
        path is either '/', '/index.html', or a literal file under
        web/dist/assets/. That keeps this handler's path-traversal surface
        to "does the resolved path stay inside WEB_DIST", not "reimplement
        client-side routing on the server" -- see the plan's security
        review notes on this being new attack surface.
        """
        if not WEB_DIST.is_dir():
            self._respond(200, _no_build_message(), "text/html; charset=utf-8")
            return

        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        try:
            resolved = (WEB_DIST / rel).resolve()
        except (OSError, ValueError):
            self._send_error_json(400, "malformed path")
            return

        if resolved != WEB_DIST and WEB_DIST not in resolved.parents:
            # Would escape web/dist/ (e.g. "..%2f..%2fetc/passwd") -- reject
            # outright rather than let it fall through to a 404 that might
            # leak whether the file exists elsewhere on disk.
            self._send_error_json(403, "forbidden")
            return
        if not resolved.is_file():
            self._send_error_json(404, f"no such file: {path}")
            return

        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._respond(200, resolved.read_bytes(), content_type)

    # --- endpoint implementations -------------------------------------------

    def _health(self) -> dict:
        """Liveness plus the model/key configuration of THIS process."""
        settings = self.settings or get_settings()
        return {
            "status": "ok",
            "llm_model": settings.llm_model,
            "sensitive_model": settings.llm_sensitive_model,
            "keys_count": len(settings.llm_api_keys),
        }

    def _read_json_body(self) -> dict:
        """Read and parse the request body.

        Raises ValueError with a client-facing message for anything the
        caller can fix (bad length, oversized body, malformed JSON).
        Returns {} for a DELETE with no body (confirm-flag endpoints check
        for that explicitly rather than treating an empty body as a 400).
        """
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except ValueError:
            raise ValueError("invalid Content-Length header")
        if length == 0:
            return {}
        if length < 0:
            raise ValueError("invalid Content-Length header")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("request body must be valid UTF-8 JSON")
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    # --- history / cache -----------------------------------------------------

    def _check_cache(self, cache_key: str) -> Optional[dict]:
        if self.db is None:
            return None
        return store.find_cached(self.db, cache_key)

    @staticmethod
    def _is_transient_failure(report: dict) -> bool:
        """True iff this report abstained because the LLM backend itself
        was unreachable (ABSTAIN_LLM_SYNTHESIS / ABSTAIN_LLM_DEAD), not
        because of anything about the evidence or the question.

        BUG FIXED HERE: such a report used to be cached exactly like a
        real answer -- so a question that happened to hit an LLM outage
        got permanently stuck replaying "no answer" on every later ask,
        indistinguishable from "this question doesn't work" (see
        core/store.py's module docstring for the full story; confirmed
        against a real stuck row in this project's own history). A report
        this flags gets recorded with cacheable=False: still real,
        visible history, just never served back by find_cached().
        """
        if not report.get("abstained"):
            return False
        reasons = report.get("abstain_reasons") or []
        return ABSTAIN_LLM_SYNTHESIS in reasons or ABSTAIN_LLM_DEAD in reasons

    def _record(self, *, question: str, cache_key: str, report: dict, source: str) -> Optional[str]:
        if self.db is None:
            return None
        try:
            return store.record_run(
                self.db, question=question, cache_key=cache_key,
                report=report, abstained=bool(report.get("abstained")), source=source,
                cacheable=not self._is_transient_failure(report),
            )
        except Exception as exc:  # history is a convenience, never a hard dependency
            print(f"[server] failed to record history ({exc}); continuing")
            return None

    def _handle_history_list(self) -> None:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)

        def _int_param(name: str, default: int) -> int:
            raw = query.get(name, [None])[0]
            if raw is None:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"'{name}' must be an integer")

        try:
            limit = _int_param("limit", 50)
            offset = _int_param("offset", 0)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return

        abstained: Optional[bool] = None
        raw_abstained = query.get("abstained", [None])[0]
        if raw_abstained is not None:
            abstained = raw_abstained.strip().lower() in ("1", "true", "yes")

        if self.db is None:
            self._send_json(200, {"runs": [], "total": 0})
            return
        runs, total = store.list_runs(self.db, limit=limit, offset=offset, abstained=abstained)
        self._send_json(200, {"runs": runs, "total": total})

    def _handle_history_detail(self, run_id: str) -> None:
        if not store.RUN_ID_PATTERN.match(run_id):
            self._send_error_json(400, "malformed run id")
            return
        if self.db is None:
            self._send_error_json(404, f"no such run: {run_id}")
            return
        run = store.get_run(self.db, run_id)
        if run is None:
            self._send_error_json(404, f"no such run: {run_id}")
            return
        self._send_json(200, run)

    def _handle_history_delete(self, run_id: str) -> None:
        if not store.RUN_ID_PATTERN.match(run_id):
            self._send_error_json(400, "malformed run id")
            return
        if self.db is None or not store.delete_run(self.db, run_id):
            self._send_error_json(404, f"no such run: {run_id}")
            return
        self._respond(204, b"", "application/json; charset=utf-8")

    def _handle_history_clear(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        if payload.get("confirm") is not True:
            self._send_error_json(400, "clearing all history requires a JSON body of {\"confirm\": true}")
            return
        deleted = store.clear_all(self.db) if self.db is not None else 0
        self._send_json(200, {"deleted": deleted})

    # --- ask -----------------------------------------------------------------

    def _read_ask_payload(self) -> Optional[tuple[str, bool]]:
        """Shared body parsing for /api/ask and /api/ask/stream.

        Returns (question, force_refresh) or None after already having
        sent an error response.
        """
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return None
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self._send_error_json(400, "field 'question' is required and must be a non-empty string")
            return None
        force_refresh = payload.get("force_refresh") is True
        return question.strip(), force_refresh

    def _handle_ask(self) -> None:
        """POST /api/ask -- run one question through the pipeline (or
        replay a cached report), synchronously.

        An abstention is a successful outcome (HTTP 200 with
        ``abstained: true``), so 500 here means the server itself broke.
        """
        parsed = self._read_ask_payload()
        if parsed is None:
            return
        question, force_refresh = parsed
        cache_key = store.normalize_question(question)

        if not force_refresh:
            cached = self._check_cache(cache_key)
            if cached is not None:
                print(f"[server] ask (cache hit): {question[:120]}")
                report = dict(cached["report"])
                report["cached"] = True
                report["run_id"] = cached["id"]
                self._send_json(200, report)
                return

        if self.pipeline is None:
            self._send_error_json(503, "pipeline is not available on this server")
            return

        mode = " (mock)" if self.use_mock else ""
        print(f"[server] ask{mode}: {question[:120]}")
        try:
            report = self.pipeline.run(question, use_mock=self.use_mock)
        except Exception as exc:  # pipeline.run() should not raise -- be safe
            print(f"[server] error: {exc}")
            traceback.print_exc()
            self._send_error_json(500, f"pipeline failed: {exc}")
            return

        if not isinstance(report, dict):
            print(f"[server] error: pipeline returned {type(report).__name__}, expected dict")
            self._send_error_json(500, "pipeline returned a malformed report")
            return

        run_id = self._record(
            question=question, cache_key=cache_key, report=report,
            source="mock" if self.use_mock else "live",
        )
        report = dict(report)
        report["cached"] = False
        report["run_id"] = run_id

        try:
            self._send_json(200, report)
        except (TypeError, ValueError) as exc:
            print(f"[server] error: report is not JSON-serializable: {exc}")
            self._send_error_json(500, "report is not JSON-serializable")
            return
        print(
            f"[server] answered: abstained={report.get('abstained')} "
            f"claims={len(report.get('claims') or [])} "
            f"evidence={len(report.get('evidence') or [])}"
        )

    def _handle_ask_stream(self) -> None:
        """POST /api/ask/stream -- same question, but pushes one NDJSON
        line per real pipeline stage as it actually completes, then a
        final line with the full report. A cache hit sends exactly one
        ``{"type":"cache_hit",...}`` line instead -- NEVER a faked set of
        six stage events for agents that didn't run this time.

        Every progress line otherwise comes from EvidencePipeline's
        on_event callback (see pipeline.py): real stage boundaries, real
        counts, real LLM-call deltas from the shared FailoverLLMClient.
        Nothing here is timed or simulated -- if a stage is slow, its line
        simply arrives late.

        Framed as HTTP/1.1 chunked transfer (no Content-Length is
        possible for a body whose length isn't known up front) so the
        connection stays keep-alive-safe like every other response here.
        """
        parsed = self._read_ask_payload()
        if parsed is None:
            return
        question, force_refresh = parsed
        cache_key = store.normalize_question(question)

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()

        def send_line(payload: dict) -> None:
            line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            self._write_chunk(line)

        if not force_refresh:
            cached = self._check_cache(cache_key)
            if cached is not None:
                print(f"[server] ask (stream, cache hit): {question[:120]}")
                try:
                    send_line({"type": "cache_hit", "run_id": cached["id"], "cached_at": cached["created_at"]})
                    report = dict(cached["report"])
                    report["cached"] = True
                    report["run_id"] = cached["id"]
                    send_line({"type": "result", "report": report})
                except Exception as exc:
                    print(f"[server] stream write failed ({exc}); client likely gone")
                finally:
                    try:
                        self._end_chunks()
                    except Exception:
                        pass
                return

        if self.pipeline is None:
            try:
                send_line({"type": "error", "error": "pipeline is not available on this server"})
                self._end_chunks()
            except Exception:
                pass
            return

        mode = " (mock)" if self.use_mock else ""
        print(f"[server] ask{mode} (stream): {question[:120]}")

        def on_event(event: dict) -> None:
            # A client that has gone away must not break the pipeline run
            # (it keeps computing the answer for its own logs either way);
            # the broad except mirrors pipeline._emit's own fail-open rule.
            try:
                send_line({"type": "stage", **event})
            except Exception as exc:
                print(f"[server] stream write failed ({exc}); client likely gone")

        try:
            report = self.pipeline.run(
                question, use_mock=self.use_mock, on_event=on_event
            )
        except Exception as exc:  # pipeline.run() should not raise -- be safe
            print(f"[server] error: {exc}")
            traceback.print_exc()
            try:
                send_line({"type": "error", "error": f"pipeline failed: {exc}"})
                self._end_chunks()
            except Exception:
                pass
            return

        if not isinstance(report, dict):
            print(f"[server] error: pipeline returned {type(report).__name__}, expected dict")
            try:
                send_line({"type": "error", "error": "pipeline returned a malformed report"})
                self._end_chunks()
            except Exception:
                pass
            return

        run_id = self._record(
            question=question, cache_key=cache_key, report=report,
            source="mock" if self.use_mock else "live",
        )
        report = dict(report)
        report["cached"] = False
        report["run_id"] = run_id

        try:
            send_line({"type": "result", "report": report})
        except (TypeError, ValueError) as exc:
            print(f"[server] error: report is not JSON-serializable: {exc}")
            try:
                send_line({"type": "error", "error": "report is not JSON-serializable"})
            except Exception:
                pass
        except Exception as exc:
            print(f"[server] stream write failed ({exc}); client likely gone")
        finally:
            try:
                self._end_chunks()
            except Exception:
                pass
        print(
            f"[server] answered (stream): abstained={report.get('abstained')} "
            f"claims={len(report.get('claims') or [])} "
            f"evidence={len(report.get('evidence') or [])}"
        )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def run_server(
    pipeline: EvidencePipeline,
    settings: Settings,
    use_mock: bool = False,
) -> None:
    """Bind ``settings.server_host:server_port`` and serve until interrupted."""
    EvidenceHandler.pipeline = pipeline
    EvidenceHandler.settings = settings
    EvidenceHandler.use_mock = use_mock
    EvidenceHandler.db = store.connect(settings.db_path)

    httpd = ThreadingHTTPServer((settings.server_host, settings.server_port), EvidenceHandler)
    httpd.daemon_threads = True
    host, port = settings.server_host, settings.server_port
    print(f"[server] listening on http://{host}:{port}")
    print(f"[server] history/cache database: {settings.db_path}")
    if WEB_DIST.is_dir():
        print(f"[server] serving built frontend from {WEB_DIST}")
    else:
        print("[server] no frontend build found -- run `npm run build` in web/, or `npm run dev` there for local development")
    if use_mock:
        print("[server] mock mode: every ask replays demo/mock_response.json")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[server] shutting down")
    finally:
        httpd.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: build the pipeline once, then serve."""
    # Python fully buffers stdout when it isn't a terminal (e.g. redirected
    # to a log file or piped), so the [server]/[pipeline]/agent lines that
    # are this process's only visible proof of what each stage actually did
    # would otherwise sit invisible in a buffer until exit. Force line
    # buffering so every print() lands immediately, live-tail-able.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # stdout/stderr already unbuffered or not reconfigurable; fine

    parser = argparse.ArgumentParser(
        prog="api.server",
        description="Serve the EvidenceBoard API (and built frontend, if present).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="replay demo/mock_response.json for every ask (offline demo)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        pipeline = build_default_pipeline(settings)
    except Exception as exc:
        # Almost always "no LLM key configured". Fatal for a live run; in
        # mock mode there is nothing to answer with either, so say so plainly.
        print(f"[server] error: cannot build pipeline: {exc}")
        print("[server] set LLM_API_KEY or LLM_API_KEYS (see .env.example)")
        return 1
    print("[server] pipeline built")

    run_server(pipeline, settings, use_mock=args.mock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
