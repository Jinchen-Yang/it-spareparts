"""列名容差归一：氚云非标导出视图让列丢 (必填) 注解（实测 #25 采购订单整文件 empty_pn）。"""
import openpyxl
import pytest
from sqlalchemy import select

from app.etl import mapping, pipeline, reader, transform
from app.models.sales import FSalesOrder


def test_canonicalize_columns():
    # (必填) 容差：裸列名归一到规范键
    assert mapping.canonicalize_columns(["采购单号", "明细.产品名称", "供应商编码#"]) == \
        ["采购单号(必填)", "明细.产品名称(必填)", "供应商编码#"]
    # 标准导出零改动（已是规范键，走精确分支）
    assert mapping.canonicalize_columns(["明细.产品名称(必填)"]) == ["明细.产品名称(必填)"]
    # (PN) 等语义后缀不被剥
    assert mapping.canonicalize_columns(["产品名称(PN)"]) == ["产品名称(PN)"]


def test_canonicalize_no_duplicate_columns():
    # 裸名与规范名并存 → 只认规范名、不造重复列
    out = mapping.canonicalize_columns(["明细.产品名称", "明细.产品名称(必填)"])
    assert out.count("明细.产品名称(必填)") == 1
    # 产品名称 与 产品名称#（#25 实况：两列并存）→ 不冲突，# 列原样保留
    out = mapping.canonicalize_columns(["明细.产品名称", "明细.产品名称#"])
    assert out.count("明细.产品名称(必填)") == 1 and "明细.产品名称#" in out


def test_detect_file_type_tolerant():
    assert mapping.detect_file_type(["采购单号", "明细.产品名称"]) == mapping.PURCHASE
    assert mapping.detect_file_type(["采购单号(必填)"]) == mapping.PURCHASE
    assert mapping.detect_file_type(["订单编号", "业务类型"]) == mapping.SALES        # 无 必填/# 也认
    assert mapping.detect_file_type(["订单编号(必填)", "业务类型#"]) == mapping.SALES
    assert mapping.detect_file_type(["产品库存ID"]) == mapping.INVENTORY
    assert mapping.detect_file_type(["库存数量", "产品名称(PN)"]) == mapping.INVENTORY
    assert mapping.detect_file_type(["乱七八糟", "随便"]) is None


def _purchase_xlsx_bare(tmp_path):
    """双表头采购文件，但中文列名是「裸」的（无 (必填)）——复刻 #25 故障形态。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([f"F000000{i}" for i in range(1, 8)])              # 第0行 F码 → 双表头
    ws.append(["采购单号", "数据ID(不可修改)", "明细.数据ID(不可修改)",
               "明细.产品名称", "明细.采购数量", "明细.单价", "供应商"])  # 第1行 裸中文名
    ws.append(["CGDD-1", "O1", "L1", "ST8000NM000A", "2", "100", "测试供应商"])
    p = tmp_path / "purchase_bare.xlsx"
    wb.save(p)
    return str(p)


def test_reader_canonicalizes_bare_headers(tmp_path):
    df, ft = reader.read_excel(_purchase_xlsx_bare(tmp_path))
    assert ft == mapping.PURCHASE
    # 裸列名已归一为规范键，PN 列有值（不再整列空 → 不会全行 empty_pn）
    assert "明细.产品名称(必填)" in df.columns
    assert "采购单号(必填)" in df.columns
    assert df["明细.产品名称(必填)"].iloc[0] == "ST8000NM000A"


def _sales_xlsx(tmp_path, business_type_header, double_header, raw_order_id, business_type):
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = [
        "订单编号", "数据ID(不可修改)", business_type_header,
        "订单明细.数据ID(不可修改)", "订单明细.产品名称",
        "订单明细.订单数量", "订单明细.单价", "订单明细.金额",
    ]
    if double_header:
        ws.append([f"F000000{i}" for i in range(1, len(headers) + 1)])
    ws.append(headers)
    ws.append([
        f"XSDD-{raw_order_id}", raw_order_id, business_type,
        f"{raw_order_id}-LINE", "ST8000NM000A", "1", "100", "100",
    ])
    path = tmp_path / f"{raw_order_id}.xlsx"
    wb.save(path)
    return str(path)


def _sales_xlsx_with_business_type_aliases(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = [
        "订单编号", "数据ID(不可修改)", "业务类型#", "业务类型",
        "订单明细.数据ID(不可修改)", "订单明细.产品名称",
        "订单明细.订单数量", "订单明细.单价", "订单明细.金额",
    ]
    ws.append([f"F000000{i}" for i in range(1, len(headers) + 1)])
    ws.append(headers)
    ws.append([
        "XSDD-SALES-A", "SALES-A", "标准A", "裸A",
        "SALES-A-LINE", "ST8000NM000A", "1", "100", "100",
    ])
    ws.append([
        "XSDD-SALES-B", "SALES-B", None, "裸B",
        "SALES-B-LINE-1", "ST8000NM000B", "1", "200", "200",
    ])
    ws.append([
        None, None, None, None,
        "SALES-B-LINE-2", "ST8000NM000C", "1", "300", "300",
    ])
    path = tmp_path / "sales-business-type-aliases.xlsx"
    wb.save(path)
    return str(path)


@pytest.mark.parametrize(
    ("business_type_header", "double_header", "raw_order_id", "business_type"),
    [
        ("业务类型", False, "SALES-BARE", "备件销售"),
        ("业务类型#", True, "SALES-TRITIUM", "整机销售"),
    ],
)
def test_sales_business_type_survives_transform_and_load(
    db, tmp_path, business_type_header, double_header, raw_order_id, business_type,
):
    path = _sales_xlsx(
        tmp_path, business_type_header, double_header, raw_order_id, business_type,
    )
    df, file_type = reader.read_excel(path)
    result = transform.transform(df, file_type)

    assert file_type == mapping.SALES
    assert "业务类型#" in df.columns
    assert "业务类型" not in df.columns
    assert df["业务类型#"].tolist() == [business_type]
    assert result.orders[raw_order_id]["business_type"] == business_type

    pipeline.run_import(db, path, f"{raw_order_id}.xlsx")
    loaded = db.scalar(
        select(FSalesOrder).where(FSalesOrder.raw_order_id == raw_order_id)
    )
    assert loaded is not None
    assert loaded.business_type == business_type


def test_sales_business_type_aliases_coalesce_before_ffill(db, tmp_path):
    path = _sales_xlsx_with_business_type_aliases(tmp_path)
    df, file_type = reader.read_excel(path)
    result = transform.transform(df, file_type)

    assert file_type == mapping.SALES
    assert "业务类型" not in df.columns
    assert df["业务类型#"].tolist() == ["标准A", "裸B", "裸B"]
    assert result.orders["SALES-A"]["business_type"] == "标准A"
    assert result.orders["SALES-B"]["business_type"] == "裸B"

    pipeline.run_import(db, path, "sales-business-type-aliases.xlsx")
    loaded = dict(db.execute(
        select(FSalesOrder.raw_order_id, FSalesOrder.business_type).where(
            FSalesOrder.raw_order_id.in_(["SALES-A", "SALES-B"])
        )
    ).all())
    assert loaded == {"SALES-A": "标准A", "SALES-B": "裸B"}
