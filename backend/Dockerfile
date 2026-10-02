# CareLink backend -- doctor-patient portal API, plus the two bridge
# routes (sehatai_bridge.py, lab_reports.py) that talk to SehatAI and
# DataFetch.
#
# BUILD CONTEXT MUST BE THE REPO ROOT, not this backend/ directory --
# lab_reports.py runs DataFetch's main.py (see its own DATAFETCH_DIR/
# DATAFETCH_PYTHON doc comment), and Docker cannot COPY a path outside its
# build context. Run this as:
#   docker build -f backend/Dockerfile -t carelink-backend .
# (docker-compose.yml already does this correctly.)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Two requirements files: this app's own, and DataFetch's (datafetch/) --
# both installed into the same environment so one `python` interpreter can
# run either.
COPY backend/requirements.txt ./backend/requirements.txt
COPY datafetch/requirements.txt ./datafetch/requirements.txt
RUN pip install -r backend/requirements.txt -r datafetch/requirements.txt

# Unprivileged runtime user; created before the COPYs so ownership is set
# in the same layer as the files.
RUN useradd --create-home --uid 10001 appuser

# CareLink's own app.
COPY --chown=appuser:appuser backend/ ./backend/

# DataFetch's modules that lab_reports.py's subprocess call needs (see
# its own module doc comment) -- deliberately not the whole repo root,
# just what this container actually runs. Nested into ./datafetch/ so
# the subprocess's "datafetch/main.py" argument resolves identically
# here (DATAFETCH_DIR=/app below) and in local dev (DATAFETCH_DIR
# defaults to the repo root).
COPY --chown=appuser:appuser datafetch/auth.py datafetch/clients.py datafetch/history.py datafetch/main.py datafetch/pipeline.py datafetch/schemas.py ./datafetch/

# Upload/avatar/staging dirs must exist (and be owned by appuser) in the
# image so the named volumes mounted over them inherit that ownership.
RUN mkdir -p backend/app/static/uploads backend/app/static/avatars backend/app/static/lab_report_staging \
    && chown -R appuser:appuser /app

# Matches lab_reports.py's env-var contract: DATAFETCH_DIR is where
# main.py landed above (the container's /app), DATAFETCH_PYTHON is this
# same interpreter -- no second venv needed inside one container.
ENV DATAFETCH_DIR=/app \
    DATAFETCH_PYTHON=python

USER appuser
WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"

# --proxy-headers: behind the gateway, request.client must be the real
# browser IP (rate limiting keys on it), not nginx's container address.
# One worker on purpose: the rate limiter and the lab-extraction pool are
# per-process; scale out with more containers, not more workers.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log", "--timeout-graceful-shutdown", "20"]
