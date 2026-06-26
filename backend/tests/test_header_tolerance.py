"""列名容差归一：氚云非标导出视图让列丢 (必填) 注解（实测 #25 采购订单整文件 empty_pn）。"""
import openpyxl

from app.etl import mapping, reader


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
