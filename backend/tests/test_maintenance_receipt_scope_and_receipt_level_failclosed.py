"""收款单导入只管维保，且 fail-closed 要按「收款单」而不是按「销售订单」（2026-09-08）。

两件事，同源于一份生产实拍导出（9-08 的 298 行 SKD）：

一、**非维保订单在金额校验之前就该跳过**。那份文件 297/298 行是备件销售 / 销售换货 /
    租赁 / 整机销售，只有 1 行是整体维保。现在这些行会先撞上金额、状态、备注三道校验：
    11 行负数报「实收金额必须是非负有限数字」、10 行「其他收款」（无销售订单，主表金额
    合计 556 万）报「销售订单号和收款单号不能为空」——收款单号明明有值，文案与事实不符。
    这些订单根本没有维保合同，报错纯属噪音，还会把真正该看的错误淹掉。

    判定权威仍是**维保合同**，不是业务类型：2026-09-03 拍板「业务类型只作分类、维保业务=是
    才是建项依据」（maintenance_bulk_import.py 的 _is_explicit_maintenance_row），
    docs/reference/sales-order-columns.md:22 同。所以这里只是把已有的合同匹配提前，
    不新增任何按业务类型挡钱的规则。

二、**fail-closed 要按收款单连坐**。现在按销售订单连坐：同一订单有坏行 ⇒ 该订单全冻。
    但「退换货核销 / 平账」单的正腿和负腿天生落在**不同**销售订单上（实拍 10 张单，
    7 张整单净额为 0、银行一分钱没动），按订单连坐永远够不着正腿：负腿被判无效丢掉，
    正腿绿色、无警告、默认勾选，一点就写进台账和 confirmed 累计快照。
    收款单是一张资金凭证，它整体成立或整体不成立——任何一行不可信，整张单涉及的订单
    都不该按剩下的行算累计。

    实拍这 10 张单的正腿合计 40,740 元，其中属于维保三类的是 **0 元**（全是备件销售/
    销售换货），所以今天的实际敞口是 0；挡住它的是「这些订单碰巧没建过维保合同」，
    那是巧合不是规则。这条测试把它变成规则。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from openpyxl import Workbook
from sqlalchemy import select

from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
from app.services import maintenance_bulk_import as bulk

_HEADERS = [
    "收款明细.销售订单(必填)", "收款单号(必填)", "收款日期(必填)",
    "收款明细.实收金额", "数据状态", "收款明细.备注",
]


def _xlsx(rows: list[tuple]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(_HEADERS)
    for row in rows:
        order_no, receipt_no, receipt_date, amount = row[:4]
        remark = row[4] if len(row) > 4 else ""
        status = row[5] if len(row) > 5 else "已生效"
        sheet.append([order_no, receipt_no, receipt_date, amount, status, remark])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _seed_contract(db, contract_no: str) -> MaintenanceProjectContract:
    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=f"SKD-{uuid4().hex[:8]}",
        display_name=f"收款范围测试项目-{contract_no}",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id=str(uuid4()),
        project_id=project.project_id,
        contract_id=f"C-{contract_no}",
        contract_no=contract_no,
        amount_inc_tax=Decimal("1000000.00"),
        included_in_total=True,
        status_mapping_state="mapped",
        status_mapping_version="v1",
        effective_from=date(2026, 1, 1),
        source="ledger",
        version=1,
    )
    db.add(contract)
    db.commit()
    return contract


def _by_row(preview: dict) -> dict[int, dict]:
    return {row["source_row"]: row for row in preview["rows"]}


def _codes(row: dict) -> set[str]:
    return {
        item["code"]
        for key in ("errors", "warnings")
        for item in row.get(key) or []
    }


# ---------- 一、非维保订单在金额校验之前跳过 ----------

def test_negative_amount_on_non_maintenance_order_is_skipped_not_an_error(db):
    """备件销售的退货负数行：没有维保合同 ⇒ 跳过，不该报「金额必须非负」。"""

    _seed_contract(db, "20240116-0047")
    data = _xlsx([
        ("XSDD-20240116-0047", "SKD-A-0001", "2026-04-17", "4477"),
        # 这个订单没有维保合同（备件销售的退换货）
        ("XSDD-20250210-0064", "SKD-A-0002", "2026-04-17", "-240"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")

    rows = _by_row(preview)
    assert rows[2]["match_state"] == "matched"
    assert rows[3]["match_state"] == "unmatched"
    assert rows[3]["errors"] == []
    assert "project_not_found" in _codes(rows[3])
    assert preview["summary"]["invalid"] == 0


def test_receipt_without_sales_order_reports_the_real_reason(db):
    """「其他收款」没有销售订单：文案不能是「销售订单号和收款单号不能为空」——单号有值。"""

    _seed_contract(db, "20240116-0047")
    data = _xlsx([
        ("XSDD-20240116-0047", "SKD-A-0003", "2026-04-17", "4477"),
        ("", "SKD-A-0004", "2026-04-17", "5000000"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")

    row = _by_row(preview)[3]
    assert "receipt_without_order" in _codes(row)
    assert row["match_state"] == "unmatched"
    assert row["errors"] == []


def test_risk_remark_on_non_maintenance_order_does_not_raise_a_hard_error(db):
    """非维保订单的备注写什么都与维保无关，不该进硬错误把真问题淹掉。"""

    _seed_contract(db, "20240116-0047")
    data = _xlsx([
        ("XSDD-20240116-0047", "SKD-A-0005", "2026-04-17", "4477"),
        ("XSDD-20230914-0040", "SKD-A-0006", "2026-04-17", "9600", "退换货核销，直接红冲"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")

    assert preview["summary"]["invalid"] == 0
    assert _by_row(preview)[3]["match_state"] == "unmatched"


def test_maintenance_order_still_validated_as_before(db):
    """防误伤：有维保合同的订单，三道校验一道不能少。"""

    _seed_contract(db, "20240116-0047")
    data = _xlsx([
        ("XSDD-20240116-0047", "SKD-A-0007", "2026-04-17", "-4477"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    assert preview["summary"]["invalid"] == 1
    assert "invalid_receipt" in _codes(_by_row(preview)[2])


# ---------- 二、收款单级 fail-closed ----------

def test_one_bad_line_blocks_every_order_on_the_same_receipt(db):
    """平账单：正腿和负腿在不同订单上，两个都得是维保合同才试得出来。"""

    _seed_contract(db, "20260810-0064")
    _seed_contract(db, "20260608-0018")
    data = _xlsx([
        ("XSDD-20260810-0064", "SKD-B-0001", "2026-04-17", "8000"),
        ("XSDD-20260608-0018", "SKD-B-0001", "2026-04-17", "-8000"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")

    rows = _by_row(preview)
    assert rows[2]["row_status"] == "blocked", "正腿必须跟着整张单一起冻结"
    assert "receipt_level_fail_closed" in _codes(rows[2])
    assert preview["summary"]["ready"] == 0
    assert preview["summary"]["matched"] == 0


def test_receipt_level_block_survives_apply(db):
    """光在预览里标 blocked 不够——提交也不能把正腿写进台账。"""

    _seed_contract(db, "20260810-0064")
    _seed_contract(db, "20260608-0018")
    data = _xlsx([
        ("XSDD-20260810-0064", "SKD-B-0002", "2026-04-17", "8000"),
        ("XSDD-20260608-0018", "SKD-B-0002", "2026-04-17", "-8000"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    ready = [row["row_key"] for row in preview["rows"] if row["row_status"] == "ready"]
    assert ready == []

    ledger = db.scalars(
        select(MaintenanceCollectionReceipt).where(
            MaintenanceCollectionReceipt.receipt_no == "SKD-B-0002"
        )
    ).all()
    assert ledger == []


def test_three_leg_receipt_blocks_the_surviving_positive_leg(db):
    """复刻 SKD-20260903-0015：+800 / -350 / -800，三个不同订单。"""

    for contract_no in ("20230831-0089", "20230607-0083", "20230327-0030"):
        _seed_contract(db, contract_no)
    data = _xlsx([
        ("XSDD-20230831-0089", "SKD-B-0003", "2026-04-17", "800"),
        ("XSDD-20230607-0083", "SKD-B-0003", "2026-04-17", "-350"),
        ("XSDD-20230327-0030", "SKD-B-0003", "2026-04-17", "-800"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    assert preview["summary"]["ready"] == 0
    assert _by_row(preview)[2]["row_status"] == "blocked"


def test_clean_receipt_spanning_several_orders_is_untouched(db):
    """回归锁：整张单都合法时，多订单收款单照常逐个入账（你那张 4 单合一的单）。"""

    for contract_no in ("20240116-0047", "20230522-0032", "20230517-0023"):
        _seed_contract(db, contract_no)
    data = _xlsx([
        ("XSDD-20240116-0047", "SKD-B-0004", "2026-04-17", "4477"),
        ("XSDD-20230522-0032", "SKD-B-0004", "2026-04-17", "1307.65"),
        ("XSDD-20230517-0023", "SKD-B-0004", "2026-04-17", "5340"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    assert preview["summary"]["matched"] == 3
    assert preview["summary"]["ready"] == 3
    assert preview["summary"]["invalid"] == 0


def test_a_bad_line_does_not_block_a_different_receipt(db):
    """连坐范围是这张单，不能溢到同一订单的其他收款单以外去。"""

    _seed_contract(db, "20240116-0047")
    _seed_contract(db, "20230522-0032")
    data = _xlsx([
        ("XSDD-20240116-0047", "SKD-B-0005", "2026-04-17", "4477"),
        ("XSDD-20230522-0032", "SKD-B-0006", "2026-04-17", "1307.65"),
        ("XSDD-20230522-0032", "SKD-B-0006", "2026-04-17", "-1307.65"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")

    rows = _by_row(preview)
    assert rows[2]["row_status"] == "ready", "另一张单不受牵连"
    assert rows[3]["row_status"] == "blocked"
