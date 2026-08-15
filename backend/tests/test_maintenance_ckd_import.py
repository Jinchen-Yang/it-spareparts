"""氚云发货单导入与前置库入账测试（C1a/F1）。"""

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.maintenance_front_stock import (
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch
from app.services import maintenance_ckd_import as ckd
from app.services import maintenance_front_stock as front_stock


def _ckd_workbook_bytes(*, rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["F0000001", "F0000032", "F0000061"])  # 字段码行（占位）
    ws.append(
        ["出库单号", "出库日期", "出库类别", "出库备件/整机", "出库仓库", "仓储中心",
         "维保需求单(备件)", "维保需求单", "销售订单(备件)", "销售订单", "销售人员",
         "项目经理", "维保需求人", "备注", "数据状态",
         "备件明细.数据ID(不可修改)", "备件明细.序号", "备件明细.数据标题", "备件明细.产品名称", "备件明细.备件自贴码",
         "备件明细.备件PN", "备件明细.备件SN号", "备件明细.备件描述", "备件明细.所在仓库",
         "备件明细.所在库位", "备件明细.产品大类", "备件明细.产品小类", "备件明细.品牌",
         "备件明细.单位", "备件明细.出库数量", "备件明细.成本单价", "备件明细.成本金额",
         "备件明细.备件测试合格"]
    )
    for row in rows:
        head = row["head"]
        ws.append(head)
        for line in row["lines"]:
            ws.append([""] * 15 + line)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


_HEAD_MAINT = [
    "CKD-20260806-0014", "2026-08-06", "维保供货", "备件", "北京成品仓", "北京仓",
    "WBDD-20260702-0014", "", "", "XSDD-20250731-0035", "尤玉玲",
    "李冰冰", "张工", "备注A", "已生效",
]
_HEAD_SALES = [
    "CKD-20260806-0099", "2026-08-06", "销售出库", "备件", "北京成品仓", "北京仓",
    "", "", "", "XSDD-20260806-0011", "尤玉玲",
    "", "", "销售发货", "已生效",
]


@pytest.fixture()
def wbdd_project(db):
    project = MaintenanceProject(
        project_id="ckd-project-1",
        project_code="CKD测试项目",
        display_name="CKD测试项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    import_batch = SysImportBatch(
        filename="w.xlsx", file_type="maintenance", file_hash="h1", status="success"
    )
    db.add(import_batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id="wbd-raw-1",
        order_no="WBDD-20260702-0014",
        order_date=date(2026, 7, 2),
        demand_type="补库供货",
        business_type="整体维保",
        project_raw="CKD测试项目",
        project_std="CKD测试项目",
        warehouse="北京成品仓",
        data_status="已生效",
        linked_sales_order_no="XSDD-20250731-0035",
        import_batch_id=import_batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id="ckd-assign-1",
            source_order_id="wbd-raw-1",
            project_id="ckd-project-1",
            is_active=True,
            created_by="合成归属人",
        )
    )
    part = DimPart(pn_std="02311AYV", description="8G 内存")
    db.add(part)
    db.commit()
    return {"project_id": project.project_id, "part_id": part.id, "order": order}


