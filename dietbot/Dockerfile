# DietBot -- diet-recommendation HTTP service consumed by
# sehatai/dietBotClient.js over POST /diet. Build context: dietbot/ (this
# file's own directory) -- same pattern as sehatEvidence/Dockerfile.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8001"]
