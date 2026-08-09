"""strict operating status pairs without rewriting legacy anomalies

Revision ID: c4e8a1d7f2b6
Revises: b7d2f4a6c8e1
"""

from collections.abc import Sequence

from alembic import op


revision: str = "c4e8a1d7f2b6"
down_revision: str | None = "b7d2f4a6c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(
        "ck_maintenance_site_issue_unmapped_unknown",
        "maintenance_site_issue",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_project_expense_unmapped_unknown",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    # NOT VALID deliberately preserves any historical mapped+unknown rows,
    # while PostgreSQL still enforces the stricter pair on every new write.
    op.execute(
        """
        ALTER TABLE maintenance_site_issue
        ADD CONSTRAINT ck_maintenance_site_issue_unmapped_unknown
        CHECK (
            (status_mapping_state = 'mapped')
            = (normalized_status <> 'unknown')
        ) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE maintenance_project_expense_attribution
        ADD CONSTRAINT ck_maintenance_project_expense_unmapped_unknown
        CHECK (
            (status_mapping_state = 'mapped')
            = (normalized_status <> 'unknown')
        ) NOT VALID
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_maintenance_project_expense_unmapped_unknown",
        "maintenance_project_expense_attribution",
        type_="check",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_unmapped_unknown",
        "maintenance_site_issue",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_unmapped_unknown",
        "maintenance_site_issue",
        "status_mapping_state = 'mapped' OR normalized_status = 'unknown'",
    )
    op.create_check_constraint(
        "ck_maintenance_project_expense_unmapped_unknown",
        "maintenance_project_expense_attribution",
        "status_mapping_state = 'mapped' OR normalized_status = 'unknown'",
    )