def test_parse_groups_master_and_detail_rows():
    data = _ckd_workbook_bytes(
        rows=[
            {
                "head": _HEAD_MAINT,
                "lines": [
                    ["LID-1", "1", "02311AYV", "内存", "B1", "02311AYV", "SN1", "", "北京成品仓",
                     "BJCP-AA", "内存", "", "品牌", "个", "2", "100", "200", "是"],
                    ["LID-2", "2", "", "", "", "02311AYV", "", "", "", "", "", "", "", "", "3", "100", "300", "是"],
                ],
            },
            {"head": _HEAD_SALES, "lines": [["LID-S", "1", "", "", "", "SALE-PN", "", "", "", "", "", "", "", "", "5", "", "", ""]]},
        ]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    assert len(parsed["heads"]) == 2
    assert parsed["line_count"] == 3
    maint = parsed["heads"][0]
    assert maint.values["出库类别"] == "维保供货"
    assert len(maint.lines) == 2
    assert maint.lines[1].values["备件明细.出库数量"] == "3"


def test_store_preview_normalizes(db, wbdd_project):
    data = _ckd_workbook_bytes(
        rows=[{"head": _HEAD_MAINT, "lines": [
            ["LID-1", "1", "02311AYV", "内存", "B1", "02311AYV", "SN1", "", "", "", "", "", "", "", "2", "100", "200", "是"]]}]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-0001")
    head = db.execute(
        select(MaintenanceCkdHeadRow).where(MaintenanceCkdHeadRow.batch_id == batch_id)
    ).scalar_one()
    assert head.order_no == "CKD-20260806-0014"
    assert head.wbdd_no == "WBDD-20260702-0014"
    assert head.category == "维保供货"
    line = db.execute(
        select(MaintenanceCkdLineRow).where(MaintenanceCkdLineRow.batch_id == batch_id)
    ).scalar_one()
    assert line.pn == "02311AYV"
    assert float(line.out_qty) == 2.0
    assert float(line.unit_cost) == 100.0
    assert float(line.cost_amount) == 200.0


def test_apply_wires_front_stock(db, wbdd_project):
    data = _ckd_workbook_bytes(
        rows=[
            {"head": _HEAD_MAINT, "lines": [
                ["LID-1", "1", "02311AYV", "内存", "B1", "02311AYV", "SN1", "", "", "", "", "", "", "", "2", "100", "200", "是"],
                ["LID-2", "2", "", "", "", "02311AYV", "", "", "", "", "", "", "", "", "3", "100", "300", "是"],
            ]},
            {"head": _HEAD_SALES, "lines": [["LID-S", "1", "", "", "", "SALE-PN", "", "", "", "", "", "", "", "", "5", "", "", ""]]},
        ]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-0001")
    summary = ckd.apply_batch(db, batch_id, "合成管理员")

    assert summary["maintenance_heads"] == 1
    assert summary["ignored_heads"] == 1  # 销售出库
    assert summary["applied_lines"] == 2

    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "ckd-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 5.0

    ledgers = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.project_id == "ckd-project-1"
        )
    ).scalars().all()
    assert len(ledgers) == 2
    assert all(l.kind == "shipment_in" for l in ledgers)
    assert all(l.source_type == "ckd_shipment_line" for l in ledgers)

    batch = db.get(MaintenanceCkdImportBatch, batch_id)
    assert batch.status == "applied"


def test_apply_idempotent_across_reimport(db, wbdd_project):
    data = _ckd_workbook_bytes(
        rows=[{"head": _HEAD_MAINT, "lines": [
            ["LID-1", "1", "02311AYV", "内存", "B1", "02311AYV", "SN1", "", "", "", "", "", "", "", "2", "100", "200", "是"]]}]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-0001")
    ckd.apply_batch(db, batch_id, "合成管理员")

    # 同文件再次导入 → 新批次 → 流水幂等（同一明细 ID 不重复入账）
    parsed2 = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id2 = ckd.store_preview(db, parsed2, "合成管理员", idempotency_key="ckd-test-key-0002")
    summary2 = ckd.apply_batch(db, batch_id2, "合成管理员")
    assert summary2["applied_lines"] == 1
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "ckd-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 2.0


def test_apply_skips_unassigned_wbdd(db, wbdd_project):
    # 未归属 WBDD → 应用失败关闭，整批零写入
    data = _ckd_workbook_bytes(
        rows=[{
            "head": ["CKD-20260806-0100", "2026-08-06", "维保供货", "备件", "北京成品仓", "北京仓",
                     "WBDD-20260801-9999", "", "", "", "尤玉玲", "", "", "", "已生效"],
            "lines": [["LID-1", "1", "", "", "", "02311AYV", "", "", "", "", "", "", "", "", "4", "", "", ""]],
        }]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-0001")
    with pytest.raises(ckd.CkdBatchError):
        ckd.apply_batch(db, batch_id, "合成管理员")
    batch = db.get(MaintenanceCkdImportBatch, batch_id)
    assert batch.status == "failed"
    stocks = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "ckd-project-1"
        )
    ).scalars().all()
    assert stocks == []


def test_apply_skips_unknown_pn(db, wbdd_project):
    # 未知 PN → 应用失败关闭，整批零写入
    data = _ckd_workbook_bytes(
        rows=[{"head": _HEAD_MAINT, "lines": [
            ["LID-1", "1", "", "", "", "NOPE-999", "", "", "", "", "", "", "", "", "4", "", "", ""]]}]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-0001")
    with pytest.raises(ckd.CkdBatchError):
        ckd.apply_batch(db, batch_id, "合成管理员")
    batch = db.get(MaintenanceCkdImportBatch, batch_id)
    assert batch.status == "failed"


def test_apply_rejects_duplicate_apply(db, wbdd_project):
    data = _ckd_workbook_bytes(
        rows=[{"head": _HEAD_MAINT, "lines": [
            ["1", "02311AYV", "", "", "02311AYV", "", "", "", "", "", "", "", "", "2", "", "", ""]]}]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-0001")
    ckd.apply_batch(db, batch_id, "合成管理员")
    with pytest.raises(ckd.CkdBatchError):
        ckd.apply_batch(db, batch_id, "合成管理员")


def test_parse_rejects_missing_columns():
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["F0000001"])
    ws.append(["其他列"])
    buffer = io.BytesIO()
    wb.save(buffer)
    with pytest.raises(ckd.CkdParseError):
        ckd.parse_ckd_workbook(buffer.getvalue(), "x.xlsx")


def test_apply_rejects_cross_batch_lines(db, wbdd_project):
    """跨批明细（line.batch_id ≠ head.batch_id）→ 拒绝应用。"""
    data = _ckd_workbook_bytes(
        rows=[{"head": _HEAD_MAINT, "lines": [
            ["LID-1", "1", "02311AYV", "内存", "B1", "02311AYV", "SN1", "", "", "", "", "", "", "", "2", "100", "200", "是"]]}]
    )
    parsed = ckd.parse_ckd_workbook(data, "发货单.xlsx")
    batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-xb1")
    # 人为构造跨批：把明细行挪到另一批次
    other_batch_id = ckd.store_preview(db, parsed, "合成管理员", idempotency_key="ckd-test-key-xb2")
    from sqlalchemy import update

    db.execute(
        update(MaintenanceCkdLineRow)
        .where(MaintenanceCkdLineRow.batch_id == batch_id)
        .values(batch_id=other_batch_id)
    )
    db.commit()
    with pytest.raises(ckd.CkdBatchError):
        ckd.apply_batch(db, batch_id, "合成管理员")
