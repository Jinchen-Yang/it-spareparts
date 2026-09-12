"""Preserve legacy receipt scope and verify populated-schema migration paths."""
from decimal import Decimal
from io import StringIO

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.models.maintenance_doc_import import MaintenanceDocLineRow, MaintenanceRkdReturnLine
from app.services.maintenance_recovery import recovery_summary
from app.services.maintenance_return_receipts import receipt_summary
from tests.conftest import _alembic_cfg
from tests.test_maintenance_recovery import _seed_recovery
from tests.test_return_receipt_import import apply, preview, workbook, project


def _version():
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


def test_alembic_check_and_single_head_match_orm(migrated):
    # Run alone for release verification: the session fixture creates a fresh
    # independent database and runs upgrade head before this check.
    cfg = _alembic_cfg()
    cfg.stdout = StringIO()
    command.check(cfg)
    command.heads(cfg)
    output = cfg.stdout.getvalue()
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1 and _version() == heads[0]
    assert "No new upgrade operations detected." in output
    assert output.count("(head)") == 1
    assert f"{heads[0]} (head)" in output
    print(output, end="")


@pytest.mark.parametrize("category", ["维保拆旧返件", "旧库退返"])
def test_both_historical_return_categories_remain_in_legacy_recovery(db, project, category):
    batch = preview(db, workbook(category=category, condition="坏品"))
    apply(db, batch)
    row = db.scalar(select(MaintenanceRkdReturnLine))
    assert row.source_payload["category"] == category
    assert recovery_summary(db, project.project_id)["bad_returned_total_qty"] == 2
    assert receipt_summary(db, project_id=project.project_id)["project_total_qty"] == "2.000"


def test_new_machine_parent_counts_once_and_components_never_enter_legacy_recovery(db, project):
    batch = preview(db, workbook(machine=True, condition="坏品", extra=[{
        "备件明细.备件PN": "DISK", "备件明细.入库数量": "6",
        "备件明细.测试结果": "坏品", "备件明细.序号": "2",
        "备件明细.数据ID(不可修改)": "SECOND-COMPONENT",
    }]))
    apply(db, batch)
    assert len(db.scalars(select(MaintenanceDocLineRow)).all()) == 2
    ledger = db.scalars(select(MaintenanceRkdReturnLine)).all()
    assert len(ledger) == 1 and ledger[0].receipt_kind == "machine"
    summary = recovery_summary(db, project.project_id)
    assert summary["bad_returned_total_qty"] == 1
    assert [row["pn"] for row in summary["bad_returned"]] == ["SERVER"]
    assert receipt_summary(db, project_id=project.project_id)["project_total_qty"] == "1.000"


@pytest.mark.parametrize("category", ["采购入库", "销售退货", "其他入库", None])
def test_new_source_evidence_outside_legacy_categories_is_not_reinterpreted(db, category):
    _seed_recovery(db)
    row = db.get(MaintenanceRkdReturnLine, "rc-line-1")
    assert recovery_summary(db, row.project_id)["bad_returned_total_qty"] == 1
    row.source_payload = {"category": category}
    db.commit()
    assert recovery_summary(db, row.project_id)["bad_returned_total_qty"] == 0
    # The compatibility filter does not change the unified ledger's actual facts.
    assert receipt_summary(db, project_id=row.project_id)["project_total_qty"] == "1.000"


def test_populated_legacy_rows_upgrade_and_downgrade_without_quoted_defaults(db):
    _seed_recovery(db)
    db.close()
    cfg = _alembic_cfg()
    try:
        command.downgrade(cfg, "c9e5a1b7d3f8")
        assert _version() == "c9e5a1b7d3f8"
        # This used to fail on the new source/status CHECKs when legacy facts existed.
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            row = conn.execute(text("SELECT source, line_status, created_by, qty, receipt_kind "
                                    "FROM maintenance_rkd_return_line WHERE rkd_line_id='rc-line-1'")).one()
            assert tuple(row) == ("rkd_import", "active", "rkd_import", Decimal(1), "part")
            # Exercise server defaults, not the ORM Python defaults.
            conn.execute(text("""INSERT INTO maintenance_rkd_return_line
                (rkd_line_id,batch_id,head_row_id,project_id,head_no,source_ref,pn,qty,test_result)
                SELECT 'legacy-raw-insert',batch_id,head_row_id,project_id,head_no,
                       'rkd:legacy-raw-insert',pn,2,test_result
                FROM maintenance_rkd_return_line WHERE rkd_line_id='rc-line-1'"""))
            assert conn.execute(text("SELECT source,line_status,created_by,receipt_kind FROM maintenance_rkd_return_line "
                                     "WHERE rkd_line_id='legacy-raw-insert'")).one() == (
                                         "rkd_import", "active", "rkd_import", "part")
        command.downgrade(cfg, "c9e5a1b7d3f8")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT sum(qty) FROM maintenance_rkd_return_line")) == Decimal(3)
    finally:
        command.upgrade(cfg, "head")


def test_e3_repairs_previously_installed_d2_defaults(db):
    _seed_recovery(db)
    db.close()
    cfg = _alembic_cfg()
    try:
        command.downgrade(cfg, "d2f8b4e6c9a1")
        with engine.begin() as conn:
            for column, value in (("source", "rkd_import"), ("line_status", "active"), ("created_by", "rkd_import")):
                conn.execute(text(f"ALTER TABLE maintenance_rkd_return_line ALTER COLUMN {column} SET DEFAULT '''{value}'''"))
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("""INSERT INTO maintenance_rkd_return_line
                (rkd_line_id,batch_id,head_row_id,project_id,head_no,source_ref,pn,qty,test_result)
                SELECT 'repaired-defaults',batch_id,head_row_id,project_id,head_no,
                       'rkd:repaired-defaults',pn,2,test_result
                FROM maintenance_rkd_return_line WHERE rkd_line_id='rc-line-1'"""))
            assert conn.scalar(text("SELECT created_by FROM maintenance_rkd_return_line WHERE rkd_line_id='repaired-defaults'")) == "rkd_import"
    finally:
        command.upgrade(cfg, "head")


@pytest.mark.parametrize("values", [
    {"source_payload": {"category": "维保拆旧返件"}},
    {"review_required": True},
    {"receipt_kind": "machine"},
])
def test_e3_downgrade_refuses_to_drop_source_evidence_or_review_state(db, values):
    _seed_recovery(db)
    row = db.get(MaintenanceRkdReturnLine, "rc-line-1")
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    db.close()
    original_version = _version()
    with pytest.raises(DBAPIError, match="downgrade refused"):
        command.downgrade(_alembic_cfg(), "d2f8b4e6c9a1")
    assert _version() == original_version
    with engine.connect() as conn:
        current = conn.execute(text("SELECT source_payload,review_required,receipt_kind FROM maintenance_rkd_return_line "
                                    "WHERE rkd_line_id='rc-line-1'")).mappings().one()
        for key, value in values.items():
            assert current[key] == value
