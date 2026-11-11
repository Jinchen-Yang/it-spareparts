"""坏件循环待修池：用结果账本替换空的一件一行表（D-0058）。

升级时若单位、事件、意图或 cycle_test/cycle_repair 待办已有行，拒绝并保留原行。
空库则删掉旧三表，建立结果、扣数、幂等命令，并按 v2 重建可售意图。
不改五个页键的默认值，不写库存。

Revision ID: c3e8a1d94b27
Revises: 86f7486b9ed5
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3e8a1d94b27"
down_revision: str | None = "86f7486b9ed5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOCK_CODES = (
    "missing_formal_image",
    "missing_category_mapping",
    "missing_list_price",
    "missing_catalog_identity",
    "publish_api_missing_stock_update",
    "publish_disabled",
    "missing_operator",
)


def _refuse_occupied_legacy() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                n bigint;
            BEGIN
                IF to_regclass('public.maintenance_cycle_unit') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_cycle_unit;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool replacement: maintenance_cycle_unit has % row(s)', n;
                    END IF;
                END IF;
                IF to_regclass('public.maintenance_cycle_event') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_cycle_event;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool replacement: maintenance_cycle_event has % row(s)', n;
                    END IF;
                END IF;
                IF to_regclass('public.maintenance_cycle_sell_intent') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_cycle_sell_intent;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool replacement: maintenance_cycle_sell_intent has % row(s)', n;
                    END IF;
                END IF;
                IF to_regclass('public.work_item') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM work_item
                     WHERE subject_type IN ('cycle_test', 'cycle_repair');
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool replacement: work_item cycle subjects have % row(s)', n;
                    END IF;
                END IF;
            END $$;
            """
        )
    )


