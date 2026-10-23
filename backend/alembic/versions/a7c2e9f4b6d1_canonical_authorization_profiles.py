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
    # Record provenance at this migration boundary, before canonical creation can
    # produce intentionally empty profiles/bundles. Later inserts default false;
    # future resource seeds must not infer origin from empty authorization data.
    for table in ("sys_user", "sys_role_template"):
        op.add_column(table, sa.Column(
            "authorization_legacy_seed_eligible", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))
        op.execute(sa.table(table, sa.column("authorization_legacy_seed_eligible", sa.Boolean()))
                   .update().values(authorization_legacy_seed_eligible=True))
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
    # Guard and DDL must share a protected snapshot: concurrent activation or a
    # canonical insert cannot commit between the checks and dropping provenance.
    op.execute("LOCK TABLE sys_authorization_cutover, sys_role_template, sys_user IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM sys_authorization_cutover)")).scalar():
        raise RuntimeError("Cannot downgrade after authorization cutover: persistent authority must not be removed")
    # A canonical empty account/template is meaningful even before any resource
    # cutover. Dropping its false origin would turn it into legacy true on the
    # next upgrade and let future seeds invent permissions it never received.
    if op.get_bind().execute(sa.text("""
        SELECT EXISTS (SELECT 1 FROM sys_user WHERE NOT authorization_legacy_seed_eligible)
            OR EXISTS (SELECT 1 FROM sys_role_template WHERE NOT authorization_legacy_seed_eligible)
    """)).scalar():
        raise RuntimeError("Cannot downgrade with non-legacy authorization origin: persistent provenance must not be removed")
    # Existing Grant rows and monotonically advanced session versions survive.
    op.drop_table("sys_authorization_cutover")
    op.drop_column("sys_role_template", "authorization_bundle")
    op.drop_column("sys_user", "authorization_profile")
    op.drop_column("sys_role_template", "authorization_legacy_seed_eligible")
    op.drop_column("sys_user", "authorization_legacy_seed_eligible")
