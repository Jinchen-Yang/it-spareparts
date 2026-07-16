"""DEV-06：老板早会价格纪律摘要与稳定订单主键下钻。

本文件只验证历史事实展示：不包含报价、审批、拦截或人员评价。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions, security
from app.api import dashboard as dashboard_api
from app.business_time import business_today
from app.db import get_db
from app.etl import loader
from app.main import app
from app.models.dimensions import DimPart
from app.models.inventory import PartPoolMember, PartPoolPricePolicy
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch
from app.services import pool, pool_catalog, pool_price_analysis, price_discipline
from tests import factories as f

AS_OF = date(2026, 6, 1)
WINDOW = {"date_from": date(2026, 1, 1), "date_to": AS_OF}


def _ctx(**overrides) -> security.UserContext:
    perms = permissions._full()
    perms.update(overrides)
    return security.UserContext(
        user_id="discipline-user", role="custom", permissions=perms,
        is_authenticated=True,
    )


@pytest.fixture()
def discipline_seed(db):
    a = DimPart(pn_std="DS-A", description="A")
    b = DimPart(pn_std="DS-B", description="B")
    c = DimPart(pn_std="DS-C", description="C")
    d = DimPart(pn_std="DS-D", description="D")
    db.add_all([a, b, c, d]); db.flush()
    governed = pool_catalog.create_pool(
        db, name="已设约束池", member_part_ids=[a.id, b.id], operated_by="seed")
    pool_catalog.set_price_policy(
        db, group_id=governed["group_id"], version=1,
        purchase_value=Decimal("150"), purchase_basis="ex_tax",
        sales_value=Decimal("180"), sales_basis="ex_tax", operated_by="seed")
    unset = pool_catalog.create_pool(
        db, name="未设约束池", member_part_ids=[c.id, d.id], operated_by="seed")

    batch = SysImportBatch(
        filename="discipline.xlsx", file_type="purchase", file_hash="discipline-seed")
    db.add(batch); db.flush()
    purchases = {
        "PC": f.purchase_head(
            "PC", order_no="DUP-NO", on=date(2026, 1, 1), purchaser="采购张",
            source_type="销售订单", is_tax_inclusive=True),
        # 补库不属于利润 COST_PURCHASE_TYPES，但属于老板要看的真实采购纪律。
        "PN": f.purchase_head(
            "PN", order_no="DUP-NO", on=date(2026, 1, 2), purchaser="采购李",
            source_type="补库", is_tax_inclusive=True),
        "PE": f.purchase_head(
            "PE", on=date(2026, 1, 3), purchaser="采购张",
            source_type="销售订单", is_tax_inclusive=True),
        "PX": f.purchase_head(
            "PX", on=date(2026, 1, 4), purchaser="采购取消",
            source_type="补库", is_tax_inclusive=True, data_status="已取消"),
        "PF": f.purchase_head(
            "PF", on=date(2026, 12, 1), purchaser="采购未来",
            source_type="补库", is_tax_inclusive=True),
        "PU": f.purchase_head(
            "PU", on=date(2026, 1, 5), purchaser="采购无约束",
            source_type="补库", is_tax_inclusive=True),
    }
    purchase_lines = [
        f.purchase_line("PC", "PLC", "DS-A", qty="2", price="226"),   # ex200，差100
        f.purchase_line("PN", "PLN", "DS-B", qty="3", price="339"),   # ex300，差450
        f.purchase_line("PE", "PLE", "DS-A", qty="1", price="169.5"), # ex150，等于不越
        f.purchase_line("PX", "PLX", "DS-A", qty="1", price="565"),
        f.purchase_line("PF", "PLF", "DS-A", qty="1", price="565"),
        f.purchase_line("PU", "PLU", "DS-C", qty="1", price="565"),
    ]
    loader.load(db, f.purchase_result(purchases, purchase_lines), batch.id, AS_OF)

    sales = {
        "S1": f.sales_head("S1", order_no="SALE-LOW", on=date(2026, 2, 1)),
        "SE": f.sales_head("SE", on=date(2026, 2, 2)),
        "SX": f.sales_head("SX", on=date(2026, 2, 3), data_status="已取消"),
        "SF": f.sales_head("SF", on=date(2026, 12, 2)),
    }
    sales["S1"]["salesperson"] = "销售王"
    sales["SE"]["salesperson"] = "销售赵"
    sales["SX"]["salesperson"] = "销售取消"
    sales["SF"]["salesperson"] = "销售未来"
    sales_lines = [
        f.sales_line("S1", "SL1", "DS-A", qty="2", price="113"),       # ex100，差160
        f.sales_line("SE", "SLE", "DS-B", qty="1", price="203.4"),     # ex180，等于不越
        f.sales_line("SX", "SLX", "DS-A", qty="1", price="113"),
        f.sales_line("SF", "SLF", "DS-A", qty="1", price="113"),
    ]
    loader.load(db, f.sales_result(sales, sales_lines), batch.id, AS_OF)
    db.commit()
    purchase_ids = {
        raw: oid for raw, oid in db.execute(
            select(FPurchaseOrder.raw_order_id, FPurchaseOrder.id)
            .where(FPurchaseOrder.raw_order_id.in_(["PC", "PN"]))
        )
    }
    return {
        "governed": governed["group_id"], "unset": unset["group_id"],
        "part_a": a.id, "part_b": b.id, "purchase_ids": purchase_ids,
    }


def test_summary_matches_independent_line_math_and_boundaries(db, discipline_seed):
    out = price_discipline.summary(db, as_of=AS_OF, **WINDOW)
    assert out["restricted"] is False
    assert out["basis"] == "ex_tax"
    assert out["window"] == {
        "range": "custom", "date_from": "2026-01-01",
        "date_to": "2026-06-01", "as_of": "2026-06-01",
    }
    assert out["purchase"] == {
        "violation_line_count": 2, "order_count": 2, "pool_count": 1,
        "total_gap": 550.0,
    }
    assert out["sales"] == {
        "violation_line_count": 1, "order_count": 1, "pool_count": 1,
        "total_gap": 160.0,
    }
    # 独立逐行手算，不复用服务结果：严格越线；等于、取消、未来、无约束都不计。
    assert out["purchase"]["total_gap"] == (200 - 150) * 2 + (300 - 150) * 3
    assert out["sales"]["total_gap"] == (180 - 100) * 2

    severe = out["most_severe_pool"]
    assert severe == {
        "pool_group_id": discipline_seed["governed"], "pool_name": "已设约束池",
        "purchase_total_gap": 550.0, "sales_total_gap": 160.0,
        "total_gap": 710.0, "violation_line_count": 3,
    }
    assert out["handler_summary"]["purchase"] == [
        {"person": "采购李", "violation_line_count": 1, "order_count": 1,
         "total_gap": 450.0},
        {"person": "采购张", "violation_line_count": 1, "order_count": 1,
         "total_gap": 100.0},
    ]
    assert out["handler_summary"]["sales"] == [
        {"person": "销售王", "violation_line_count": 1, "order_count": 1,
         "total_gap": 160.0},
    ]
    assert [row["order_no"] for row in out["recent_violations"]] == [
        "SALE-LOW", "DUP-NO", "DUP-NO"]
    assert all(set((
        "side", "order_id", "order_no", "order_date", "line_id", "part_id", "pn_std",
        "pool_group_id", "pool_name", "person", "quantity", "actual_unit_ex_tax",
        "manual_limit_ex_tax", "unit_gap", "total_gap",
    )).issubset(row) for row in out["recent_violations"])
    assert out["missing_constraints"] == {
        "active_pool_count": 2,
        "purchase_ceiling_unset_count": 1,
        "sales_floor_unset_count": 1,
        "both_unset_count": 1,
    }


def test_price_window_uses_shanghai_business_day(monkeypatch):
    """默认窗口走统一北京时间业务日，而不是容器 UTC 的 date.today。"""
    monkeypatch.setattr(pool_price_analysis, "business_today", lambda: date(2026, 7, 16))
    lower, upper, today, token = pool_price_analysis.resolve_window("30d", None, None)
    assert (lower, upper, today, token) == (
        date(2026, 6, 17), date(2026, 7, 16), date(2026, 7, 16), "30d")


def test_non_cost_purchase_type_matches_boss_pool_list_same_window(db, discipline_seed):
    """摘要和老板池清单必须共享全部真实采购类型口径；利润成本池口径不在此修改。"""
    summary = price_discipline.summary(db, as_of=AS_OF, **WINDOW)
    board = pool.list_pools(db, as_of=AS_OF, sort="purchase_total", **WINDOW)
    item = next(x for x in board["items"]
                if x["group_id"] == discipline_seed["governed"])
    # 含补库 PN×3@ex300；若错误沿用 COST_PURCHASE_TYPES，这里会少 900、少 1 次越线。
    assert item["purchase_metrics"]["total_amount"] == 1450.0
    assert item["purchase_violation_count"] == 2
    assert item["purchase_violation_count"] == summary["purchase"]["violation_line_count"]


def test_each_recent_gap_matches_independent_source_row_calculation(db, discipline_seed):
    """不用摘要服务的价格表达式，直接从源行+当前约束逐条复算三条金额。"""
    recent = price_discipline.summary(db, as_of=AS_OF, **WINDOW)["recent_violations"]
    assert len(recent) == 3
    for item in recent:
        if item["side"] == "purchase":
            source = db.execute(
                select(
                    FPurchaseLine.unit_price, FPurchaseLine.qty,
                    FPurchaseOrder.is_tax_inclusive,
                    PartPoolPricePolicy.purchase_ceiling_ex_tax.label("limit"),
                )
                .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
                .join(PartPoolMember, PartPoolMember.part_id == FPurchaseLine.part_id)
                .join(PartPoolPricePolicy,
                      PartPoolPricePolicy.group_id == PartPoolMember.group_id)
                .where(FPurchaseLine.id == item["line_id"],
                       PartPoolPricePolicy.valid_to.is_(None))
            ).one()
            actual = (source.unit_price if source.is_tax_inclusive is False
                      else source.unit_price / Decimal("1.13"))
            unit_gap = actual - source.limit
        else:
            source = db.execute(
                select(
                    FSalesLine.unit_price, FSalesLine.qty,
                    PartPoolPricePolicy.sales_floor_ex_tax.label("limit"),
                )
                .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
                .join(PartPoolMember, PartPoolMember.part_id == FSalesLine.part_id)
                .join(PartPoolPricePolicy,
                      PartPoolPricePolicy.group_id == PartPoolMember.group_id)
                .where(FSalesLine.id == item["line_id"],
                       PartPoolPricePolicy.valid_to.is_(None))
            ).one()
            actual = source.unit_price / Decimal("1.13")
            unit_gap = source.limit - actual
        assert item["actual_unit_ex_tax"] == round(float(actual), 2)
        assert item["unit_gap"] == round(float(unit_gap), 2)
        assert item["total_gap"] == round(float(unit_gap * source.qty), 2)


def _api_client(db, ctx: security.UserContext):
    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[dashboard_api.get_current_user_context] = lambda: ctx
    app.dependency_overrides[dashboard_api.current_role] = lambda: ctx.role
    return TestClient(app), original


def _restore_overrides(original):
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original)


def test_governance_blind_summary_is_structurally_empty(db, discipline_seed):
    """关治理权限时整体失败关闭，不能靠次数、人员、排名或记录反推约束。"""
    client, original = _api_client(db, _ctx(data_pool_price_governance=False))
    try:
        response = client.get("/api/dashboard/price-discipline-summary", params={
            "date_from": "2026-01-01", "date_to": "2026-06-01"})
    finally:
        _restore_overrides(original)
    assert response.status_code == 200
    assert response.json() == {
        "restricted": True,
        "basis": "ex_tax",
        "window": {"range": "custom", "date_from": "2026-01-01",
                   "date_to": "2026-06-01", "as_of": business_today().isoformat()},
        "purchase": None, "sales": None, "most_severe_pool": None,
        "handler_summary": {"purchase": [], "sales": []},
        "recent_violations": [], "missing_constraints": None,
    }


def test_boss_order_detail_uses_stable_id_and_own_page_gate(db, discipline_seed):
    """重复显示单号不会串单；boss 页不依赖 page_pool_analysis。"""
    boss_only = _ctx(page_boss_board=True, page_pool_analysis=False)
    client, original = _api_client(db, boss_only)
    target_id = discipline_seed["purchase_ids"]["PN"]
    try:
        response = client.get(f"/api/dashboard/orders/purchase/{target_id}")
    finally:
        _restore_overrides(original)
    assert response.status_code == 200
    body = response.json()
    assert body["order"]["order_id"] == target_id
    assert body["order"]["order_no"] == "DUP-NO"
    assert [row["part_id"] for row in body["items"]] == [discipline_seed["part_b"]]

    pool_only = _ctx(page_boss_board=False, page_pool_analysis=True)
    denied, original = _api_client(db, pool_only)
    try:
        denied_response = denied.get(f"/api/dashboard/orders/purchase/{target_id}")
    finally:
        _restore_overrides(original)
    assert denied_response.status_code == 403


def test_boss_order_detail_reuses_pool_price_visibility(db, discipline_seed):
    limited = _ctx(
        page_boss_board=True, page_pool_analysis=False,
        data_pool_price_governance=False,
    )
    client, original = _api_client(db, limited)
    try:
        response = client.get(
            f"/api/dashboard/orders/purchase/{discipline_seed['purchase_ids']['PC']}")
    finally:
        _restore_overrides(original)
    assert response.status_code == 200
    body = response.json()
    assert body["price_restricted"] is True
    for row in body["items"]:
        assert row["purchase_original_unit_price"] is None
        assert row["purchase_unit_price_ex_tax"] is None
        assert row["purchase_line_value_ex_tax"] is None
