# EvidenceBoard -- doctor-side clinical-evidence chatbot (api/server.py).
# Build context: sehatEvidence/ (this file's own directory).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Must bind 0.0.0.0 inside the container, not the config default
# 127.0.0.1 -- otherwise the port mapping below has nothing to reach.
ENV SERVER_HOST=0.0.0.0
ENV SERVER_PORT=8002

EXPOSE 8002

CMD ["python", "api/server.py"]
