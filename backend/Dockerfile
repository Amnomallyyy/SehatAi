# CareLink backend -- doctor-patient portal API, plus the two bridge
# routes (sehatai_bridge.py, lab_reports.py) that talk to SehatAI and
# DataFetch.
#
# BUILD CONTEXT MUST BE THE REPO ROOT, not this backend/ directory --
# lab_reports.py shells out to DataFetch's main.py (see its own DATAFETCH_DIR/
# DATAFETCH_PYTHON doc comment), and Docker cannot COPY a path outside its
# build context. Run this as:
#   docker build -f backend/Dockerfile -t carelink-backend .
# (docker-compose.yml already does this correctly.)
FROM python:3.11-slim

WORKDIR /app

# Two requirements files: this app's own, and DataFetch's (datafetch/) --
# both installed into the same environment so one `python` interpreter can
# run either.
COPY backend/requirements.txt ./backend/requirements.txt
COPY datafetch/requirements.txt ./datafetch/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt -r datafetch/requirements.txt

# CareLink's own app.
COPY backend/ ./backend/

# DataFetch's modules that lab_reports.py's subprocess call needs (see
# its own module doc comment) -- deliberately not the whole repo root,
# just what this container actually runs. Nested into ./datafetch/ so
# the subprocess's "datafetch/main.py" argument resolves identically
# here (DATAFETCH_DIR=/app below) and in local dev (DATAFETCH_DIR
# defaults to the repo root).
COPY datafetch/auth.py datafetch/clients.py datafetch/history.py datafetch/main.py datafetch/pipeline.py datafetch/schemas.py ./datafetch/

# Matches lab_reports.py's env-var contract: DATAFETCH_DIR is where
# main.py landed above (the container's /app), DATAFETCH_PYTHON is this
# same interpreter -- no second venv needed inside one container.
ENV DATAFETCH_DIR=/app
ENV DATAFETCH_PYTHON=python

WORKDIR /app/backend
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
