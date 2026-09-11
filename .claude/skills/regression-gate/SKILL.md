---
name: regression-gate
description: Verify a completed implementation or bug fix before declaring it done, and review PR diffs in it-spareparts. Covers regressions, contracts, migrations, permissions, data integrity, and user-visible flows. Use before committing and when reviewing changes.
---

# Regression Gate (it-spareparts)

## Review the actual diff — not just the intended solution

Read the current diff plus every directly affected caller, test, schema and migration.

## Verification (real commands — nothing else counts as "run")

- Backend (needs local Postgres on :5433; conftest provisions the test DB):
  `cd backend && uv run --extra dev pytest -q` — narrowest relevant tests first, then the
  affected group or full suite.
- Migration checks whenever DB code changed:
  `uv run --extra dev alembic upgrade head && uv run --extra dev alembic check && uv run --extra dev alembic heads`
  (must be a single head).
- Frontend when TS/TSX changed: `cd frontend && npm run build` (tsc && vite build) and
  `npm run test` (vitest). Run `npm run audit:prod` before any release claim.
- There is **no separate lint script and no type-generation step** in this repo — never
  claim to have run them.
- Optional heavier smoke: `.claude/skills/run-it-spareparts/dev-up.sh` + `driver.mjs`
  (local dev targets only).

## Checks

- [ ] Authorization and project scoping enforced server-side
- [ ] Pagination and result limits; first page ≠ full dataset
- [ ] Empty, null, duplicate and large-data cases
- [ ] Concurrency where shared state is updated (row locks, idempotency keys)
- [ ] Upload name / MIME / path / storage failures
- [ ] Amount, tax, precision and rounding consistency
- [ ] Route and schema compatibility with the hand-maintained TS client
- [ ] Beta flag off behaves as specified (404/403)
- [ ] No unrelated refactors mixed in unless required for correctness

## Rules

- Never report success for tests that were not run. Distinguish: **verified** /
  **statically inspected** / **not tested** / **blocked by environment**.
- After gates pass: **commit and push the feature branch** (authorized).
- **Merging, deploying, production access: ask the user — never do them as part of a fix.**

## Completion report

Original issue; root cause; minimal fix; commands run with results; unverified paths;
residual risks.
