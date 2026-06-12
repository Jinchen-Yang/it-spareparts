"""loader 商品身份重定向：别名重定向 / 合并墓碑不复活 / 库存路径（整改 P3 核心回归）。"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.inventory import Inventory
from app.models.purchase import FPurchaseLine
from app.models.system import SysImportBatch
from app.services import merge
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="h1")
    db.add(b)
    db.flush()
    return b


def _import_purchase(db, batch, lines, orders=None, mode="skip"):
    orders = orders or {}
    for ln in lines:
        orders.setdefault(ln["_order_raw_id"], f.purchase_head(ln["_order_raw_id"]))
    res = loader.load(db, f.purchase_result(orders, lines), batch.id,
                      date(2026, 6, 1), mode=mode)
    db.commit()
    return res


def test_normal_import_creates_part_and_alias(db, batch):
    _import_purchase(db, batch, [f.purchase_line("O1", "L1", "PN-A", description="部件A")])
    part = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-A"))
    assert part is not None and part.status == "active"
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "PN-A"))
    assert alias.part_id == part.id and alias.status == "active"
    line = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "L1"))
    assert line.part_id == part.id


def test_alias_redirect_prevents_duplicate_part(db, batch):
    """已生效别名把原始写法映射到既有商品 → 不重复建档（验收场景2）。"""
    _import_purchase(db, batch, [f.purchase_line("O1", "L1", "PN-TARGET")])
    target = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-TARGET"))
    # 人工审核出的别名：OLD-WRITING → PN-TARGET
    db.add(PartAlias(pn_raw="OLD-WRITING", pn_std="PN-TARGET", part_id=target.id,
                     source="manual", status="active"))
    db.commit()

    _import_purchase(db, batch, [f.purchase_line("O2", "L2", "OLD-WRITING")])
    assert db.scalar(select(DimPart).where(DimPart.pn_std == "OLD-WRITING")) is None
    line = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "L2"))
    assert line.part_id == target.id
    assert line.pn_std == "OLD-WRITING"          # 原文痕迹不改写


def test_merged_pn_reimport_lands_on_target(db, batch):
    """合并后的 pn 再次导入：skip 与 upsert 两种模式都落到目标，不复活墓碑（验收场景5）。"""
    _import_purchase(db, batch, [f.purchase_line("O1", "L1", "PN-SRC"),
                                 f.purchase_line("O1", "L2", "PN-TGT")])
    merge.merge_parts(db, "PN-SRC", "PN-TGT", "测试合并", "tester")
    src = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-SRC"))
    tgt = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-TGT"))
    assert src.status == "merged" and src.merged_into_id == tgt.id

    for mode, lid in (("skip", "L3"), ("upsert", "L3")):
        _import_purchase(db, batch,
                         [f.purchase_line("O2", lid, "PN-SRC", description="复活尝试")],
                         mode=mode)
        line = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == lid))
        assert line.part_id == tgt.id, f"mode={mode} 事实行必须指向合并目标"

    db.refresh(src)
    assert src.status == "merged" and src.description is None, "墓碑行不得被 enrich 复活"


def test_inventory_import_follows_redirect(db, batch):
    """库存导入同样走重定向：合并后库存快照 part_id 指向目标。"""
    _import_purchase(db, batch, [f.purchase_line("O1", "L1", "PN-S2"),
                                 f.purchase_line("O1", "L2", "PN-T2")])
    merge.merge_parts(db, "PN-S2", "PN-T2", None, "tester")
    tgt = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-T2"))

    loader.load(db, f.inventory_result([f.inventory_row("INV1", "PN-S2", qty="7")]),
                batch.id, date(2026, 6, 2))
    db.commit()
    inv = db.scalar(select(Inventory).where(Inventory.raw_inventory_id == "INV1"))
    assert inv.part_id == tgt.id
    assert inv.pn_std == "PN-S2"                 # 库存行保留源 pn 文本，part 级口径=SUM
    assert inv.source_qty == Decimal("7")


def test_needs_review_alias_status_pending(db, batch):
    _import_purchase(db, batch,
                     [f.purchase_line("O1", "L1", "PN X1", pn_raw="pn x1", needs_review=True)])
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "pn x1"))
    assert alias.status == "pending" and alias.needs_review is True
