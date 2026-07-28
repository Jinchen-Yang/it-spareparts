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
_HEAD = "e5f9a2b3c4d5"


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


def test_dual_cost_reference_migration_round_trip_preserves_legacy_facts(db):
    """e5 降到 d4 再升级：旧成本事实无损，新生成桶按来源集合自动重算。"""
    batch = SysImportBatch(
        filename="dual-cost-migration.xlsx",
        file_type="maintenance",
        file_hash="dual-cost-migration",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {"M1": f.maintenance_head("M1", on=date(2026, 7, 1))},
            [f.maintenance_line("M1", "ML-DUAL-MIG", "PN-DUAL-MIG", qty="1")],
        ),
        batch.id,
        date(2026, 7, 2),
    )
    line = db.execute(select(FMaintenanceLine)).scalar_one()
    line.cost_source = "pool_purchase"
    line.cost_tax_basis = "ex"
    line.unit_cost = Decimal("12.34")
    line.cost_amount = Decimal("12.34")
    line.unit_cost_inc_tax = Decimal("13.94")
    line.unit_cost_ex_tax = Decimal("12.34")
    line.cost_amount_inc_tax = Decimal("13.94")
    line.cost_amount_ex_tax = Decimal("12.34")
    line.confidence = "low"
    line.reference_side = "purchase"
    line.reference_pool_group_id = 77
    line.reference_pool_version = 3
    line.reference_sample_count = 2
    line.reference_from_date = date(2025, 5, 1)
    line.reference_to_date = date(2025, 5, 2)
    line.reference_latest_date = date(2025, 5, 2)
    db.commit()
    assert (
        line.cost_bucket
        == maintenance_cost_quality.COST_BUCKET_ESTIMATED_EX_LOW
    )

    engine = db.get_bind()
    raw_line_id = line.raw_line_id
    db.close()
    cfg = _cfg()
    try:
        alembic_command.downgrade(cfg, "d4e8f1a2b3c4")
        with engine.connect() as connection:
            legacy = connection.execute(
                text(
                    "SELECT unit_cost, cost_amount, cost_source, cost_tax_basis, cost_bucket "
                    "FROM f_maintenance_line WHERE raw_line_id = :raw_line_id"
                ),
                {"raw_line_id": raw_line_id},
            ).one()
            assert legacy[:4] == (
                Decimal("12.34"),
                Decimal("12.34"),
                "pool_purchase",
                "ex",
            )
            # d4 的旧生成表达式不认识新来源，按 fail-closed 归 missing。
            assert legacy.cost_bucket == maintenance_cost_quality.COST_BUCKET_MISSING
            assert connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema=current_schema() "
                    "AND table_name='f_maintenance_line' "
                    "AND column_name='unit_cost_inc_tax'"
                )
            ).scalar_one() == 0

        alembic_command.upgrade(cfg, "head")
        with engine.connect() as connection:
            upgraded = connection.execute(
                text(
                    "SELECT unit_cost, cost_amount, cost_bucket, unit_cost_inc_tax "
                    "FROM f_maintenance_line WHERE raw_line_id = :raw_line_id"
                ),
                {"raw_line_id": raw_line_id},
            ).one()
            assert upgraded.unit_cost == Decimal("12.34")
            assert upgraded.cost_amount == Decimal("12.34")
            assert (
                upgraded.cost_bucket
                == maintenance_cost_quality.COST_BUCKET_ESTIMATED_EX_LOW
            )
            # 降级会丢新增列，重新升级按 nullable 契约恢复为空，下一次 recompute 回填。
            assert upgraded.unit_cost_inc_tax is None
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == _HEAD
    finally:
        alembic_command.upgrade(cfg, "head")
