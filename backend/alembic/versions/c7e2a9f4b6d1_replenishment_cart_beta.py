"""add isolated replenishment cart Beta facts and permissions

Revision ID: c7e2a9f4b6d1
Revises: d3e5f7a9b1c2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "c7e2a9f4b6d1"
down_revision: str | None = "d3e5f7a9b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_permissions() -> None:
    for key in (
        "page_replenishment_beta",
        "action_replenishment_create",
        "action_replenishment_review",
    ):
        # The human Beta page is an account-level production allowlist, not an
        # admin role capability.  Admin retains create/review actions, but every
        # existing account starts with the page closed until an audited override.
        admin_default = key != "page_replenishment_beta"
        op.execute(
            sa.text(
                """
                UPDATE sys_role_template
                SET permissions = CASE
                      WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                      ELSE '{}'::jsonb
                    END || jsonb_build_object(:key, code = 'admin' AND :admin_default)
                """
            ).bindparams(key=key, admin_default=admin_default)
        )
        op.execute(
            sa.text(
                """
                UPDATE sys_user
                SET template_perms = template_perms
                      || jsonb_build_object(:key, role = 'admin' AND :admin_default),
                    perm_overrides = CASE
                      WHEN jsonb_typeof(perm_overrides) = 'object' THEN perm_overrides
                      ELSE '{}'::jsonb
                    END - :key
                WHERE jsonb_typeof(template_perms) = 'object'
                """
            ).bindparams(key=key, admin_default=admin_default)
        )
        op.execute(
            sa.text(
                """
                UPDATE sys_user
                SET permissions = CASE
                      WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                      ELSE '{}'::jsonb
                    END || jsonb_build_object(:key, role = 'admin' AND :admin_default)
                WHERE permissions IS NOT NULL
                """
            ).bindparams(key=key, admin_default=admin_default)
        )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "replenishment_application",
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("application_no", sa.String(length=64), nullable=False),
        sa.Column("owner_username", sa.String(length=64), nullable=False),
        sa.Column("owner_display_name", sa.String(length=128), nullable=True),
        sa.Column("salesperson_name_snapshot", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("latest_version_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('draft','submitted','needs_revision','approved')",
            name="ck_replenishment_application_status",
        ),
        sa.CheckConstraint(
            "latest_version_no >= 1 AND version >= 1",
            name="ck_replenishment_application_versions",
        ),
        sa.CheckConstraint(
            "char_length(btrim(application_no)) > 0 "
            "AND char_length(btrim(owner_username)) > 0",
            name="ck_replenishment_application_identity",
        ),
        sa.ForeignKeyConstraint(["owner_username"], ["sys_user.username"]),
        sa.PrimaryKeyConstraint("application_id"),
        sa.UniqueConstraint("application_no"),
    )
    op.create_index(
        "ix_replenishment_application_owner_updated",
        "replenishment_application",
        ["owner_username", "updated_at", "application_id"],
    )
    op.create_index(
        "ix_replenishment_application_status_updated",
        "replenishment_application",
        ["status", "updated_at", "application_id"],
    )

    op.create_table(
        "replenishment_application_version",
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("warehouse", sa.String(length=64), nullable=True),
        sa.Column("request_note", sa.Text(), nullable=True),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("submitted_by", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version_no >= 1", name="ck_replenishment_version_no"),
        sa.CheckConstraint(
            "status IN ('draft','submitted')", name="ck_replenishment_version_status"
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND content_digest IS NULL "
            "AND submitted_by IS NULL AND submitted_at IS NULL) OR "
            "(status = 'submitted' AND content_digest ~ '^[a-f0-9]{64}$' "
            "AND char_length(btrim(submitted_by)) > 0 AND submitted_at IS NOT NULL "
            "AND char_length(btrim(warehouse)) > 0)",
            name="ck_replenishment_version_submission_state",
        ),
        sa.CheckConstraint(
            "char_length(btrim(created_by)) > 0", name="ck_replenishment_version_creator"
        ),
        sa.ForeignKeyConstraint(["application_id"], ["replenishment_application.application_id"]),
        sa.ForeignKeyConstraint(
            ["parent_version_id"], ["replenishment_application_version.version_id"]
        ),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint(
            "application_id", "version_no", name="uq_replenishment_application_version"
        ),
    )
    op.create_table(
        "replenishment_application_line",
        sa.Column("line_id", sa.String(length=36), nullable=False),
        sa.Column("request_line_id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("source_line_id", sa.String(length=36), nullable=True),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn_std", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("brand", sa.String(length=128), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("special_note", sa.Text(), nullable=True),
        sa.Column("pool_group_id", sa.Integer(), nullable=True),
        sa.Column("pool_name", sa.String(length=128), nullable=True),
        sa.Column("pool_version", sa.Integer(), nullable=True),
        sa.Column("price_window_from", sa.Date(), nullable=False),
        sa.Column("price_window_to", sa.Date(), nullable=False),
        sa.Column("price_as_of", sa.Date(), nullable=False),
        sa.Column("purchase_stats_json", JSONB(), nullable=False),
        sa.Column("sales_stats_json", JSONB(), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("line_no >= 1", name="ck_replenishment_line_no"),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 999999.999",
            name="ck_replenishment_line_quantity",
        ),
        sa.CheckConstraint(
            "price_window_from <= price_window_to AND price_window_to <= price_as_of",
            name="ck_replenishment_line_window",
        ),
        sa.CheckConstraint(
            "evidence_digest ~ '^[a-f0-9]{64}$'",
            name="ck_replenishment_line_evidence_digest",
        ),
        sa.CheckConstraint(
            "(pool_group_id IS NULL AND pool_name IS NULL AND pool_version IS NULL) OR "
            "(pool_group_id IS NOT NULL AND pool_version >= 1)",
            name="ck_replenishment_line_pool_snapshot",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["source_line_id"], ["replenishment_application_line.line_id"]),
        sa.ForeignKeyConstraint(["version_id"], ["replenishment_application_version.version_id"]),
        sa.PrimaryKeyConstraint("line_id"),
        sa.UniqueConstraint("version_id", "line_no", name="uq_replenishment_line_no"),
        sa.UniqueConstraint(
            "version_id", "request_line_id", name="uq_replenishment_request_line"
        ),
        sa.UniqueConstraint("version_id", "part_id", name="uq_replenishment_line_part"),
    )
    op.create_table(
        "replenishment_review",
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("external_reference", sa.String(length=128), nullable=True),
        sa.Column("summary_note", sa.Text(), nullable=True),
        sa.Column("approved_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=False),
        sa.Column(
            "reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "approved_count >= 0 AND rejected_count >= 0 "
            "AND approved_count + rejected_count > 0",
            name="ck_replenishment_review_counts",
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[a-f0-9]{64}$' "
            "AND char_length(btrim(idempotency_key)) >= 8 "
            "AND char_length(btrim(reviewed_by)) > 0",
            name="ck_replenishment_review_identity",
        ),
        sa.ForeignKeyConstraint(["version_id"], ["replenishment_application_version.version_id"]),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("version_id"),
    )
    op.create_index(
        "ix_replenishment_review_time",
        "replenishment_review",
        ["reviewed_at", "review_id"],
    )

    op.create_table(
        "replenishment_review_line",
        sa.Column("review_line_id", sa.String(length=36), nullable=False),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("version_line_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('approved','rejected')",
            name="ck_replenishment_review_line_decision",
        ),
        sa.CheckConstraint(
            "decision = 'approved' OR char_length(btrim(reason)) > 0",
            name="ck_replenishment_review_line_reason",
        ),
        sa.ForeignKeyConstraint(["review_id"], ["replenishment_review.review_id"]),
        sa.ForeignKeyConstraint(
            ["version_line_id"], ["replenishment_application_line.line_id"]
        ),
        sa.PrimaryKeyConstraint("review_line_id"),
        sa.UniqueConstraint("version_line_id"),
    )
    op.create_index(
        "ix_replenishment_review_line_review",
        "replenishment_review_line",
        ["review_id", "version_line_id"],
    )

    op.create_table(
        "replenishment_audit_event",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("before_json", JSONB(), nullable=True),
        sa.Column("after_json", JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "operated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('application_created','draft_updated','line_added','line_updated',"
            "'line_removed','version_submitted','review_recorded','revision_started',"
            "'manual_exported','wbdd_draft_exported')",
            name="ck_replenishment_audit_action",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0 AND char_length(btrim(operated_by)) > 0",
            name="ck_replenishment_audit_actor_reason",
        ),
        sa.ForeignKeyConstraint(["application_id"], ["replenishment_application.application_id"]),
        sa.ForeignKeyConstraint(["version_id"], ["replenishment_application_version.version_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_replenishment_audit_application_time",
        "replenishment_audit_event",
        ["application_id", "operated_at", "event_id"],
    )

    op.execute(
        """
        CREATE FUNCTION guard_replenishment_application_identity()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
          IF TG_OP = 'DELETE'
             OR NEW.application_id IS DISTINCT FROM OLD.application_id
             OR NEW.application_no IS DISTINCT FROM OLD.application_no
             OR NEW.owner_username IS DISTINCT FROM OLD.owner_username
             OR NEW.owner_display_name IS DISTINCT FROM OLD.owner_display_name
             OR NEW.salesperson_name_snapshot IS DISTINCT FROM OLD.salesperson_name_snapshot
             OR NEW.created_at IS DISTINCT FROM OLD.created_at
             OR NEW.version <> OLD.version + 1
             OR NEW.latest_version_no < OLD.latest_version_no
             OR NEW.latest_version_no > OLD.latest_version_no + 1
          THEN
            RAISE EXCEPTION 'replenishment application identity/version is immutable';
          END IF;
          RETURN NEW;
        END; $guard$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_replenishment_application_identity "
        "BEFORE UPDATE OR DELETE ON replenishment_application FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_application_identity()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_replenishment_version_history()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
          IF TG_OP = 'DELETE' OR OLD.status = 'submitted'
             OR NEW.version_id IS DISTINCT FROM OLD.version_id
             OR NEW.application_id IS DISTINCT FROM OLD.application_id
             OR NEW.version_no IS DISTINCT FROM OLD.version_no
             OR NEW.parent_version_id IS DISTINCT FROM OLD.parent_version_id
             OR NEW.created_by IS DISTINCT FROM OLD.created_by
             OR NEW.created_at IS DISTINCT FROM OLD.created_at
          THEN
            RAISE EXCEPTION 'submitted replenishment version is immutable';
          END IF;
          RETURN NEW;
        END; $guard$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_replenishment_version_history "
        "BEFORE UPDATE OR DELETE ON replenishment_application_version FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_version_history()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_replenishment_line_draft_only()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        DECLARE target_version text;
        DECLARE target_status text;
        BEGIN
          target_version := CASE WHEN TG_OP = 'DELETE' THEN OLD.version_id ELSE NEW.version_id END;
          SELECT status INTO target_status
          FROM replenishment_application_version WHERE version_id = target_version;
          IF target_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'submitted replenishment lines are immutable';
          END IF;
          IF TG_OP = 'UPDATE'
             AND (NEW.line_id IS DISTINCT FROM OLD.line_id
               OR NEW.request_line_id IS DISTINCT FROM OLD.request_line_id
               OR NEW.version_id IS DISTINCT FROM OLD.version_id
               OR NEW.source_line_id IS DISTINCT FROM OLD.source_line_id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at)
          THEN
            RAISE EXCEPTION 'replenishment line identity is immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END; $guard$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_replenishment_line_draft_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON replenishment_application_line FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_line_draft_only()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_replenishment_review_line_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        DECLARE review_version text;
        DECLARE line_version text;
        BEGIN
          SELECT version_id INTO review_version
          FROM replenishment_review WHERE review_id = NEW.review_id;
          SELECT version_id INTO line_version
          FROM replenishment_application_line WHERE line_id = NEW.version_line_id;
          IF review_version IS NULL OR line_version IS NULL
             OR review_version IS DISTINCT FROM line_version
          THEN
            RAISE EXCEPTION 'replenishment review line version mismatch';
          END IF;
          RETURN NEW;
        END; $guard$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_replenishment_review_line_version "
        "BEFORE INSERT ON replenishment_review_line FOR EACH ROW "
        "EXECUTE FUNCTION guard_replenishment_review_line_version()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_replenishment_append_only_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
          RAISE EXCEPTION 'replenishment review and audit history is append-only';
        END; $guard$
        """
    )
    for table in (
        "replenishment_review",
        "replenishment_review_line",
        "replenishment_audit_event",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_replenishment_append_only_mutation()"
        )

    _add_permissions()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    tables = (
        "replenishment_review_line",
        "replenishment_review",
        "replenishment_audit_event",
        "replenishment_application_line",
        "replenishment_application_version",
        "replenishment_application",
    )
    op.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
    checks = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in tables)
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF {checks} THEN
            RAISE EXCEPTION
              'c7e2a9f4b6d1 downgrade blocked: replenishment Beta business history is not empty';
          END IF;
        END $migration$;
        """
    )
    for key in (
        "page_replenishment_beta",
        "action_replenishment_create",
        "action_replenishment_review",
    ):
        op.execute(
            sa.text(
                "UPDATE sys_role_template SET permissions = permissions - :key "
                "WHERE jsonb_typeof(permissions) = 'object'"
            ).bindparams(key=key)
        )
        op.execute(
            sa.text(
                "UPDATE sys_user SET template_perms = template_perms - :key "
                "WHERE jsonb_typeof(template_perms) = 'object'"
            ).bindparams(key=key)
        )
        op.execute(
            sa.text(
                "UPDATE sys_user SET permissions = permissions - :key "
                "WHERE jsonb_typeof(permissions) = 'object'"
            ).bindparams(key=key)
        )
        op.execute(
            sa.text(
                "UPDATE sys_user SET perm_overrides = perm_overrides - :key "
                "WHERE jsonb_typeof(perm_overrides) = 'object'"
            ).bindparams(key=key)
        )
    for table in (
        "replenishment_review",
        "replenishment_review_line",
        "replenishment_audit_event",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_replenishment_append_only_mutation()")
    op.execute("DROP TRIGGER trg_replenishment_review_line_version ON replenishment_review_line")
    op.execute("DROP FUNCTION guard_replenishment_review_line_version()")
    op.execute("DROP TRIGGER trg_replenishment_line_draft_only ON replenishment_application_line")
    op.execute("DROP FUNCTION guard_replenishment_line_draft_only()")
    op.execute("DROP TRIGGER trg_replenishment_version_history ON replenishment_application_version")
    op.execute("DROP FUNCTION guard_replenishment_version_history()")
    op.execute("DROP TRIGGER trg_replenishment_application_identity ON replenishment_application")
    op.execute("DROP FUNCTION guard_replenishment_application_identity()")
    op.drop_table("replenishment_review_line")
    op.drop_table("replenishment_review")
    op.drop_table("replenishment_audit_event")
    op.drop_table("replenishment_application_line")
    op.drop_table("replenishment_application_version")
    op.drop_table("replenishment_application")
