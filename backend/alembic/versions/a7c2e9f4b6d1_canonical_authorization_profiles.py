"""Canonical authorization source profiles and persistent resource cutover.

Revision ID: a7c2e9f4b6d1
Revises: f1a3c5e7b9d2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a7c2e9f4b6d1"
down_revision = "f1a3c5e7b9d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sys_user", sa.Column(
        "authorization_profile", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{\"schema_version\": 1,\"template_snapshot\": null,\"resource_overrides\": {}}'::jsonb"),
    ))
    op.add_column("sys_role_template", sa.Column(
        "authorization_bundle", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"),
    ))
    op.create_table(
        "sys_authorization_cutover",
        sa.Column("resource", sa.String(64), primary_key=True),
        sa.Column("activated_catalog_version", sa.String(128), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Preserve the actual canonical S0 grant, including an explicit empty deny.
    # Legacy boolean/template values are deliberately not interpreted here.
    op.execute("""
        UPDATE sys_user AS u
        SET authorization_profile = jsonb_build_object(
            'schema_version', 1, 'template_snapshot', NULL,
            'resource_overrides', jsonb_build_object('sales_analysis',
                jsonb_build_object('actions', g.actions, 'scope', g.scope,
                                   'field_permissions', g.field_permissions))),
            token_version = u.token_version + 1
        FROM sys_authorization_grant AS g
        WHERE g.user_id = u.id AND g.resource = 'sales_analysis'
    """)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM sys_authorization_cutover)")).scalar():
        raise RuntimeError("Cannot downgrade after authorization cutover: persistent authority must not be removed")
    # Existing Grant rows and monotonically advanced session versions survive.
    op.drop_table("sys_authorization_cutover")
    op.drop_column("sys_role_template", "authorization_bundle")
    op.drop_column("sys_user", "authorization_profile")
