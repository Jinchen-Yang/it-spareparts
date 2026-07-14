"""看板 UI v2：订单列表全局筛选（part_id / pool_group_id / purchaser）。

口径：整单召回——订单只要**含**目标型号/池成员即整单返回，聚合值（型号数/总量/金额）
仍是整单口径，不因筛选缩水成命中行。
"""
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import dashboard, pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def seeded(db):
    """PN-A、PN-B 同池；PN-C 池外。
    销售：S1=(PN-A + PN-C)、S2=(PN-B)、S3=(PN-C)。
    采购：P1=(PN-A，张三)、P2=(PN-C，李四)。"""
    a = DimPart(pn_std="PN-A", brand="BA")
    bp = DimPart(pn_std="PN-B", brand="BB")
    c = DimPart(pn_std="PN-C", brand="BC")
    db.add_all([a, bp, c]); db.flush()
    created = pool_catalog.create_pool(db, name="池-AB", member_part_ids=[a.id, bp.id],
                                       operated_by="t")
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hof")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), purchaser="张三", is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 6), purchaser="李四", is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "PL1", "PN-A", qty="10", price="113"),
          f.purchase_line("P2", "PL2", "PN-C", qty="5", price="113")]
    loader.load(db, f.purchase_result(po, pl), b.id, AS_OF)
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1)),
          "S2": f.sales_head("S2", on=date(2026, 2, 5)),
          "S3": f.sales_head("S3", on=date(2026, 3, 1))}
    sl = [f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),
          f.sales_line("S1", "SL2", "PN-C", qty="2", price="113"),
          f.sales_line("S2", "SL3", "PN-B", qty="1", price="226"),
          f.sales_line("S3", "SL4", "PN-C", qty="1", price="113")]
    loader.load(db, f.sales_result(so, sl), b.id, AS_OF)
    db.commit(); profit.recompute(db)
    ids = {p.pn_std: p.id for p in db.execute(select(DimPart)).scalars()}
    return {"gid": created["group_id"], "ids": ids}


def _nos(d):
    return sorted(i["order_no"] for i in d["items"])


def test_sales_part_filter_whole_order(db, seeded):
    d = dashboard.sales_orders(db, part_id=seeded["ids"]["PN-A"], as_of=AS_OF)
    assert _nos(d) == ["S1"] and d["total"] == 1
    row = d["items"][0]
    # 整单口径：S1 含 PN-A + PN-C 两个型号，聚合不因筛选缩水
    assert row["part_count"] == 2
    assert row["total_qty"] == 3.0
    assert {p["pn_std"] for p in row["parts"]} == {"PN-A", "PN-C"}


def test_sales_pool_filter(db, seeded):
    d = dashboard.sales_orders(db, pool_group_id=seeded["gid"], as_of=AS_OF)
    # S1 含池成员 PN-A、S2 含 PN-B；S3 只有池外 PN-C → 不召回
    assert _nos(d) == ["S1", "S2"]


def test_sales_part_and_pool_combined(db, seeded):
    d = dashboard.sales_orders(db, part_id=seeded["ids"]["PN-B"],
                               pool_group_id=seeded["gid"], as_of=AS_OF)
    assert _nos(d) == ["S2"]


def test_sales_filter_with_time_window(db, seeded):
    d = dashboard.sales_orders(db, date_from=date(2026, 2, 3), date_to=date(2026, 2, 28),
                               pool_group_id=seeded["gid"], as_of=AS_OF)
    assert _nos(d) == ["S2"]


def test_purchase_part_filter(db, seeded):
    d = dashboard.purchase_orders(db, part_id=seeded["ids"]["PN-A"], as_of=AS_OF)
    assert _nos(d) == ["P1"]


def test_purchase_pool_filter(db, seeded):
    d = dashboard.purchase_orders(db, pool_group_id=seeded["gid"], as_of=AS_OF)
    assert _nos(d) == ["P1"]      # P2 只有池外 PN-C


def test_purchase_purchaser_filter(db, seeded):
    d = dashboard.purchase_orders(db, purchaser="李", as_of=AS_OF)
    assert _nos(d) == ["P2"]
    assert d["items"][0]["purchaser"] == "李四"


def test_no_match_returns_empty(db, seeded):
    d = dashboard.sales_orders(db, part_id=999999, as_of=AS_OF)
    assert d["total"] == 0 and d["items"] == []
