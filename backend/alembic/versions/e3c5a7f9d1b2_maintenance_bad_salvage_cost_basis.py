"""maintenance bad salvage: frozen cost basis + stock deduction flag + salvage_in kind

Revision ID: e3c5a7f9d1b2
Revises: d7f1a3c5e8b2
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa

revision = "e3c5a7f9d1b2"
down_revision = "d7f1a3c5e8b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) 前置库流水 kind 增加 salvage_in（变卖作废回冲）
    op.execute(
        "ALTER TABLE maintenance_front_stock_ledger "
        "DROP CONSTRAINT ck_maintenance_front_stock_ledger_kind"
    )
    op.create_check_constraint(
        "ck_maintenance_front_stock_ledger_kind",
        "maintenance_front_stock_ledger",
        "kind IN ('shipment_in', 'purchase_in', 'return_out', 'salvage_out',"
        " 'salvage_in')",
    )
    # 2) 坏件变卖冻结成本证据 + 是否已扣前置库
    op.add_column(
        "maintenance_bad_salvage",
        sa.Column("cost_basis_inc_tax", sa.Numeric(precision=14, scale=2), nullable=True),
    )
    op.add_column(
        "maintenance_bad_salvage",
        sa.Column("cost_source_ref", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "maintenance_bad_salvage",
        sa.Column("cost_algorithm_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "maintenance_bad_salvage",
        sa.Column(
            "stock_deducted", sa.Boolean(), server_default="true", nullable=False
        ),
    )
    # 存量回填（round-6 Blocker 1）：stock_deducted 由真实 salvage_out 流水推导，
    # 旧实现从未写过流水 → 无对应流水的旧登记一律改为未扣账，不得 blanket true。
    op.execute(
        """
        UPDATE maintenance_bad_salvage AS salvage
        SET stock_deducted = false
        WHERE NOT EXISTS (
            SELECT 1 FROM maintenance_front_stock_ledger AS ledger
            WHERE ledger.source_type = 'salvage'
              AND ledger.source_ref LIKE 'salvage:' || salvage.salvage_id || ':%'
        )
        """
    )
    op.create_check_constraint(
        "ck_maintenance_bad_salvage_cost_pair",
        "maintenance_bad_salvage",
        "(cost_basis_inc_tax IS NULL AND cost_source_ref IS NULL "
        "AND cost_algorithm_version IS NULL) OR "
        "(cost_basis_inc_tax IS NOT NULL AND cost_source_ref IS NOT NULL "
        "AND cost_algorithm_version IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE maintenance_bad_salvage, maintenance_front_stock_ledger"
        " IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $guard$
        BEGIN
          IF EXISTS (SELECT 1 FROM maintenance_bad_salvage)
          THEN
            RAISE EXCEPTION
              'e3c5a7f9d1b2 downgrade blocked: bad salvage facts exist';
          END IF;
          IF EXISTS (
            SELECT 1 FROM maintenance_front_stock_ledger
            WHERE kind = 'salvage_in'
          )
          THEN
            RAISE EXCEPTION
              'e3c5a7f9d1b2 downgrade blocked: salvage_in ledger facts exist';
          END IF;
        END
        $guard$;
        """
    )
    op.drop_constraint(
        "ck_maintenance_bad_salvage_cost_pair",
        "maintenance_bad_salvage",
        type_="check",
    )
    op.drop_column("maintenance_bad_salvage", "stock_deducted")
    op.drop_column("maintenance_bad_salvage", "cost_algorithm_version")
    op.drop_column("maintenance_bad_salvage", "cost_source_ref")
    op.drop_column("maintenance_bad_salvage", "cost_basis_inc_tax")
    op.execute(
        "ALTER TABLE maintenance_front_stock_ledger "
        "DROP CONSTRAINT ck_maintenance_front_stock_ledger_kind"
    )
    op.create_check_constraint(
        "ck_maintenance_front_stock_ledger_kind",
        "maintenance_front_stock_ledger",
        "kind IN ('shipment_in', 'purchase_in', 'return_out', 'salvage_out')",
    )
