"""氚云三单（RKD/返库/BXD）导入与返库出账测试（C1b）。"""

import io
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
    MaintenanceRkdReturnLine,
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
        warehouse_name="DOC测试项目",
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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["applied_lines"] == 1
    assert summary["canonical_effect"] == "front_stock_return_out"
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "doc-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_apply_return_order_bad_part_not_deducted(db, front_stock_seed):
    # 坏品是消耗返还（F3 分子），不扣前置库；批次仍可 applied（非异常）
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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["applied_lines"] == 0
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "doc-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 1.0
    batch = db.get(MaintenanceDocImportBatch, batch_id)
    assert batch.status == "applied"


def test_apply_return_order_negative_balance_fail_closed(db, front_stock_seed):
    # 已消耗件不在账本：出账超结存 → 应用失败关闭，整批零写入
    _seed_front_stock(db, part_id=front_stock_seed["part_id"], qty=1)
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RETURN_HEAD,
        line_headers=_RETURN_LINE,
        rows=[{
            "head": ["维保返件", "其他退回", "备件", "2026-08-03", "新华三集团",
                     "DOC测试项目", "", "WBDD-20260702-0014", "",
                     "广州仓", "已生效", "", "RKN-003", "HEAD-3"],
            "lines": [
                ["", "02311AYV", "", "成品", "", "", "5", "LID-1", "1", ""],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("return_order", data, "退货返库单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0002")
    with pytest.raises(docs.DocBatchError):
        docs.apply_batch(db, batch_id, "合成管理员")
    batch = db.get(MaintenanceDocImportBatch, batch_id)
    assert batch.status == "failed"
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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
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


def test_apply_rkd_bad_part_creates_return_fact(db, front_stock_seed):
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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["canonical_effect"] == "rkd_return_facts"
    assert summary["applied_lines"] == 1
    batch = db.get(MaintenanceDocImportBatch, batch_id)
    assert batch.status == "applied"
    facts = db.execute(
        select(MaintenanceRkdReturnLine).where(
            MaintenanceRkdReturnLine.batch_id == batch_id
        )
    ).scalars().all()
    assert len(facts) == 1
    fact = facts[0]
    assert fact.project_id == "doc-project-1"
    assert fact.head_no == "RKD-20260806-0010"
    assert fact.pn == "02311AYV"
    assert fact.part_id == front_stock_seed["part_id"]
    assert float(fact.qty) == 1.0
    assert fact.test_result == "坏品"
    assert fact.occurred_at is not None


def test_apply_rkd_good_part_is_not_a_return_fact(db, front_stock_seed):
    # 成品/好件入库不是坏件返还事实，也不进前置库账本（RKD 只记坏件返还）
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RKD_HEAD,
        line_headers=_RKD_LINE,
        rows=[{
            "head": ["RKD-20260806-0011", "2026-08-06", "其他入库", "备件", "已生效",
                     "北京仓", "", "WBDD-20260702-0014", "", "", "", "否", ""],
            "lines": [
                ["02311AYV", "", "", "成品", "", "", "2", "", "", "", "", "RKD-LID-1", "1"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("rkd_inbound", data, "入库单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["canonical_effect"] == "rkd_return_facts"
    assert summary["applied_lines"] == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceRkdReturnLine)) == 0


def test_apply_rkd_unknown_pn_keeps_quantity_fact(db, front_stock_seed):
    # 未知 PN 不影响返还率分子数量：事实保留 pn 文本，part_id 置空待治理
    data = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RKD_HEAD,
        line_headers=_RKD_LINE,
        rows=[{
            "head": ["RKD-20260806-0012", "2026-08-06", "其他入库", "备件", "已生效",
                     "北京仓", "", "WBDD-20260702-0014", "", "", "", "否", ""],
            "lines": [
                ["ZZZ-UNKNOWN", "", "", "故障", "", "", "3", "", "", "", "", "RKD-LID-1", "1"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("rkd_inbound", data, "入库单.xlsx")
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["applied_lines"] == 1
    fact = db.execute(
        select(MaintenanceRkdReturnLine).where(
            MaintenanceRkdReturnLine.batch_id == batch_id
        )
    ).scalar_one()
    assert fact.pn == "ZZZ-UNKNOWN"
    assert fact.part_id is None
    assert float(fact.qty) == 3.0


def test_apply_rkd_duplicate_source_conflict_fail_closed(db, front_stock_seed):
    workbook = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RKD_HEAD,
        line_headers=_RKD_LINE,
        rows=[{
            "head": ["RKD-20260806-0013", "2026-08-06", "其他入库", "备件", "已生效",
                     "北京仓", "", "WBDD-20260702-0014", "", "", "", "否", ""],
            "lines": [
                ["02311AYV", "", "", "坏品", "", "", "1", "", "", "", "", "RKD-LID-1", "1"],
            ],
        }],
    )
    parsed = docs.parse_doc_workbook("rkd_inbound", workbook, "入库单.xlsx")
    first = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    docs.apply_batch(db, first, "合成管理员")
    # 同一文件换 Idempotency-Key 重传：明细键与已入账事实冲突 → 整批失败关闭
    second = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0002")
    with pytest.raises(docs.DocBatchError):
        docs.apply_batch(db, second, "合成管理员")
    assert db.get(MaintenanceDocImportBatch, second).status == "failed"
    assert db.scalar(select(func.count()).select_from(MaintenanceRkdReturnLine)) == 1


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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
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
    batch_id = docs.store_preview(db, parsed, "合成管理员", idempotency_key="doc-test-key-0001")
    summary = docs.apply_batch(db, batch_id, "合成管理员")
    assert summary["canonical_effect"] == "pending_c4_expense_reconcile"


def test_parse_rejects_unknown_type():
    with pytest.raises(docs.DocParseError):
        docs.parse_doc_workbook("other_type", b"", "x.xlsx")


def test_stream_rows_resolve_shared_strings_and_visible_sheet():
    """流式解析还原 sharedStrings 文本并按 workbook 关系取第一个可见 sheet。"""
    from io import BytesIO

    from app.services import import_safety

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["入库单号", "项目名称"])
    ws.append(["RKD-20260806-0010", "DOC测试项目"])
    ws.append(["RKD-20260806-0011", "DOC测试项目"])  # 重复字符串 → sharedStrings
    buffer = BytesIO()
    wb.save(buffer)
    rows = list(import_safety.stream_first_sheet_rows(buffer.getvalue()))
    assert rows[0][:2] == ("入库单号", "项目名称")
    assert rows[1][:2] == ("RKD-20260806-0010", "DOC测试项目")
    assert rows[2][:2] == ("RKD-20260806-0011", "DOC测试项目")


def test_parse_doc_streaming_branch_matches_normal_parse(db, monkeypatch):
    """超过阈值的文件走流式分支，结果与普通模式一致（C5 大文件路径）。"""
    from app.services import import_safety

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
    normal = docs.parse_doc_workbook("return_order", data, "退货返库单.xlsx")
    monkeypatch.setattr(import_safety, "STREAM_THRESHOLD_BYTES", 10)
    streamed = docs.parse_doc_workbook("return_order", data, "退货返库单.xlsx")
    assert streamed["file_hash"] == normal["file_hash"]
    assert streamed["line_count"] == normal["line_count"]
    normal_head = normal["heads"][0]
    streamed_head = streamed["heads"][0]
    assert streamed_head.values == normal_head.values
    assert [line.values for line in streamed_head.lines] == [
        line.values for line in normal_head.lines
    ]
