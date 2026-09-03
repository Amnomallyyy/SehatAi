# SehatAI backend -- symptom/diet triage chat (webserver.js).
# Build context: repo root (this file's own directory) -- package.json/
# package-lock.json/node_modules and the shared root .env live here; the
# application source lives under sehatai/.
FROM node:24-slim

WORKDIR /app

# Installed from the manifest first so this layer only rebuilds when
# dependencies actually change, not on every source edit.
COPY package.json package-lock.json ./
RUN npm install --omit=dev

# Copies sehatai/'s CONTENTS (including public/) into /app, so
# webserver.js lands at /app/webserver.js and its own
# path.join(__dirname, 'public', ...) lookups keep resolving exactly as
# they do locally -- no other service's directory tree gets pulled in,
# unlike the old COPY . . which relied entirely on .dockerignore to
# exclude backend/, frontend/, sehatEvidence/, etc.
COPY sehatai/ ./

EXPOSE 3000

CMD ["node", "webserver.js"]
