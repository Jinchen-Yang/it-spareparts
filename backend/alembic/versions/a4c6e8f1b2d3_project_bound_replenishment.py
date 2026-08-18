"""project-bound atomic replenishment submissions

Revision ID: a4c6e8f1b2d3
Revises: f3b5d7c9e2a4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a4c6e8f1b2d3"
down_revision: str | None = "f3b5d7c9e2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "replenishment_application",
        sa.Column("project_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "replenishment_application",
        sa.Column("project_code_snapshot", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "replenishment_application",
        sa.Column("project_name_snapshot", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "replenishment_application",
        sa.Column("client_request_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "replenishment_application",
        sa.Column("request_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "replenishment_application",
        sa.Column(
            "is_legacy_project_unbound",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    # PostgreSQL applies the initial TRUE default to every pre-existing row.
    # Project identity stays unknown; no row UPDATE (and therefore no version or
    # identity-trigger churn) is required.
    op.alter_column(
        "replenishment_application",
        "is_legacy_project_unbound",
        server_default=sa.false(),
    )
    op.create_foreign_key(
        "fk_replenishment_application_project",
        "replenishment_application",
        "maintenance_project",
        ["project_id"],
        ["project_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_replenishment_application_project_binding",
        "replenishment_application",
        "(is_legacy_project_unbound AND project_id IS NULL "
        "AND project_code_snapshot IS NULL AND project_name_snapshot IS NULL) OR "
        "(NOT is_legacy_project_unbound AND project_id IS NOT NULL "
        "AND project_code_snapshot IS NOT NULL "
        "AND project_name_snapshot IS NOT NULL "
        "AND char_length(btrim(project_code_snapshot)) > 0 "
        "AND char_length(btrim(project_name_snapshot)) > 0)",
    )
    op.create_check_constraint(
        "ck_replenishment_application_client_request",
        "replenishment_application",
        "(client_request_id IS NULL AND request_digest IS NULL) OR "
        "(client_request_id IS NOT NULL AND request_digest IS NOT NULL "
        "AND char_length(btrim(client_request_id)) BETWEEN 8 AND 128 "
        "AND request_digest ~ '^[a-f0-9]{64}$')",
    )
    op.create_index(
        "ux_replenishment_application_owner_request",
        "replenishment_application",
        ["owner_username", "client_request_id"],
        unique=True,
        postgresql_where=sa.text("client_request_id IS NOT NULL"),
    )
    op.create_index(
        "ix_replenishment_application_project_updated",
        "replenishment_application",
        ["project_id", "updated_at", "application_id"],
    )
    op.add_column(
        "replenishment_application_line",
        sa.Column("screening_json", JSONB(), nullable=True),
    )
    op.execute(
        """
        CREATE FUNCTION replenishment_screening_json_is_valid(payload jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $validator$
        SELECT CASE
          WHEN jsonb_typeof(payload) IS DISTINCT FROM 'object' THEN false
          WHEN NOT (payload ?& ARRAY[
            'schema_version', 'as_of', 'lookback_days', 'checks',
            'anomaly_count', 'latest_sales', 'pool_floor_ex_tax'
          ]) THEN false
          WHEN jsonb_typeof(payload->'checks') IS DISTINCT FROM 'array' THEN false
          ELSE
            payload->>'schema_version' = '1'
            AND jsonb_typeof(payload->'as_of') = 'string'
            AND payload->>'as_of' ~ '^\\d{4}-\\d{2}-\\d{2}$'
            AND jsonb_typeof(payload->'lookback_days') = 'number'
            AND payload->>'lookback_days' = '182'
            AND jsonb_typeof(payload->'anomaly_count') = 'number'
            AND payload->>'anomaly_count' ~ '^[0-3]$'
            AND jsonb_typeof(payload->'latest_sales') = 'object'
            AND jsonb_typeof(payload->'pool_floor_ex_tax') IN (
              'null', 'number', 'string'
            )
            AND jsonb_array_length(payload->'checks') = 3
            AND (
              SELECT count(*) = 3
                AND count(DISTINCT item->>'key') = 3
                AND bool_and(
                  jsonb_typeof(item) IS NOT DISTINCT FROM 'object'
                  AND jsonb_typeof(item->'key') IS NOT DISTINCT FROM 'string'
                  AND jsonb_typeof(item->'passed') IS NOT DISTINCT FROM 'boolean'
                  AND jsonb_typeof(item->'detail') IS NOT DISTINCT FROM 'object'
                )
              FROM jsonb_array_elements(payload->'checks') AS checks(item)
            )
            AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements(payload->'checks') AS checks(item)
              WHERE item->>'key' = 'pool_membership'
                AND item->'detail' ?& ARRAY[
                  'in_pool', 'pool_name', 'pool_status'
                ]
                AND jsonb_typeof(item#>'{detail,in_pool}') IN (
                  'null', 'boolean'
                )
                AND jsonb_typeof(item#>'{detail,pool_name}') IN (
                  'null', 'string'
                )
                AND jsonb_typeof(item#>'{detail,pool_status}') IN (
                  'null', 'string'
                )
            )
            AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements(payload->'checks') AS checks(item)
              WHERE item->>'key' = 'recent_activity'
                AND item->'detail' ?& ARRAY[
                  'window', 'purchase_samples', 'sales_samples'
                ]
                AND jsonb_typeof(item#>'{detail,window}') = 'object'
                AND item#>'{detail,window}' ?& ARRAY['from', 'to']
                AND jsonb_typeof(item#>'{detail,window,from}') = 'string'
                AND jsonb_typeof(item#>'{detail,window,to}') = 'string'
                AND jsonb_typeof(item#>'{detail,purchase_samples}') = 'number'
                AND item#>>'{detail,purchase_samples}' ~ '^\\d+$'
                AND jsonb_typeof(item#>'{detail,sales_samples}') = 'number'
                AND item#>>'{detail,sales_samples}' ~ '^\\d+$'
            )
            AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements(payload->'checks') AS checks(item)
              WHERE item->>'key' = 'niche_pn'
                AND item->'detail' ?& ARRAY[
                  'is_niche', 'purchase_samples', 'sales_samples', 'rule'
                ]
                AND jsonb_typeof(item#>'{detail,is_niche}') = 'boolean'
                AND jsonb_typeof(item#>'{detail,purchase_samples}') = 'number'
                AND item#>>'{detail,purchase_samples}' ~ '^\\d+$'
                AND jsonb_typeof(item#>'{detail,sales_samples}') = 'number'
                AND item#>>'{detail,sales_samples}' ~ '^\\d+$'
                AND jsonb_typeof(item#>'{detail,rule}') = 'string'
            )
        END
        $validator$
        """
    )

    op.drop_constraint(
        "ck_replenishment_version_submission_state",
        "replenishment_application_version",
        type_="check",
    )
    op.create_check_constraint(
        "ck_replenishment_version_submission_state",
        "replenishment_application_version",
        "(status = 'draft' AND content_digest IS NULL "
        "AND submitted_by IS NULL AND submitted_at IS NULL) OR "
        "(status = 'submitted' AND content_digest ~ '^[a-f0-9]{64}$' "
        "AND char_length(btrim(submitted_by)) > 0 AND submitted_at IS NOT NULL)",
    )
    op.execute(
        """
        CREATE FUNCTION guard_replenishment_project_binding()
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
               AND NOT (OLD.status = 'draft' AND NEW.status = 'submitted')
            THEN
              RAISE EXCEPTION
                'bound replenishment status permits only draft to submitted';
            END IF;
            RETURN NEW;
          END IF;

          RAISE EXCEPTION 'legacy replenishment history is read-only';
        END; $guard$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_replenishment_project_binding "
        "BEFORE INSERT OR UPDATE ON replenishment_application FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_project_binding()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_replenishment_atomic_submission()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        DECLARE atomic_origin boolean;
        BEGIN
          IF OLD.status = 'draft' AND NEW.status = 'submitted' THEN
            SELECT client_request_id IS NOT NULL AND request_digest IS NOT NULL
            INTO atomic_origin
            FROM replenishment_application
            WHERE application_id = NEW.application_id;

            IF atomic_origin AND (
              NOT EXISTS (
                SELECT 1 FROM replenishment_application_line
                WHERE version_id = NEW.version_id
              )
              OR EXISTS (
                SELECT 1
                FROM replenishment_application_line
                WHERE version_id = NEW.version_id
                  AND NOT replenishment_screening_json_is_valid(screening_json)
              )
            ) THEN
              RAISE EXCEPTION
                'atomic replenishment submission requires complete frozen screening_json';
            END IF;
            IF atomic_origin AND EXISTS (
              SELECT 1
              FROM replenishment_application_line
              WHERE version_id = NEW.version_id
                AND (quantity <= 0 OR quantity <> trunc(quantity))
            ) THEN
              RAISE EXCEPTION
                'atomic replenishment submission requires positive integer quantity';
            END IF;
          END IF;
          RETURN NEW;
        END; $guard$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_replenishment_atomic_submission "
        "BEFORE UPDATE OF status ON replenishment_application_version FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_atomic_submission()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_replenishment_submitted_application_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
          IF OLD.status = 'draft'
             AND NEW.status = 'submitted'
             AND NOT NEW.is_legacy_project_unbound
             AND NOT EXISTS (
               SELECT 1
               FROM replenishment_application_version
               WHERE application_id = NEW.application_id
                 AND version_no = NEW.latest_version_no
                 AND status = 'submitted'
                 AND content_digest ~ '^[a-f0-9]{64}$'
                 AND char_length(btrim(submitted_by)) > 0
                 AND submitted_at IS NOT NULL
             )
          THEN
            RAISE EXCEPTION
              'submitted replenishment application requires submitted latest version';
          END IF;
          RETURN NEW;
        END; $guard$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_replenishment_submitted_application_version "
        "AFTER UPDATE OF status ON replenishment_application "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_submitted_application_version()"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM replenishment_application WHERE project_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'a4c6e8f1b2d3 downgrade blocked: project-bound replenishment history exists';
          END IF;
        END $migration$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_replenishment_submitted_application_version "
        "ON replenishment_application"
    )
    op.execute("DROP FUNCTION guard_replenishment_submitted_application_version()")
    op.execute(
        "DROP TRIGGER trg_replenishment_atomic_submission "
        "ON replenishment_application_version"
    )
    op.execute("DROP FUNCTION guard_replenishment_atomic_submission()")
    op.execute("DROP FUNCTION replenishment_screening_json_is_valid(jsonb)")
    op.execute(
        "DROP TRIGGER trg_replenishment_project_binding "
        "ON replenishment_application"
    )
    op.execute("DROP FUNCTION guard_replenishment_project_binding()")
    op.drop_constraint(
        "ck_replenishment_version_submission_state",
        "replenishment_application_version",
        type_="check",
    )
    op.create_check_constraint(
        "ck_replenishment_version_submission_state",
        "replenishment_application_version",
        "(status = 'draft' AND content_digest IS NULL "
        "AND submitted_by IS NULL AND submitted_at IS NULL) OR "
        "(status = 'submitted' AND content_digest ~ '^[a-f0-9]{64}$' "
        "AND char_length(btrim(submitted_by)) > 0 AND submitted_at IS NOT NULL "
        "AND char_length(btrim(warehouse)) > 0)",
    )
    op.drop_column("replenishment_application_line", "screening_json")
    op.drop_constraint(
        "ck_replenishment_application_project_binding",
        "replenishment_application",
        type_="check",
    )
    op.drop_index(
        "ux_replenishment_application_owner_request",
        table_name="replenishment_application",
    )
    op.drop_index(
        "ix_replenishment_application_project_updated",
        table_name="replenishment_application",
    )
    op.drop_constraint(
        "ck_replenishment_application_client_request",
        "replenishment_application",
        type_="check",
    )
    op.drop_constraint(
        "fk_replenishment_application_project",
        "replenishment_application",
        type_="foreignkey",
    )
    op.drop_column("replenishment_application", "is_legacy_project_unbound")
    op.drop_column("replenishment_application", "request_digest")
    op.drop_column("replenishment_application", "client_request_id")
    op.drop_column("replenishment_application", "project_name_snapshot")
    op.drop_column("replenishment_application", "project_code_snapshot")
    op.drop_column("replenishment_application", "project_id")
