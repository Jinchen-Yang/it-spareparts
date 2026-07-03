"""锚定动态库存（2026-07-04 甲方口径）：快照期初 + 快照日后流水；8月盘点导入即自动变准。"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.models.system import SysImportBatch
from app.services import inventory as inv_svc
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hdyn")
    db.add(b)
    db.flush()
    return b


def _pid(db, pn):
    return db.scalar(select(DimPart.id).where(DimPart.pn_std == pn))


def test_anchored_dynamic_stock(db, batch):
    # 锚点前采购（不计）：5/20 进 100
    loader.load(db, f.purchase_result(
        {"P0": f.purchase_head("P0", on=date(2026, 5, 20))},
        [f.purchase_line("P0", "PL0", "PN-DYN", qty="100", price="10")]), batch.id, date(2026, 6, 1))
    pid = _pid(db, "PN-DYN")
    # 期初快照 6/1：北京 5 + 上海(人工修正 7→5) = 10
    db.add(Inventory(raw_inventory_id="DYN1", part_id=pid, pn_std="PN-DYN", warehouse="北京成品仓",
                     source_qty=Decimal("5"), snapshot_date=date(2026, 6, 1)))
    db.add(Inventory(raw_inventory_id="DYN2", part_id=pid, pn_std="PN-DYN", warehouse="上海成品仓",
                     source_qty=Decimal("7"), manual_qty=Decimal("5"), is_qty_overridden=True,
                     snapshot_date=date(2026, 6, 1)))
    # 锚点后流水：采购 +3、销售 −2、维保 出2退1 → −1、未来日期销售（不计）
    loader.load(db, f.purchase_result(
        {"P1": f.purchase_head("P1", on=date(2026, 6, 10))},
        [f.purchase_line("P1", "PL1", "PN-DYN", qty="3", price="10")]), batch.id, date(2026, 6, 1))
    loader.load(db, f.sales_result(
        {"S1": f.sales_head("S1", on=date(2026, 6, 12))},
        [f.sales_line("S1", "SL1", "PN-DYN", qty="2", price="20")]), batch.id, date(2026, 6, 1))
    loader.load(db, f.sales_result(
        {"S2": f.sales_head("S2", on=date(2099, 1, 1))},          # 未来脏单：不扣库存
        [f.sales_line("S2", "SL2", "PN-DYN", qty="50", price="20")]), batch.id, date(2026, 6, 1))
    ml = f.maintenance_line("M1", "ML1", "PN-DYN", qty="2")
    ml["return_qty"] = Decimal("1")
    loader.load(db, f.maintenance_result(
        {"M1": f.maintenance_head("M1", order_no="WBDD-DYN", on=date(2026, 6, 15))},
        [ml]), batch.id, date(2026, 6, 1))
    # 无快照的新型号：+4 −1 = 3（期初 0、纯流水）
    loader.load(db, f.purchase_result(
        {"P2": f.purchase_head("P2", on=date(2026, 6, 5))},
        [f.purchase_line("P2", "PL2", "PN-NEW", qty="4", price="10")]), batch.id, date(2026, 6, 1))
    loader.load(db, f.sales_result(
        {"S3": f.sales_head("S3", on=date(2026, 6, 6))},
        [f.sales_line("S3", "SL3", "PN-NEW", qty="1", price="20")]), batch.id, date(2026, 6, 1))
    db.commit()

    m = inv_svc.dynamic_stock_map(db)
    dyn = m[pid]
    assert dyn["anchor_qty"] == Decimal("10")            # 人工修正值优先
    assert dyn["anchor_date"] == date(2026, 6, 1)
    assert dyn["in_qty"] == Decimal("3")                 # 锚点前的 100 不计
    assert dyn["out_sales"] == Decimal("2")              # 未来单不计
    assert dyn["out_maint"] == Decimal("1")              # 退货冲抵
    assert dyn["dynamic_qty"] == Decimal("10")

    new = m[_pid(db, "PN-NEW")]
    assert new["anchor_date"] is None and new["dynamic_qty"] == Decimal("3")

    # 列表：关键词过滤 + 分仓参考行带修正所需字段
    out = inv_svc.list_dynamic(db, "PN-DYN", 1, 20)
    assert out["total"] == 1
    it = out["items"][0]
    assert it["dynamic_qty"] == 10.0 and len(it["warehouses"]) == 2
    assert all({"id", "source_qty", "safety_stock"} <= set(w) for w in it["warehouses"])
