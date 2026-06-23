---
name: run-it-spareparts
description: Run, build, and drive the IT 备件智能管理系统 — a FastAPI backend + React/Vite/AntD frontend + PostgreSQL spare-parts app. Use when asked to run/start/launch the app, bring up the backend or frontend, smoke-test the API, screenshot the UI, or seed a dev database. Driven by dev-up.sh (launch) + driver.mjs (API smoke) + shot.mjs (authenticated screenshot).
---

# Run: IT 备件智能管理系统

FastAPI backend (`backend/`, Python + `uv` + Alembic + SQLAlchemy) + React/Vite/AntD SPA
(`frontend/`) + PostgreSQL. The app does **not** auto-migrate and the SPA needs a seeded
DB + a logged-in token to show anything, so you drive it through three committed scripts in
this skill dir (paths below are from the **repo root**):

- **`dev-up.sh`** — brings up Postgres (Docker) + backend (uvicorn :8000) + frontend (vite :5176), migrates, seeds. Idempotent.
- **`driver.mjs`** — API smoke: logs in, hits the core read endpoints, search→overview chain. **This is the primary harness** — most PRs here touch the backend.
- **`shot.mjs`** — authenticated screenshot of the SPA via Chrome/CDP (no npm deps).

## Prerequisites

Already present on the dev Mac: `docker`, `node` (≥22 — `shot.mjs` uses the global `WebSocket`),
`uv`, and Google Chrome. On a fresh Ubuntu box:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv (backend deps)
apt-get install -y nodejs npm docker.io chromium     # node, docker, a browser for shot.mjs
```

**Docker must be running** before anything else (`docker info` should succeed).

## Run (agent path) — START HERE

```bash
# 1. launch the whole stack (first run: ~1–2 min for image pull + uv sync + npm ci)
.claude/skills/run-it-spareparts/dev-up.sh

# 2. smoke-test the API  → prints "PASS — 7 passed, 0 failed", exit 0
node .claude/skills/run-it-spareparts/driver.mjs

# 3. screenshot a real authenticated page  → writes the PNG, exit 0
node .claude/skills/run-it-spareparts/shot.mjs /tmp/sp.png 采购记录
```

`dev-up.sh` prints the URLs + creds (`admin` / `admin888`). `driver.mjs` and `shot.mjs`
default to `localhost:8000` / `localhost:5176`; override with `BASE=` / `FRONTEND=`.
`shot.mjs <out.png> <menu-label>` picks the page — e.g. `利润分析`, `库存查询`, `数据治理`.

Point either driver at **production** instead: `BASE=http://<host>:8080 node .../driver.mjs`.

## Direct invocation (backend internals — many PRs need only this)

Most changes are backend logic (ETL / services / agent / API). Test them without the full app
against the **test** DB (the suite truncates it per-test; never points at dev/prod):

```bash
cd backend
uv sync --extra dev
DATABASE_URL="postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test" \
  uv run python -m pytest -q        # needs Postgres on :5433 (dev-up.sh's container works)
```

Import-and-call a single function the same way: `cd backend && uv run python -c "from app.services import profit; ..."`.

## Run (human path)

Production-faithful full stack via Compose (builds images, serves on `:8080`):

```bash
docker compose up -d --build
docker compose run --rm app alembic upgrade head    # app has NO auto-migration
# open http://localhost:8080  (login admin / <ADMIN_PASSWORD from .env>)
```

Heavier than the dev path; use it to reproduce a deploy. Stop: `docker compose down`.

## Gotchas (battle scars, all hit this session)

- **No auto-migration.** The app's `CMD` is just `uvicorn` — startup never runs Alembic. You must `alembic upgrade head` yourself (dev-up.sh does). Symptom otherwise: `UndefinedColumn` / `relation does not exist`.
- **Health is `/health`, not `/api/health`.** The `/api` prefix is only for routers; `/api/health` is a 404.
- **The SPA reads auth from 4 localStorage keys** — `token`, `role`, `name`, `permissions` (the last is JSON). Setting only `token` still bounces you to login. `shot.mjs` injects all four then reloads.
- **URL-encode non-ASCII query params.** `GET /api/parts/search?q=三星` raw → 400; `encodeURIComponent` → 200. (Bit `driver.mjs` until encoded.)
- **Postgres lives on host port 5433**, DB name `spareparts_dev` (dev) / `spareparts_test` (tests) — *not* 5432, to avoid clashing with a system Postgres.
- **Backend needs `ENVIRONMENT=dev`** (or a real `ADMIN_PASSWORD`/`SECRET_KEY` in `.env`): in `prod` it refuses to start on the default weak admin password.
- **`uv`, not pip/poetry.** `uv sync --extra dev`, `uv run <cmd>`. There is no `requirements.txt` install path.
- **Backend uses `LLM_API_KEY`/`VISION_API_KEY` for the AI assistant**; unset is fine — chat returns a graceful "not configured" message, everything else works.
- **Docker Desktop on this Mac dies intermittently.** If a `docker` call says "Cannot connect to the daemon", `open -a Docker`, wait, retry.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `driver.mjs`: login fails / "no token" | DB not seeded → re-run `dev-up.sh` (it seeds `admin`/`admin888`). |
| `driver.mjs`: crashes "is the backend up?" | uvicorn not running → `dev-up.sh`; check `.claude/skills/run-it-spareparts/.run/backend.log`. |
| API 500s with `relation ... does not exist` | migrations not applied → `cd backend && DATABASE_URL=... uv run alembic upgrade head`. |
| `shot.mjs`: blank / login-page screenshot | frontend down or wrong `FRONTEND` → check `:5176`; verify backend reachable from the browser (`VITE_API_TARGET`). |
| `shot.mjs`: "No Chrome/Chromium found" | set `CHROME=/path/to/chrome`, or `apt-get install -y chromium`. |
| Port 8000/5176/5433 already in use | `dev-up.sh` reuses a healthy server on those ports; if a stale one is wedged, kill it (`pkill -f uvicorn` / `pkill -f vite`) and re-run. |
