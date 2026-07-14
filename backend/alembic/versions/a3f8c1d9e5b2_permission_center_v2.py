"""permission center v2: sys_role_template + sys_user template snapshot columns

职位模板持久化（替代 Python 硬编码）+ 账号权限改为「模板快照 ⊕ 稀疏覆盖」。

冻结原则：本文件不 import app.permissions——模板权限值是**编写时刻的冻结字面量**
（含新键 page_accounts/action_account_manage，对非 admin 全 False，行为与迁移前零变化）。
回填保证：逐账号新口径有效权限 ≡ 旧口径 effective(role, permissions)，对账测试
（tests/test_permission_center.py）直接 import 本模块的 _backfill_one 验证。
旧列 sys_user.permissions 不删不改：downgrade 删新列新表即回旧世界。

Revision ID: a3f8c1d9e5b2
Revises: f2a7d9c3e6b1
Create Date: 2026-07-14 12:00:00.000000

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "a3f8c1d9e5b2"
down_revision: Union[str, Sequence[str], None] = "f2a7d9c3e6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---- 冻结的内置模板（= 迁移编写时刻 permissions.effective(role, None) 的输出） ----
_KEYS = [
    "data_supplier", "data_customer", "data_purchase_cost", "data_profit",
    "data_pool_price_governance",
    "page_parts", "page_purchases", "page_profit", "page_inventory", "page_chat",
    "page_import", "page_governance", "page_master_data", "page_maintenance",
    "page_boss_board", "page_pool_analysis", "page_accounts",
    "action_pool_manage", "action_pool_set_policy", "action_account_manage",
    "own_customers_only",
]


def _tpl(true_keys: set[str]) -> dict[str, bool]:
    return {k: (k in true_keys) for k in _KEYS}


_ALL_BUT = lambda *off: {k for k in _KEYS if k not in off}  # noqa: E731

FROZEN_TEMPLATES: dict[str, dict[str, bool]] = {
    "admin": _tpl(_ALL_BUT("own_customers_only")),
    "boss": _tpl(_ALL_BUT("own_customers_only", "page_accounts", "action_account_manage")),
    "sales": _tpl({
        "data_customer", "data_purchase_cost", "data_profit", "data_pool_price_governance",
        "page_parts", "page_purchases", "page_inventory", "page_chat", "page_pool_analysis",
        "own_customers_only",
    }),
    "purchaser": _tpl({
        "data_supplier", "data_purchase_cost", "data_pool_price_governance",
        "page_parts", "page_purchases", "page_inventory", "page_chat",
        "page_master_data", "page_maintenance", "page_pool_analysis",
    }),
    "readonly": _tpl(_ALL_BUT(
        "own_customers_only", "page_import", "page_governance", "page_master_data",
        "page_maintenance", "page_boss_board",
        "action_pool_manage", "action_pool_set_policy",
        "page_accounts", "action_account_manage",
    )),
}

_TEMPLATE_NAMES = {"admin": "管理员", "boss": "老板", "sales": "销售",
                   "purchaser": "采购", "readonly": "只读"}
_TEMPLATE_DESCS = {
    "admin": "系统管理员：全部权限恒开且不可修改。锁定模板——升管理员走单账号操作。",
    "boss": "老板：全部业务数据与页面可见，不含账号管理。",
    "sales": "销售：看客户/成本/毛利，供应商隐藏，只看自己成交的客户。",
    "purchaser": "采购：看供应商/成本，客户与利润隐藏，可维护主数据与项目成本。",
    "readonly": "只读：可查询业务数据，无导入/治理/管理入口（也是未知角色的兜底口径）。",
}


def _backfill_one(role: str, legacy_permissions: dict | None) -> dict:
    """单账号回填（纯函数，对账测试直接调用）：
    旧有效权限 = 冻结模板打底 + legacy 自定义逐键覆盖（合法键）；
    返回 template_code / template_perms（快照）/ perm_overrides（与快照的稀疏 diff）。
    admin 角色恒全开（effective_for_user 对 admin 短路），照样回填快照保持数据完整。"""
    code = role if role in FROZEN_TEMPLATES else "readonly"
    snapshot = dict(FROZEN_TEMPLATES[code])
    old_effective = dict(snapshot)
    for k, v in (legacy_permissions or {}).items():
        if k in old_effective:
            old_effective[k] = bool(v)
    overrides = {k: old_effective[k] for k in _KEYS if old_effective[k] != snapshot[k]}
    return {"template_code": code, "template_version": 1,
            "template_perms": snapshot, "perm_overrides": overrides}


def upgrade() -> None:
    op.create_table(
        "sys_role_template",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_role", sa.String(length=16), nullable=False),
        sa.Column("permissions", JSONB(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_sys_role_template_code"),
    )

    op.add_column("sys_user", sa.Column("template_code", sa.String(length=64), nullable=True))
    op.add_column("sys_user", sa.Column("template_version", sa.Integer(), nullable=True))
    op.add_column("sys_user", sa.Column("template_perms", JSONB(), nullable=True))
    op.add_column("sys_user", sa.Column("perm_overrides", JSONB(), nullable=True))

    conn = op.get_bind()
    # seed 内置模板
    for code, perms in FROZEN_TEMPLATES.items():
        conn.execute(
            sa.text("""
                INSERT INTO sys_role_template
                    (code, name, description, base_role, permissions, is_system, is_active, version, created_by)
                VALUES (:code, :name, :descr, :base_role, CAST(:perms AS jsonb), true, true, 1, 'migration')
            """),
            {"code": code, "name": _TEMPLATE_NAMES[code], "descr": _TEMPLATE_DESCS[code],
             "base_role": code, "perms": json.dumps(perms)},
        )
    # 回填既有账号：逐账号快照+diff（保证新旧口径有效权限一致）
    rows = conn.execute(sa.text("SELECT id, role, permissions FROM sys_user")).fetchall()
    for uid, role, legacy in rows:
        legacy_dict = legacy if isinstance(legacy, dict) else (json.loads(legacy) if legacy else None)
        filled = _backfill_one(role, legacy_dict)
        conn.execute(
            sa.text("""
                UPDATE sys_user
                SET template_code = :code, template_version = :ver,
                    template_perms = CAST(:snap AS jsonb), perm_overrides = CAST(:ovr AS jsonb)
                WHERE id = :uid
            """),
            {"uid": uid, "code": filled["template_code"], "ver": filled["template_version"],
             "snap": json.dumps(filled["template_perms"]),
             "ovr": json.dumps(filled["perm_overrides"])},
        )


def downgrade() -> None:
    # 旧列 permissions 从未被本迁移改动（且 v2 写路径双写完整有效图进旧列），
    # 删新列新表即可回到旧行为，有效权限零漂移。
    op.drop_column("sys_user", "perm_overrides")
    op.drop_column("sys_user", "template_perms")
    op.drop_column("sys_user", "template_version")
    op.drop_column("sys_user", "template_code")
    op.drop_table("sys_role_template")
