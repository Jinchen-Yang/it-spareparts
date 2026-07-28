"""维保成本桶迁移：真实 downgrade/re-upgrade，证明存量事实无损且自动回算。"""

import os
from datetime import date
from decimal import Decimal

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, text

from app.etl import loader
from app.models.maintenance import FMaintenanceLine
from app.models.system import SysImportBatch
from app.services import maintenance_cost_quality
from tests import factories as f

_PREV = "f8c3d1a6b2e4"
_HEAD = "c9d4e7f2a6b1"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def test_nonempty_cost_bucket_downgrade_and_reupgrade_is_lossless(db):
    batch = SysImportBatch(
        filename="cost-bucket-migration.xlsx",
        file_type="maintenance",
        file_hash="cost-bucket-migration",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M1": f.maintenance_head(
                    "M1",
                    on=date(2026, 7, 1),
                    project="成本桶迁移",
                ),
            },
            [f.maintenance_line("M1", "ML-BUCKET-MIG", "PN-MIG", qty="1")],
        ),
        batch.id,
        date(2026, 7, 2),
    )
    line = db.execute(select(FMaintenanceLine)).scalar_one()
    line.cost_source = "trace_avg"
    line.cost_tax_basis = "inc"
    line.cost_amount = Decimal("12.34")
    line.confidence = "low"
    db.commit()
    assert (
        line.cost_bucket
        == maintenance_cost_quality.COST_BUCKET_ESTIMATED_INC_LOW
    )

    engine = db.get_bind()
    raw_line_id = line.raw_line_id
    db.close()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, _PREV)
        with engine.connect() as connection:
            assert connection.execute(text(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'f_maintenance_line'
                  AND column_name = 'cost_bucket'
                """,
            )).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT cost_amount FROM f_maintenance_line "
                    "WHERE raw_line_id = :raw_line_id",
                ),
                {"raw_line_id": raw_line_id},
            ).scalar_one() == Decimal("12.34")

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT cost_bucket FROM f_maintenance_line "
                    "WHERE raw_line_id = :raw_line_id",
                ),
                {"raw_line_id": raw_line_id},
            ).scalar_one() == maintenance_cost_quality.COST_BUCKET_ESTIMATED_INC_LOW
            assert connection.execute(
                text("SELECT version_num FROM alembic_version"),
            ).scalar_one() == _HEAD
    finally:
        alembic_command.upgrade(cfg, "head")
