# EvidenceBoard -- doctor-side clinical-evidence chatbot (api/server.py).
# Build context: sehatEvidence/ (this file's own directory).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser . .
# /app/data holds the question-history/cache SQLite DB (a named volume in
# docker-compose.yml) -- created here so the volume inherits appuser
# ownership.
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

# Must bind 0.0.0.0 inside the container, not the config default
# 127.0.0.1 -- otherwise the port mapping has nothing to reach.
ENV SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8002 \
    DB_PATH=/app/data/evidenceboard.db

USER appuser
EXPOSE 8002

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "api/server.py"]
