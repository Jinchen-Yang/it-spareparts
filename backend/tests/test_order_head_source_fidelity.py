"""D-0050 单头来源保真与含税旗标别名的真实入口契约。

两条主线：
1. DB upsert：坏 PN/数量明细行不得冻结单头——库内既有客户/供应商与日期
   不能被坏行的空值清掉，应更新为文件中首个有效明细行的值（真实
   ``pipeline.run_import`` / loader upsert 路径 + DB 事实断言）。
2. reader 同义列合并 × cleaner 既有含税词表：真实 xlsx 上「含税/是」「不含税/否」
   「True/yes」等价；False 是有效业务值；相反旗标与未知非空必须整批拒绝，
   不能被当作空值让另一列静默补位。

全部合成数据；DB 用例复用 conftest 迁移库夹具，不连接生产库。
"""
from datetime import date
from decimal import Decimal
import re
import zipfile

import openpyxl
from openpyxl.utils import get_column_letter
import pytest
from sqlalchemy import select

from app.etl import cleaner, mapping, pipeline, precheck, reader, transform
from app.models.dimensions import DimCustomer, DimSupplier
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder

SALES_LINE_ID = "订单明细.数据ID(不可修改)"
PURCHASE_LINE_ID = "明细.数据ID(不可修改)"
_SHEET_XML = "xl/worksheets/sheet1.xml"
_INLINE_CELL = re.compile(
    r'<c r="(?P<ref>[A-Z]+\d+)"(?P<attrs>[^>]*)><is>(?P<body>.*?)</is></c>',
    re.S,
)


def _write_xlsx(path, rows):
    """双表头（第 0 行 F 码、第 1 行中文列名）真实 xlsx，复用导出文件形态。"""
    headers = list(rows[0])
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([f"F{i:07d}" for i in range(1, len(headers) + 1)])
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header) for header in headers])
    workbook.save(path)
    return str(path)


def _rewrite_xlsx(path, mutate, *, add=None):
    """重写 xlsx 成员；mutate(name, data)->bytes，add 为新增成员。"""
    with zipfile.ZipFile(path) as archive:
        items = [
            (info.filename, archive.read(info.filename))
            for info in archive.infolist()
        ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in items:
            archive.writestr(name, mutate(name, data))
        for name, data in (add or {}).items():
            archive.writestr(name, data)


def _patch_numeric_tax_cell(path, header, headers, value):
    """openpyxl 把 float 1.0 序列化成 <v>1</v>；直接改成真实源形态 <v>1.0</v>。

    返回列引用，供断言原 XML 确实不是整数文本。
    """
    ref = f"{get_column_letter(headers.index(header) + 1)}3"

    def mutate(name, data):
        if name != _SHEET_XML:
            return data
        xml = data.decode("utf-8")
        pattern = re.compile(rf'(<c r="{ref}"[^>]*><v>)(-?\d+(?:\.\d+)?)(</v>)')
        xml, count = pattern.subn(
            lambda match: match.group(1) + f"{value:.1f}" + match.group(3), xml
        )
        assert count == 1, ("税标记单元格缺失", ref, count)
        return xml.encode("utf-8")

    _rewrite_xlsx(path, mutate)
    with zipfile.ZipFile(path) as archive:
        xml = archive.read(_SHEET_XML).decode("utf-8")
    assert re.search(rf'<c r="{ref}"[^>]*><v>{value:.1f}</v></c>', xml), (ref, xml)
    return ref


def _force_inline_cell(path, ref, text):
    """把指定文本单元格改写成 inlineStr，真实触发 xlsx_stream 读取路径。"""

    def mutate(name, data):
        if name != _SHEET_XML:
            return data
        xml = data.decode("utf-8")
        match = re.search(rf'<c r="{ref}"[^>]*>.*?</c>', xml, re.S)
        assert match is not None, ("目标单元格缺失", ref)
        attrs = re.match(r'<c r="[^"]+"([^>]*)>', match.group(0)).group(1)
        attrs = re.sub(r'\st="[^"]*"', "", attrs)
        replacement = (
            f'<c r="{ref}"{attrs} t="inlineStr"><is><t>{text}</t></is></c>'
        )
        return (xml[:match.start()] + replacement + xml[match.end():]).encode("utf-8")

    _rewrite_xlsx(path, mutate)


def _convert_inline_strings_to_shared(path):
    """把所有 inlineStr 改写为 sharedStrings，真实触发 pandas/openpyxl 读取路径。"""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        sheet_xml = archive.read(_SHEET_XML).decode("utf-8")
    if "inlineStr" not in sheet_xml:
        assert "xl/sharedStrings.xml" in names, "既非 inlineStr 也缺 sharedStrings"
        return

    strings, index = [], {}

    def replace(match):
        attrs = re.sub(r'\st="inlineStr"', "", match.group("attrs"))
        body = match.group("body")
        if body not in index:
            index[body] = len(strings)
            strings.append(body)
        return (
            f'<c r="{match.group("ref")}"{attrs} t="s">'
            f'<v>{index[body]}</v></c>'
        )

    sheet_xml = _INLINE_CELL.sub(replace, sheet_xml)
    assert "inlineStr" not in sheet_xml
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        f' count="{len(strings)}" uniqueCount="{len(strings)}">'
        + "".join(f"<si>{body}</si>" for body in strings)
        + "</sst>"
    )
    relation = (
        '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/sharedStrings" Target="sharedStrings.xml" Id="rIdSst"/>'
    )
    content_type = (
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    )

    def mutate(name, data):
        if name == _SHEET_XML:
            return sheet_xml.encode("utf-8")
        if name == "xl/_rels/workbook.xml.rels" and b"sharedStrings" not in data:
            return data.replace(
                b"</Relationships>", relation.encode("utf-8") + b"</Relationships>")
        if name == "[Content_Types].xml" and b"sharedStrings.xml" not in data:
            return data.replace(
                b"</Types>", content_type.encode("utf-8") + b"</Types>")
        return data

    _rewrite_xlsx(path, mutate, add={"xl/sharedStrings.xml": shared_xml.encode("utf-8")})


