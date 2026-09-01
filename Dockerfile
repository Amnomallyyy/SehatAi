# SehatAI backend -- symptom/diet triage chat (webserver.js).
# Build context: repo root (this file's own directory).
FROM node:24-slim

WORKDIR /app

# Installed from the manifest first so this layer only rebuilds when
# dependencies actually change, not on every source edit.
COPY package.json package-lock.json ./
RUN npm install --omit=dev

COPY . .
# .dockerignore strips node_modules/, every other service's own directory
# tree isn't needed here -- this container only ever runs webserver.js.

EXPOSE 3000

CMD ["node", "webserver.js"]
