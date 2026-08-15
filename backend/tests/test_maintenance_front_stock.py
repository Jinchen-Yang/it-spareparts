"""维保前置库账本服务测试（B1）。"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance_front_stock import (
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)
from app.models.maintenance_project import MaintenanceProject
from app.services import maintenance_front_stock as front_stock


@pytest.fixture()
def parts(db):
    part_a = DimPart(pn_std="FS-A-001", description="测试备件A")
    part_b = DimPart(pn_std="FS-B-001", description="测试备件B")
    db.add_all([part_a, part_b])
    db.flush()
    return {"a": part_a.id, "b": part_b.id}


@pytest.fixture()
def project(db):
    p = MaintenanceProject(
        project_id="fs-project-1",
        project_code="前置库测试项目",
        display_name="前置库测试项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(p)
    db.flush()
    return p.project_id


def _move(db, *, part_id, kind, source_ref, qty, warehouse="", **kw):
    return front_stock.apply_movement(
        db,
        project_id="fs-project-1",
        part_id=part_id,
        kind=kind,
        source_type=kw.pop("source_type", "f_maintenance_line"),
        source_ref=source_ref,
        qty=Decimal(qty),
        warehouse_name=warehouse,
        operated_by=kw.pop("operated_by", "合成测试员"),
        **kw,
    )


def test_shipment_in_creates_stock_and_ledger(db, parts, project):
    ledger = _move(db, part_id=parts["a"], kind="shipment_in", source_ref="WBDD-LINE-1", qty="3")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0
    assert stock.last_inbound_at is not None
    assert float(ledger.qty_after) == 3.0
    assert float(ledger.qty_change) == 3.0


def test_movement_idempotent_by_source(db, parts, project):
    first = _move(db, part_id=parts["a"], kind="shipment_in", source_ref="WBDD-LINE-1", qty="3")
    second = _move(db, part_id=parts["a"], kind="shipment_in", source_ref="WBDD-LINE-1", qty="3")
    assert first.ledger_id == second.ledger_id
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_warehouse_name_splits_identity(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="2", warehouse="现场小库甲")
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-2", qty="5", warehouse="现场小库乙")
    db.commit()
    stocks = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalars().all()
    assert len(stocks) == 2
    by_wh = {s.warehouse_name: float(s.qty) for s in stocks}
    assert by_wh == {"现场小库甲": 2.0, "现场小库乙": 5.0}


def test_return_out_reduces_and_preserves_inbound_age(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="5")
    _move(db, part_id=parts["a"], kind="return_out", source_ref="RET-1", qty="2")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0
    assert stock.last_inbound_at is not None  # 出账不清库龄锚点


def test_salvage_out_reduces(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="4")
    _move(db, part_id=parts["a"], kind="salvage_out", source_ref="SV-1", qty="1")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_negative_balance_rejected(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="2")
    db.commit()
    with pytest.raises(front_stock.FrontStockNegativeBalance):
        _move(db, part_id=parts["a"], kind="return_out", source_ref="RET-9", qty="5")
    db.rollback()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 2.0


def test_balance_rows_with_age(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3",
          unit_cost_ex_tax=Decimal("100.00"), unit_cost_inc_tax=Decimal("113.00"))
    _move(db, part_id=parts["b"], kind="shipment_in", source_ref="L-2", qty="1")
    db.commit()
    rows = front_stock.balance_rows(db, "fs-project-1")
    assert len(rows) == 2
    row_a = next(r for r in rows if r["pn"] == "FS-A-001")
    assert row_a["qty"] == 3.0
    assert row_a["value_ex_tax"] == 300.0
    assert row_a["age_days"] == 0  # 刚入账
    assert row_a["unit_cost_inc_tax"] == 113.0
    row_b = next(r for r in rows if r["pn"] == "FS-B-001")
    assert row_b["value_ex_tax"] is None


def test_ledger_entries_ordered(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3")
    _move(db, part_id=parts["a"], kind="return_out", source_ref="RET-1", qty="1")
    db.commit()
    entries = front_stock.ledger_entries(db, "fs-project-1")
    assert len(entries) == 2
    assert entries[0]["kind"] == "return_out"  # 倒序：最新在前
    assert entries[1]["qty_after"] == 3.0


def test_invalid_kind_rejected(db, parts, project):
    with pytest.raises(front_stock.FrontStockInvalidMovement):
        _move(db, part_id=parts["a"], kind="use_out", source_ref="X-1", qty="1")


def test_zero_or_negative_qty_rejected(db, parts, project):
    with pytest.raises(front_stock.FrontStockInvalidMovement):
        _move(db, part_id=parts["a"], kind="shipment_in", source_ref="X-1", qty="0")
    with pytest.raises(front_stock.FrontStockInvalidMovement):
        _move(db, part_id=parts["a"], kind="shipment_in", source_ref="X-2", qty="-3")


def test_same_source_ref_different_payload_rejected(db, parts, project):
    """同一来源事件以不同内容（PN/数量）重放 → payload 冲突失败关闭。"""
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="ORDER-LINE-9", qty="2")
    db.commit()
    with pytest.raises(front_stock.FrontStockPayloadConflict):
        _move(db, part_id=parts["b"], kind="shipment_in", source_ref="ORDER-LINE-9", qty="7")
    db.rollback()
    rows = front_stock.balance_rows(db, "fs-project-1")
    assert {r["pn"]: r["qty"] for r in rows} == {"FS-A-001": 2.0}


def test_version_bumps_on_each_movement(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3")
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-2", qty="2")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert stock.version == 3  # 创建 1 + 两笔入账
    count = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.project_id == "fs-project-1"
        )
    ).scalars().all()
    assert len(count) == 2
