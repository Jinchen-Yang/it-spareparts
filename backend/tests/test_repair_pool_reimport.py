"""重新导入不得把返还行改到已扣件数以下。"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine as Receipt
from app.services import maintenance_return_receipt_import as imports
from tests.test_return_receipt_import import apply, preview, workbook
from tests.test_site_issue_v2_api import _project


def _project_named(db):
    project = _project(db, project_id=str(uuid4()))
    project.project_code = project.display_name = "TEST-PROJECT"
    part = DimPart(pn_std="MEMORY", description="内存")
    db.add(part)
    db.commit()
    return project, part


def test_reimport_below_deducted_qty_is_rejected(db):
    _project_named(db)
    first = preview(db, workbook(qty="5", condition="坏品"))
    assert apply(db, first)["applied_lines"] == 1
    row = db.scalar(select(Receipt))
    assert row.part_id is not None
    from tests.boss_board_helpers import client_for

    repairer = client_for(
        db, username="reimport-repairer", role="readonly",
        overrides={"page_cycle_repair": True},
    )
    created = repairer.post(
        "/api/cycle/repair-records",
        json={"part_id": row.part_id, "fixed_qty": 3, "scrap_qty": 0},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert created.status_code == 201, created.text
    second = preview(db, workbook(qty="2", condition="坏品"))
    with pytest.raises(imports.ImportError) as raised:
        apply(db, second, confirm_changes=True, reason="想改小")
    assert raised.value.code == "repair_deduction_exceeds_receipt"
    assert raised.value.status == 422
    assert "已有维修扣数" in str(raised.value)
    db.rollback()
    db.expire_all()
    stored = db.scalar(select(Receipt))
    assert stored.qty == Decimal("5.000")
    batch = db.get(type(first), second.batch_id)
    assert batch.status != "applied"


def test_reimport_without_deduction_still_applies(db):
    _project_named(db)
    first = preview(db, workbook(qty="5", condition="坏品"))
    assert apply(db, first)["applied_lines"] == 1
    second = preview(db, workbook(qty="4", condition="坏品"))
    assert apply(db, second, confirm_changes=True, reason="无扣数更正")["status"] == "applied"
    db.expire_all()
    stored = db.scalar(select(Receipt))
    assert stored.qty == Decimal("4.000")