def _numeric_tax_rows(business, value, alias):
    """同单两行：首行税标记为数值单元格 + 同义文本列，次行留空（缺行形态）。"""
    if business == "sales":
        first = _sales_row(**{"是否含税": value, "是否含税(必填)": alias})
        second = _sales_row(**{
            SALES_LINE_ID: "LINE-2", "订单明细.序号": 2,
            "订单明细.产品名称": "TEST-PN-2",
            "订单明细.单价": Decimal("113.00"),
            "订单明细.金额": Decimal("113.00"),
            "是否含税": None, "是否含税(必填)": None,
        })
        return [first, second], "是否含税", "是否含税(必填)", "RAW-HEAD-1"
    first = _purchase_row(**{"是否含税(必填)": value, "是否含税": alias})
    second = _purchase_row(**{
        PURCHASE_LINE_ID: "PL-2", "明细.序号": 2,
        "明细.产品名称(必填)": "TEST-PN-2",
        "明细.单价(必填)": Decimal("113.00"),
        "明细.合计金额": Decimal("113.00"),
        "是否含税(必填)": None, "是否含税": None,
    })
    return [first, second], "是否含税(必填)", "是否含税", "PUR-HEAD-1"


# ---------- DB upsert：坏明细行不冻结/不清空单头 ----------


def _sales_row(**over):
    """一行销售导出（头级字段随行重复，模拟 ffill 前源行）。"""
    row = {
        "订单编号(必填)": "XSDD-HEAD-1", "数据ID(不可修改)": "RAW-HEAD-1",
        "订单日期": "2026-06-30", "客户名称": "旧客户",
        "业务类型#": "备件销售",
        "订单金额": Decimal("113.00"), "是否含税": "含税",
        "不含税金额": Decimal("100.00"), "税率": "13%",
        "税金": Decimal("13.00"), "数据状态": "已生效",
        SALES_LINE_ID: "LINE-1", "订单明细.序号": 1,
        "订单明细.产品名称": "TEST-PN-1", "订单明细.订单数量": 1,
        "订单明细.单价": Decimal("113.00"),
        "订单明细.金额": Decimal("113.00"),
    }
    row.update(over)
    return row


def _purchase_row(**over):
    """一行采购导出（头级字段随行重复，模拟 ffill 前源行）。"""
    row = {
        "采购单号(必填)": "CGDD-HEAD-1", "数据ID(不可修改)": "PUR-HEAD-1",
        "采购日期(必填)": "2026-06-30", "采购人员(必填)": "采购员",
        "供应商(必填)": "旧供应商", "批量采购(必填)": "备货",
        "不含税金额": Decimal("100.00"), "税率(必填)": "13%",
        "是否含税(必填)": "含税", "税金": Decimal("13.00"),
        "采购金额": Decimal("113.00"), "数据状态": "已生效",
        PURCHASE_LINE_ID: "PL-1", "明细.序号": 1,
        "明细.产品名称(必填)": "TEST-PN-1", "明细.采购数量(必填)": 1,
        "明细.单价(必填)": Decimal("113.00"),
        "明细.合计金额": Decimal("113.00"),
    }
    row.update(over)
    return row


