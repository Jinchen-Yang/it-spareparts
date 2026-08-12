"""maintenance warehouse adapters and ambiguity workbench

Revision ID: a6d1e9c3b7f2
Revises: f4b8c2d1e7a6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a6d1e9c3b7f2"
down_revision: str | None = "f4b8c2d1e7a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "maintenance_warehouse_import_batch",
        sa.Column("import_id", sa.String(36), primary_key=True),
        sa.Column("source_file_hash", sa.String(64), nullable=False),
        sa.Column("source_filename", sa.String(256), nullable=False),
        sa.Column("adapter_key", sa.String(32), nullable=False),
        sa.Column("adapter_version", sa.String(32), nullable=False),
        sa.Column("version_state", sa.String(24), nullable=False),
        sa.Column("header_signature", sa.String(64), nullable=False),
        sa.Column("header_pairs_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("ambiguity_count", sa.Integer(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("applied_by", sa.String(64), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source_file_hash ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_batch_hash"),
        sa.CheckConstraint("header_signature ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_batch_header_hash"),
        sa.CheckConstraint("version_state IN ('known', 'unknown_version')", name="ck_maintenance_wh_batch_version_state"),
        sa.CheckConstraint("status = 'applied'", name="ck_maintenance_wh_batch_status"),
        sa.CheckConstraint("document_count >= 0 AND line_count >= 0 AND ambiguity_count >= 0", name="ck_maintenance_wh_batch_counts"),
        sa.CheckConstraint("char_length(btrim(reason)) > 0", name="ck_maintenance_wh_batch_reason"),
        sa.CheckConstraint("char_length(btrim(applied_by)) > 0", name="ck_maintenance_wh_batch_operator"),
        sa.UniqueConstraint("source_file_hash", "adapter_version", name="uq_maintenance_wh_batch_file_adapter"),
    )
    op.create_index("ix_maintenance_wh_batch_applied", "maintenance_warehouse_import_batch", ["applied_at", "import_id"])

    op.create_table(
        "maintenance_warehouse_document",
        sa.Column("document_id", sa.String(36), primary_key=True),
        sa.Column("document_type", sa.String(16), nullable=False),
        sa.Column("source_document_id", sa.String(128), nullable=False),
        sa.Column("document_no", sa.String(128), nullable=False),
        sa.Column("document_date", sa.Date()),
        sa.Column("raw_status", sa.String(128)),
        sa.Column("normalized_status", sa.String(16), nullable=False),
        sa.Column("raw_fields_json", postgresql.JSONB(), nullable=False),
        sa.Column("raw_fingerprint", sa.String(64), nullable=False),
        sa.Column("first_import_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_import_batch.import_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("document_type IN ('shipment', 'return', 'receipt')", name="ck_maintenance_wh_document_type"),
        sa.CheckConstraint("normalized_status IN ('confirmed', 'pending', 'void', 'unknown')", name="ck_maintenance_wh_document_status"),
        sa.CheckConstraint("raw_fingerprint ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_document_fingerprint"),
        sa.UniqueConstraint("document_type", "document_no", name="uq_maintenance_wh_document_no"),
    )
    op.create_index("ix_maintenance_wh_document_source", "maintenance_warehouse_document", ["document_type", "source_document_id"])
    op.create_index("ix_maintenance_wh_document_date", "maintenance_warehouse_document", ["document_type", "document_date"])

    op.create_table(
        "maintenance_warehouse_document_line",
        sa.Column("line_id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_document.document_id"), nullable=False),
        sa.Column("source_line_id", sa.String(128), nullable=False),
        sa.Column("line_no", sa.Integer()),
        sa.Column("pn", sa.String(256)),
        sa.Column("sn", sa.String(256)),
        sa.Column("self_code", sa.String(256)),
        sa.Column("quantity", sa.Numeric(14, 3)),
        sa.Column("raw_fields_json", postgresql.JSONB(), nullable=False),
        sa.Column("raw_fingerprint", sa.String(64), nullable=False),
        sa.Column("first_import_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_import_batch.import_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("line_no IS NULL OR line_no >= 1", name="ck_maintenance_wh_line_no"),
        sa.CheckConstraint("quantity IS NULL OR (quantity >= 0 AND quantity < 1000000000000)", name="ck_maintenance_wh_line_qty"),
        sa.CheckConstraint("raw_fingerprint ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_line_fingerprint"),
        sa.UniqueConstraint("document_id", "source_line_id", name="uq_maintenance_wh_line_source"),
    )
    op.create_index("ix_maintenance_wh_line_pn", "maintenance_warehouse_document_line", ["pn"])
    op.create_index("ix_maintenance_wh_line_sn", "maintenance_warehouse_document_line", ["sn"])
    op.create_index("ix_maintenance_wh_line_self_code", "maintenance_warehouse_document_line", ["self_code"])

    op.create_table(
        "maintenance_warehouse_document_link",
        sa.Column("link_id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_document.document_id"), nullable=False),
        sa.Column("line_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_document_line.line_id")),
        sa.Column("link_kind", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("stable_key_kind", sa.String(32), nullable=False),
        sa.Column("stable_key_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(
            "supersedes_link_id",
            sa.String(36),
            sa.ForeignKey("maintenance_warehouse_document_link.link_id"),
            unique=True,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("link_kind IN ('maintenance_order', 'project', 'site_issue', 'bad_return', 'part', 'warehouse_document')", name="ck_maintenance_wh_link_kind"),
        sa.CheckConstraint("target_type IN ('maintenance_order', 'maintenance_project', 'maintenance_site_issue', 'maintenance_bad_return', 'dim_part', 'warehouse_document')", name="ck_maintenance_wh_link_target_type"),
        sa.CheckConstraint(
            "(link_kind = 'maintenance_order' AND target_type = 'maintenance_order') OR "
            "(link_kind = 'project' AND target_type = 'maintenance_project') OR "
            "(link_kind = 'site_issue' AND target_type = 'maintenance_site_issue') OR "
            "(link_kind = 'bad_return' AND target_type = 'maintenance_bad_return') OR "
            "(link_kind = 'part' AND target_type = 'dim_part') OR "
            "(link_kind = 'warehouse_document' AND target_type = 'warehouse_document')",
            name="ck_maintenance_wh_link_target_matrix",
        ),
        sa.CheckConstraint("source IN ('automatic', 'manual')", name="ck_maintenance_wh_link_source"),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_maintenance_wh_link_status"),
        sa.CheckConstraint(
            "(status = 'active' AND ((version = 1 AND supersedes_link_id IS NULL) OR "
            "(version >= 2 AND supersedes_link_id IS NOT NULL))) OR "
            "(status = 'superseded' AND version >= 2)",
            name="ck_maintenance_wh_link_supersession",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_wh_link_version"),
        sa.CheckConstraint("stable_key_hash ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_link_key_hash"),
        sa.CheckConstraint("char_length(btrim(reason)) > 0", name="ck_maintenance_wh_link_reason"),
        sa.CheckConstraint("char_length(btrim(operated_by)) > 0", name="ck_maintenance_wh_link_operator"),
    )
    op.create_index(
        "uq_maintenance_wh_link_target",
        "maintenance_warehouse_document_link",
        ["document_id", sa.text("coalesce(line_id, '')"), "link_kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_maintenance_wh_link_document", "maintenance_warehouse_document_link", ["document_id", "line_id", "link_kind"])
    op.create_index("ix_maintenance_wh_link_target", "maintenance_warehouse_document_link", ["target_type", "target_id"])

    op.create_table(
        "maintenance_warehouse_ambiguity",
        sa.Column("ambiguity_id", sa.String(36), primary_key=True),
        sa.Column("import_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_import_batch.import_id"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_document.document_id")),
        sa.Column("line_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_document_line.line_id")),
        sa.Column("ambiguity_type", sa.String(32), nullable=False),
        sa.Column("field_code", sa.String(256)),
        sa.Column("source_row", sa.Integer()),
        sa.Column("value_hash", sa.String(64)),
        sa.Column("candidates_json", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_json", postgresql.JSONB()),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resolution_json", postgresql.JSONB()),
        sa.Column("resolution_reason", sa.Text()),
        sa.Column("resolved_by", sa.String(64)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "ambiguity_type IN ('unknown_version', 'missing_document_id', 'missing_line_id', "
            "'missing_stable_link', 'multiple_candidates', 'field_conflict', "
            "'unknown_enum', 'controlled_attachment', 'integration_blocker')",
            name="ck_maintenance_wh_ambiguity_type",
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_maintenance_wh_ambiguity_status"),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_wh_ambiguity_version"),
        sa.CheckConstraint("source_row IS NULL OR source_row >= 3", name="ck_maintenance_wh_ambiguity_row"),
        sa.CheckConstraint("value_hash IS NULL OR value_hash ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_ambiguity_value_hash"),
        sa.CheckConstraint("fingerprint ~ '^[a-f0-9]{64}$'", name="ck_maintenance_wh_ambiguity_fingerprint"),
        sa.CheckConstraint(
            "(status = 'open' AND resolution_json IS NULL AND resolution_reason IS NULL "
            "AND resolved_by IS NULL AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolution_json IS NOT NULL "
            "AND char_length(btrim(resolution_reason)) > 0 "
            "AND char_length(btrim(resolved_by)) > 0 AND resolved_at IS NOT NULL)",
            name="ck_maintenance_wh_ambiguity_resolution",
        ),
        sa.UniqueConstraint("import_id", "fingerprint", name="uq_maintenance_wh_ambiguity_fingerprint"),
    )
    op.create_index("ix_maintenance_wh_ambiguity_queue", "maintenance_warehouse_ambiguity", ["status", "ambiguity_type", "created_at"])
    op.create_index("ix_maintenance_wh_ambiguity_document", "maintenance_warehouse_ambiguity", ["document_id", "line_id"])

    op.create_table(
        "maintenance_warehouse_audit_event",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("import_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_import_batch.import_id")),
        sa.Column("ambiguity_id", sa.String(36), sa.ForeignKey("maintenance_warehouse_ambiguity.ambiguity_id")),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("before_json", postgresql.JSONB()),
        sa.Column("after_json", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operated_by", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("action IN ('import_applied', 'ambiguity_resolved')", name="ck_maintenance_wh_audit_action"),
        sa.CheckConstraint("char_length(btrim(reason)) > 0", name="ck_maintenance_wh_audit_reason"),
        sa.CheckConstraint("char_length(btrim(operated_by)) > 0", name="ck_maintenance_wh_audit_operator"),
    )
    op.create_index("ix_maintenance_wh_audit_time", "maintenance_warehouse_audit_event", ["occurred_at", "event_id"])
    op.create_index("ix_maintenance_wh_audit_ambiguity", "maintenance_warehouse_audit_event", ["ambiguity_id", "occurred_at"])

    op.execute(
        """
        CREATE FUNCTION reject_maintenance_warehouse_immutable_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'maintenance warehouse history is immutable';
        END; $$
        """
    )
    for table in (
        "maintenance_warehouse_import_batch",
        "maintenance_warehouse_document",
        "maintenance_warehouse_document_line",
        "maintenance_warehouse_audit_event",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_maintenance_warehouse_immutable_mutation()"
        )
    op.execute(
        """
        CREATE FUNCTION enforce_maintenance_warehouse_link_supersession()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE'
             OR OLD.status <> 'active'
             OR NEW.status <> 'superseded'
             OR NEW.version <> OLD.version + 1
             OR NEW.link_id IS DISTINCT FROM OLD.link_id
             OR NEW.document_id IS DISTINCT FROM OLD.document_id
             OR NEW.line_id IS DISTINCT FROM OLD.line_id
             OR NEW.link_kind IS DISTINCT FROM OLD.link_kind
             OR NEW.target_type IS DISTINCT FROM OLD.target_type
             OR NEW.target_id IS DISTINCT FROM OLD.target_id
             OR NEW.stable_key_kind IS DISTINCT FROM OLD.stable_key_kind
             OR NEW.stable_key_hash IS DISTINCT FROM OLD.stable_key_hash
             OR NEW.source IS DISTINCT FROM OLD.source
             OR NEW.supersedes_link_id IS DISTINCT FROM OLD.supersedes_link_id
             OR NEW.reason IS DISTINCT FROM OLD.reason
             OR NEW.operated_by IS DISTINCT FROM OLD.operated_by
             OR NEW.created_at IS DISTINCT FROM OLD.created_at
          THEN
            RAISE EXCEPTION 'maintenance warehouse link identity is immutable';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_maintenance_warehouse_document_link_supersession "
        "BEFORE UPDATE OR DELETE ON maintenance_warehouse_document_link "
        "FOR EACH ROW EXECUTE FUNCTION enforce_maintenance_warehouse_link_supersession()"
    )
    op.execute(
        """
        CREATE FUNCTION enforce_maintenance_warehouse_ambiguity_resolution()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR OLD.status = 'resolved'
             OR NEW.import_id IS DISTINCT FROM OLD.import_id
             OR NEW.document_id IS DISTINCT FROM OLD.document_id
             OR NEW.line_id IS DISTINCT FROM OLD.line_id
             OR NEW.ambiguity_type IS DISTINCT FROM OLD.ambiguity_type
             OR NEW.field_code IS DISTINCT FROM OLD.field_code
             OR NEW.source_row IS DISTINCT FROM OLD.source_row
             OR NEW.value_hash IS DISTINCT FROM OLD.value_hash
             OR NEW.candidates_json IS DISTINCT FROM OLD.candidates_json
             OR NEW.evidence_json IS DISTINCT FROM OLD.evidence_json
             OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
             OR NEW.created_at IS DISTINCT FROM OLD.created_at
             OR NEW.status <> 'resolved'
             OR NEW.version <> OLD.version + 1
          THEN
            RAISE EXCEPTION 'maintenance warehouse ambiguity identity is immutable';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_maintenance_warehouse_ambiguity_resolution "
        "BEFORE UPDATE OR DELETE ON maintenance_warehouse_ambiguity FOR EACH ROW "
        "EXECUTE FUNCTION enforce_maintenance_warehouse_ambiguity_resolution()"
    )

    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE WHEN jsonb_typeof(permissions) = 'object' THEN permissions ELSE '{}'::jsonb END
          || jsonb_build_object('action_maintenance_warehouse_manage', code = 'admin')
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
              'action_maintenance_warehouse_manage', role = 'admin'),
            perm_overrides = CASE WHEN jsonb_typeof(perm_overrides) = 'object'
              THEN perm_overrides ELSE '{}'::jsonb END - 'action_maintenance_warehouse_manage'
        WHERE jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE WHEN jsonb_typeof(permissions) = 'object' THEN permissions ELSE '{}'::jsonb END
          || jsonb_build_object('action_maintenance_warehouse_manage', role = 'admin')
        WHERE permissions IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    tables = (
        "maintenance_warehouse_audit_event",
        "maintenance_warehouse_ambiguity",
        "maintenance_warehouse_document_link",
        "maintenance_warehouse_document_line",
        "maintenance_warehouse_document",
        "maintenance_warehouse_import_batch",
    )
    op.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
    checks = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in tables)
    op.execute(
        f"""
        DO $migration$
        BEGIN
          IF {checks} THEN
            RAISE EXCEPTION
              'a6d1e9c3b7f2 downgrade blocked: maintenance warehouse business history is not empty';
          END IF;
        END $migration$;
        """
    )
    op.execute("DROP TRIGGER trg_maintenance_warehouse_ambiguity_resolution ON maintenance_warehouse_ambiguity")
    op.execute("DROP FUNCTION enforce_maintenance_warehouse_ambiguity_resolution()")
    op.execute(
        "DROP TRIGGER trg_maintenance_warehouse_document_link_supersession "
        "ON maintenance_warehouse_document_link"
    )
    op.execute("DROP FUNCTION enforce_maintenance_warehouse_link_supersession()")
    for table in (
        "maintenance_warehouse_audit_event",
        "maintenance_warehouse_document_line",
        "maintenance_warehouse_document",
        "maintenance_warehouse_import_batch",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_maintenance_warehouse_immutable_mutation()")
    op.execute("UPDATE sys_role_template SET permissions = permissions - 'action_maintenance_warehouse_manage' WHERE jsonb_typeof(permissions) = 'object'")
    op.execute("UPDATE sys_user SET template_perms = template_perms - 'action_maintenance_warehouse_manage' WHERE jsonb_typeof(template_perms) = 'object'")
    op.execute("UPDATE sys_user SET permissions = permissions - 'action_maintenance_warehouse_manage' WHERE jsonb_typeof(permissions) = 'object'")
    op.execute("UPDATE sys_user SET perm_overrides = perm_overrides - 'action_maintenance_warehouse_manage' WHERE jsonb_typeof(perm_overrides) = 'object'")
    op.drop_table("maintenance_warehouse_audit_event")
    op.drop_table("maintenance_warehouse_ambiguity")
    op.drop_table("maintenance_warehouse_document_link")
    op.drop_table("maintenance_warehouse_document_line")
    op.drop_table("maintenance_warehouse_document")
    op.drop_table("maintenance_warehouse_import_batch")
