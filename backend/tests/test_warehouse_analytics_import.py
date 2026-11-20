from datetime import date, datetime
from decimal import Decimal

from openpyxl import Workbook, load_workbook
import pytest

from app.services.warehouse_analytics_import import parse_warehouse_workbook


DIRECT_HEADERS = [
    "退货单号(必填)", "数据ID(不可修改)", "退货日期", "修改时间(必填)", "数据状态", "退货类型", "退货人员",
    "退货明细.数据ID(不可修改)", "退货明细.产品名称", "退货明细.产品描述", "退货明细.退货数量",
    "退货明细.单位", "退货明细.产品大类", "退货明细.品牌",
]
STAMP = datetime(2026, 1, 5, 17, 25)


def direct_book(path, rows, headers=DIRECT_HEADERS):
    book = Workbook()
    sheet = book.active
    sheet.title = "直采直退"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.save(path)
    book.close()
    return path


def doc(number="待测01", ident="head1", when=STAMP, line="line1", qty=1):
    return [number, ident, date(2026, 1, 5), when, "已生效", "销售订单", "退货甲",
            line, "PN-A", "合成备件甲", qty, "件", "大类甲", "品牌甲"]


def follow(line="line2", qty=None):
    return [None] * 7 + [line, "PN-B", None, qty, "套", None, None]


def test_parses_merged_header_and_preserves_missing_quantity(tmp_path):
    path = direct_book(tmp_path / "direct.xlsx", [doc(qty=0), follow()])
    book = load_workbook(path)
    for column in "ABCDEFG":
        book.active.merge_cells(f"{column}2:{column}3")
    book.save(path)
    book.close()
    parsed = parse_warehouse_workbook(path)
    records = parsed["direct"]
    assert len(records) == 1
    assert records[0].head["operator"] == "退货甲"
    assert records[0].head["business_date"] == date(2026, 1, 5)
    assert records[0].head["source_modified_at"] == STAMP
    assert records[0].lines["line1"]["quantity"] == Decimal(0)
    assert records[0].lines["line2"]["quantity"] is None


@pytest.mark.parametrize(("rows", "error"), [
    ([follow()], "所属单头"),
    ([doc(), follow("line1")], "明细源 ID"),
    ([doc(), doc(number="待测02", line="line3")], "重复单头"),
    ([doc(), doc(ident="head2", line="line3")], "重复单头"),
    ([doc(qty="1.0000001")], "精确表示"),
    ([doc(qty="100000000000000")], "精确表示"),
    ([doc(qty="未知")], "精确表示"),
    ([doc(when="不详")], "本地时间无效"),
])
def test_rejects_broken_identity_or_invalid_fields(tmp_path, rows, error):
    from app.services.warehouse_analytics_import import WarehouseImportError

    with pytest.raises(WarehouseImportError, match=error):
        parse_warehouse_workbook(direct_book(tmp_path / "invalid.xlsx", rows))


def test_same_workbook_retry_and_newer_replacement_preserve_history(db, tmp_path):
    from sqlalchemy import select
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Doc, WarehouseAnalyticsLine as Line
    from app.services.warehouse_analytics_import import apply_warehouse_workbook

    def apply(rows):
        parsed = parse_warehouse_workbook(direct_book(tmp_path / "load.xlsx", rows))
        result = apply_warehouse_workbook(db, parsed)
        db.commit()
        return result

    rows = [doc(), follow(), doc(number="待测02", ident="head2", line="line3")]
    assert apply(rows)["inserted"] == 2
    ids = db.execute(select(Line.source_id, Line.id)).all()
    assert apply(rows)["unchanged"] == 2
    assert set(db.execute(select(Line.source_id, Line.id)).all()) == set(ids)
    changed = doc(when=datetime(2026, 1, 6, 10), qty=3)
    changed[4] = "已取消"
    assert apply([changed])["updated"] == 1
    assert set(db.execute(select(Doc.source_id, Doc.status))) == {("head1", "已取消"), ("head2", "已生效")}
    assert set(db.execute(select(Line.source_id, Line.quantity))) == {("line1", Decimal(3)), ("line3", Decimal(1))}