# ---------- DB upsert：坏明细行不冻结/不清空单头 ----------

def _existing_document(business):
    return [_sales_row()] if business == "sales" else [_purchase_row()]


def _bad_then_good_document(business, defect):
    """首源行坏 PN/数量且单头字段空白；第二个有效明细行给出单头事实。"""
    if business == "sales":
        bad = _sales_row(**{
            SALES_LINE_ID: "LINE-BAD", "订单明细.序号": 1,
            "客户名称": None, "订单日期": None,
        })
        good = _sales_row(**{
            SALES_LINE_ID: "LINE-2", "订单明细.序号": 2,
            "订单明细.产品名称": "TEST-PN-2",
            "客户名称": "正确客户", "订单日期": "2026-07-01",
        })
        bad_column = ("订单明细.产品名称" if defect == "bad-pn"
                      else "订单明细.订单数量")
    else:
        bad = _purchase_row(**{
            PURCHASE_LINE_ID: "PL-BAD", "明细.序号": 1,
            "供应商(必填)": None, "采购日期(必填)": None,
        })
        good = _purchase_row(**{
            PURCHASE_LINE_ID: "PL-2", "明细.序号": 2,
            "明细.产品名称(必填)": "TEST-PN-2",
            "供应商(必填)": "正确供应商", "采购日期(必填)": "2026-07-05",
        })
        bad_column = ("明细.产品名称(必填)" if defect == "bad-pn"
                      else "明细.采购数量(必填)")
    bad[bad_column] = None if defect == "bad-pn" else "abc"
    return [bad, good]


@pytest.mark.parametrize("business", ["sales", "purchase"])
@pytest.mark.parametrize("defect", ["bad-pn", "bad-qty"])
def test_upsert_head_follows_first_valid_detail_not_bad_row(
    db, tmp_path, business, defect,
):
    """首源行坏 PN/数量且单头空白：库内既有头不得被 NULL 清空，
    应更新为文件中首个有效明细行的客户/供应商与日期。"""
    old_name = f"old-{business}.xlsx"
    pipeline.run_import(
        db, _write_xlsx(tmp_path / old_name, _existing_document(business)),
        old_name)
    db.commit()

    if business == "sales":
        order = db.scalar(select(FSalesOrder).where(
            FSalesOrder.raw_order_id == "RAW-HEAD-1"))
        assert order is not None
        order_id = order.id
        assert order.customer_id is not None
        old_customer = db.scalar(select(DimCustomer.name_raw).where(
            DimCustomer.id == order.customer_id))
        assert (old_customer, order.order_date) == ("旧客户", date(2026, 6, 30))
    else:
        order = db.scalar(select(FPurchaseOrder).where(
            FPurchaseOrder.raw_order_id == "PUR-HEAD-1"))
        assert order is not None
        order_id = order.id
        assert order.supplier_id is not None
        old_supplier = db.scalar(select(DimSupplier.name_raw).where(
            DimSupplier.id == order.supplier_id))
        assert (old_supplier, order.order_date) == ("旧供应商", date(2026, 6, 30))

    new_name = f"fix-{business}-{defect}.xlsx"
    batch = pipeline.run_import(
        db,
        _write_xlsx(tmp_path / new_name, _bad_then_good_document(business, defect)),
        new_name,
        mode="upsert",
    )
    db.commit()
    assert batch.status == "success"

    db.refresh(order)
    assert order.id == order_id                       # 更新既有单，不另建半张单
    assert order.import_batch_id == batch.id
    if business == "sales":
        assert order.customer_id is not None          # 坏行空值不得清空库内头
        customer = db.scalar(select(DimCustomer.name_raw).where(
            DimCustomer.id == order.customer_id))
        assert (customer, order.order_date) == ("正确客户", date(2026, 7, 1))
        assert set(db.scalars(select(FSalesLine.raw_line_id).where(
            FSalesLine.order_id == order.id))) == {"LINE-1", "LINE-2"}
    else:
        assert order.supplier_id is not None
        supplier = db.scalar(select(DimSupplier.name_raw).where(
            DimSupplier.id == order.supplier_id))
        assert (supplier, order.order_date) == ("正确供应商", date(2026, 7, 5))
        assert set(db.scalars(select(FPurchaseLine.raw_line_id).where(
            FPurchaseLine.order_id == order.id))) == {"PL-1", "PL-2"}


