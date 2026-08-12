#!/usr/bin/env bash
# Reproducible local development bootstrap. It never creates or copies secrets.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
with_db=false
skip_backend=false
skip_frontend=false

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap-dev.sh [--with-db] [--skip-backend] [--skip-frontend]

Installs repository dependencies into backend/.venv and frontend/node_modules.
--with-db starts only the local PostgreSQL Docker service.

Before starting app services, create a local .env from .env.example and use
development-only values. Never copy production .env, API keys, passwords, or SSH
private keys into this checkout.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-db) with_db=true ;;
    --skip-backend) skip_backend=true ;;
    --skip-frontend) skip_frontend=true ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$1" >&2
    exit 1
  }
}

require_command python3
python_minor=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python_ok=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 11)))')
[ "$python_ok" = 1 ] || {
  printf 'Python 3.11+ is required; found %s\n' "$python_minor" >&2
  exit 1
}

if [ "$skip_backend" = false ]; then
  python3 -m venv "$repo_root/backend/.venv"
  "$repo_root/backend/.venv/bin/python" -m pip install --upgrade pip
  (
    cd "$repo_root/backend"
    .venv/bin/python -m pip install -e '.[dev]'
  )
fi

if [ "$skip_frontend" = false ]; then
  require_command node
  require_command npm
  (
    cd "$repo_root/frontend"
    npm ci
  )
fi

if [ "$with_db" = true ]; then
  require_command docker
  docker compose version >/dev/null
  (
    cd "$repo_root"
    docker compose up -d db
  )
fi

if [ ! -f "$repo_root/.env" ]; then
  printf '%s\n' 'No .env was created. Copy .env.example manually and set development-only values.'
fi

printf '%s\n' 'Bootstrap complete.'
