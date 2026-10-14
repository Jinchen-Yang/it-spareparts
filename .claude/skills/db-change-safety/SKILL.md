---
name: db-change-safety
description: Plan, implement, and verify changes touching PostgreSQL, SQLAlchemy models, Alembic migrations, schema design, indexes, constraints, data backfills, transactions, concurrency, row locks, monetary fields, bulk imports, or destructive data operations. Use BEFORE editing any database-related backend code.
---

# Database Change Safety (it-spareparts)

## Project context

- PostgreSQL 15 + SQLAlchemy 2.0 + Alembic (psycopg 3). **No auto-migration**: deploys run
  `alembic upgrade head` manually; CI enforces single migration head and zero ORM drift.
- Test DB: local Postgres on port **5433**, database `spareparts_test` (conftest provisions it).
- **Production DB is read-only for agents.** Writes go through audited application APIs only —
  never direct `UPDATE`/`DELETE` against production tables.
- Business source of truth: `docs/decisions/0001-维保整改八条口径.md` (D-01…D-16; later
  decisions supersede earlier ones). Project identity is **XSDD**, not names or date ranges.
- Workbook deleted rows mean **void**, not physical deletion; identity, audit and
  void-precedence semantics must survive every change.

## Required workflow

1. **Inspect before editing**: relevant SQLAlchemy models, Alembic history
   (`uv run --extra dev alembic heads` must show exactly one head), repositories/services,
   Pydantic schemas, tests/fixtures.
2. **State the data flow**: source of truth, affected tables, readers/writers, API and
   frontend consumers of the affected columns.
3. **Produce a short change plan**: schema change, backfill strategy, indexes/constraints,
   locking impact, rollback story (or explicit irreversibility), regression tests.
4. **Do not**:
   - use `Base.metadata.create_all()` as a migration path;
   - silently drop, truncate, merge, or reinterpret data;
   - change money/tax semantics without stating the rounding rules;
   - add an index without checking query predicates and cardinality;
   - claim a downgrade works without testing or explaining its limits;
   - treat one request, one account, or the first page as the complete dataset.
5. **Check query behavior**: N+1, eager-loading strategy, pagination, ordering stability,
   duplicate rows from joins, transaction boundaries, row locks on concurrent updates.
6. **Verify** (from `backend/`, against the test DB):
   - `uv run --extra dev alembic upgrade head`
   - `uv run --extra dev alembic check` (zero ORM/schema drift)
   - `uv run --extra dev pytest -q` (narrowest relevant tests first)
   - review generated SQL / EXPLAIN for performance-sensitive queries.

## Completion report

Affected tables and contracts; migration revision; backfill behavior; rollback status;
concurrency/locking impact; tests executed; remaining risks.