def _refuse_occupied_new() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                n bigint;
            BEGIN
                IF to_regclass('public.maintenance_repair_record') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_repair_record;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool downgrade: maintenance_repair_record has % row(s)', n;
                    END IF;
                END IF;
                IF to_regclass('public.maintenance_repair_record_source') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_repair_record_source;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool downgrade: maintenance_repair_record_source has % row(s)', n;
                    END IF;
                END IF;
                IF to_regclass('public.maintenance_repair_command') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_repair_command;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool downgrade: maintenance_repair_command has % row(s)', n;
                    END IF;
                END IF;
                IF to_regclass('public.maintenance_cycle_sell_intent') IS NOT NULL THEN
                    SELECT count(*) INTO n FROM maintenance_cycle_sell_intent;
                    IF n > 0 THEN
                        RAISE EXCEPTION
                            'refusing repair-pool downgrade: maintenance_cycle_sell_intent has % row(s)', n;
                    END IF;
                END IF;
            END $$;
            """
        )
    )


def _drop_legacy_cycle_tables() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_cycle_sell_intent"))
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_cycle_event"))
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_cycle_unit"))


def _create_repair_objects() -> None:
    op.execute(
        sa.text(
            "CREATE SEQUENCE maintenance_repair_record_no_seq AS bigint"
        )
    )
    op.create_table(
        "maintenance_repair_record",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wire_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("record_no", sa.String(length=32), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column("fixed_qty", sa.Integer(), nullable=False),
        sa.Column("scrap_qty", sa.Integer(), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="active", nullable=False
        ),
        sa.Column("voided_by", sa.String(length=64), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.String(length=256), nullable=True),
        sa.Column(
            "shop_adjusted_manually",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("wire_unit_id > 0", name="ck_maintenance_repair_record_wire"),
        sa.CheckConstraint(
            "record_no = 'WX' || lpad(wire_unit_id::text, 8, '0')",
            name="ck_maintenance_repair_record_no",
        ),
        sa.CheckConstraint(
            "fixed_qty BETWEEN 0 AND 1000",
            name="ck_maintenance_repair_record_fixed",
        ),
        sa.CheckConstraint(
            "scrap_qty BETWEEN 0 AND 1000",
            name="ck_maintenance_repair_record_scrap",
        ),
        sa.CheckConstraint(
            "fixed_qty + scrap_qty >= 1",
            name="ck_maintenance_repair_record_total",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'voided')",
            name="ck_maintenance_repair_record_status",
        ),
        sa.CheckConstraint(
            "("
            "status = 'active' AND voided_by IS NULL AND voided_at IS NULL "
            "AND void_reason IS NULL"
            ") OR ("
            "status = 'voided' AND voided_by IS NOT NULL AND voided_at IS NOT NULL "
            "AND void_reason IS NOT NULL"
            ")",
            name="ck_maintenance_repair_record_void_shape",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_repair_record_version"),
        sa.CheckConstraint(
            "char_length(btrim(pn)) > 0 AND char_length(btrim(operator)) > 0",
            name="ck_maintenance_repair_record_text",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.PrimaryKeyConstraint("record_id"),
        sa.UniqueConstraint("wire_unit_id", name="uq_maintenance_repair_record_wire"),
    )
    op.execute(
        sa.text(
            "ALTER SEQUENCE maintenance_repair_record_no_seq "
            "OWNED BY maintenance_repair_record.wire_unit_id"
        )
    )
    op.create_index(
        "ix_maintenance_repair_record_part",
        "maintenance_repair_record",
        ["part_id", "status"],
    )
    op.create_index(
        "ix_maintenance_repair_record_operator",
        "maintenance_repair_record",
        ["operator", "created_at"],
    )

    op.create_table(
        "maintenance_repair_record_source",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", sa.String(length=36), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.CheckConstraint("qty > 0", name="ck_maintenance_repair_source_qty"),
        sa.ForeignKeyConstraint(
            ["record_id"], ["maintenance_repair_record.record_id"]
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["maintenance_rkd_return_line.rkd_line_id"]
        ),
        sa.PrimaryKeyConstraint("record_id", "receipt_id"),
    )
    op.create_index(
        "ix_maintenance_repair_source_receipt",
        "maintenance_repair_record_source",
        ["receipt_id"],
    )

    op.create_table(
        "maintenance_repair_command",
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('submit', 'void', 'attest_shop_adjustment')",
            name="ck_maintenance_repair_command_action",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_repair_command_fingerprint",
        ),
        sa.CheckConstraint(
            "status_code BETWEEN 200 AND 299",
            name="ck_maintenance_repair_command_status",
        ),
        sa.PrimaryKeyConstraint("actor", "idempotency_key"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_maintenance_repair_command_key"
        ),
    )

    block_list = ", ".join(f"'{code}'" for code in _BLOCK_CODES)
    op.create_table(
        "maintenance_cycle_sell_intent",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("wire_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("condition_bucket", sa.String(length=8), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_status", sa.String(length=16), nullable=True),
        sa.Column("block_code", sa.String(length=40), nullable=True),
        sa.Column("last_error_code", sa.String(length=40), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_sell_bucket",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'delivering', 'delivered', 'blocked', 'failed')",
            name="ck_maintenance_cycle_sell_state",
        ),
        sa.CheckConstraint(
            "qty BETWEEN 1 AND 1000", name="ck_maintenance_cycle_sell_qty"
        ),
        sa.CheckConstraint("wire_unit_id > 0", name="ck_maintenance_cycle_sell_wire"),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_maintenance_cycle_sell_attempts"
        ),
        sa.CheckConstraint(
            "(state = 'delivering') = "
            "(lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_maintenance_cycle_sell_lease",
        ),
        sa.CheckConstraint(
            "receipt_status IS NULL OR "
            "receipt_status IN ('applied', 'unchanged', 'blocked', 'conflict')",
            name="ck_maintenance_cycle_sell_receipt",
        ),
        sa.CheckConstraint(
            f"block_code IS NULL OR block_code IN ({block_list})",
            name="ck_maintenance_cycle_sell_block",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(
            ["record_id"], ["maintenance_repair_record.record_id"]
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint(
            "record_id", name="uq_maintenance_cycle_sell_intent_record"
        ),
        sa.UniqueConstraint(
            "wire_unit_id", name="uq_maintenance_cycle_sell_intent_wire"
        ),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_pending",
        "maintenance_cycle_sell_intent",
        ["available_at", "created_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_lease",
        "maintenance_cycle_sell_intent",
        ["lease_until"],
        postgresql_where=sa.text("state = 'delivering'"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_blocked",
        "maintenance_cycle_sell_intent",
        ["available_at"],
        postgresql_where=sa.text("state = 'blocked'"),
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION repair_receipt_eligible_qty(line maintenance_rkd_return_line)
            RETURNS integer
            LANGUAGE sql
            STABLE
            AS $function$
                SELECT CASE
                    WHEN line.line_status = 'active'
                     AND line.test_result IN ('坏品', '坏件', '故障')
                     AND line.part_id IS NOT NULL
                     AND line.qty >= 1
                     AND line.qty = trunc(line.qty)
                     AND line.qty <= 2147483647
                    THEN trunc(line.qty)::integer
                    ELSE 0
                END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION assert_repair_deduction_within_receipt(target_receipt text)
            RETURNS void
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                line maintenance_rkd_return_line;
                eligible integer;
                deducted bigint;
            BEGIN
                SELECT * INTO line
                FROM maintenance_rkd_return_line AS receipt_line
                WHERE receipt_line.rkd_line_id = target_receipt;
                IF NOT FOUND THEN
                    eligible := 0;
                ELSE
                    eligible := repair_receipt_eligible_qty(line);
                END IF;
                SELECT COALESCE(SUM(s.qty), 0) INTO deducted
                FROM maintenance_repair_record_source AS s
                JOIN maintenance_repair_record AS r ON r.record_id = s.record_id
                WHERE s.receipt_id = target_receipt
                  AND r.status = 'active';
                IF deducted > eligible THEN
                    RAISE EXCEPTION 'repair deduction exceeds receipt'
                        USING ERRCODE = '23514';
                END IF;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION trg_repair_deduction_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                deducted bigint;
                receipt text;
            BEGIN
                IF TG_TABLE_NAME = 'maintenance_repair_record_source' THEN
                    receipt := COALESCE(NEW.receipt_id, OLD.receipt_id);
                    PERFORM assert_repair_deduction_within_receipt(receipt);
                    RETURN NULL;
                END IF;
                IF TG_OP = 'UPDATE' AND OLD.part_id IS DISTINCT FROM NEW.part_id THEN
                    SELECT COALESCE(SUM(s.qty), 0) INTO deducted
                    FROM maintenance_repair_record_source AS s
                    JOIN maintenance_repair_record AS r ON r.record_id = s.record_id
                    WHERE s.receipt_id = NEW.rkd_line_id
                      AND r.status = 'active';
                    IF deducted > 0 THEN
                        RAISE EXCEPTION 'repair deduction exceeds receipt'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
                PERFORM assert_repair_deduction_within_receipt(NEW.rkd_line_id);
                RETURN NULL;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER trg_repair_source_deduction_guard
            AFTER INSERT OR UPDATE OR DELETE ON maintenance_repair_record_source
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION trg_repair_deduction_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER trg_repair_receipt_deduction_guard
            AFTER UPDATE OF qty, line_status, test_result, part_id
            ON maintenance_rkd_return_line
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION trg_repair_deduction_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE VIEW v_repair_pool AS
            WITH line_eligible AS (
                SELECT t.part_id,
                       SUM(repair_receipt_eligible_qty(t))::bigint AS eligible_qty
                FROM maintenance_rkd_return_line t
                WHERE t.part_id IS NOT NULL
                GROUP BY t.part_id
            ),
            deducted AS (
                SELECT t.part_id, SUM(s.qty)::bigint AS deducted_qty
                FROM maintenance_repair_record_source s
                JOIN maintenance_repair_record r
                  ON r.record_id = s.record_id AND r.status = 'active'
                JOIN maintenance_rkd_return_line t ON t.rkd_line_id = s.receipt_id
                GROUP BY t.part_id
            ),
            results AS (
                SELECT part_id,
                       SUM(fixed_qty)::bigint AS shop_qty,
                       SUM(scrap_qty)::bigint AS scrap_qty
                FROM maintenance_repair_record
                WHERE status = 'active'
                GROUP BY part_id
            ),
            parts AS (
                SELECT part_id FROM line_eligible
                UNION
                SELECT part_id FROM results
            )
            SELECT p.part_id,
                   (COALESCE(e.eligible_qty, 0) - COALESCE(d.deducted_qty, 0)) AS pending_qty,
                   COALESCE(r.shop_qty, 0) AS shop_qty,
                   COALESCE(r.scrap_qty, 0) AS scrap_qty
            FROM parts p
            LEFT JOIN line_eligible e ON e.part_id = p.part_id
            LEFT JOIN deducted d ON d.part_id = p.part_id
            LEFT JOIN results r ON r.part_id = p.part_id
            """
        )
    )


