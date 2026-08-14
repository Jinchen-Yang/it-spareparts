"""add maintenance collection reminders storage

Revision ID: c8e2a4f6b1d3
Revises: d9f1a3c7e5b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c8e2a4f6b1d3"
down_revision: str | None = "d9f1a3c7e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FOLLOW_UP_KEY = "action_maintenance_collection_follow_up"
_PLAN_IMPORT_KEY = "action_maintenance_collection_plan_import"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ---- 1. 三张新表（先建表，milestone 的新 FK 才能引用）----
    # 导入批次：预览与应用共用的不可变证据（设计 §4.4）。storage_key 全局唯一；
    # 同一 owner 相同 operation_key 只允许一个批次（并发相同预览收敛）。
    op.create_table(
        "maintenance_collection_plan_import_batch",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("data_version", sa.String(64), nullable=False),
        sa.Column("apply_payload_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("plan_json", postgresql.JSONB(), nullable=True),
        sa.Column("issues_json", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_by", sa.String(64), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["sys_user.id"]),
        sa.CheckConstraint(
            "status IN ('valid', 'error', 'applied', 'expired')",
            name="ck_maintenance_collection_plan_import_batch_status",
        ),
        sa.CheckConstraint(
            "file_size > 0",
            name="ck_maintenance_collection_plan_import_batch_file_size",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_collection_plan_import_batch_version"),
        # 应用证据（P1-5）：applied 必须四证据齐备，非 applied 一律不得携带证据。
        sa.CheckConstraint(
            "(status = 'applied' AND apply_payload_hash IS NOT NULL AND result_json IS NOT NULL "
            "AND applied_by IS NOT NULL AND applied_at IS NOT NULL) OR "
            "(status <> 'applied' AND apply_payload_hash IS NULL AND result_json IS NULL "
            "AND applied_by IS NULL AND applied_at IS NULL)",
            name="ck_maintenance_collection_plan_import_batch_applied_evidence",
        ),
        sa.UniqueConstraint(
            "owner_user_id", "operation_key",
            name="uq_maintenance_collection_plan_import_batch_owner_operation",
        ),
        sa.UniqueConstraint(
            "storage_key",
            name="uq_maintenance_collection_plan_import_batch_storage_key",
        ),
    )

    # 外部订单绑定：人工确认的稳定关系（设计 §5）。source/status 固定值由 CHECK 强制。
    op.create_table(
        "maintenance_collection_plan_source_binding",
        sa.Column("binding_id", sa.String(36), primary_key=True),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("external_order_no", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("project_contract_id", sa.String(36), nullable=False),
        sa.Column("binding_status", sa.String(16), nullable=False),
        sa.Column("reviewed_by", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["maintenance_project.project_id"]),
        sa.ForeignKeyConstraint(
            ["project_contract_id"], ["maintenance_project_contract.project_contract_id"]
        ),
        sa.ForeignKeyConstraint(["reviewed_by"], ["sys_user.id"]),
        sa.CheckConstraint(
            "source_system = 'project_manager_xls_v1'",
            name="ck_maintenance_collection_plan_source_binding_source_system",
        ),
        sa.CheckConstraint(
            "binding_status = 'reviewed'",
            name="ck_maintenance_collection_plan_source_binding_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_maintenance_collection_plan_source_binding_version"),
        sa.UniqueConstraint(
            "source_system", "external_order_no",
            name="uq_maintenance_collection_plan_source_binding_pair",
        ),
    )

    # 不可变操作账本（设计 §4.2）：action/reason 由 CHECK 强制，UPDATE/DELETE 由 trigger 拒绝。
    op.create_table(
        "maintenance_collection_milestone_operation",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("milestone_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("before_payload", postgresql.JSONB(), nullable=False),
        sa.Column("after_payload", postgresql.JSONB(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["milestone_id"], ["maintenance_collection_milestone.milestone_id"]
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["sys_user.id"]),
        sa.CheckConstraint(
            "action IN ('handle', 'reschedule', 'reopen')",
            name="ck_maintenance_collection_milestone_operation_action",
        ),
        sa.CheckConstraint(
            "expected_version >= 1 AND result_version >= 1",
            name="ck_maintenance_collection_milestone_operation_versions",
        ),
        sa.CheckConstraint(
            "(action IN ('reschedule', 'reopen') AND reason IS NOT NULL "
            "AND char_length(btrim(reason)) > 0) OR (action = 'handle')",
            name="ck_maintenance_collection_milestone_operation_reason",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_maintenance_collection_milestone_operation_payload_hash",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_maintenance_collection_milestone_operation_idempotency",
        ),
    )
    # 操作账本按节点+时间索引（P1-5）：按 milestone 回放操作历史、幂等重放查询。
    op.create_index(
        "ix_maintenance_collection_milestone_operation_milestone_created",
        "maintenance_collection_milestone_operation",
        ["milestone_id", "created_at"],
    )

    # ---- 2. milestone 加法扩展（设计 §4.1）：server-default-first 回填，再收紧 ----
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("date_precision", sa.String(8), nullable=False, server_default="day"),
    )
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("follow_up_status", sa.String(16), nullable=False, server_default="pending"),
    )
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column(
            "follow_up_review_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("follow_up_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("followed_up_by", sa.Integer(), sa.ForeignKey("sys_user.id"), nullable=True),
    )
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column("followed_up_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "maintenance_collection_milestone",
        sa.Column(
            "collection_plan_import_batch_id",
            sa.String(64),
            sa.ForeignKey("maintenance_collection_plan_import_batch.batch_id"),
            nullable=True,
        ),
    )
    # 回填完成：移除临时 server default，列默认值交由 ORM 的 Python 侧 default 提供。
    for column in ("date_precision", "follow_up_status", "follow_up_review_required"):
        op.alter_column(
            "maintenance_collection_milestone", column, server_default=None
        )

    # 约束收紧：原有二分 source 约束替换为三分支互斥规则。
    op.drop_constraint(
        "ck_maintenance_collection_milestone_source",
        "maintenance_collection_milestone",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_source",
        "maintenance_collection_milestone",
        "source IN ('direct_api', 'manager_workbook_v3', 'project_manager_xls_v1')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL "
        "AND collection_plan_import_batch_id IS NULL) OR "
        "(source = 'project_manager_xls_v1' AND collection_plan_import_batch_id IS NOT NULL "
        "AND source_batch_id IS NULL) OR "
        "(source = 'direct_api' AND source_batch_id IS NULL "
        "AND collection_plan_import_batch_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_date_precision",
        "maintenance_collection_milestone",
        "date_precision IN ('day', 'month')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_follow_up_status",
        "maintenance_collection_milestone",
        "follow_up_status IN ('pending', 'handled')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_follow_up_state",
        "maintenance_collection_milestone",
        "(follow_up_status = 'handled' AND followed_up_by IS NOT NULL "
        "AND followed_up_at IS NOT NULL) OR "
        "(follow_up_status = 'pending' AND followed_up_by IS NULL "
        "AND followed_up_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_follow_up_review_required",
        "maintenance_collection_milestone",
        "follow_up_review_required = false OR follow_up_status = 'handled'",
    )

    # 提醒目录/详情查询索引。
    op.create_index(
        "ix_maintenance_collection_milestone_follow_up_status",
        "maintenance_collection_milestone",
        ["project_id", "follow_up_status", "planned_date", "sequence"],
    )

    # ---- 3. 操作账本 append-only trigger（复用 e6a9c3f1b2d4 模式）----
    op.execute(
        """
        CREATE FUNCTION reject_maintenance_collection_milestone_operation_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'maintenance_collection_milestone_operation is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_collection_milestone_operation_append_only
        BEFORE UPDATE OR DELETE ON maintenance_collection_milestone_operation
        FOR EACH ROW
        EXECUTE FUNCTION reject_maintenance_collection_milestone_operation_mutation()
        """
    )

    # ---- 4. 权限失败关闭回填：两个新 action 在所有模板与存量账号中强制 false ----
    # （权限中心 v2 语义：新键由各自迁移显式补入；模板权限 || 覆盖旧值，杜绝存量 true。）
    for key in (_FOLLOW_UP_KEY, _PLAN_IMPORT_KEY):
        op.execute(
            sa.text(
                """
                UPDATE sys_role_template
                SET permissions = CASE
                      WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                      ELSE '{}'::jsonb
                    END || jsonb_build_object(:key, false)
                """
            ).bindparams(key=key)
        )
    op.execute(
        sa.text(
            """
            UPDATE sys_user
            SET template_perms = CASE
                  WHEN jsonb_typeof(template_perms) = 'object' THEN template_perms
                  ELSE '{}'::jsonb
                END || jsonb_build_object(:follow_up, false, :plan_import, false),
                perm_overrides = CASE
                  WHEN jsonb_typeof(perm_overrides) = 'object' THEN perm_overrides
                  ELSE '{}'::jsonb
                END - :follow_up - :plan_import
            """
        ).bindparams(follow_up=_FOLLOW_UP_KEY, plan_import=_PLAN_IMPORT_KEY)
    )
    op.execute(
        sa.text(
            """
            UPDATE sys_user
            SET permissions = CASE
                  WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                  ELSE '{}'::jsonb
                END || jsonb_build_object(:follow_up, false, :plan_import, false)
            WHERE permissions IS NOT NULL
            """
        ).bindparams(follow_up=_FOLLOW_UP_KEY, plan_import=_PLAN_IMPORT_KEY)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        LOCK TABLE
            maintenance_collection_milestone,
            maintenance_collection_milestone_operation,
            maintenance_collection_plan_source_binding,
            maintenance_collection_plan_import_batch
        IN ACCESS EXCLUSIVE MODE
        """
    )
    bind = op.get_bind()
    # 0. 失败关闭（P1-6）：任何本 revision 产生的业务状态存在时一律拒绝降级，
    #    不做静默清理；RuntimeError 触发外层单事务回滚，DB 保持在 c8e2a4f6b1d3
    #    且全部行原样保留（发布回滚优先 image/flag 回退，不在生产自动 downgrade）。
    if bind.scalar(
        sa.text(
            "SELECT 1 FROM maintenance_collection_plan_import_batch LIMIT 1"
        )
    ) is not None:
        raise RuntimeError("存在回款计划导入批次，禁止降级：请先清理新功能数据")
    if bind.scalar(
        sa.text(
            "SELECT 1 FROM maintenance_collection_plan_source_binding LIMIT 1"
        )
    ) is not None:
        raise RuntimeError("存在外部订单绑定，禁止降级：请先清理新功能数据")
    if bind.scalar(
        sa.text(
            "SELECT 1 FROM maintenance_collection_milestone_operation LIMIT 1"
        )
    ) is not None:
        raise RuntimeError("存在回款提醒操作账本，禁止降级：请先清理新功能数据")
    if bind.scalar(
        sa.text(
            "SELECT 1 FROM maintenance_collection_milestone "
            "WHERE source = 'project_manager_xls_v1' "
            "OR follow_up_status = 'handled' "
            "OR follow_up_review_required = true "
            "OR follow_up_note IS NOT NULL "
            "OR followed_up_by IS NOT NULL "
            "OR followed_up_at IS NOT NULL "
            "OR collection_plan_import_batch_id IS NOT NULL "
            "OR date_precision = 'month' "
            "LIMIT 1"
        )
    ) is not None:
        raise RuntimeError("存在 XLS 来源或已人工跟进的回款计划节点，禁止降级：请先清理新功能数据")
    # 1. append-only trigger 与函数
    op.execute(
        """
        DROP TRIGGER trg_maintenance_collection_milestone_operation_append_only
        ON maintenance_collection_milestone_operation
        """
    )
    op.execute(
        "DROP FUNCTION reject_maintenance_collection_milestone_operation_mutation()"
    )
    # 2. 本 revision 新增索引（含操作账本 (milestone_id, created_at) 索引）
    op.drop_index(
        "ix_maintenance_collection_milestone_follow_up_status",
        table_name="maintenance_collection_milestone",
    )
    op.drop_index(
        "ix_maintenance_collection_milestone_operation_milestone_created",
        table_name="maintenance_collection_milestone_operation",
    )
    # 3. 收紧后的约束还原：先删新增/替换的 CHECK，再恢复原有二分 source 约束。
    for constraint in (
        "ck_maintenance_collection_milestone_source",
        "ck_maintenance_collection_milestone_batch_source",
        "ck_maintenance_collection_milestone_date_precision",
        "ck_maintenance_collection_milestone_follow_up_status",
        "ck_maintenance_collection_milestone_follow_up_state",
        "ck_maintenance_collection_milestone_follow_up_review_required",
    ):
        op.drop_constraint(
            constraint, "maintenance_collection_milestone", type_="check"
        )
    # 不再静默删除 XLS 来源节点（P1-6）：降级守卫已保证不存在任何新功能状态，
    # 因此此处没有可删除的 XLS 节点；迁移前已存在的 direct_api /
    # manager_workbook_v3 节点原样保留。
    for column in (
        "collection_plan_import_batch_id",
        "date_precision",
        "follow_up_status",
        "follow_up_review_required",
        "follow_up_note",
        "followed_up_by",
        "followed_up_at",
    ):
        op.drop_column("maintenance_collection_milestone", column)
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_source",
        "maintenance_collection_milestone",
        "source IN ('direct_api', 'manager_workbook_v3')",
    )
    op.create_check_constraint(
        "ck_maintenance_collection_milestone_batch_source",
        "maintenance_collection_milestone",
        "(source = 'manager_workbook_v3' AND source_batch_id IS NOT NULL) OR "
        "(source = 'direct_api' AND source_batch_id IS NULL)",
    )
    # 4. 三张新表
    op.drop_table("maintenance_collection_milestone_operation")
    op.drop_table("maintenance_collection_plan_source_binding")
    op.drop_table("maintenance_collection_plan_import_batch")
    # 5. 权限回填反向：移除本 revision 新增的两个 action 键。
    for table, column in (
        ("sys_role_template", "permissions"),
        ("sys_user", "template_perms"),
        ("sys_user", "perm_overrides"),
        ("sys_user", "permissions"),
    ):
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = {column} - :follow_up - :plan_import "
                f"WHERE jsonb_typeof({column}) = 'object'"
            ).bindparams(follow_up=_FOLLOW_UP_KEY, plan_import=_PLAN_IMPORT_KEY)
        )