def test_only_named_sources_and_saved_values_are_read(tmp_path):
    path = direct_book(tmp_path / "technical.xlsx", [])
    wb = load_workbook(path)
    sheet = wb.active
    sheet.delete_rows(1)
    sheet.append(["SeqNo", "ObjectId", "F1", "ModifiedTime", "Status", "F2", "F3",
                  "Child.ObjectId", "Child.F1", "Child.F2", "Child.F3", "Child.F4", "Child.F5", "Child.F6"])
    sheet.append(DIRECT_HEADERS)
    sheet.append(doc())
    sheet["J3"] = "=1+2"  # openpyxl 没有保存计算缓存，导入时该可选描述为空。
    wb.create_sheet("重点保留").append(["样本备注"])
    hidden = wb.create_sheet("Hidden_Options_3")
    hidden.sheet_state = "veryHidden"
    wb.save(path)
    wb.close()
    parsed = parse_warehouse_workbook(path)
    assert list(parsed) == ["direct"]
    assert parsed["direct"][0].lines["line1"]["description"] is None

    wb = load_workbook(path)
    wb.active["B1"] = "Other"
    wb.active["C1"] = "ObjectId"
    wb.save(path)
    wb.close()
    with pytest.raises(ValueError, match="技术表头"):
        parse_warehouse_workbook(path)

    path = direct_book(path, [doc()])
    wb = load_workbook(path)
    wb.create_sheet("出库单").append(["待核验来源"])
    wb.save(path)
    wb.close()
    with pytest.raises(ValueError, match="尚未支持"):
        parse_warehouse_workbook(path)


def test_same_version_conflict_or_stale_doc_rolls_back_whole_workbook(db, tmp_path):
    from sqlalchemy import select
    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Doc, WarehouseAnalyticsLine as Line
    from app.services.warehouse_analytics_import import apply_warehouse_workbook, WarehouseImportError

    def parsed(rows):
        return parse_warehouse_workbook(direct_book(tmp_path / "versions.xlsx", rows))

    apply_warehouse_workbook(db, parsed([doc(), follow()]))
    db.commit()
    # 400 单的前一批已发出 INSERT，最后一批才会发现旧单。同事务内不能漏写前一批。
    preceding = [doc(number=f"合成{i}", ident=f"批次{i}", line=f"明细{i}") for i in range(401)]
    with pytest.raises(WarehouseImportError, match="早于"):
        apply_warehouse_workbook(db, parsed(preceding + [doc(when=datetime(2026, 1, 4, 12))]))
    db.commit()
    assert db.scalars(select(Doc.source_id)).all() == ["head1"]
    for snapshot in [[doc()], [doc(qty=7), follow()]]:
        with pytest.raises(WarehouseImportError, match="同一源修改时间"):
            apply_warehouse_workbook(db, parsed(snapshot))
    assert set(db.execute(select(Line.source_id, Line.quantity))) == {("line1", Decimal(1)), ("line2", None)}


def test_historical_line_id_cannot_move_even_when_old_parent_replaces_it(db, tmp_path):
    from sqlalchemy import select
    from app.models.warehouse_analytics import WarehouseAnalyticsLine as Line
    from app.services.warehouse_analytics_import import apply_warehouse_workbook, WarehouseImportError

    path = tmp_path / "identity.xlsx"
    apply_warehouse_workbook(db, parse_warehouse_workbook(direct_book(path, [doc(), follow()])))
    db.commit()
    changed = [doc(when=datetime(2026, 1, 6, 10)), doc(number="待测02", ident="head2", line="line2")]
    with pytest.raises(WarehouseImportError, match="移动"):
        apply_warehouse_workbook(db, parse_warehouse_workbook(direct_book(path, changed)))
    db.commit()
    assert len(set(db.scalars(select(Line.document_id)))) == 1


