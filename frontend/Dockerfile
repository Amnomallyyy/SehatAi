# CareLink frontend -- serves templates/index.html + static/ (app.js, app.css).
# Build context: frontend/ (this file's own directory).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# .dockerignore below strips venv/, __pycache__ -- everything else here
# (frontend_main.py, templates/, static/) is what actually gets served.

EXPOSE 3002

CMD ["python", "-m", "uvicorn", "frontend_main:app", "--host", "0.0.0.0", "--port", "3002"]
