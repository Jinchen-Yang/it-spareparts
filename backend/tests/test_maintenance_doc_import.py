"""氚云三单（RKD/返库/BXD）导入与返库出账测试（C1b）。"""

import io
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
)
from app.models.maintenance_front_stock import MaintenanceFrontStock
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch
from app.services import maintenance_doc_import as docs
from app.services import maintenance_front_stock as front_stock


def _doc_workbook(*, sheet_title: str, head_headers: list[str], line_headers: list[str],
                  rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(["F0000001", "F0000032"])  # 字段码行
    ws.append(head_headers + line_headers)
    for row in rows:
        ws.append(row["head"])
        for line in row["lines"]:
            ws.append([""] * len(head_headers) + line)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


_RETURN_HEAD = [
    "返库类别", "返库类型", "返库备件/整机", "返库日期", "客户名称", "项目名称",
    "维保销售订单", "维保需求单(备件)", "维保需求单", "仓储中心", "数据状态", "备注",
    "返库单号", "数据ID(不可修改)",
]
_RETURN_LINE = [
    "备件明细.备件自贴码", "备件明细.备件PN", "备件明细.备件SN号", "备件明细.备件测试结果",
    "备件明细.入库库位", "备件明细.产品描述", "备件明细.返库数量", "备件明细.数据ID(不可修改)",
    "备件明细.序号", "备件明细.入库仓库",
]

_RKD_HEAD = [
    "入库单号", "入库日期", "入库类别", "入库备件/整机", "数据状态", "仓储中心",
    "项目名称", "维保需求单", "采购订单", "供应商", "退货类型", "退返入库通知单", "备注",
]
_RKD_LINE = [
    "备件明细.备件PN", "备件明细.备件自贴码", "备件明细.备件SN", "备件明细.测试结果",
    "备件明细.入库库位", "备件明细.备件描述", "备件明细.入库数量", "备件明细.销售单价",
    "备件明细.销售金额", "备件明细.采购单价", "备件明细.金额", "备件明细.数据ID(不可修改)",
    "备件明细.序号",
]

_BXD_HEAD = [
    "费用单号", "报销人员", "报销类别", "支出事由", "维保销售订单", "销售订单",
    "客户名称", "销售人员", "报销金额", "实付金额", "付款方式", "报销日期", "备注",
    "数据状态", "数据ID(不可修改)", "流程状态",
]
_BXD_LINE = [
    "报销明细.费用分类", "报销明细.单据数量", "报销明细.报销金额", "报销明细.备注",
    "报销明细.数据ID(不可修改)", "报销明细.序号",
]


@pytest.fixture()
def front_stock_seed(db):
    project = MaintenanceProject(
        project_id="doc-project-1",
        project_code="DOC测试项目",
        display_name="DOC测试项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(project)
    db.flush()
    import_batch = SysImportBatch(
        filename="w.xlsx", file_type="maintenance", file_hash="h2", status="success"
    )
    db.add(import_batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id="doc-wbd-raw-1",
        order_no="WBDD-20260702-0014",
        order_date=date(2026, 7, 2),
        demand_type="补库供货",
        business_type="整体维保",
        project_raw="DOC测试项目",
        project_std="DOC测试项目",
        warehouse="北京成品仓",
        data_status="已生效",
        linked_sales_order_no="XSDD-20250731-0035",
        import_batch_id=import_batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        MaintenanceSourceOrderAssignment(
            assignment_id="doc-assign-1",
            source_order_id="doc-wbd-raw-1",
            project_id="doc-project-1",
            is_active=True,
            created_by="合成归属人",
        )
    )
    part = DimPart(pn_std="02311AYV", description="8G 内存")
    db.add(part)
    db.commit()
    return {"project_id": project.project_id, "part_id": part.id}


def _seed_front_stock(db, *, part_id, qty):
    front_stock.apply_movement(
        db,
        project_id="doc-project-1",
        part_id=part_id,
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref=f"seed-{qty}",
        qty=qty,
        operated_by="合成测试员",
    )
    db.commit()


def test_parse_return_order(db, front_stock_seed):
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RETURN_HEAD,
        line_headers=_RETURN_LINE,
        rows=[{
            "head": ["维保返件", "其他退回", "备件", "2026-08-03", "新华三集团",
                     "DOC测试项目", "XSDD-20260203-0029", "WBDD-20260203-0026", "",
                     "广州仓", "已生效", "项目不用了", "RKN-001", "HEAD-1"],
            "lines": [
                ["BBF67977341", "02311AYV", "S0M59S5M", "成品", "GZCP-01", "HP HDD", "1", "LID-1", "1", "广州仓"],
                ["BBF67977342", "02311AYV", "S0M59S5N", "成品", "GZCP-01", "HP HDD", "2", "LID-2", "2", "广州仓"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("return_order", data, "退货返库单.xlsx")
    assert len(parsed["heads"]) == 1
    assert parsed["line_count"] == 2
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    heads = db.execute(
        select(MaintenanceDocHeadRow).where(MaintenanceDocHeadRow.batch_id == batch_id)
    ).scalars().all()
    assert heads[0].head_no == "RKN-001"
    assert heads[0].wbdd_no == "WBDD-20260203-0026"
    assert heads[0].xsdd_no == "XSDD-20260203-0029"
    lines = db.execute(
        select(MaintenanceDocLineRow).where(MaintenanceDocLineRow.batch_id == batch_id)
    ).scalars().all()
    assert len(lines) == 2
    assert lines[0].pn == "02311AYV"
    assert float(lines[0].qty) == 1.0
    assert lines[0].test_result == "成品"


def test_apply_return_order_reduces_front_stock(db, front_stock_seed):
    _seed_front_stock(db, part_id=front_stock_seed["part_id"], qty=5)
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RETURN_HEAD,
        line_headers=_RETURN_LINE,
        rows=[{
            "head": ["维保返件", "其他退回", "备件", "2026-08-03", "新华三集团",
                     "DOC测试项目", "XSDD-20260203-0029", "WBDD-20260702-0014", "",
                     "广州仓", "已生效", "", "RKN-001", "HEAD-1"],
            "lines": [
                ["", "02311AYV", "", "成品", "", "", "2", "LID-1", "1", ""],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("return_order", data, "退货返库单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["applied_lines"] == 1
    assert summary["canonical_effect"] == "front_stock_return_out"
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "doc-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_apply_return_order_negative_balance_skipped(db, front_stock_seed):
    # 已消耗件不在账本：出账超结存 → 失败关闭并跳过（好件坏件天然分流）
    _seed_front_stock(db, part_id=front_stock_seed["part_id"], qty=1)
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RETURN_HEAD,
        line_headers=_RETURN_LINE,
        rows=[{
            "head": ["维保返件", "其他退回", "备件", "2026-08-03", "新华三集团",
                     "DOC测试项目", "", "WBDD-20260702-0014", "",
                     "广州仓", "已生效", "", "RKN-002", "HEAD-2"],
            "lines": [
                ["", "02311AYV", "", "坏品", "", "", "5", "LID-1", "1", ""],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("return_order", data, "退货返库单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["applied_lines"] == 0
    assert summary["skipped_lines"] == 1
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "doc-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 1.0


def test_parse_rkd_inbound(db, front_stock_seed):
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RKD_HEAD,
        line_headers=_RKD_LINE,
        rows=[{
            "head": ["RKD-20260806-0010", "2026-08-06", "其他入库", "备件", "已生效",
                     "北京仓", "DOC测试项目", "WBDD-20260702-0014", "", "供应商A", "",
                     "否", ""],
            "lines": [
                ["02311AYV", "S480NA0N920414", "S480NA0N920414", "成品", "BJCP-LS001",
                 "Intel SSD", "2", "800", "1600", "500", "1000", "RKD-LID-1", "1"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("rkd_inbound", data, "入库单.xlsx")
    assert parsed["line_count"] == 1
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    head = db.execute(
        select(MaintenanceDocHeadRow).where(MaintenanceDocHeadRow.batch_id == batch_id)
    ).scalar_one()
    assert head.head_no == "RKD-20260806-0010"
    line = db.execute(
        select(MaintenanceDocLineRow).where(MaintenanceDocLineRow.batch_id == batch_id)
    ).scalar_one()
    assert line.pn == "02311AYV"
    assert float(line.qty) == 2.0
    assert float(line.amount) == 1000.0


def test_apply_rkd_is_raw_only(db, front_stock_seed):
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RKD_HEAD,
        line_headers=_RKD_LINE,
        rows=[{
            "head": ["RKD-20260806-0010", "2026-08-06", "其他入库", "备件", "已生效",
                     "北京仓", "", "WBDD-20260702-0014", "", "", "", "否", ""],
            "lines": [
                ["02311AYV", "", "", "坏品", "", "", "1", "", "", "", "", "RKD-LID-1", "1"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("rkd_inbound", data, "入库单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["canonical_effect"] == "pending_f3_return_facts"
    batch = db.get(MaintenanceDocImportBatch, batch_id)
    assert batch.status == "applied"


def test_parse_bxd_expense(db, front_stock_seed):
    data = _doc_workbook(
        sheet_title="费用报销_支付单",
        head_headers=_BXD_HEAD,
        line_headers=_BXD_LINE,
        rows=[{
            "head": ["BXD-20260721-0019", "罗汇康", "维保费用", "北京2026年6月外援费用",
                     "XSDD-20260203-0029", "XSDD-20260203-0029", "新华三集团", "李呈辉",
                     "800", "800", "现金", "2026-07-27", "", "已结束", "BXD-HEAD-1", "已结束"],
            "lines": [
                ["外援费用", "1", "800", "大疆西安机房第二季度巡检", "BXD-LID-1", "1"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("bxd_expense", data, "费用报销支付单.xlsx")
    assert parsed["line_count"] == 1
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    head = db.execute(
        select(MaintenanceDocHeadRow).where(MaintenanceDocHeadRow.batch_id == batch_id)
    ).scalar_one()
    assert head.head_no == "BXD-20260721-0019"
    assert head.xsdd_no == "XSDD-20260203-0029"
    line = db.execute(
        select(MaintenanceDocLineRow).where(MaintenanceDocLineRow.batch_id == batch_id)
    ).scalar_one()
    assert float(line.amount) == 800.0


def test_apply_bxd_is_raw_only(db, front_stock_seed):
    data = _doc_workbook(
        sheet_title="费用报销_支付单",
        head_headers=_BXD_HEAD,
        line_headers=_BXD_LINE,
        rows=[{
            "head": ["BXD-20260721-0019", "罗汇康", "维保费用", "外援费用",
                     "XSDD-20260203-0029", "", "新华三集团", "李呈辉",
                     "800", "800", "现金", "2026-07-27", "", "已结束", "BXD-HEAD-1", "已结束"],
            "lines": [["外援费用", "1", "800", "", "BXD-LID-1", "1"]],
        }],
    )
    parsed = docs.parse_doc_workbook("bxd_expense", data, "费用报销支付单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["canonical_effect"] == "pending_c4_expense_reconcile"


def test_parse_rejects_unknown_type():
    with pytest.raises(docs.DocParseError):
        docs.parse_doc_workbook("other_type", b"", "x.xlsx")
