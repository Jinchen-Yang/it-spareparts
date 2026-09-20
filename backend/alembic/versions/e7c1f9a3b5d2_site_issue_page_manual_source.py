"""Site issue: allow source='page_manual' and command action='create'.

The stable workbook's 06 sheet already accepts manual site-issue facts
(source='workbook', import-batch bound). Users also need to register the same
explicit human facts directly from the project panel — without inventing a
warehouse delivery line. 'page_manual' is that honest provenance: page-entered,
no import batch, no delivery source. Its create command stores an idempotency
receipt like every other command, so ck_maintenance_site_issue_command_action
gains 'create'. Downgrades refuse while protected rows still exist (never
erase provenance).

Revision ID: e7c1f9a3b5d2
Revises: d9a4c6e2f8b1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c1f9a3b5d2"
down_revision: str | None = "d9a4c6e2f8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ISSUE_TABLE = "maintenance_site_issue"
_COMMAND_TABLE = "maintenance_site_issue_command"
_SOURCE = "page_manual"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 与 f4b8d2e6a1c3 同款：固定命名 CHECK 直接 drop/create。pg_get_constraintdef
    # 会把表达式正规化（IN → = ANY(ARRAY[...])），子串匹配不可靠。
    op.drop_constraint(
        "ck_maintenance_site_issue_source", _ISSUE_TABLE, type_="check"
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_source",
        _ISSUE_TABLE,
        "source IN ('legacy', 'direct_api', 'workbook', 'site_issue_v2', "
        "'page_manual')",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_import_batch", _ISSUE_TABLE, type_="check"
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_import_batch",
        _ISSUE_TABLE,
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api', 'site_issue_v2', 'page_manual') "
        "AND import_batch_id IS NULL)",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_command_action",
        _COMMAND_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_command_action",
        _COMMAND_TABLE,
        "action IN ('create', 'update', 'confirm', 'void', 'correct')",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    remaining = op.get_bind().execute(
        sa.text(
            f"SELECT (SELECT count(*) FROM {_ISSUE_TABLE} WHERE source = "
            f"'{_SOURCE}'), (SELECT count(*) FROM {_COMMAND_TABLE} WHERE "
            "action = 'create')"
        )
    ).one()
    issue_rows, command_rows = int(remaining[0]), int(remaining[1])
    if issue_rows or command_rows:
        raise RuntimeError(
            f"downgrade refused: {issue_rows} site issues still carry "
            f"source='{_SOURCE}' and {command_rows} command receipts carry "
            "action='create'; page-registered facts must not lose provenance"
        )
    op.drop_constraint(
        "ck_maintenance_site_issue_command_action",
        _COMMAND_TABLE,
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_command_action",
        _COMMAND_TABLE,
        "action IN ('update', 'confirm', 'void', 'correct')",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_import_batch", _ISSUE_TABLE, type_="check"
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_import_batch",
        _ISSUE_TABLE,
        "(source = 'workbook' AND import_batch_id IS NOT NULL) OR "
        "(source IN ('legacy', 'direct_api', 'site_issue_v2') "
        "AND import_batch_id IS NULL)",
    )
    op.drop_constraint(
        "ck_maintenance_site_issue_source", _ISSUE_TABLE, type_="check"
    )
    op.create_check_constraint(
        "ck_maintenance_site_issue_source",
        _ISSUE_TABLE,
        "source IN ('legacy', 'direct_api', 'workbook', 'site_issue_v2')",
    )
