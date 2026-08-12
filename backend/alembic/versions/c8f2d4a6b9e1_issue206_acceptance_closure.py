"""close manager workbook and acceptance workflow gaps

Revision ID: c8f2d4a6b9e1
Revises: b7e1c3a9d5f2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c8f2d4a6b9e1"
down_revision: str | None = "b7e1c3a9d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIONS = (
    "action_maintenance_manager_workbook_apply",
    "action_maintenance_acceptance_submit",
    "action_maintenance_acceptance_review",
)


def _validate_permission_json() -> None:
    op.execute(
        r"""
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM sys_role_template
              WHERE permissions IS NOT NULL
                AND permissions <> 'null'::jsonb
                AND jsonb_typeof(permissions) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE template_perms IS NOT NULL
                AND template_perms <> 'null'::jsonb
                AND jsonb_typeof(template_perms) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE permissions IS NOT NULL
                AND permissions <> 'null'::jsonb
                AND jsonb_typeof(permissions) IS DISTINCT FROM 'object'
          )
          OR EXISTS (
              SELECT 1 FROM sys_user
              WHERE perm_overrides IS NOT NULL
                AND perm_overrides <> 'null'::jsonb
                AND jsonb_typeof(perm_overrides) IS DISTINCT FROM 'object'
          )
          THEN
            RAISE EXCEPTION
              'c8f2d4a6b9e1 upgrade blocked: permission JSONB payload must be an object, SQL NULL, or JSON null';
          END IF;
        END
        $migration$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _validate_permission_json()
    op.execute(
        r"""
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1
              FROM business_file_link link
              LEFT JOIN maintenance_acceptance_deliverable deliverable
                ON deliverable.deliverable_id = link.entity_id
              WHERE deliverable.deliverable_id IS NULL
          )
          THEN
            RAISE EXCEPTION
              'c8f2d4a6b9e1 upgrade blocked: business_file_link has a dangling acceptance entity';
          END IF;
          IF EXISTS (
              SELECT 1 FROM business_file
              WHERE btrim(object_key) ~* '^(https?|ftp|file)://'
                 OR btrim(object_key) ~ '(^/|(^|/)\.\.(/|$))'
                 OR strpos(object_key, chr(92)) > 0
                 OR char_length(btrim(original_filename)) NOT BETWEEN 1 AND 256
                 OR strpos(original_filename, '/') > 0
                 OR strpos(original_filename, chr(92)) > 0
                 OR original_filename ~ '[[:cntrl:]]'
          )
          THEN
            RAISE EXCEPTION
              'c8f2d4a6b9e1 upgrade blocked: stored business file path or filename is unsafe';
          END IF;
        END
        $migration$;
        """
    )

    op.drop_constraint(
        "ck_business_file_object_key_not_external_url",
        "business_file",
        type_="check",
    )
    op.create_check_constraint(
        "ck_business_file_object_key_not_external_url",
        "business_file",
        "char_length(btrim(object_key)) > 0 AND "
        "btrim(object_key) !~* '^(https?|ftp|file)://' AND "
        "btrim(object_key) !~ '(^/|(^|/)\\.\\.(/|$))' AND "
        "strpos(object_key, chr(92)) = 0",
    )
    op.create_check_constraint(
        "ck_business_file_original_filename_safe",
        "business_file",
        "char_length(btrim(original_filename)) BETWEEN 1 AND 256 AND "
        "strpos(original_filename, '/') = 0 AND "
        "strpos(original_filename, chr(92)) = 0 AND "
        "original_filename !~ '[[:cntrl:]]'",
    )
    op.create_foreign_key(
        "fk_business_file_link_acceptance",
        "business_file_link",
        "maintenance_acceptance_deliverable",
        ["entity_id"],
        ["deliverable_id"],
    )

    op.create_table(
        "maintenance_acceptance_operation",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("operation_key", sa.String(128), nullable=False, unique=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("operation_type", sa.String(24), nullable=False),
        sa.Column("deliverable_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("operated_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["deliverable_id"],
            ["maintenance_acceptance_deliverable.deliverable_id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "operation_type IN ('attachment_upload', 'submit', 'approve', 'reject')",
            name="ck_maintenance_acceptance_operation_type",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_acceptance_operation_payload_present",
        ),
        sa.CheckConstraint(
            "char_length(btrim(operated_by)) > 0",
            name="ck_maintenance_acceptance_operation_operator",
        ),
    )
    op.create_index(
        "ix_maintenance_acceptance_operation_deliverable_time",
        "maintenance_acceptance_operation",
        ["deliverable_id", "created_at"],
    )

    op.create_table(
        "business_file_download_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("file_id", sa.String(36), nullable=False),
        sa.Column("link_id", sa.String(36), nullable=False),
        sa.Column("deliverable_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("downloaded_by", sa.String(64), nullable=False),
        sa.Column("sha256_at_download", sa.String(64), nullable=False),
        sa.Column(
            "downloaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["file_id"], ["business_file.file_id"]),
        sa.ForeignKeyConstraint(["link_id"], ["business_file_link.link_id"]),
        sa.ForeignKeyConstraint(
            ["deliverable_id"],
            ["maintenance_acceptance_deliverable.deliverable_id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.CheckConstraint(
            "char_length(btrim(downloaded_by)) > 0",
            name="ck_business_file_download_audit_operator",
        ),
        sa.CheckConstraint(
            "sha256_at_download ~ '^[0-9a-f]{64}$'",
            name="ck_business_file_download_audit_sha256",
        ),
    )
    op.create_index(
        "ix_business_file_download_audit_file_time",
        "business_file_download_audit",
        ["file_id", "downloaded_at", "id"],
    )
    op.create_index(
        "ix_business_file_download_audit_project_time",
        "business_file_download_audit",
        ["project_id", "downloaded_at", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_issue206_acceptance_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $guard$
        BEGIN
          RAISE EXCEPTION 'issue206 acceptance operation and download audit are append-only';
        END;
        $guard$
        """
    )
    for table in (
        "maintenance_acceptance_operation",
        "business_file_download_audit",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION reject_issue206_acceptance_audit_mutation()
            """
        )

    permission_values = ", ".join(
        f"'{action}', code = 'admin'" for action in _ACTIONS
    )
    op.execute(
        f"""
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{{}}'::jsonb
            END || jsonb_build_object({permission_values})
        """
    )
    user_values = ", ".join(
        f"'{action}', role = 'admin'" for action in _ACTIONS
    )
    remove_overrides = " ".join(f"- '{action}'" for action in _ACTIONS)
    op.execute(
        f"""
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object({user_values}),
            perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides
                    ELSE '{{}}'::jsonb
                END {remove_overrides}
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        f"""
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{{}}'::jsonb
            END || jsonb_build_object({user_values})
        WHERE permissions IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE maintenance_acceptance_operation, business_file_download_audit "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_acceptance_operation)
             OR EXISTS (SELECT 1 FROM business_file_download_audit)
          THEN
            RAISE EXCEPTION
              'c8f2d4a6b9e1 downgrade blocked: acceptance operation or download audit history exists';
          END IF;
        END
        $migration$;
        """
    )
    for table in (
        "maintenance_acceptance_operation",
        "business_file_download_audit",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_issue206_acceptance_audit_mutation()")
    op.drop_index(
        "ix_business_file_download_audit_project_time",
        table_name="business_file_download_audit",
    )
    op.drop_index(
        "ix_business_file_download_audit_file_time",
        table_name="business_file_download_audit",
    )
    op.drop_table("business_file_download_audit")
    op.drop_index(
        "ix_maintenance_acceptance_operation_deliverable_time",
        table_name="maintenance_acceptance_operation",
    )
    op.drop_table("maintenance_acceptance_operation")
    op.drop_constraint(
        "fk_business_file_link_acceptance",
        "business_file_link",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_business_file_original_filename_safe",
        "business_file",
        type_="check",
    )
    op.drop_constraint(
        "ck_business_file_object_key_not_external_url",
        "business_file",
        type_="check",
    )
    op.create_check_constraint(
        "ck_business_file_object_key_not_external_url",
        "business_file",
        "char_length(btrim(object_key)) > 0 AND "
        "btrim(object_key) !~* '^(https?|ftp)://'",
    )
    role_expression = "permissions" + "".join(
        f" - '{action}'" for action in _ACTIONS
    )
    op.execute(
        "UPDATE sys_role_template SET permissions = "
        + role_expression
        + " WHERE jsonb_typeof(permissions) = 'object'"
    )
    for column in ("template_perms", "permissions", "perm_overrides"):
        expression = column + "".join(f" - '{action}'" for action in _ACTIONS)
        op.execute(
            f"UPDATE sys_user SET {column} = {expression} "
            f"WHERE jsonb_typeof({column}) = 'object'"
        )
