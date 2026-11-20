"""Share maintenance-return capacity between both repair workflows."""

from alembic import op


revision = "c8e62a7d4f31"
down_revision = "b7d43e19af62"
branch_labels = None
depends_on = None


_LEGACY_DEDUCTIONS = """
    SELECT s.receipt_id, SUM(s.qty)::bigint AS legacy_qty
    FROM maintenance_repair_record_source s
    JOIN maintenance_repair_record r ON r.record_id = s.record_id AND r.status = 'active'
    GROUP BY s.receipt_id
"""


def _pool_view(shared: bool) -> None:
    amounts = (
        "SELECT receipt_id, legacy_qty + order_qty AS qty FROM v_repair_receipt_deductions"
        if shared else
        f"SELECT receipt_id, legacy_qty AS qty FROM ({_LEGACY_DEDUCTIONS}) legacy"
    )
    op.execute(f"""
        CREATE OR REPLACE VIEW v_repair_pool AS
        WITH line_eligible AS (
            SELECT t.part_id, SUM(repair_receipt_eligible_qty(t))::bigint AS eligible_qty
            FROM maintenance_rkd_return_line t
            WHERE t.part_id IS NOT NULL
            GROUP BY t.part_id
        ),
        deducted AS (
            SELECT t.part_id, SUM(d.qty)::bigint AS deducted_qty
            FROM ({amounts}) d
            JOIN maintenance_rkd_return_line t ON t.rkd_line_id = d.receipt_id
            GROUP BY t.part_id
        ),
        results AS (
            SELECT part_id, SUM(fixed_qty)::bigint AS shop_qty, SUM(scrap_qty)::bigint AS scrap_qty
            FROM maintenance_repair_record WHERE status = 'active' GROUP BY part_id
        ),
        parts AS (
            SELECT part_id FROM line_eligible UNION SELECT part_id FROM results
        )
        SELECT p.part_id,
               (COALESCE(e.eligible_qty, 0) - COALESCE(d.deducted_qty, 0)) AS pending_qty,
               COALESCE(r.shop_qty, 0) AS shop_qty,
               COALESCE(r.scrap_qty, 0) AS scrap_qty
        FROM parts p
        LEFT JOIN line_eligible e ON e.part_id = p.part_id
        LEFT JOIN deducted d ON d.part_id = p.part_id
        LEFT JOIN results r ON r.part_id = p.part_id
    """)


def _receipt_guard(shared: bool) -> None:
    amounts = (
        "SELECT v.legacy_qty, v.order_qty FROM v_repair_receipt_deductions v WHERE v.receipt_id = target_receipt"
        if shared else
        f"SELECT v.legacy_qty, 0::bigint AS order_qty FROM ({_LEGACY_DEDUCTIONS}) v WHERE v.receipt_id = target_receipt"
    )
    row_lock = "FOR NO KEY UPDATE" if shared else ""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION assert_repair_deduction_within_receipt(target_receipt text)
        RETURNS void LANGUAGE plpgsql AS $function$
        DECLARE
            line maintenance_rkd_return_line;
            eligible integer;
            legacy_qty bigint;
            order_qty bigint;
        BEGIN
            SELECT * INTO line FROM maintenance_rkd_return_line
            WHERE rkd_line_id = target_receipt {row_lock};
            eligible := repair_receipt_eligible_qty(line);
            SELECT d.legacy_qty, d.order_qty INTO legacy_qty, order_qty FROM ({amounts}) d;
            IF COALESCE(legacy_qty, 0) + COALESCE(order_qty, 0) > eligible
                OR (COALESCE(order_qty, 0) > 0 AND (line.receipt_kind <> 'part' OR line.review_required))
            THEN
                RAISE EXCEPTION 'repair deduction exceeds receipt' USING ERRCODE = '23514';
            END IF;
        END
        $function$
    """)
    part_deductions = (
        "SELECT receipt_id, legacy_qty + order_qty AS qty FROM v_repair_receipt_deductions"
        if shared else
        f"SELECT receipt_id, legacy_qty AS qty FROM ({_LEGACY_DEDUCTIONS}) legacy"
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION trg_repair_deduction_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
            IF TG_TABLE_NAME = 'maintenance_repair_record_source' THEN
                PERFORM assert_repair_deduction_within_receipt(COALESCE(NEW.receipt_id, OLD.receipt_id));
                RETURN NULL;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.part_id IS DISTINCT FROM NEW.part_id AND EXISTS (
                SELECT 1 FROM ({part_deductions}) d WHERE d.receipt_id = NEW.rkd_line_id AND d.qty > 0
            ) THEN
                RAISE EXCEPTION 'repair deduction exceeds receipt' USING ERRCODE = '23514';
            END IF;
            PERFORM assert_repair_deduction_within_receipt(NEW.rkd_line_id);
            RETURN NULL;
        END
        $function$
    """)


def upgrade() -> None:
    op.execute(f"""
        CREATE VIEW v_repair_receipt_deductions AS
        WITH legacy AS ({_LEGACY_DEDUCTIONS}),
        reserved AS (
            SELECT source_receipt_id AS receipt_id, SUM(qty)::bigint AS order_qty
            FROM maintenance_repair_order_line
            WHERE source_receipt_id IS NOT NULL
            GROUP BY source_receipt_id
        )
        SELECT receipt_id,
               COALESCE(legacy.legacy_qty, 0)::bigint AS legacy_qty,
               COALESCE(reserved.order_qty, 0)::bigint AS order_qty
        FROM legacy FULL JOIN reserved USING (receipt_id)
    """)
    _receipt_guard(shared=True)
    _pool_view(shared=True)
    op.execute("""
        CREATE FUNCTION trg_repair_order_deduction_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
            IF TG_OP <> 'INSERT' AND OLD.source_receipt_id IS NOT NULL THEN
                PERFORM assert_repair_deduction_within_receipt(OLD.source_receipt_id);
            END IF;
            IF TG_OP <> 'DELETE' AND NEW.source_receipt_id IS NOT NULL
                AND (TG_OP = 'INSERT' OR NEW.source_receipt_id IS DISTINCT FROM OLD.source_receipt_id)
            THEN
                PERFORM assert_repair_deduction_within_receipt(NEW.source_receipt_id);
            END IF;
            RETURN NULL;
        END
        $function$
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_repair_order_source_deduction_guard
        AFTER INSERT OR UPDATE OR DELETE ON maintenance_repair_order_line
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_repair_order_deduction_guard()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_repair_order_receipt_eligibility_guard
        AFTER UPDATE OF receipt_kind, review_required ON maintenance_rkd_return_line
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trg_repair_deduction_guard()
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE maintenance_repair_order, maintenance_repair_order_line IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM maintenance_repair_order_line WHERE source_receipt_id IS NOT NULL) THEN
                RAISE EXCEPTION 'refusing to disable shared repair capacity while return sources are reserved';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_repair_order_receipt_eligibility_guard ON maintenance_rkd_return_line")
    op.execute("DROP TRIGGER trg_repair_order_source_deduction_guard ON maintenance_repair_order_line")
    op.execute("DROP FUNCTION trg_repair_order_deduction_guard()")
    _receipt_guard(shared=False)
    _pool_view(shared=False)
    op.execute("DROP VIEW v_repair_receipt_deductions")
