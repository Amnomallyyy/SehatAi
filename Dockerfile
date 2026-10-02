# SehatAI backend -- symptom/diet triage chat (webserver.js).
# Build context: repo root (this file's own directory) -- package.json/
# package-lock.json live here; the application source lives under sehatai/.
FROM node:24-slim

ENV NODE_ENV=production
WORKDIR /app

# Installed from the manifest first so this layer only rebuilds when
# dependencies actually change, not on every source edit. `npm ci` installs
# exactly what package-lock.json pins (never re-resolves).
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

# Copies sehatai/'s CONTENTS (including public/) into /app, so
# webserver.js lands at /app/webserver.js and its own
# path.join(__dirname, 'public', ...) lookups keep resolving exactly as
# they do locally -- no other service's directory tree gets pulled in.
COPY --chown=node:node sehatai/ ./

# The official image ships an unprivileged `node` user -- never run as root.
USER node

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "webserver.js"]
