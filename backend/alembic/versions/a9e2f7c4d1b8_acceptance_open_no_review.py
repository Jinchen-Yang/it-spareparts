"""acceptance: open to sales/PM/maintenance-manager, submit takes effect

2026-08-24 客户拍板：
1) 验收开放给销售/项目经理/维保负责人——sales 与 maintenance_manager 模板
   默认带 action_maintenance_acceptance_submit（sales 另需 page_maintenance 与
   own_maintenance_projects_only 才能看到自己的项目）；
2) 提交即生效，取消独立审批——删除"提交人≠审批人"CHECK，存量 submitted+not_reviewed
   的验收单直接转为 approved（approved_by=提交人），历史驳回记录保留。

Revision ID: a9e2f7c4d1b8
Revises: c7d2e9a4b1f6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "a9e2f7c4d1b8"
down_revision: str | None = "c7d2e9a4b1f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1) 提交即生效：先删"禁止自审"约束，再让存量待审批单直接通过。
    op.drop_constraint(
        "ck_maintenance_acceptance_no_self_approval",
        "maintenance_acceptance_deliverable",
        type_="check",
    )
    op.execute(
        """
        UPDATE maintenance_acceptance_deliverable
        SET approval_status = 'approved',
            approved_at = now(),
            approved_by = submitted_by,
            rejection_reason = NULL
        WHERE submission_status = 'submitted'
          AND approval_status = 'not_reviewed'
        """
    )

    # 2) 模板开口子：sales 与 maintenance_manager（项目经理/维保负责人账号角色）。
    #    与 c8f2d4a6b9e1 同款写法：JSONB 合并 + 账号快照同步 + 覆盖层让位
    #    （管理员之后仍可在权限中心按账号重新收紧）。
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'page_maintenance', true,
                'own_maintenance_projects_only', true,
                'action_maintenance_acceptance_submit', true
            )
        WHERE code = 'sales'
        """
    )
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_acceptance_submit', true
            )
        WHERE code = 'maintenance_manager'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'page_maintenance', true,
                'own_maintenance_projects_only', true,
                'action_maintenance_acceptance_submit', true
            ),
            perm_overrides = CASE
                WHEN jsonb_typeof(perm_overrides) = 'object'
                THEN perm_overrides - 'page_maintenance'
                                        - 'own_maintenance_projects_only'
                                        - 'action_maintenance_acceptance_submit'
                ELSE perm_overrides
            END
        WHERE role = 'sales'
          AND jsonb_typeof(template_perms) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET template_perms = template_perms || jsonb_build_object(
                'action_maintenance_acceptance_submit', true
            ),
            perm_overrides = CASE
                WHEN jsonb_typeof(perm_overrides) = 'object'
                THEN perm_overrides - 'action_maintenance_acceptance_submit'
                ELSE perm_overrides
            END
        WHERE role = 'maintenance_manager'
          AND jsonb_typeof(template_perms) = 'object'
        """
    )
    # 老 legacy permissions 图双写（权限中心 v2 之前的老账号读这里）
    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'page_maintenance', true,
                'own_maintenance_projects_only', true,
                'action_maintenance_acceptance_submit', true
            )
        WHERE role = 'sales'
          AND permissions IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE sys_user
        SET permissions = CASE
                WHEN jsonb_typeof(permissions) = 'object' THEN permissions
                ELSE '{}'::jsonb
            END || jsonb_build_object(
                'action_maintenance_acceptance_submit', true
            )
        WHERE role = 'maintenance_manager'
          AND permissions IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 自动通过的存量单退回待审批口径（仅回滚本次由 not_reviewed 转入的行：
    # approved_by = submitted_by 且 rejection_reason IS NULL 的自审行）。
    op.execute(
        """
        UPDATE maintenance_acceptance_deliverable
        SET approval_status = 'not_reviewed',
            approved_at = NULL,
            approved_by = NULL
        WHERE approval_status = 'approved'
          AND approved_by IS NOT NULL
          AND approved_by = submitted_by
          AND rejection_reason IS NULL
        """
    )

    # 模板/账号快照回退为关闭验收提交；覆盖层键同样移除（回到模板默认）。
    # 只回滚 upgrade 真正加过的键：sales 模板三键，maintenance_manager 模板
    # 仅 action_maintenance_acceptance_submit——此前两模板一刀切删三键，
    # 会把 maintenance_manager 模板既有的 page_maintenance 一并剥掉。
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = permissions - 'page_maintenance'
                                       - 'own_maintenance_projects_only'
                                       - 'action_maintenance_acceptance_submit'
        WHERE code = 'sales'
          AND jsonb_typeof(permissions) = 'object'
        """
    )
    op.execute(
        """
        UPDATE sys_role_template
        SET permissions = permissions - 'action_maintenance_acceptance_submit'
        WHERE code = 'maintenance_manager'
          AND jsonb_typeof(permissions) = 'object'
        """
    )
    for role_filter, keys in (
        ("role = 'sales'", ("- 'page_maintenance'", "- 'own_maintenance_projects_only'", "- 'action_maintenance_acceptance_submit'")),
        ("role = 'maintenance_manager'", ("- 'action_maintenance_acceptance_submit'",)),
    ):
        strip = " ".join(keys)
        op.execute(
            f"""
            UPDATE sys_user
            SET template_perms = template_perms {strip},
                permissions = CASE
                    WHEN jsonb_typeof(permissions) = 'object'
                    THEN permissions {strip}
                    ELSE permissions
                END,
                perm_overrides = CASE
                    WHEN jsonb_typeof(perm_overrides) = 'object'
                    THEN perm_overrides {strip}
                    ELSE perm_overrides
                END
            WHERE {role_filter}
            """
        )

    # 恢复"禁止自审"约束：若仍存在自审行则显式失败，提示先人工处理。
    op.execute(
        """
        DO $migration$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM maintenance_acceptance_deliverable
              WHERE submitted_by IS NOT NULL
                AND approved_by IS NOT NULL
                AND submitted_by = approved_by
          )
          THEN
            RAISE EXCEPTION
              'a9e2f7c4d1b8 downgrade blocked: self-approved acceptance rows exist';
          END IF;
        END
        $migration$;
        """
    )
    op.create_check_constraint(
        "ck_maintenance_acceptance_no_self_approval",
        "maintenance_acceptance_deliverable",
        "submitted_by IS NULL OR approved_by IS NULL OR submitted_by <> approved_by",
    )