def test_concurrent_retry_commits_one_coherent_version(db, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.warehouse_analytics import WarehouseAnalyticsDocument as Doc, WarehouseAnalyticsLine as Line
    from app.services.warehouse_analytics_import import apply_warehouse_workbook

    parsed = parse_warehouse_workbook(direct_book(tmp_path / "same.xlsx", [doc(qty=2), follow(qty=3)]))
    ready = Barrier(2)
    engine = db.get_bind()

    def submit():
        with Session(engine) as worker, worker.begin():
            ready.wait(timeout=10)
            return apply_warehouse_workbook(worker, parsed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(submit) for _ in range(2)]
        reports = [r.result(timeout=30) for r in pending]
    assert sorted(r["inserted"] for r in reports) == [0, 1]
    assert sorted(r["unchanged"] for r in reports) == [0, 1]
    assert db.scalars(select(Doc.number)).all() == ["待测01"]
    assert set(db.execute(select(Line.source_id, Line.quantity))) == {("line1", Decimal(2)), ("line2", Decimal(3))}


def test_return_warehouse_bin_and_roles_are_selected_from_confirmed_columns(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = "返库单"
    headers = ["数据ID(不可修改)", "修改时间(必填)", "数据状态", "是否需测试", "测试完成", "测试人员",
               "测试完成日期", "整机描述", "整机单位", "返库单号(必填)", "返库日期", "返库类别", "仓储中心",
               "拣货人员", "整机PN", "整机返库数", "备件明细.数据ID(不可修改)", "备件明细.备件PN(必填)",
               "备件明细.单位", "备件明细.产品描述", "备件明细.返库数量", "备件明细.产品大类",
               "备件明细.产品品牌", "备件明细.入库库位", "备件明细.入库仓库", "备件明细.入库库位",
               "备件明细.备件测试结果", "仓管员", "本仓测试人员"]
    row = ["h1", STAMP, "已生效", "是", "是", None, date(2026, 1, 6), "整机说明", "台", "待测01",
           date(2026, 1, 5), "销售退货", "中心甲", "拣货甲", "整机A", 1, "l1", "PN-A", "件", "备件A",
           2.5, "大类甲", "品牌甲", "库位编码", "库房甲", "可读库位", "坏品", "候选甲;候选乙", "候选测试甲"]
    sheet.append(headers)
    sheet.append(row)
    path = tmp_path / "return.xlsx"
    book.save(path)
    book.close()
    record, = parse_warehouse_workbook(path)["return"]
    assert record.head["operator"] == "拣货甲"
    assert record.head["tester"] is None
    assert record.head["tested"] is True
    assert record.head["whole_qty"] == Decimal(1)
    assert record.lines["l1"]["bin_name"] == "可读库位"
    assert record.lines["l1"]["quantity"] == Decimal("2.5")

    # 剩下的同名列是编码，不能在可读列遗失时错认。
    book = load_workbook(path)
    book.active.delete_cols(26)
    book.save(path)
    book.close()
    with pytest.raises(ValueError, match="库位"):
        parse_warehouse_workbook(path)


def test_unselected_source_columns_still_detect_incomplete_identities(tmp_path):
    headers = DIRECT_HEADERS + ["退货明细.退货产品SN", "创建人(必填)"]
    # 有业务明细（仅 SN），不能当作无明细的合法单头。
    head_only = doc()[:7] + [None] * 7
    path = direct_book(tmp_path / "broken-child.xlsx", [head_only + ["合成SN", None]], headers)
    with pytest.raises(ValueError, match="明细源 ID"):
        parse_warehouse_workbook(path)
    # 单头信息落在所谓“续行”中，不能把该行悄悄归给上一单。
    path = direct_book(tmp_path / "broken-parent.xlsx", [doc(), follow() + [None, "新建人"]], headers)
    with pytest.raises(ValueError, match="单头缺少"):
        parse_warehouse_workbook(path)
