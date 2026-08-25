"""acceptance: attachment_archive 操作类型入台账 CHECK

2026-08-25 客户口径「能传也能删」：删除验收附件为软删并写操作台账
（operation_type='attachment_archive'），ck_maintenance_acceptance_operation_type
放行该值；batch_alter_table 写法保持 SQLite 兼容（范式同 b1d4f6a8c2e7）。

Revision ID: e2f6a9c4b1d8
Revises: c4d9a2e7f1b0
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e2f6a9c4b1d8"
down_revision: str | None = "c4d9a2e7f1b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    with op.batch_alter_table("maintenance_acceptance_operation") as batch:
        batch.drop_constraint(
            "ck_maintenance_acceptance_operation_type", type_="check"
        )
        batch.create_check_constraint(
            "ck_maintenance_acceptance_operation_type",
            "operation_type IN ('attachment_upload', 'attachment_archive', "
            "'submit', 'approve', 'reject')",
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 台账是审计事实，不代为删除：仍存在删除台账行时显式失败，先人工处理
    # （同 a9e2f7c4d1b8 downgrade 的 blocked 写法）。
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM maintenance_acceptance_operation
              WHERE operation_type = 'attachment_archive'
          )
          THEN
            RAISE EXCEPTION
              'e2f6a9c4b1d8 downgrade blocked: attachment_archive operation rows exist';
          END IF;
        END
        $migration$;
        """
    )
    with op.batch_alter_table("maintenance_acceptance_operation") as batch:
        batch.drop_constraint(
            "ck_maintenance_acceptance_operation_type", type_="check"
        )
        batch.create_check_constraint(
            "ck_maintenance_acceptance_operation_type",
            "operation_type IN ('attachment_upload', 'submit', 'approve', 'reject')",
        )