# ---------- reader 同义列 × cleaner 含税词表（真实 xlsx） ----------

@pytest.mark.parametrize(
    ("canonical", "alias", "expected"),
    [("含税", "是", True), ("True", "yes", True), ("不含税", "否", False)],
    ids=["han-shui-shi", "true-yes", "bu-han-shui-fou"],
)
def test_sales_tax_flag_alias_vocabulary_is_equivalent(
    tmp_path, canonical, alias, expected,
):
    """销售「是否含税」+「是否含税(必填)」双列：同义词表内等价；
    False（不含税/否）是有效业务值，不得被当空值丢弃。"""
    path = _write_xlsx(
        tmp_path / "sales-flag.xlsx",
        [_sales_row(**{"是否含税": canonical, "是否含税(必填)": alias})],
    )
    prechecked = precheck.inspect_file(path, "sales-flag.xlsx")
    assert prechecked["can_import"] is True

    frame, file_type = reader.read_excel(path)
    result = transform.transform(frame, file_type)

    assert file_type == mapping.SALES
    assert frame["是否含税"].iloc[0] == canonical
    assert "是否含税(必填)" not in frame.columns
    assert result.orders["RAW-HEAD-1"]["is_tax_inclusive"] is expected
    assert not result.errors


@pytest.mark.parametrize(
    ("canonical", "alias", "expected"),
    [("含税", "是", True), ("True", "yes", True), ("不含税", "否", False)],
    ids=["han-shui-shi", "true-yes", "bu-han-shui-fou"],
)
def test_purchase_tax_flag_alias_vocabulary_is_equivalent(
    tmp_path, canonical, alias, expected,
):
    """采购「是否含税(必填)」+「是否含税」双列：同义词表内等价；
    False（不含税/否）是有效业务值，不得被当空值丢弃。"""
    path = _write_xlsx(
        tmp_path / "purchase-flag.xlsx",
        [_purchase_row(**{"是否含税(必填)": canonical, "是否含税": alias})],
    )
    prechecked = precheck.inspect_file(path, "purchase-flag.xlsx")
    assert prechecked["can_import"] is True

    frame, file_type = reader.read_excel(path)
    result = transform.transform(frame, file_type)

    assert file_type == mapping.PURCHASE
    assert frame["是否含税(必填)"].iloc[0] == canonical
    assert "是否含税" not in frame.columns
    assert result.orders["PUR-HEAD-1"]["is_tax_inclusive"] is expected
    assert not result.errors


@pytest.mark.parametrize(
    ("canonical", "alias"),
    [("含税", "否"), ("否", "含税"), ("待确认", "含税"), ("含税", "待确认")],
    ids=["opposite-target", "opposite-alias", "unknown-target", "unknown-alias"],
)
def test_sales_tax_flag_alias_conflict_or_unknown_is_rejected(
    tmp_path, canonical, alias,
):
    """相反旗标与未知非空都不能当空值让另一列补位：预检与读取都整批拒绝。"""
    path = _write_xlsx(
        tmp_path / "sales-flag-conflict.xlsx",
        [_sales_row(**{"是否含税": canonical, "是否含税(必填)": alias})],
    )
    prechecked = precheck.inspect_file(path, "sales-flag-conflict.xlsx")
    assert prechecked["can_import"] is False
    issue = next(
        issue for sheet in prechecked["sheets"] for issue in sheet["issues"]
        if issue["code"] == "alias_column_conflict")
    assert issue["severity"] == "error"

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"
    assert canonical in str(exc_info.value)
    assert alias in str(exc_info.value)


