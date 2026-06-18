"""合并重定向标签：查询墓碑 pn 时，quick_pricing/list_*/get_overview 都必须用
*规范* PN 标注价格并暴露 redirected_from——否则 AI 会把目标的价格冒充入参 pn 上报。

回归 review #1/#5/#7：merge 后 quick_pricing 之前 `part, _ = resolve_part(...)`
把 redirected_from 丢了，下游 lookup_prices_bulk 输出 {pn_std: 墓碑, 价格: 目标}。
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import merge, part_overview
from tests import factories as f


@pytest.fixture()
def merged(db):
    """PN-OLD 与 PN-NEW 各有采购/销售/库存；将 OLD 合并入 NEW。

    日期一律相对 today：采购须落在 RECENT_PURCHASE_DAYS(15) 窗口内，否则 quick_pricing 的
    recent_purchase_count 会随真实时钟推移而漂移——固定 2026-06-01 即因越窗导致 CI 时间炸弹。
    保留"采购早于销售"的相对次序以维持成本回放语义。"""
    def d(n: int) -> date:   # n 天前
        return date.today() - timedelta(days=n)
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hredir")
    db.add(b)
    db.flush()
    loader.load(db, f.purchase_result(
        {"PO1": f.purchase_head("PO1", on=d(10)),
         "PO2": f.purchase_head("PO2", on=d(6))},
        [f.purchase_line("PO1", "L1", "PN-OLD", qty="10", price="80"),
         f.purchase_line("PO2", "L2", "PN-NEW", qty="10", price="100")]),
        b.id, d(0))
    loader.load(db, f.sales_result(
        {"SO1": f.sales_head("SO1", on=d(9)),
         "SO2": f.sales_head("SO2", on=d(5))},
        [f.sales_line("SO1", "SL1", "PN-OLD", qty="2", price="150"),
         f.sales_line("SO2", "SL2", "PN-NEW", qty="2", price="200")]),
        b.id, d(0))
    loader.load(db, f.inventory_result(
        [f.inventory_row("INV1", "PN-OLD", qty="3"),
         f.inventory_row("INV2", "PN-NEW", qty="7")]),
        b.id, d(0))
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


def test_resolve_part_chain_broken_returns_none(db, merged):
    """链中节点缺失（断 FK / 异常数据）：返 (None, None) 而非 AttributeError 500。"""
    from app.models.dimensions import DimPart as _Part

    # 构造断链：BROKEN-A → merged_into_id=99999（无对应 dim_part 行）
    # 注：dim_part.merged_into_id 是 ForeignKey 但未显式 RESTRICT；现实路径是
    # 数据迁移/手工修复留下的孤儿。本测试直接造孤儿来验证防御。
    orphan_id = 999999
    a = _Part(pn_std="BROKEN-A", status="merged", merged_into_id=orphan_id)
    # 绕过 FK 约束：若库有 FK 拒绝，跳过；治理库通常未对 merged_into 加 RESTRICT
    db.add(a)
    try:
        db.flush()
    except Exception:
        db.rollback()
        pytest.skip("FK 约束阻止构造孤儿 merged_into_id；防御路径靠 logger 报警")

    part, rf = part_overview.resolve_part(db, "BROKEN-A")
    assert part is None and rf is None


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
