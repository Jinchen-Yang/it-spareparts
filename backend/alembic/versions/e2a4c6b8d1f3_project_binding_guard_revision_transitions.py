"""project binding guard allows revision transitions

Revision ID: e2a4c6b8d1f3
Revises: e1a3c5b7d9f1
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e2a4c6b8d1f3"
down_revision: str | None = "e1a3c5b7d9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD_V3 = """
CREATE OR REPLACE FUNCTION guard_replenishment_project_binding()
RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'draft' THEN
      RAISE EXCEPTION 'new replenishment application must start draft';
    END IF;
    IF NEW.is_legacy_project_unbound
       OR NEW.project_id IS NULL
       OR NEW.client_request_id IS NULL
       OR NEW.request_digest IS NULL
       OR char_length(btrim(NEW.client_request_id)) NOT BETWEEN 8 AND 128
       OR NEW.request_digest !~ '^[a-f0-9]{64}$'
       OR NEW.project_code_snapshot IS NULL
       OR NEW.project_name_snapshot IS NULL
    THEN
      RAISE EXCEPTION
        'new replenishment application requires project and client request id';
    END IF;
    RETURN NEW;
  END IF;

  IF NOT OLD.is_legacy_project_unbound THEN
    IF NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.is_legacy_project_unbound IS DISTINCT FROM OLD.is_legacy_project_unbound
       OR NEW.client_request_id IS DISTINCT FROM OLD.client_request_id
       OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
       OR NEW.project_code_snapshot IS DISTINCT FROM OLD.project_code_snapshot
       OR NEW.project_name_snapshot IS DISTINCT FROM OLD.project_name_snapshot
    THEN
      RAISE EXCEPTION 'bound replenishment project identity is immutable';
    END IF;
    IF OLD.status = 'submitted' AND NEW.status IS DISTINCT FROM OLD.status THEN
      RAISE EXCEPTION 'submitted replenishment status is immutable';
    END IF;
    -- 允许状态机（2026-08-18）：
    --   draft -> submitted / approved / needs_revision（原子提交 + 自动裁决终态）
    --   needs_revision -> submitted / approved / needs_revision（打回后重提 / 再次打回）
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (
         (OLD.status = 'draft' AND NEW.status IN ('submitted', 'approved', 'needs_revision'))
         OR (OLD.status = 'needs_revision'
             AND NEW.status IN ('submitted', 'approved', 'needs_revision'))
       )
    THEN
      RAISE EXCEPTION
        'bound replenishment status permits only draft/needs_revision to submitted/approved/needs_revision';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'legacy replenishment history is read-only';
END; $guard$
"""

_GUARD_V2 = """
CREATE OR REPLACE FUNCTION guard_replenishment_project_binding()
RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'draft' THEN
      RAISE EXCEPTION 'new replenishment application must start draft';
    END IF;
    IF NEW.is_legacy_project_unbound
       OR NEW.project_id IS NULL
       OR NEW.client_request_id IS NULL
       OR NEW.request_digest IS NULL
       OR char_length(btrim(NEW.client_request_id)) NOT BETWEEN 8 AND 128
       OR NEW.request_digest !~ '^[a-f0-9]{64}$'
       OR NEW.project_code_snapshot IS NULL
       OR NEW.project_name_snapshot IS NULL
    THEN
      RAISE EXCEPTION
        'new replenishment application requires project and client request id';
    END IF;
    RETURN NEW;
  END IF;

  IF NOT OLD.is_legacy_project_unbound THEN
    IF NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.is_legacy_project_unbound IS DISTINCT FROM OLD.is_legacy_project_unbound
       OR NEW.client_request_id IS DISTINCT FROM OLD.client_request_id
       OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
       OR NEW.project_code_snapshot IS DISTINCT FROM OLD.project_code_snapshot
       OR NEW.project_name_snapshot IS DISTINCT FROM OLD.project_name_snapshot
    THEN
      RAISE EXCEPTION 'bound replenishment project identity is immutable';
    END IF;
    IF OLD.status = 'submitted' AND NEW.status IS DISTINCT FROM OLD.status THEN
      RAISE EXCEPTION 'submitted replenishment status is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (OLD.status = 'draft'
                AND NEW.status IN ('submitted', 'approved', 'needs_revision'))
    THEN
      RAISE EXCEPTION
        'bound replenishment status permits only draft to submitted/approved/needs_revision';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'legacy replenishment history is read-only';
END; $guard$
"""


def upgrade() -> None:
    op.execute(_GUARD_V3)


def downgrade() -> None:
    op.execute(_GUARD_V2)
