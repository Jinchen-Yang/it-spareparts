"""合并重定向标签：查询墓碑 pn 时，quick_pricing/list_*/get_overview 都必须用
*规范* PN 标注价格并暴露 redirected_from——否则 AI 会把目标的价格冒充入参 pn 上报。

回归 review #1/#5/#7：merge 后 quick_pricing 之前 `part, _ = resolve_part(...)`
把 redirected_from 丢了，下游 lookup_prices_bulk 输出 {pn_std: 墓碑, 价格: 目标}。
"""
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import merge, part_overview, substitute
from tests import factories as f


@pytest.fixture()
def merged(db):
    """PN-OLD 与 PN-NEW 各有采购/销售/库存；将 OLD 合并入 NEW。"""
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hredir")
    db.add(b)
    db.flush()
    loader.load(db, f.purchase_result(
        {"PO1": f.purchase_head("PO1", on=date(2026, 6, 1)),
         "PO2": f.purchase_head("PO2", on=date(2026, 6, 5))},
        [f.purchase_line("PO1", "L1", "PN-OLD", qty="10", price="80"),
         f.purchase_line("PO2", "L2", "PN-NEW", qty="10", price="100")]),
        b.id, date(2026, 6, 10))
    loader.load(db, f.sales_result(
        {"SO1": f.sales_head("SO1", on=date(2026, 6, 2)),
         "SO2": f.sales_head("SO2", on=date(2026, 6, 6))},
        [f.sales_line("SO1", "SL1", "PN-OLD", qty="2", price="150"),
         f.sales_line("SO2", "SL2", "PN-NEW", qty="2", price="200")]),
        b.id, date(2026, 6, 10))
    loader.load(db, f.inventory_result(
        [f.inventory_row("INV1", "PN-OLD", qty="3"),
         f.inventory_row("INV2", "PN-NEW", qty="7")]),
        b.id, date(2026, 6, 10))
    db.commit()
    merge.merge_parts(db, "PN-OLD", "PN-NEW", "review #1 regression", "tester")
    db.commit()
    db.expire_all()
    return b


def test_resolve_part_chain_redirect(db, merged):
    """resolve_part 走 merged 链：入参墓碑 → 返回目标 part + redirected_from=入参。"""
    part, redirected_from = part_overview.resolve_part(db, "PN-OLD")
    assert part is not None
    assert part.pn_std == "PN-NEW"
    assert part.status == "active"
    assert redirected_from == "PN-OLD"

    # 直接命中目标：不应标 redirect
    part2, rf2 = part_overview.resolve_part(db, "PN-NEW")
    assert part2.pn_std == "PN-NEW" and rf2 is None


def test_resolve_part_chain_limit_returns_none(db, merged):
    """链深溢出（理论上路径压缩后≤1）：返 (None, None) 而非墓碑，避免下游 part_id 查询全空。"""
    # 人工构造一条 >_MERGE_CHAIN_LIMIT 的链：OLD→NEW，再让 NEW 指向自己外的另一个墓碑链
    # 简化构造：把 NEW 也设成 merged 但指向自己（CHECK 不允许 self-merge），
    # 改用：建一系列 fake parts，串成 X0→X1→...→X12 链
    parts = []
    for i in range(12):
        p = DimPart(pn_std=f"CHAIN-{i}", status="active")
        db.add(p)
        db.flush()
        parts.append(p)
    # 反向 link：X0 -> X1 -> X2 -> ... -> X11（X11 active 作终点）
    for i in range(11):
        parts[i].status = "merged"
        parts[i].merged_into_id = parts[i + 1].id
    db.flush()

    part, rf = part_overview.resolve_part(db, "CHAIN-0")
    # 链长 11 > _MERGE_CHAIN_LIMIT(10) → 触发溢出 → None
    assert part is None
    assert rf is None


def test_quick_pricing_labels_canonical_pn_with_redirect(db, merged):
    """整改 P3 核心回归：查询墓碑 PN-OLD 时，quick_pricing 必须用规范 PN-NEW 标注价格，
    并暴露 redirected_from='PN-OLD'。否则下游 lookup_prices_bulk 把 NEW 的价格冒充 OLD。"""
    qp = part_overview.quick_pricing(db, "PN-OLD")
    # 关键断言：标签是规范 PN，不是入参墓碑
    assert qp["pn_std"] == "PN-NEW", "墓碑查询必须用规范 PN 标注价格"
    assert qp["redirected_from"] == "PN-OLD", "必须暴露重定向溯源链"
    # 价格数据必须覆盖合并双方（OLD 的 80 与 NEW 的 100 都进入 NEW 的 part_id 历史）
    assert qp["recent_purchase_count"] >= 2
    assert qp["stock_total"] == 10  # 3 (OLD) + 7 (NEW) — 库存合计跨源 pn 行

    # 直接命中目标：标签即入参，无 redirect
    direct = part_overview.quick_pricing(db, "PN-NEW")
    assert direct["pn_std"] == "PN-NEW"
    assert direct["redirected_from"] is None