@pytest.mark.parametrize(
    ("canonical", "alias"),
    [("含税", "否"), ("否", "含税"), ("待确认", "含税"), ("含税", "待确认")],
    ids=["opposite-target", "opposite-alias", "unknown-target", "unknown-alias"],
)
def test_purchase_tax_flag_alias_conflict_or_unknown_is_rejected(
    tmp_path, canonical, alias,
):
    """采购双列同样拒绝相反旗标与未知非空，不按列序静默取一个。"""
    path = _write_xlsx(
        tmp_path / "purchase-flag-conflict.xlsx",
        [_purchase_row(**{"是否含税(必填)": canonical, "是否含税": alias})],
    )
    prechecked = precheck.inspect_file(path, "purchase-flag-conflict.xlsx")
    assert prechecked["can_import"] is False
    issue = next(
        issue for sheet in prechecked["sheets"] for issue in sheet["issues"]
        if issue["code"] == "alias_column_conflict")
    assert issue["severity"] == "error"

    with pytest.raises(reader.ReaderError) as exc_info:
        reader.read_excel(path)
    assert exc_info.value.code == "alias_column_conflict"
    assert canonical in str(exc_info.value)
    assert alias in str(exc_info.value)


def test_sales_tax_flag_blank_target_is_filled_by_legal_alias(tmp_path):
    """空白规范列由合法别名补位；补进来的 False 仍是有效业务值（A 合并边界）。"""
    path = _write_xlsx(
        tmp_path / "sales-flag-blank.xlsx",
        [_sales_row(**{"是否含税": None, "是否含税(必填)": "否"})],
    )
    frame, file_type = reader.read_excel(path)
    result = transform.transform(frame, file_type)

    assert frame["是否含税"].iloc[0] == "否"
    assert result.orders["RAW-HEAD-1"]["is_tax_inclusive"] is False
    assert not result.errors


@pytest.mark.parametrize("business", ["sales", "purchase"])
@pytest.mark.parametrize(
    ("value", "alias", "expected"),
    [(1.0, "是", True), (0.0, "否", False)],
    ids=["numeric-1.0", "numeric-0.0"],
)
@pytest.mark.parametrize(
    "storage", ["shared", "inline", "mixed"], ids=["shared", "inline", "mixed"],
)
def test_numeric_tax_flag_xml_matches_alias_across_readers(
    tmp_path, business, value, alias, expected, storage,
):
    """真实 XLSX：源 <v> 为 1.0/0.0 时，三种真实存储布局都必须把它当受控税标记，
    并与同义文本列（是/否）一致；次行留空覆盖缺行。

    三种布局：全部 sharedStrings（openpyxl 读取入口）；全部 inlineStr
    （xlsx_stream 入口）；sharedStrings 与个别 inlineStr 混合（同样走
    xlsx_stream，但同表 t="s" 与 t="inlineStr" 并存，别名列须逐格解析）。

    openpyxl 写 float 常序列化为 <v>1</v>，这里直接改回真实源形态并断言原 XML，
    避免「夹具其实写的是整数」的假覆盖。
    """
    filename = f"{business}-numeric-tax-{storage}.xlsx"
    rows, canonical, alias_column, raw_id = _numeric_tax_rows(business, value, alias)
    path = _write_xlsx(tmp_path / filename, rows)
    headers = list(rows[0])
    ref = _patch_numeric_tax_cell(path, canonical, headers, value)
    alias_ref = f"{get_column_letter(headers.index(alias_column) + 1)}3"
    if storage == "shared":
        _convert_inline_strings_to_shared(path)
    elif storage == "mixed":
        _convert_inline_strings_to_shared(path)
        _force_inline_cell(path, alias_ref, alias)
    else:
        _force_inline_cell(path, alias_ref, alias)

    with zipfile.ZipFile(path) as archive:
        xml = archive.read(_SHEET_XML).decode("utf-8")
        assert re.search(rf'<c r="{ref}"[^>]*><v>{value:.1f}</v></c>', xml)
        if storage == "shared":
            assert "inlineStr" not in xml
            assert "xl/sharedStrings.xml" in archive.namelist()
        elif storage == "mixed":
            assert 't="s"' in xml
            assert 't="inlineStr"' in xml
            assert re.search(rf'<c r="{alias_ref}"[^>]*t="inlineStr"', xml)
            assert "xl/sharedStrings.xml" in archive.namelist()
        else:
            assert 't="inlineStr"' in xml

    prechecked = precheck.inspect_file(path, filename)
    assert prechecked["can_import"] is True

    frame, file_type = reader.read_excel(path)
    assert file_type == (
        mapping.SALES if business == "sales" else mapping.PURCHASE)
    cell = frame[canonical].iloc[0]
    assert not isinstance(cell, str)          # 真实数值单元格，不是文本 "1.0"
    assert cell == value
    assert cleaner.parse_tax_inclusive(cell) is expected

    result = transform.transform(frame, file_type)
    assert result.orders[raw_id]["is_tax_inclusive"] is expected
    assert not result.errors
