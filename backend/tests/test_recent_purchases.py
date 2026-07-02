"""全局采购记录查询（合同重点）：时间窗 / 关键词 / 供应商 / 合并后 canonical 口径。"""
from datetime import date, timedelta

import pytest

from app.etl import loader
from app.models.system import SysImportBatch
from app.services import merge, purchase_query
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hp1")
    db.add(b)
    db.flush()
    return b


def _import(db, batch, lines, orders):
    res = loader.load(db, f.purchase_result(orders, lines), batch.id,
                      date(2026, 6, 1), mode="skip")
    db.commit()
    return res


def _seed(db, batch):
    today = date.today()
    orders = {
        "O-NEW": f.purchase_head("O-NEW", on=today - timedelta(days=3)),
        "O-MID": f.purchase_head("O-MID", on=today - timedelta(days=45)),
        "O-OLD": f.purchase_head("O-OLD", on=today - timedelta(days=400)),
    }
    lines = [
        f.purchase_line("O-NEW", "L-NEW", "PN-FRESH", qty="2", price="100",
                        description="新进的盘", brand="Seagate"),
        f.purchase_line("O-MID", "L-MID", "PN-MID", qty="1", price="50",
                        description="中期内存", brand="Samsung"),
        f.purchase_line("O-OLD", "L-OLD", "PN-ANCIENT", qty="3", price="10",
                        description="一年前的旧货"),
    ]
    _import(db, batch, lines, orders)


def test_days_window_filters(db, batch):
    _seed(db, batch)
    d30 = purchase_query.recent_purchases(db, days=30)
    assert d30["total"] == 1
    assert d30["items"][0]["pn_std"] == "PN-FRESH"

    d90 = purchase_query.recent_purchases(db, days=90)
    assert d90["total"] == 2
    # 按日期倒序：最近的在前
    assert [r["pn_std"] for r in d90["items"]] == ["PN-FRESH", "PN-MID"]

    dall = purchase_query.recent_purchases(db, days=3660)
    assert dall["total"] == 3


def test_keyword_and_supplier_filter(db, batch):
    _seed(db, batch)
    by_desc = purchase_query.recent_purchases(db, q="内存", days=365)
    assert by_desc["total"] == 1 and by_desc["items"][0]["pn_std"] == "PN-MID"

    by_pn = purchase_query.recent_purchases(db, q="pn-fresh", days=365)
    assert by_pn["total"] == 1

    by_brand = purchase_query.recent_purchases(db, q="seagate", days=365)
    assert by_brand["total"] == 1 and by_brand["items"][0]["brand"] == "Seagate"

    by_supplier = purchase_query.recent_purchases(db, supplier="测试供应商", days=3660)
    assert by_supplier["total"] == 3
    none_supplier = purchase_query.recent_purchases(db, supplier="不存在的供应商", days=365)
    assert none_supplier["total"] == 0


def test_merged_part_shows_canonical_pn(db, batch):
    """part_id 主口径：合并后历史采购行展示存活型号，不再露墓碑 pn。"""
    today = date.today()
    orders = {
        "O-A": f.purchase_head("O-A", on=today - timedelta(days=2)),
        "O-B": f.purchase_head("O-B", on=today - timedelta(days=1)),
    }
    lines = [
        f.purchase_line("O-A", "L-A", "PN-DUP-OLD", description="同款旧写法"),
        f.purchase_line("O-B", "L-B", "PN-DUP-NEW", description="同款新写法"),
    ]
    _import(db, batch, lines, orders)
    merge.merge_parts(db, "PN-DUP-OLD", "PN-DUP-NEW", "同款", "tester")
    db.commit()

    out = purchase_query.recent_purchases(db, days=30)
    pns = {r["pn_std"] for r in out["items"]}
    assert pns == {"PN-DUP-NEW"}, f"合并后应全部显示存活型号，实际: {pns}"
    assert out["total"] == 2


def test_alias_recall_old_pn_finds_merged_rows(db, batch):
    """按单据原文/已合并旧 PN 查询：经 part_alias 召回（仍是 part_id 主口径）。"""
    today = date.today()
    orders = {
        "O-A2": f.purchase_head("O-A2", on=today - timedelta(days=2)),
        "O-B2": f.purchase_head("O-B2", on=today - timedelta(days=1)),
    }
    lines = [
        f.purchase_line("O-A2", "L-A2", "PN-ALIAS-OLD"),
        f.purchase_line("O-B2", "L-B2", "PN-ALIAS-NEW"),
    ]
    _import(db, batch, lines, orders)
    merge.merge_parts(db, "PN-ALIAS-OLD", "PN-ALIAS-NEW", "同款", "tester")
    db.commit()

    # 拿旧 PN（现在只存在于 part_alias.pn_raw）查：两行都应命中（同一商品）
    out = purchase_query.recent_purchases(db, q="PN-ALIAS-OLD", days=30)
    assert out["total"] == 2
    assert {r["pn_std"] for r in out["items"]} == {"PN-ALIAS-NEW"}


def test_pagination_and_caps(db, batch):
    _seed(db, batch)
    p1 = purchase_query.recent_purchases(db, days=3660, page=1, page_size=2)
    p2 = purchase_query.recent_purchases(db, days=3660, page=2, page_size=2)
    assert len(p1["items"]) == 2 and len(p2["items"]) == 1
    assert p1["total"] == p2["total"] == 3
    # days 防呆：超上限被钳制而非报错
    capped = purchase_query.recent_purchases(db, days=999999)
    assert capped["days"] == purchase_query.MAX_DAYS


def test_master_description_recall_and_token_and(db, batch):
    """批量规范化只改主数据描述、单据行保留原文——拿标准描述查采购记录必须经主数据召回；
    多关键词为 AND 语义（同一行全部命中），词序无关。"""
    from app.models.dimensions import DimPart

    _seed(db, batch)
    part = db.query(DimPart).filter_by(pn_std="PN-FRESH").one()
    part.description = "8TB 6Gb/s 7.2K 256MB Cache 3.5-inch SATA HDD"
    db.commit()

    # 标准描述的规格词组合 → 经 DimPart.description 召回单据行（行原文是"新进的盘"）
    r = purchase_query.recent_purchases(db, q="8TB 7.2K SATA HDD", days=365)
    assert r["total"] == 1 and r["items"][0]["pn_std"] == "PN-FRESH"
    # 词序无关：品牌 + 行原文描述词
    assert purchase_query.recent_purchases(db, q="Seagate 新进", days=365)["total"] == 1
    # AND 语义：两词分属不同行 → 不命中
    assert purchase_query.recent_purchases(db, q="Seagate 中期", days=365)["total"] == 0
