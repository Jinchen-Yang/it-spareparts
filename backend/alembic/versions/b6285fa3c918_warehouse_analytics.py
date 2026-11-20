"""库存管理分析只读投影、访问权限（D-0066）。"""

from alembic import op
import sqlalchemy as sa

revision = "b6285fa3c918"
down_revision = "c8e62a7d4f31"
branch_labels = None
depends_on = None

_PAGE = "page_inventory_analytics"
_DOCUMENT = "warehouse_analytics_document"
_LINE = "warehouse_analytics_line"
_PERMISSION_MAPS = (
    ("sys_role_template", "permissions", "code"),
    ("sys_user", "template_perms", "role"),
    ("sys_user", "permissions", "role"),
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        _DOCUMENT,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_modified_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        *(sa.Column(c, sa.Text()) for c in (
            "kind", "center", "operator", "tester", "whole_pn", "whole_description", "whole_unit",
        )),
        sa.Column("needs_test", sa.Boolean()),
        sa.Column("tested", sa.Boolean()),
        sa.Column("tested_date", sa.Date()),
        sa.Column("whole_qty", sa.Numeric(20, 6)),
        sa.CheckConstraint("source IN ('inbound','return','direct','outbound')", name="ck_warehouse_analytics_source"),
        sa.UniqueConstraint("source", "source_id", name="uq_wa_document_identity"),
        sa.UniqueConstraint("source", "number", name="uq_wa_document_number"),
    )
    op.create_index("ix_wa_document_status_date", _DOCUMENT, ["status", "business_date", "source"])
    op.create_table(
        _LINE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey(_DOCUMENT + ".id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        *(sa.Column(c, sa.Text()) for c in (
            "pn", "description", "unit", "category", "brand", "warehouse", "bin_name", "test_result",
        )),
        sa.Column("quantity", sa.Numeric(20, 6)),
        sa.UniqueConstraint("document_id", "source_id", name="uq_wa_line_identity"),
    )
    op.create_index("ix_wa_line_source_id", _LINE, ["source_id"])
    op.create_index("ix_wa_line_pn_document", _LINE, ["pn", "document_id"])

    # 保留显式设置和空快照的现有回退语义。访问时还会检查账号角色。
    for table, column, role in _PERMISSION_MAPS:
        op.execute(sa.text(
            f"UPDATE {table} SET {column} = {column} || jsonb_build_object(CAST(:key AS text), {role} IN ('admin','boss')) "
            f"WHERE jsonb_typeof({column}) = 'object' AND NOT ({column} ? :key)"
        ).bindparams(key=_PAGE))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table, column, _ in (*_PERMISSION_MAPS, ("sys_user", "perm_overrides", "role")):
        op.execute(sa.text(
            f"UPDATE {table} SET {column} = {column} - :key WHERE jsonb_typeof({column}) = 'object' AND {column} ? :key"
        ).bindparams(key=_PAGE))
    op.drop_table(_LINE)
    op.drop_table(_DOCUMENT)
