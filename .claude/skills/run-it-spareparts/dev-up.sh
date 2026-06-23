#!/usr/bin/env bash
# Launch the full local dev stack: Postgres + backend (uvicorn) + frontend (vite).
# Idempotent: reuses anything already running. Servers run in the background; their
# logs land in .run/. Then drive with driver.mjs (API) and shot.mjs (screenshot).
#
#   .claude/skills/run-it-spareparts/dev-up.sh
#   node .claude/skills/run-it-spareparts/driver.mjs
#   node .claude/skills/run-it-spareparts/shot.mjs
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
DB_CONTAINER="${DB_CONTAINER:-spareparts-dev-db}"
DB_PORT="${DB_PORT:-5433}"
DEV_DB="${DEV_DB:-spareparts_dev}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5176}"
export DATABASE_URL="postgresql+psycopg://spareparts:spareparts@127.0.0.1:${DB_PORT}/${DEV_DB}"
LOGS="$SKILL_DIR/.run"; mkdir -p "$LOGS"

echo "▶ Postgres ($DB_CONTAINER on :$DB_PORT)"
if ! docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
  docker start "$DB_CONTAINER" 2>/dev/null || docker run -d --name "$DB_CONTAINER" \
    -e POSTGRES_USER=spareparts -e POSTGRES_PASSWORD=spareparts -e POSTGRES_DB=spareparts \
    -p "${DB_PORT}:5432" postgres:15 >/dev/null
fi
for i in $(seq 1 30); do docker exec "$DB_CONTAINER" pg_isready -U spareparts >/dev/null 2>&1 && break; sleep 1; done
docker exec "$DB_CONTAINER" psql -U spareparts -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname='${DEV_DB}'" | grep -q 1 || \
  docker exec "$DB_CONTAINER" psql -U spareparts -d postgres -c "CREATE DATABASE ${DEV_DB}" >/dev/null

echo "▶ backend deps + migrate + seed"
cd "$ROOT/backend"
uv sync --extra dev >/dev/null
uv run alembic upgrade head >/dev/null
uv run python "$SKILL_DIR/seed.py"

echo "▶ backend (uvicorn :$BACKEND_PORT)"
if ! curl -sf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
  ( cd "$ROOT/backend" && DATABASE_URL="$DATABASE_URL" ENVIRONMENT=dev \
    nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
    >"$LOGS/backend.log" 2>&1 & echo $! >"$LOGS/backend.pid" )
  for i in $(seq 1 40); do curl -sf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1 && break; sleep 1; done
fi

echo "▶ frontend (vite :$FRONTEND_PORT)"
cd "$ROOT/frontend"; [ -d node_modules ] || npm ci >/dev/null
if ! curl -sf "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null 2>&1; then
  ( cd "$ROOT/frontend" && VITE_PORT="$FRONTEND_PORT" VITE_API_TARGET="http://localhost:${BACKEND_PORT}" \
    nohup npm run dev >"$LOGS/frontend.log" 2>&1 & echo $! >"$LOGS/frontend.pid" )
  for i in $(seq 1 40); do curl -sf "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null 2>&1 && break; sleep 1; done
fi

echo
echo "  ✓ backend   http://localhost:${BACKEND_PORT}    (health: /health)"
echo "  ✓ frontend  http://localhost:${FRONTEND_PORT}"
echo "  ✓ login     admin / admin888"
echo "  ✓ logs      $LOGS/{backend,frontend}.log"
echo
echo "  drive it:   node $SKILL_DIR/driver.mjs   &&   node $SKILL_DIR/shot.mjs"