def test_quick_pricing_not_found_keeps_shape(db):
    """未命中：入参原样回传作 pn_std，redirected_from=None，价格字段全 None——
    供 lookup_prices_bulk 的 spread 拿到一致形状，AI 端无需特判。"""
    qp = part_overview.quick_pricing(db, "DOES-NOT-EXIST")
    assert qp["pn_std"] == "DOES-NOT-EXIST"
    assert qp["description"] is None
    assert qp["redirected_from"] is None
    assert qp["last_purchase_price"] is None
    assert qp["stock_total"] == 0


def test_list_purchases_surfaces_redirect(db, merged):
    """list_purchases 入参墓碑 → 返回目标的采购历史 + redirected_from。"""
    out = part_overview.list_purchases(db, "PN-OLD", 1, 20)
    assert out["redirected_from"] == "PN-OLD"
    # 合并后 OLD 的采购行应已 repoint 到 NEW 的 part_id（part_id 查询自然带回）
    assert out["total"] >= 2

    direct = part_overview.list_purchases(db, "PN-NEW", 1, 20)
    assert direct["redirected_from"] is None


def test_list_sales_surfaces_redirect(db, merged):
    """list_sales 入参墓碑 → 返回目标的销售历史 + redirected_from，且匿名化逻辑仍生效。"""
    out = part_overview.list_sales(db, "PN-OLD", 1, 20)
    assert out["redirected_from"] == "PN-OLD"
    assert out["total"] >= 2

    direct = part_overview.list_sales(db, "PN-NEW", 1, 20)
    assert direct["redirected_from"] is None


def test_get_overview_surfaces_redirect(db, merged):
    """get_overview.part 含 redirected_from（既有契约，回归保护）。"""
    ov = part_overview.get_overview(db, "PN-OLD")
    assert ov is not None
    assert ov["part"]["pn_std"] == "PN-NEW"
    assert ov["part"]["redirected_from"] == "PN-OLD"


def test_search_parts_items_have_is_excluded(db, merged):
    """search_parts 返回 item 现在带 is_excluded，与 resolver 分支形状对齐。"""
    r = part_overview.search_parts(db, None, 1, 20)
    assert r["items"], "should have parts"
    assert "is_excluded" in r["items"][0]


def test_list_substitutes_surfaces_redirect(db, merged):
    """list_substitutes 查询墓碑 PN-OLD：旧实现用 OLD.id 直查 PartSubstitute，
    必为空（_merge_substitutes 已把所有源关系 repoint 到 NEW.id）。修复后先
    resolve_part 拿到 NEW，再用 NEW.id 命中关系，并暴露 redirected_from='PN-OLD'。
    """
    # 在 NEW（合并后存活的目标）上建一个替代关系：等价于"合并前 OLD 有的关系
    # 被 _merge_substitutes 重指向到 NEW.id"——查询路径是同一条
    db.add(DimPart(pn_std="PN-OTHER", status="active"))
    db.commit()
    substitute.add_substitute(db, "PN-NEW", "PN-OTHER", "regression #2", "tester")

    out = substitute.list_substitutes(db, "PN-OLD")
    assert out["redirected_from"] == "PN-OLD", "墓碑查询必须暴露 redirected_from"
    pns = {r["pn_std"] for r in out["items"]}
    assert pns == {"PN-OTHER"}, f"应命中 NEW.id 上的关系，而非 OLD.id 空查；得到 {pns}"

    # 直接命中目标：同一份数据，无 redirect 标
    direct = substitute.list_substitutes(db, "PN-NEW")
    assert direct["redirected_from"] is None
    assert {r["pn_std"] for r in direct["items"]} == {"PN-OTHER"}

    # 不存在的 PN：形状一致，items 空，redirected_from None
    miss = substitute.list_substitutes(db, "DOES-NOT-EXIST")
    assert miss == {"items": [], "redirected_from": None}


def test_overview_inventory_rows_carry_pn_std_label(db, merged):
    """合并后同一 part_id 在同仓有多行（不同源 pn 痕迹），_inventory 必须把 pn_std
    暴露到每行 dict——否则前端两条相同仓库的行无法区分（review 报的 bug）。"""
    ov = part_overview.get_overview(db, "PN-NEW")
    assert ov is not None
    inv = ov["inventory"]
    # fixture 在 "总仓" 有 PN-OLD(qty=3) 与 PN-NEW(qty=7) 两行，合并后 part_id 同
    same_wh = [r for r in inv if r["warehouse"] == "总仓"]
    assert len(same_wh) == 2, "同仓多源 pn 应保留多行"
    pns = {r["pn_std"] for r in same_wh}
    assert pns == {"PN-OLD", "PN-NEW"}, f"每行必须带源 pn_std 标签，得到 {pns}"

    # 入参墓碑同样走 resolve_part，应拿到一致的多行
    ov2 = part_overview.get_overview(db, "PN-OLD")
    assert ov2 is not None
    pns2 = {r["pn_std"] for r in ov2["inventory"] if r["warehouse"] == "总仓"}
    assert pns2 == {"PN-OLD", "PN-NEW"}
