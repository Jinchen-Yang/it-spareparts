"""Follow-up（PR #1 review 第二轮 chip）：merge 重定向的另两处遗漏 + resolver 墓碑过滤。

- substitute.list_substitutes：入参墓碑 pn 要沿 merged 链取目标的替代关系（之前直查墓碑 id 得空）
- part_overview._inventory：行要带 pn_std，合并后同仓多行才能区分
- part_resolver.resolve：召回排除 status='merged' 墓碑；旧 pn 经 active 别名仍重定向到目标
"""
from datetime import date

from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import merge, part_overview, part_resolver, substitute
from tests import factories as f


def _seed_and_merge(db):
    """OLD/NEW 各有采购/销售/同仓库存；OLD 另有一条替代关系；将 OLD 合并入 NEW。"""
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hfollow")
    db.add(b)
    db.flush()
    loader.load(db, f.purchase_result(
        {"PO1": f.purchase_head("PO1", on=date(2026, 6, 1)),
         "PO2": f.purchase_head("PO2", on=date(2026, 6, 5))},
        [f.purchase_line("PO1", "L1", "PN-OLD", qty="10", price="80"),
         f.purchase_line("PO2", "L2", "PN-NEW", qty="10", price="100")]),
        b.id, date(2026, 6, 10))
    loader.load(db, f.sales_result(
        {"SO1": f.sales_head("SO1", on=date(2026, 6, 2))},
        [f.sales_line("SO1", "SL1", "PN-OLD", qty="2", price="150")]),
        b.id, date(2026, 6, 10))
    # 同一仓库（总仓）两行不同源 pn —— 测合并后行可区分
    loader.load(db, f.inventory_result(
        [f.inventory_row("INV1", "PN-OLD", warehouse="总仓", qty="3"),
         f.inventory_row("INV2", "PN-NEW", warehouse="总仓", qty="7")]),
        b.id, date(2026, 6, 10))
    # 第三个型号 + (OLD, SUB) 替代关系（合并前建，因 add_substitute 拒绝墓碑）
    db.add(DimPart(pn_std="PN-SUB", status="active"))
    db.flush()
    substitute.add_substitute(db, "PN-OLD", "PN-SUB", "兼容", "tester",
                              direction="one_way", substitute_type="compatible")
    db.commit()
    merge.merge_parts(db, "PN-OLD", "PN-NEW", "follow-up regression", "tester")
    db.commit()
    db.expire_all()
    return b


def test_list_substitutes_follows_merge_redirect(db):
    """查墓碑 PN-OLD 的替代料：要沿 merged 链取 PN-NEW 的替代关系，而非直查墓碑 id 得空。"""
    _seed_and_merge(db)

    # 墓碑入参：之前返回 []（替代关系已 repoint 到 NEW.id），现在应取到 PN-SUB
    via_old = substitute.list_substitutes(db, "PN-OLD")
    assert via_old, "墓碑 pn 查替代料不应为空——应重定向到合并目标"
    assert {s["pn_std"] for s in via_old} == {"PN-SUB"}

    # 直查目标：同样能看到（合并把关系迁到了 NEW）
    via_new = substitute.list_substitutes(db, "PN-NEW")
    assert {s["pn_std"] for s in via_new} == {"PN-SUB"}


def test_inventory_rows_labeled_with_pn_std(db):
    """合并后同仓多行（源 pn 不同）：每行带 pn_std 才能区分，否则 UI 两行无法分辨。"""
    _seed_and_merge(db)
    ov = part_overview.get_overview(db, "PN-NEW")
    assert ov is not None
    inv = ov["inventory"]
    # 同仓两行（源 PN-OLD qty3 / 源 PN-NEW qty7），都在总仓
    assert len(inv) == 2
    assert all("pn_std" in row for row in inv), "每行必须带 pn_std 标签"
    by_pn = {row["pn_std"]: row for row in inv}
    assert set(by_pn) == {"PN-OLD", "PN-NEW"}
    assert by_pn["PN-OLD"]["display_qty"] == 3
    assert by_pn["PN-NEW"]["display_qty"] == 7
    assert all(row["warehouse"] == "总仓" for row in inv)


def test_resolver_excludes_merged_tombstone(db):
    """resolver 召回排除墓碑；但旧 pn 经 merge 留下的 active 别名仍重定向到目标。"""
    _seed_and_merge(db)

    r = part_resolver.resolve(db, "PN-OLD", limit=10)
    pns = {it["pn_std"] for it in r["items"]}
    assert "PN-OLD" not in pns, "已合并墓碑不应出现在近似检索结果"
    assert "PN-NEW" in pns, "旧 pn 应经 active 别名重定向到合并目标"


def test_resolver_excludes_tombstone_from_direct_pn_match(db):
    """即便精确敲墓碑 pn，也不返回墓碑本身（只返回经别名重定向的目标）。"""
    _seed_and_merge(db)
    # 确认 OLD 确实是墓碑、NEW 是 active
    old = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-OLD"))
    assert old.status == "merged"
    r = part_resolver.resolve(db, "PN-OLD", limit=10)
    assert all(it["pn_std"] != "PN-OLD" for it in r["items"])