def _drop_repair_objects() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_repair_receipt_deduction_guard "
            "ON maintenance_rkd_return_line"
        )
    )
    op.execute(sa.text("DROP VIEW IF EXISTS v_repair_pool"))
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_cycle_sell_intent"))
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_repair_record_source"))
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_repair_command"))
    op.execute(sa.text("DROP TABLE IF EXISTS maintenance_repair_record"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS trg_repair_deduction_guard()"))
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS assert_repair_deduction_within_receipt(text)"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "repair_receipt_eligible_qty(maintenance_rkd_return_line)"
        )
    )


def _restore_legacy_cycle_tables() -> None:
    """降级只恢复空的旧三表，供 86f7486b9ed5 的 downgrade 继续删除。"""
    op.create_table(
        "maintenance_cycle_unit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "unit_uid",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("pn", sa.String(length=128), nullable=False),
        sa.Column(
            "station", sa.String(length=16), server_default="pool", nullable=False
        ),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("condition_bucket", sa.String(length=8), nullable=True),
        sa.Column("intake_condition", sa.String(length=64), nullable=True),
        sa.Column("repair_round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_receipt_id", sa.String(length=36), nullable=True),
        sa.Column("serial_number", sa.String(length=128), nullable=True),
        sa.Column("vendor_name", sa.String(length=128), nullable=True),
        sa.Column("assignee_username", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "station IN ('pool', 'testing', 'repairing', 'outsource', 'ready', 'closed')",
            name="ck_maintenance_cycle_unit_station",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('to_shop', 'to_company', 'scrap', 'sold')",
            name="ck_maintenance_cycle_unit_outcome",
        ),
        sa.CheckConstraint(
            "condition_bucket IS NULL OR condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_unit_bucket",
        ),
        sa.CheckConstraint(
            "source_kind IN ('receipt', 'external')",
            name="ck_maintenance_cycle_unit_source_kind",
        ),
        sa.CheckConstraint(
            "(source_kind = 'receipt' AND source_receipt_id IS NOT NULL) OR "
            "(source_kind = 'external' AND source_receipt_id IS NULL)",
            name="ck_maintenance_cycle_unit_source_shape",
        ),
        sa.CheckConstraint(
            "(station = 'closed') = (outcome IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_maintenance_cycle_unit_closed_shape",
        ),
        sa.CheckConstraint(
            "withdrawn_at IS NULL OR "
            "(station = 'pool' AND outcome IS NULL AND closed_at IS NULL)",
            name="ck_maintenance_cycle_unit_withdrawn",
        ),
        sa.CheckConstraint(
            "(outcome IS NULL OR outcome NOT IN ('to_shop', 'sold')) OR "
            "(condition_bucket IS NOT NULL AND condition_bucket IN ('good', 'bad'))",
            name="ck_maintenance_cycle_unit_shop_bucket",
        ),
        sa.CheckConstraint(
            "repair_round >= 0 AND version >= 1",
            name="ck_maintenance_cycle_unit_counters",
        ),
        sa.CheckConstraint(
            "char_length(btrim(pn)) > 0",
            name="ck_maintenance_cycle_unit_pn",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(
            ["source_receipt_id"], ["maintenance_rkd_return_line.rkd_line_id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unit_uid", name="uq_maintenance_cycle_unit_uid"),
    )
    op.create_index(
        "ix_maintenance_cycle_unit_station",
        "maintenance_cycle_unit",
        ["station", "assignee_username"],
    )
    op.create_index(
        "ix_maintenance_cycle_unit_part",
        "maintenance_cycle_unit",
        ["part_id", "station"],
    )
    op.create_index(
        "ix_maintenance_cycle_unit_receipt",
        "maintenance_cycle_unit",
        ["source_receipt_id"],
    )
    op.create_index(
        "ux_maintenance_cycle_unit_receipt_sn",
        "maintenance_cycle_unit",
        ["source_receipt_id", "serial_number"],
        unique=True,
        postgresql_where=sa.text(
            "withdrawn_at IS NULL AND serial_number IS NOT NULL"
        ),
    )
    op.create_table(
        "maintenance_cycle_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["maintenance_cycle_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_maintenance_cycle_event_unit",
        "maintenance_cycle_event",
        ["unit_id", "id"],
    )
    op.create_table(
        "maintenance_cycle_sell_intent",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_id", sa.BigInteger(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("condition_bucket", sa.String(length=8), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_status", sa.String(length=16), nullable=True),
        sa.Column("block_code", sa.String(length=40), nullable=True),
        sa.Column("last_error_code", sa.String(length=40), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "condition_bucket IN ('good', 'bad')",
            name="ck_maintenance_cycle_sell_bucket",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'delivering', 'delivered', 'blocked', 'failed')",
            name="ck_maintenance_cycle_sell_state",
        ),
        sa.CheckConstraint("qty = 1", name="ck_maintenance_cycle_sell_qty"),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_maintenance_cycle_sell_attempts"
        ),
        sa.CheckConstraint(
            "(state = 'delivering') = "
            "(lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_maintenance_cycle_sell_lease",
        ),
        sa.CheckConstraint(
            "receipt_status IS NULL OR "
            "receipt_status IN ('applied', 'unchanged', 'blocked', 'conflict')",
            name="ck_maintenance_cycle_sell_receipt",
        ),
        sa.ForeignKeyConstraint(["part_id"], ["dim_part.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["maintenance_cycle_unit.id"]),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint(
            "unit_id", name="uq_maintenance_cycle_sell_intent_unit"
        ),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_pending",
        "maintenance_cycle_sell_intent",
        ["available_at", "created_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_lease",
        "maintenance_cycle_sell_intent",
        ["lease_until"],
        postgresql_where=sa.text("state = 'delivering'"),
    )
    op.create_index(
        "ix_maintenance_cycle_sell_blocked",
        "maintenance_cycle_sell_intent",
        ["available_at"],
        postgresql_where=sa.text("state = 'blocked'"),
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _refuse_occupied_legacy()
    _drop_legacy_cycle_tables()
    _create_repair_objects()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _refuse_occupied_new()
    _drop_repair_objects()
    _restore_legacy_cycle_tables()
