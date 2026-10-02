# DietBot -- diet/nutrition recommender (api.py). Internal-only service:
# sehatai-backend calls it over the Docker network; it is never published.
# Build context: dietbot/ (this file's own directory).
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
# Per-patient "last session" pointers (recommender.store_session_id) --
# a named volume in docker-compose.yml so they survive restarts.
RUN mkdir -p /app/sessions && chown -R appuser:appuser /app/sessions
ENV DIETBOT_SESSION_DIR=/app/sessions

USER appuser
EXPOSE 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
