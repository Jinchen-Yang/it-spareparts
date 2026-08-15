"""补库证据与四列导出测试（F2）。"""

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.replenishment import (
    ReplenishmentApplication,
    ReplenishmentApplicationLine,
    ReplenishmentApplicationVersion,
)
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_replenishment_evidence as evidence


@pytest.fixture()
def seeded_application(db):
    part_a = DimPart(pn_std="EV-A-001", description="高频件A")
    part_b = DimPart(pn_std="EV-B-001", description="冷门件B")
    part_alt = DimPart(pn_std="EV-ALT-001", description="替代件")
    db.add_all([part_a, part_b, part_alt])
    db.add(
        SysUser(
            username="ev-owner",
            role="sales",
            display_name="证据测试销售",
            password_hash="x",
        )
    )
    db.flush()
    pool = PartPool(group_id=9001, name="测试池", member_count=2)
    db.add(pool)
    db.flush()
    db.add_all(
        [
            PartPoolMember(group_id=9001, part_id=part_a.id),
            PartPoolMember(group_id=9001, part_id=part_alt.id),
        ]
    )
    import_batch = SysImportBatch(
        filename="w.xlsx", file_type="maintenance", file_hash="h", status="success"
    )
    db.add(import_batch)
    db.flush()
    # 高频件：近一年补库供货累计 60 件
    db.add(
        FMaintenanceOrder(
            raw_order_id="ev-wbdd-1",
            order_no="WBDD-20260701-0001",
            order_date=date(2026, 7, 1),
            demand_type="补库供货",
            business_type="整体维保",
            project_raw="EV项目",
            project_std="EV项目",
            warehouse="北京成品仓",
            data_status="已生效",
            import_batch_id=import_batch.id,
        )
    )
    db.flush()
    db.add(
        FMaintenanceLine(
            raw_line_id="ev-line-1",
            order_id=1,
            line_no=1,
            part_id=part_a.id,
            pn_std="EV-A-001",
            qty=Decimal("60"),
            import_batch_id=import_batch.id,
        )
    )
    application = ReplenishmentApplication(
        application_id="ev-app-1",
        application_no="EV-APP-0001",
        owner_username="ev-owner",
        status="draft",
        latest_version_no=1,
        version=1,
    )
    db.add(application)
    db.flush()
    version = ReplenishmentApplicationVersion(
        version_id="ev-ver-1",
        application_id="ev-app-1",
        version_no=1,
        status="draft",
        created_by="ev-owner",
    )
    db.add(version)
    db.flush()
    line_a = ReplenishmentApplicationLine(
        line_id="ev-line-a",
        request_line_id="ev-req-a",
        version_id="ev-ver-1",
        line_no=1,
        part_id=part_a.id,
        pn_std="EV-A-001",
        quantity=Decimal("5"),
        pool_group_id=9001,
        pool_name="测试池",
        pool_version=1,
        price_window_from=date(2026, 2, 1),
        price_window_to=date(2026, 8, 1),
        price_as_of=date(2026, 8, 1),
        purchase_stats_json={"weighted_avg": 100.0, "total_qty": 60, "order_count": 6},
        sales_stats_json={"weighted_avg": 113.0, "total_qty": 40, "order_count": 4},
        evidence_digest="d1" * 32,
    )
    line_b = ReplenishmentApplicationLine(
        line_id="ev-line-b",
        request_line_id="ev-req-b",
        version_id="ev-ver-1",
        line_no=2,
        part_id=part_b.id,
        pn_std="EV-B-001",
        quantity=Decimal("3"),
        pool_group_id=None,
        pool_name=None,
        pool_version=None,
        price_window_from=date(2026, 2, 1),
        price_window_to=date(2026, 8, 1),
        price_as_of=date(2026, 8, 1),
        purchase_stats_json={"weighted_avg": None, "total_qty": 0, "order_count": 0},
        sales_stats_json={"weighted_avg": None, "total_qty": 0, "order_count": 0},
        evidence_digest="d2" * 32,
    )
    db.add_all([line_a, line_b])
    db.commit()
    return {"application_id": application.application_id, "part_a": part_a.id}


def test_line_evidence_high_frequency_and_pool(db, seeded_application):
    result = evidence.application_evidence(db, "ev-app-1")
    lines = {row["pn_std"]: row for row in result["lines"]}
    row_a = lines["EV-A-001"]
    assert row_a["is_high_frequency"] is True
    assert row_a["recent_supply_qty"] == 60.0
    assert any(alt["pn_std"] == "EV-ALT-001" for alt in row_a["pool_alternatives"])
    row_b = lines["EV-B-001"]
    assert row_b["inactive_365d"] is True  # 无采购/销售记录
    assert row_b["is_high_frequency"] is False


def test_export_purchase_list_four_columns(db, seeded_application):
    data = evidence.export_purchase_list(db, "ev-app-1")
    workbook = load_workbook(io.BytesIO(data))
    sheet = workbook["补库采购清单"]
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:4] == ("PN", "数量", "采购金额(参考)", "销售金额(参考)")
    assert rows[1][0] == "EV-A-001"
    assert rows[1][1] == 5.0
    assert rows[1][2] == 500.0  # 100 × 5
    assert rows[1][3] == 565.0  # 113 × 5
    assert rows[2][2] in (None, "")  # 无价证据留空，不按 0
