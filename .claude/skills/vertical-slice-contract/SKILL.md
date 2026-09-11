---
name: vertical-slice-contract
description: Implement or review an end-to-end business feature that crosses database, backend API, authorization, TypeScript client, and React/AntD frontend layers. Use for new fields, statuses, approval rules, filters, statistics, upload flows, or permission-scope changes.
---

# Vertical Slice Contract (it-spareparts)

## Before editing, build an impact map

```
PostgreSQL / Alembic migration
→ SQLAlchemy model & repository
→ service / business rule (services/)
→ Pydantic request & response schemas
→ FastAPI route
→ frontend TS client (frontend/src/api/*.ts — hand-maintained, NO codegen)
→ React state / data fetching
→ Ant Design page or component
→ backend tests (+ frontend tests when behavior changes)
```

## Required checks

1. Identify the business source of truth (`docs/decisions/0001` D-xx) and stable identifiers
   (XSDD). If the change touches a decided口径, cite the decision; if it conflicts, stop and ask.
2. Define request, response, error and pagination contracts; keep route paths and the
   hand-written TS client in sync in the **same change** — there is no type generation step.
3. Verify project / account / role / record-level authorization **on the server**, not only
   by hiding UI elements.
4. Never silently truncate lists or treat the first page as the complete set.
5. Preserve explicit null / zero / empty / missing semantics — missing data is not zero, and
   restricted financial data must not leak through derived indicators.
6. Keep money / date / status representations consistent between backend and frontend.
7. Add or extend backend contract tests; when TS changes run `cd frontend && npm run build`
   (tsc) and `npm run test` for behavior tests.
8. Stay inside the requested slice — no unrelated refactors, no framework introductions.
9. Beta features need a feature flag + route whitelist guard + a "flag off → 404/403" test.
10. Native site-issue/return workflows stay deferred (D-15) — don't revive them because old
    implementation files or plans exist.

## Completion report

Layer-by-layer list of changed files, contracts affected, tests added/run, residual risks.
