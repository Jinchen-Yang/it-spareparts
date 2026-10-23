"""Seed explicit sales-analysis grants without guessing customer assignments.

Frozen projection of legacy effective_for_user/runtime_safe at 7a9fd20. It must
not import mutable application permission templates. Inactive, non-deleted
accounts retain their explicit entitlement but authentication still rejects them.
Downgrade removes new policy data; it never copies it back into legacy snapshots.

Revision ID: f1a3c5e7b9d2
Revises: d8f6a2c4b9e7
"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a3c5e7b9d2"
down_revision = "d8f6a2c4b9e7"
branch_labels = None
depends_on = None

_FIELDS = (
    "data_supplier", "data_customer", "data_purchase_cost", "data_profit",
    "data_pool_price_governance", "data_reconciliation",
)
_KEYS = (*_FIELDS, "page_sales_analysis", "own_customers_only")
_DEFAULTS = {
    "admin": (True, True, True, True, True, True, True, False),
    "boss": (True, True, True, True, True, True, True, False),
    "readonly": (True, True, True, True, True, False, False, False),
    "sales": (False, True, True, True, True, False, False, True),
    "purchaser": (True, False, True, False, True, False, False, False),
    "maintenance_manager": (False,) * 8,
}


def _legacy_permissions(user) -> dict[str, bool]:
    role = user["role"]
    if role == "admin":
        result = dict(zip(_KEYS, _DEFAULTS["admin"]))
    elif user["template_perms"] is not None:
        result = {key: bool(user["template_perms"].get(key, False)) for key in _KEYS}
        result.update({key: bool(value) for key, value in (user["perm_overrides"] or {}).items() if key in _KEYS})
    else:
        result = dict(zip(_KEYS, _DEFAULTS.get(role, _DEFAULTS["readonly"])))
        result.update({key: bool(value) for key, value in (user["permissions"] or {}).items() if key in _KEYS})
    if not result["data_purchase_cost"]:
        result["data_profit"] = False
    return result


def upgrade() -> None:
    op.create_table(
        "sys_authorization_grant",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource", sa.String(64), nullable=False),
        sa.Column("actions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("scope", sa.String(32), nullable=False, server_default="assigned_customers"),
        sa.Column("field_permissions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("user_id", "resource", name="uq_authorization_grant_user_resource"),
        sa.CheckConstraint("version >= 1", name="ck_authorization_grant_version"),
        sa.CheckConstraint("scope IN ('all', 'assigned_customers')", name="ck_authorization_grant_scope"),
        sa.CheckConstraint("jsonb_typeof(actions) = 'array'", name="ck_authorization_grant_actions_array"),
        sa.CheckConstraint("jsonb_typeof(field_permissions) = 'array'", name="ck_authorization_grant_fields_array"),
    )
    op.create_table(
        "sys_customer_assignment",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("sys_user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("dim_customer.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_customer_assignment_customer", "sys_customer_assignment", ["customer_id"])
    bind = op.get_bind()
    users = bind.execute(sa.text(
        "SELECT id, role, template_perms, perm_overrides, permissions "
        "FROM sys_user WHERE deleted_at IS NULL ORDER BY id"
    )).mappings()
    for user in users:
        effective = _legacy_permissions(user)
        if not effective["page_sales_analysis"]:
            continue
        # Reconciliation snapshots include original order amounts. D-0026
        # narrows this inherited combination; never grant costs implicitly.
        if not effective["data_purchase_cost"]:
            effective["data_reconciliation"] = False
        scope = ("all" if user["role"] in {"admin", "boss"} and not effective["own_customers_only"]
                 else "assigned_customers")
        bind.execute(sa.text(
            "INSERT INTO sys_authorization_grant (user_id, resource, actions, scope, field_permissions, version) "
            "VALUES (:user_id, 'sales_analysis', '[\"read\", \"export\"]'::jsonb, :scope, CAST(:fields AS jsonb), 1)"
        ), {"user_id": user["id"], "scope": scope,
            "fields": json.dumps([key for key in _FIELDS if effective[key]])})


def downgrade() -> None:
    op.drop_table("sys_customer_assignment")
    op.drop_table("sys_authorization_grant")
