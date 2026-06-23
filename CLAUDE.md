# IT 备件智能管理系统 — repo guide for agents

Single deployable app: **FastAPI backend** (`backend/`, Python · `uv` · Alembic · SQLAlchemy ·
PostgreSQL) + **React/Vite/AntD frontend** (`frontend/`), shipped via Docker Compose. Backend
tests run with `pytest` against a Postgres on `:5433`; CI runs backend pytest + frontend
`tsc && vite build`. Work on a branch → PR → squash-merge → deploy (no auto-migration; run
`alembic upgrade head` on deploy).

To **run / drive the app locally** (launch the stack, smoke the API, screenshot the UI), use the
`run-it-spareparts` skill (`.claude/skills/run-it-spareparts/`).

## Agent skills

The engineering skills (`to-issues`, `triage`, `to-prd`, `diagnose`, `improve-codebase-architecture`,
`tdd`, …) read their per-repo configuration from `docs/agents/`:

### Issue tracker

Issues and PRDs live as **GitHub issues** (`Jinchen-Yang/it-spareparts`), driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

**Canonical** label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`); `wontfix` already exists, the rest are created on first use. See `docs/agents/triage-labels.md`.

### Domain docs

**Single-context** — one `CONTEXT.md` + `docs/adr/` at the repo root (created lazily by `/grill-with-docs`). See `docs/agents/domain.md`.
