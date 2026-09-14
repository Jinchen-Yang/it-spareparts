"""氚云「收款单主表 + 明细展开」导出：主表列是**合并单元格**，不是空白（2026-09-08）。

生产实拍：一张收款单合并 4 个维保订单，导出后 收款单号 / 收款日期 / 数据状态
等 38 个主表列是纵向合并区（如 BA3:BA6），只有块首那格有值；openpyxl 读非左上角
单元格一律 None，于是台账导入把 232/298 行判成「收款单号为空」——上午那份 298 行
实收 149.9 万，只认了 37.8 万。

实测三份生产导出：合并区**全部**是单列纵向、不跨表头、不涉及任何「收款明细.*」
子表列（4 行文件 39 个、298 行文件 1287 个、8-28 那份子表导出 0 个），所以按 xlsx
自身语义把合并区铺满整块是无歧义的，且对没有合并区的旧导出零影响。

金额列一旦被合并就不能铺——那会把同一笔钱复制成多笔。现网没有这种文件，但这
是钱，按 fail-closed 处理：显式报错，不猜。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionReceipt
from app.services import maintenance_bulk_import as bulk


# 主表展开导出的真实列序（取生产文件的子集，含 1 个子表列在前、主表列在后）
_PARENT_HEADERS = [
    "收款明细.销售订单(必填)",   # 子表：每行都有
    "收款明细.实收金额",          # 子表：每行都有
    "收款明细.备注",              # 子表
    "收款单号(必填)",             # 主表：块首有值，其余合并
    "收款日期(必填)",             # 主表
    "数据状态",                   # 主表
]
_SYSTEM_HEADERS = [
    "D107407Fk5kascong737r7ql8ph8c8u85.F0000008",
    "D107407Fk5kascong737r7ql8ph8c8u85.F0000062",
    "D107407Fk5kascong737r7ql8ph8c8u85.F0000037",
    "SeqNo",
    "F0000001",
    "Status",
]


def _merged_parent_xlsx(
    blocks: list[tuple[str, str, list[tuple[str, str]]]],
    *,
    merge_amount_column: bool = False,
) -> bytes:
    """按生产形态构造：主表列纵向合并，子表列逐行。

    blocks = [(收款单号, 收款日期, [(销售订单, 实收金额), ...]), ...]
    merge_amount_column=True 时把子表金额列也合并——用来钉死金额列的 fail-closed。
    """

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(_SYSTEM_HEADERS)
    sheet.append(_PARENT_HEADERS)
    row_no = 3
    for receipt_no, receipt_date, lines in blocks:
        first = row_no
        for index, (order_no, amount) in enumerate(lines):
            sheet.cell(row_no, 1).value = order_no
            # 金额列合并时只有块首那格写值，模拟 openpyxl 对合并区的读法
            if not merge_amount_column or index == 0:
                sheet.cell(row_no, 2).value = amount
            row_no += 1
        last = row_no - 1
        sheet.cell(first, 4).value = receipt_no
        sheet.cell(first, 5).value = receipt_date
        sheet.cell(first, 6).value = "已生效"
        if last > first:
            for column in ("D", "E", "F"):
                sheet.merge_cells(f"{column}{first}:{column}{last}")
            if merge_amount_column:
                sheet.merge_cells(f"B{first}:B{last}")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _flat_xlsx(rows: list[tuple[str, str, str, str]]) -> bytes:
    """8-28 那份「收款明细子表导出」的形态：零合并区，每行自带主表字段。"""

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(_SYSTEM_HEADERS)
    sheet.append(_PARENT_HEADERS)
    for order_no, amount, receipt_no, receipt_date in rows:
        sheet.append([order_no, amount, "", receipt_no, receipt_date, "已生效"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _seed_contract(db, contract_no: str) -> MaintenanceProjectContract:
    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=f"SKD-{uuid4().hex[:8]}",
        display_name=f"合并单元格测试项目-{contract_no}",
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


# ---------- 展开合并区 ----------

def test_merged_parent_cells_are_expanded_so_every_detail_line_counts(db):
    """生产实拍：SKD-20260417-0027 一张单 4 个维保订单，合计 13632.28。

    不展开合并区时只认块首 4477.00，另外 9155.28 全判「收款单号为空」。
    """

    for contract_no in (
        "20240116-0047", "20230522-0032", "20230517-0023", "20230322-0020",
    ):
        _seed_contract(db, contract_no)

    data = _merged_parent_xlsx([
        ("SKD-20260417-0027", "2026-04-17", [
            ("XSDD-20240116-0047", "4477"),
            ("XSDD-20230522-0032", "1307.65"),
            ("XSDD-20230517-0023", "5340"),
            ("XSDD-20230322-0020", "2507.63"),
        ]),
    ])

    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")

    blocked = [
        (row["source_row"], row["errors"]) for row in preview["rows"]
        if row["match_state"] == "invalid"
    ]
    assert blocked == [], f"合并区未展开，续行被判无效：{blocked}"
    assert preview["summary"]["matched"] == 4
    assert preview["summary"]["invalid"] == 0

    total = sum(
        Decimal(row["canonical"]["cumulative_received_inc_tax"]) for row in preview["rows"]
    )
    assert total == Decimal("13632.28")
    # 收款日期/状态同样来自合并区：铺不到就会掉进「状态必须已生效」那道门
    assert {row["canonical"]["report_month"] for row in preview["rows"]} == {"2026-04-01"}
    assert {row["canonical"]["receipt_reference"] for row in preview["rows"]} == {
        "SKD-20260417-0027"
    }


def test_expansion_applies_to_apply_path_not_only_preview(db):
    """预览认了还不够——落台账的必须是 4 笔，钱不能停在预览层。"""

    contracts = {
        contract_no: _seed_contract(db, contract_no)
        for contract_no in ("20240116-0047", "20230522-0032")
    }
    data = _merged_parent_xlsx([
        ("SKD-20260417-0028", "2026-04-17", [
            ("XSDD-20240116-0047", "4477"),
            ("XSDD-20230522-0032", "1307.65"),
        ]),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    row_keys = [row["row_key"] for row in preview["rows"] if row["row_status"] == "ready"]
    assert len(row_keys) == 2
    bulk.apply_transfer(
        db,
        preview_token=preview["preview_token"],
        payload_hash=preview["payload_hash"],
        data_version=preview["data_version"],
        row_keys=row_keys,
        operated_by="skd-operator",
        real_operator=True,
    )

    ledger = db.scalars(
        select(MaintenanceCollectionReceipt).where(
            MaintenanceCollectionReceipt.receipt_no == "SKD-20260417-0028",
            MaintenanceCollectionReceipt.is_active.is_(True),
        )
    ).all()
    assert {row.contract_no for row in ledger} == {"20240116-0047", "20230522-0032"}
    assert sum(row.actual_amount for row in ledger) == Decimal("5784.65")
    assert {row.receipt_date for row in ledger} == {date(2026, 4, 17)}
    assert contracts  # 保持夹具引用，防止 lint 误删


def test_flat_child_export_without_merges_is_untouched(db):
    """8-28 那份子表导出零合并区：改动前后必须一模一样。"""

    _seed_contract(db, "20240116-0047")
    _seed_contract(db, "20230522-0032")
    data = _flat_xlsx([
        ("XSDD-20240116-0047", "4477", "SKD-20260417-0029", "2026-04-17"),
        ("XSDD-20230522-0032", "1307.65", "SKD-20260417-0029", "2026-04-17"),
    ])
    preview = bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    assert preview["summary"]["invalid"] == 0
    assert preview["summary"]["matched"] == 2
    assert {row["canonical"]["receipt_reference"] for row in preview["rows"]} == {
        "SKD-20260417-0029"
    }


def test_blank_rows_stay_skipped_even_inside_a_merged_block(db):
    """空行判定必须看**原始**单元格：铺完再判会把块内空行当成数据行凭空造钱。"""

    _seed_contract(db, "20240116-0047")
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(_SYSTEM_HEADERS)
    sheet.append(_PARENT_HEADERS)
    sheet.cell(3, 1).value = "XSDD-20240116-0047"
    sheet.cell(3, 2).value = "4477"
    sheet.cell(3, 4).value = "SKD-20260417-0030"
    sheet.cell(3, 5).value = "2026-04-17"
    sheet.cell(3, 6).value = "已生效"
    # 第 4 行整行空白，但主表列的合并区盖住了它
    for column in ("D", "E", "F"):
        sheet.merge_cells(f"{column}3:{column}4")
    buffer = BytesIO()
    workbook.save(buffer)

    preview = bulk.preview_transfer(
        db, [("skd.xlsx", buffer.getvalue())], operated_by="skd-operator"
    )
    assert preview["summary"]["total"] == 1
    assert preview["summary"]["matched"] == 1
    assert preview["rows"][0]["canonical"]["cumulative_received_inc_tax"] == "4477.00"


# ---------- 金额列合并：fail-closed ----------

def test_merged_amount_column_is_refused_not_duplicated(db):
    """金额列被合并 = 一笔钱铺成多笔。现网没有这种文件，出现了必须显式拒绝。"""

    _seed_contract(db, "20240116-0047")
    _seed_contract(db, "20230522-0032")
    data = _merged_parent_xlsx(
        [
            ("SKD-20260417-0031", "2026-04-17", [
                ("XSDD-20240116-0047", "4477"),
                ("XSDD-20230522-0032", "4477"),
            ]),
        ],
        merge_amount_column=True,
    )
    with pytest.raises(bulk.BulkImportInvalid) as excinfo:
        bulk.preview_transfer(db, [("skd.xlsx", data)], operated_by="skd-operator")
    assert "合并" in str(excinfo.value)
