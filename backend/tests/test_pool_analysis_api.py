"""DEV-03/04：全员池价格分析公共 API 契约。

测试只通过 HTTP 公共缝验证，不复用老板看板权限，也不直接断言内部 SQL 形状。
"""
from datetime import date
from decimal import Decimal
from time import perf_counter

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app import security
from app.auth import hash_password
from app.business_time import business_today
from app.etl import loader
from app.main import app
from app.models.dimensions import DimPart
from app.models.data_quality import FactDataQualityIssue
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysUser
from app.models.system import SysImportBatch
from app.db import engine
from app.services import pool_catalog, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _client(db, username: str, role: str, overrides: dict[str, bool] | None = None):
    db.add(SysUser(username=username, role=role, permissions=overrides,
                   password_hash=hash_password("pw123456")))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert login.status_code == 200
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def test_pool_analysis_has_independent_page_gate_for_all_named_roles(db):
    """销售/采购/只读不需要老板看板权限；但显式关池分析页后必须 403。"""
    assert TestClient(app).get("/api/pool-analysis/pools").status_code == 401

    for role in ("sales", "purchaser", "readonly"):
        response = _client(db, f"pool_{role}", role).get("/api/pool-analysis/pools")
        assert response.status_code == 200, role
        assert response.json()["items"] == []

    denied = _client(db, "pool_denied", "readonly", {"page_pool_analysis": False})
    assert denied.get("/api/pool-analysis/pools").status_code == 403


def _dq_issue(db, *, side: str, line, status: str, rule_code: str):
    issue = FactDataQualityIssue(
        side=side, line_id=line.id, part_id=line.part_id,
        import_batch_id=line.import_batch_id, rule_code=rule_code,
        rule_version="test-v1", evidence={"test": True},
        source_fingerprint=f"{side}-{line.id}-{rule_code}", status=status,
        detected_by="test-detector",
    )
    db.add(issue)
    return issue


@pytest.fixture()
def priced_pool(db):
    a = DimPart(pn_std="PA-A", description="A盘", brand="BA")
    b = DimPart(pn_std="PA-B", description="B盘", brand="BB")
    c = DimPart(pn_std="PA-OUTSIDE", description="池外配件", brand="BC")
    db.add_all([a, b, c]); db.flush()
    created = pool_catalog.create_pool(db, name="价格纪律池", member_part_ids=[a.id, b.id],
                                       operated_by="seed")
    gid = created["group_id"]
    pool_catalog.set_price_policy(
        db, group_id=gid, version=1,
        purchase_value=Decimal("160"), purchase_basis="ex_tax",
        sales_value=Decimal("180"), sales_basis="ex_tax", operated_by="seed")
    batch = SysImportBatch(filename="pool-analysis.xlsx", file_type="purchase", file_hash="pa")
    db.add(batch); db.flush()
    purchases = {
        "P1": f.purchase_head("P1", on=date(2026, 2, 1), purchaser="采购张",
                              supplier="供应商甲", is_tax_inclusive=True),
        "P2": f.purchase_head("P2", on=date(2026, 2, 2), purchaser="采购李",
                              supplier="供应商乙", is_tax_inclusive=True),
    }
    purchase_lines = [
        f.purchase_line("P1", "PL1", "PA-A", qty="2", price="113"),  # ex100
        f.purchase_line("P1", "PL-OUT", "PA-OUTSIDE", qty="1", price="56.5"),
        f.purchase_line("P2", "PL2", "PA-B", qty="1", price="226"),  # ex200
    ]
    loader.load(db, f.purchase_result(purchases, purchase_lines), batch.id, AS_OF)
    sales = {
        "S1": f.sales_head("S1", on=date(2026, 3, 1)),
        "S2": f.sales_head("S2", on=date(2026, 3, 2)),
    }
    sales["S1"]["salesperson"] = "销售王"; sales["S1"]["customer_name"] = "客户甲"
    sales["S2"]["salesperson"] = "销售赵"; sales["S2"]["customer_name"] = "客户乙"
    sales_lines = [
        f.sales_line("S1", "SL1", "PA-A", qty="1", price="226"),  # ex200
        f.sales_line("S2", "SL2", "PA-B", qty="2", price="113"),  # ex100
    ]
    loader.load(db, f.sales_result(sales, sales_lines), batch.id, AS_OF)
    db.commit(); profit.recompute(db)
    return {"gid": gid, "a": a.id, "b": b.id, "outside": c.id}


@pytest.fixture()
def non_pool_only_orders(db, priced_pool):
    """两张已生效订单都只有池外 PN，用于验证订单详情授权边界。"""
    batch = SysImportBatch(
        filename="non-pool-orders.xlsx", file_type="purchase", file_hash="non-pool-orders")
    db.add(batch); db.flush()
    loader.load(db, f.purchase_result(
        {"P-NO-POOL": f.purchase_head("P-NO-POOL", on=date(2026, 4, 10))},
        [f.purchase_line("P-NO-POOL", "PL-NO-POOL", "PA-OUTSIDE", qty="1", price="339")],
    ), batch.id, AS_OF)
    loader.load(db, f.sales_result(
        {"S-NO-POOL": f.sales_head("S-NO-POOL", on=date(2026, 4, 11))},
        [f.sales_line("S-NO-POOL", "SL-NO-POOL", "PA-OUTSIDE", qty="1", price="452")],
    ), batch.id, AS_OF)
    db.commit()
    return {
        "purchase_id": db.execute(select(FPurchaseOrder.id).where(
            FPurchaseOrder.order_no == "P-NO-POOL")).scalar_one(),
        "sales_id": db.execute(select(FSalesOrder.id).where(
            FSalesOrder.order_no == "S-NO-POOL")).scalar_one(),
    }


def test_price_map_purchase_keeps_all_members_and_separates_formal_from_raw_dq(
        db, priced_pool):
    batch = SysImportBatch(filename="price-map-dq.xlsx", file_type="purchase",
                           file_hash="price-map-dq")
    db.add(batch); db.flush()
    heads = {
        "P3": f.purchase_head("P3", on=date(2026, 4, 3), purchaser="采购张",
                              is_tax_inclusive=True),
        "P4": f.purchase_head("P4", on=date(2026, 4, 4), purchaser="采购错",
                              is_tax_inclusive=True),
    }
    loader.load(db, f.purchase_result(heads, [
        f.purchase_line("P3", "PL3", "PA-A", qty="1", price="339"),  # ex300
        f.purchase_line("P4", "PL4", "PA-A", qty="1", price="452"),  # ex400, source error
    ]), batch.id, AS_OF)
    p3 = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PL3"))
    p4 = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PL4"))
    _dq_issue(db, side="purchase", line=p3, status="open", rule_code="price_suspect")
    _dq_issue(db, side="purchase", line=p4, status="confirmed_source_error",
              rule_code="source_error")
    db.commit()

    client = _client(db, "price_map_reader", "readonly")
    response = client.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map",
        params={"side": "purchase", "date_from": "2026-01-01",
                "date_to": AS_OF.isoformat(), "sort": "constraint_delta", "order": "desc"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_version"] == 1
    assert body["side"] == "purchase" and body["basis"] == "ex_tax"
    assert body["current_constraint"]["value"] == 160.0
    assert body["pool_stats"] == {
        "weighted_avg": 175.0, "median": 200.0, "min": 100.0, "max": 300.0,
        "latest": 300.0, "total_qty": 4.0, "order_count": 3,
        "line_count": 3, "latest_date": "2026-04-03",
    }
    assert [item["pn_std"] for item in body["members"]] == ["PA-B", "PA-A"]
    member_a = next(item for item in body["members"] if item["part_id"] == priced_pool["a"])
    assert member_a["stats"]["weighted_avg"] == 166.67
    assert member_a["stats"]["median"] == 200.0
    assert member_a["stats"]["latest"] == 300.0
    assert member_a["current_reference"] == {
        "relation": "above", "delta_amount": 6.67, "delta_pct": 0.0417,
    }
    assert member_a["latest_raw_record"]["order_no"] == "P4"
    assert member_a["latest_raw_record"]["price_ex_tax"] == 400.0
    assert member_a["latest_raw_record"]["quality_status"] == "confirmed_source_error"
    assert member_a["quality_counts"] == {"suspected": 1, "confirmed_source_error": 1}
    assert body["excluded"]["suspected_records"] == 1
    assert body["excluded"]["confirmed_source_error_excluded"] == 1

    employee = client.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map",
        params={"side": "purchase", "date_from": "2026-01-01",
                "date_to": AS_OF.isoformat(), "employee": "采购张"},
    ).json()
    # 筛选后无样本的池成员仍保留，不能从图上消失。
    assert [item["pn_std"] for item in employee["members"]] == ["PA-A", "PA-B"]
    assert employee["members"][0]["stats"]["weighted_avg"] == 166.67
    assert employee["members"][1]["stats"] is None


def test_price_map_latest_raw_quality_uses_full_review_priority(db, priced_pool):
    """Latest raw status must retain the human-reviewed-valid state, while stronger
    current warning/error states still win deterministically on the same fact row."""
    batch = SysImportBatch(filename="price-map-valid.xlsx", file_type="purchase",
                           file_hash="price-map-valid")
    db.add(batch); db.flush()
    loader.load(db, f.purchase_result({
        "PV": f.purchase_head("PV", on=date(2026, 4, 5), purchaser="采购审核",
                              is_tax_inclusive=True),
    }, [f.purchase_line("PV", "PVL", "PA-B", qty="1", price="339")]), batch.id, AS_OF)
    line = db.scalar(select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PVL"))
    _dq_issue(db, side="purchase", line=line, status="confirmed_valid",
              rule_code="reviewed_valid")
    db.commit()

    client = _client(db, "price_map_quality_priority", "readonly")
    url = f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map"
    params = {"side": "purchase", "date_from": "2026-01-01", "date_to": AS_OF.isoformat()}

    def latest_status() -> str:
        body = client.get(url, params=params).json()
        member = next(item for item in body["members"] if item["part_id"] == priced_pool["b"])
        return member["latest_raw_record"]["quality_status"]

    assert latest_status() == "confirmed_valid"
    _dq_issue(db, side="purchase", line=line, status="open", rule_code="new_warning")
    db.commit()
    assert latest_status() == "open_or_source_changed"
    _dq_issue(db, side="purchase", line=line, status="confirmed_source_error",
              rule_code="source_error_after_review")
    db.commit()
    assert latest_status() == "confirmed_source_error"


def test_price_map_sales_and_governance_restriction_are_structural(db, priced_pool):
    params = {"side": "sales", "date_from": "2026-01-01",
              "date_to": AS_OF.isoformat(), "sort": "weighted_avg", "order": "desc"}
    reader = _client(db, "sales_price_map_reader", "readonly")
    body = reader.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map", params=params).json()
    assert body["pool_stats"]["weighted_avg"] == 133.33
    assert [item["pn_std"] for item in body["members"]] == ["PA-A", "PA-B"]
    assert body["members"][0]["current_reference"]["relation"] == "above"
    assert body["members"][1]["current_reference"] == {
        "relation": "below", "delta_amount": -80.0, "delta_pct": -0.4444,
    }
    assert body["members"][0]["latest_raw_record"]["employee"] == "销售王"

    blind = _client(db, "price_map_blind", "readonly",
                    {"data_pool_price_governance": False})
    hidden = blind.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map", params=params)
    assert hidden.status_code == 200
    payload = hidden.json()
    assert payload["price_restricted"] is True
    assert payload["effective_sort"] == "pn" and payload["effective_order"] == "asc"
    assert payload["current_constraint"] == {
        "status": "restricted", "value": None, "changed_at": None,
        "input_basis": None,
    }
    assert payload["pool_stats"] is None and payload["excluded"] is None
    assert [item["pn_std"] for item in payload["members"]] == ["PA-A", "PA-B"]
    for item in payload["members"]:
        assert item["stats"] is None
        assert item["current_reference"] is None
        assert item["latest_raw_record"] is None
        assert item["quality_counts"] is None

    hidden_desc = blind.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map",
        params={"side": "purchase", "date_from": "2026-01-01",
                "date_to": AS_OF.isoformat(), "sort": "pn", "order": "desc"},
    ).json()
    # 受限账号必须在服务调用前强制 pn/asc；不能只把响应元数据改成 asc，
    # 实际数组却仍按请求的 desc 返回。
    assert hidden_desc["sort"] == "pn" and hidden_desc["order"] == "desc"
    assert hidden_desc["effective_sort"] == "pn" and hidden_desc["effective_order"] == "asc"
    assert [item["pn_std"] for item in hidden_desc["members"]] == ["PA-A", "PA-B"]

    denied = _client(db, "price_map_denied", "readonly", {"page_pool_analysis": False})
    assert denied.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map").status_code == 403
    assert reader.get("/api/pool-analysis/pools/999999/price-map").status_code == 404


def test_price_map_derived_fields_are_registered_for_recursive_masking():
    """结构化净化之外的第二防线：新增派生容器/键必须由通用递归脱敏覆盖。"""
    ctx = security.UserContext(
        user_id="blind", role="readonly", is_authenticated=True,
        permissions={"data_pool_price_governance": False},
    )
    payload = {
        "part_id": 7, "pn_std": "SAFE-PN",
        "current_reference": {"relation": "above", "delta_amount": 12.3,
                              "delta_pct": 0.12},
        "latest_raw_record": {"price_ex_tax": 99.0, "order_no": "P-1"},
        "quality_counts": {"suspected": 2, "confirmed_source_error": 1},
    }
    masked = security.apply_field_visibility(payload, ctx)
    assert masked == {
        "part_id": 7, "pn_std": "SAFE-PN",
        "current_reference": None,
        "latest_raw_record": None,
        "quality_counts": None,
    }


def test_price_map_query_budget_is_constant_as_member_count_grows(db, priced_pool):
    """price-map 是固定批量查询；池成员增加不能退化成逐 PN 查询。"""
    client = _client(db, "price_map_query_budget", "readonly")
    path = f"/api/pool-analysis/pools/{priced_pool['gid']}/price-map"
    params = {"side": "purchase", "date_from": "2026-01-01",
              "date_to": AS_OF.isoformat()}

    def request_query_count() -> int:
        seen = 0

        def before_cursor(*_args):
            nonlocal seen
            seen += 1

        event.listen(engine, "before_cursor_execute", before_cursor)
        try:
            response = client.get(path, params=params)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor)
        assert response.status_code == 200
        return seen

    small = request_query_count()
    extra = []
    for index in range(18):
        part = DimPart(pn_std=f"PA-BUDGET-{index:02d}")
        db.add(part); db.flush(); extra.append(part.id)
    pool_catalog.update_members(
        db, group_id=priced_pool["gid"], version=2, add_part_ids=extra,
        remove_part_ids=[], operated_by="query-budget")
    db.commit()
    large = request_query_count()

    assert large == small
    assert large <= 12


def test_pool_detail_exposes_employees_but_masks_supplier_and_customer_independently(db, priced_pool):
    """池分析是经办人公开的显式上下文；不复用全局销售逐单隐藏策略。"""
    gid = priced_pool["gid"]

    sales = _client(db, "analysis_sales", "sales")
    window = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat()}
    sr = sales.get(f"/api/pool-analysis/pools/{gid}", params=window)
    assert sr.status_code == 200
    sbody = sr.json()
    assert {x["purchaser"] for x in sbody["purchase_transactions"]["items"]} == {"采购张", "采购李"}
    assert {x["salesperson"] for x in sbody["sales_transactions"]["items"]} == {"销售王", "销售赵"}
    assert all(x["supplier"] is None for x in sbody["purchase_transactions"]["items"])
    assert {x["customer"] for x in sbody["sales_transactions"]["items"]} == {"客户甲", "客户乙"}

    purchaser = _client(db, "analysis_purchaser", "purchaser")
    pr = purchaser.get(f"/api/pool-analysis/pools/{gid}", params=window)
    assert pr.status_code == 200
    pbody = pr.json()
    assert {x["supplier"] for x in pbody["purchase_transactions"]["items"]} == {"供应商甲", "供应商乙"}
    assert all(x["customer"] is None for x in pbody["sales_transactions"]["items"])
    assert {x["salesperson"] for x in pbody["sales_transactions"]["items"]} == {"销售王", "销售赵"}


def test_single_pool_reference_uses_weighted_average_median_and_manual_limits(db, priced_pool):
    client = _client(db, "reference_reader", "readonly")
    response = client.get(
        f"/api/parts/{priced_pool['a']}/pool-reference",
        params={"date_from": "2026-01-01", "date_to": AS_OF.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pool"] == {
        "group_id": priced_pool["gid"], "name": "价格纪律池", "member_count": 2,
    }
    assert body["part_id"] == priced_pool["a"]
    assert body["basis"] == "ex_tax"

    purchase = body["purchase_reference"]
    assert purchase["restricted"] is False
    assert purchase["pool_stats"]["weighted_avg"] == 133.33
    assert purchase["pool_stats"]["median"] == 150.0
    assert purchase["pool_stats"]["total_amount"] == 400.0
    assert purchase["pool_stats"]["total_qty"] == 3.0
    assert purchase["pool_stats"]["order_count"] == 2
    assert purchase["pool_stats"]["line_count"] == 2
    assert purchase["part_stats"]["weighted_avg"] == 100.0
    assert purchase["constraint"]["value"] == 160.0
    assert purchase["constraint"]["status"] == "set"
    assert purchase["delta_to_pool_avg"] == -33.33
    assert purchase["delta_to_constraint"] == -60.0
    assert purchase["relation_to_constraint"] == "below"

    sales = body["sales_reference"]
    assert sales["pool_stats"]["weighted_avg"] == 133.33
    assert sales["pool_stats"]["median"] == 150.0
    assert sales["part_stats"]["weighted_avg"] == 200.0
    assert sales["constraint"]["value"] == 180.0
    assert sales["relation_to_constraint"] == "above"


def test_non_pool_single_reference_is_neutral_without_prices_or_policy(db, priced_pool):
    """池分析页权限不能变成任意 PN 历史价格查询器。"""
    client = _client(db, "non_pool_single_reader", "readonly")
    response = client.get(
        f"/api/parts/{priced_pool['outside']}/pool-reference",
        params={"date_from": "2026-01-01", "date_to": AS_OF.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_in_pool"
    assert body["pool"] is None
    for key in ("purchase_reference", "sales_reference"):
        assert body[key] == {
            "status": "not_in_pool",
            "restricted": False,
            "pool_stats": None,
            "part_stats": None,
            "constraint": {
                "status": "unset", "value": None, "changed_by": None,
                "changed_at": None, "input_basis": None,
            },
            "delta_to_pool_avg": None,
            "delta_to_constraint": None,
            "relation_to_constraint": None,
        }


def test_non_pool_batch_reference_is_neutral_while_pool_member_keeps_prices(db, priced_pool):
    client = _client(db, "non_pool_batch_reader", "readonly")
    response = client.post("/api/parts/pool-references", json={
        "part_ids": [priced_pool["a"], priced_pool["outside"]],
        "date_from": "2026-01-01",
        "date_to": AS_OF.isoformat(),
    })
    assert response.status_code == 200
    by_part = {item["part_id"]: item for item in response.json()["items"]}
    assert by_part[priced_pool["a"]]["status"] == "active_pool"
    assert by_part[priced_pool["a"]]["purchase_reference"]["part_stats"]["weighted_avg"] == 100.0
    outside = by_part[priced_pool["outside"]]
    assert outside["status"] == "not_in_pool"
    assert outside["pool"] is None
    assert outside["purchase_reference"]["part_stats"] is None
    assert outside["sales_reference"]["part_stats"] is None
    assert outside["purchase_reference"]["constraint"]["value"] is None
    assert outside["sales_reference"]["constraint"]["value"] is None


def test_pool_analysis_list_uses_same_stats_contract_and_member_search(db, priced_pool):
    client = _client(db, "list_reader", "readonly")
    params = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat(), "q": "PA-B"}
    response = client.get("/api/pool-analysis/pools", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1 and body["window"]["range"] == "custom"
    item = body["items"][0]
    assert item["group_id"] == priced_pool["gid"] and item["name"] == "价格纪律池"
    assert item["purchase_reference"]["pool_stats"]["weighted_avg"] == 133.33
    assert item["purchase_reference"]["pool_stats"]["median"] == 150.0
    assert item["purchase_reference"]["constraint"]["value"] == 160.0
    assert item["sales_reference"]["pool_stats"]["weighted_avg"] == 133.33

    detail = client.get(f"/api/pool-analysis/pools/{priced_pool['gid']}", params=params).json()
    member = next(m for m in detail["members"] if m["part_id"] == priced_pool["a"])
    assert member["purchase_reference"]["part_stats"]["weighted_avg"] == 100.0

    # 新全员价格纪律接口只陈述历史事实；老板看板遗留的推荐/节省/溢价语义不得穿透。
    assert "benchmark" not in detail
    assert "savings" not in detail
    for old_key in ("purchase_price", "sale_price", "purchase_premium_pct",
                    "sale_premium_pct", "brand_premium_purchase", "brand_premium_sale"):
        assert old_key not in member


def test_pool_price_governance_is_the_only_price_gate_not_profit_cost_permission(db, priced_pool):
    params = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat()}
    cost_blind = _client(
        db, "reference_cost_blind", "readonly", {"data_purchase_cost": False})
    cost_body = cost_blind.get(
        f"/api/parts/{priced_pool['a']}/pool-reference", params=params).json()
    purchase_public = cost_body["purchase_reference"]
    assert purchase_public["restricted"] is False
    assert purchase_public["pool_stats"]["weighted_avg"] == 133.33
    assert purchase_public["part_stats"]["weighted_avg"] == 100.0
    assert purchase_public["constraint"]["value"] == 160.0
    assert purchase_public["delta_to_constraint"] == -60.0
    assert cost_body["sales_reference"]["restricted"] is False
    assert cost_body["sales_reference"]["pool_stats"]["weighted_avg"] == 133.33

    governance_blind = _client(
        db, "reference_governance_blind", "readonly",
        {"data_pool_price_governance": False})
    gov_body = governance_blind.get(
        f"/api/parts/{priced_pool['a']}/pool-reference", params=params).json()
    for key in ("purchase_reference", "sales_reference"):
        side = gov_body[key]
        assert side["restricted"] is True
        assert side["pool_stats"] is None
        assert side["part_stats"] is None
        assert side["constraint"]["status"] == "restricted"
        assert side["delta_to_pool_avg"] is None
        assert side["delta_to_constraint"] is None
        assert side["relation_to_constraint"] is None


def test_hidden_price_sort_is_structurally_downgraded(db, priced_pool):
    params = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat()}
    cost_blind = _client(db, "sort_cost_blind", "readonly", {"data_purchase_cost": False})
    purchase = cost_blind.get(
        "/api/pool-analysis/pools", params={**params, "sort": "purchase_average"}).json()
    assert purchase["ranking_restricted"] is False
    assert purchase["effective_sort"] == "purchase_average"
    # data_purchase_cost 是利润成本权限，不影响池内公开历史价格。
    sales = cost_blind.get(
        "/api/pool-analysis/pools", params={**params, "sort": "sales_average"}).json()
    assert sales["ranking_restricted"] is False
    assert sales["effective_sort"] == "sales_average"

    governance_blind = _client(
        db, "sort_governance_blind", "readonly", {"data_pool_price_governance": False})
    for sort in ("purchase_average", "sales_average"):
        hidden = governance_blind.get(
            "/api/pool-analysis/pools", params={**params, "sort": sort}).json()
        assert hidden["ranking_restricted"] is True
        assert hidden["effective_sort"] == "member_count"
        assert hidden["items"][0]["purchase_reference"]["restricted"] is True
        assert hidden["items"][0]["purchase_reference"]["pool_stats"] is None
        assert hidden["items"][0]["sales_reference"]["restricted"] is True
        assert hidden["items"][0]["sales_reference"]["pool_stats"] is None


def test_window_contract_rejects_half_open_or_reversed_and_caps_future(db, priced_pool):
    client = _client(db, "window_reader", "readonly")
    gid, part_id = priced_pool["gid"], priced_pool["a"]
    for path in ("/api/pool-analysis/pools",
                 f"/api/pool-analysis/pools/{gid}",
                 f"/api/parts/{part_id}/pool-reference"):
        assert client.get(path, params={"date_from": "2026-01-01"}).status_code == 422
        assert client.get(path, params={"range": "custom"}).status_code == 422
        assert client.get(path, params={
            "range": "custom", "date_from": "2026-01-01",
            "date_to": AS_OF.isoformat()}).status_code == 200
        assert client.get(path, params={
            "date_from": "2026-05-02", "date_to": "2026-05-01"}).status_code == 422
    batch = client.post("/api/parts/pool-references", json={
        "part_ids": [part_id], "date_to": "2026-05-01"})
    assert batch.status_code == 422
    assert client.post("/api/parts/pool-references", json={
        "part_ids": [part_id], "range": "custom"}).status_code == 422

    capped = client.get(f"/api/parts/{part_id}/pool-reference", params={
        "date_from": "2026-01-01", "date_to": "2099-12-31"})
    assert capped.status_code == 200
    assert capped.json()["window"]["date_to"] == business_today().isoformat()


def test_reference_returns_real_excluded_counts_instead_of_silent_zeroes(db, priced_pool):
    batch = SysImportBatch(filename="excluded.xlsx", file_type="purchase", file_hash="excluded")
    db.add(batch); db.flush()
    today = business_today()
    future = today.replace(year=today.year + 1)
    heads = {
        "PX-C": f.purchase_head("PX-C", on=date(2026, 4, 1), data_status="已取消"),
        "PX-ZP": f.purchase_head("PX-ZP", on=date(2026, 4, 2)),
        "PX-ZQ": f.purchase_head("PX-ZQ", on=date(2026, 4, 3)),
        "PX-F": f.purchase_head("PX-F", on=future),
    }
    lines = [
        f.purchase_line("PX-C", "PXL-C", "PA-A", qty="1", price="113"),
        f.purchase_line("PX-ZP", "PXL-ZP", "PA-A", qty="1", price="0"),
        f.purchase_line("PX-ZQ", "PXL-ZQ", "PA-A", qty="0", price="113"),
        f.purchase_line("PX-F", "PXL-F", "PA-A", qty="1", price="113"),
    ]
    loader.load(db, f.purchase_result(heads, lines), batch.id, future)
    db.commit()
    client = _client(db, "excluded_reader", "readonly")
    body = client.get(f"/api/parts/{priced_pool['a']}/pool-reference", params={
        "date_from": "2026-01-01", "date_to": business_today().isoformat()}).json()
    assert body["excluded"]["inactive_orders"] == 1
    assert body["excluded"]["nonpositive_price"] == 1
    assert body["excluded"]["nonpositive_qty"] == 1
    assert body["excluded"]["future_orders"] == 1
    assert set(body["excluded"]) == {
        "inactive_orders", "nonpositive_price", "nonpositive_qty", "future_orders",
    }


def test_batch_reference_is_capped_at_50_and_query_count_is_constant(db, priced_pool):
    extra = []
    for i in range(48):
        part = DimPart(pn_std=f"PA-X-{i:02d}")
        db.add(part); db.flush(); extra.append(part.id)
    # 设约束后池 version=2；一次事务把 48 个 PN 加入同一池。
    pool_catalog.update_members(
        db, group_id=priced_pool["gid"], version=2, add_part_ids=extra,
        remove_part_ids=[], operated_by="seed")
    db.commit()
    client = _client(db, "batch_reader", "readonly")
    ids = [priced_pool["a"], priced_pool["b"], *extra]

    counts = []
    for sample in ([ids[0]], ids):
        seen = 0

        def before_cursor(*_args):
            nonlocal seen
            seen += 1

        event.listen(engine, "before_cursor_execute", before_cursor)
        try:
            response = client.post("/api/parts/pool-references", json={
                "part_ids": sample, "date_from": "2026-01-01",
                "date_to": AS_OF.isoformat()})
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor)
        assert response.status_code == 200
        assert len(response.json()["items"]) == len(sample)
        counts.append(seen)
    assert counts[0] == counts[1]
    assert counts[1] <= 12

    # 50 PN 批量参考卡是采购/销售列表的首屏热路径：本地 PostgreSQL 合同测试固定
    # 20 次请求的 p95 < 300ms，同时上面的 SQL 数量断言防止数据量增长后退化成 N+1。
    elapsed = []
    for _ in range(20):
        started = perf_counter()
        response = client.post("/api/parts/pool-references", json={
            "part_ids": ids, "date_from": "2026-01-01", "date_to": AS_OF.isoformat()})
        elapsed.append(perf_counter() - started)
        assert response.status_code == 200
    p95 = sorted(elapsed)[18]
    assert p95 < 0.3, f"50 PN batch p95={p95 * 1000:.1f}ms"

    too_many = client.post("/api/parts/pool-references", json={"part_ids": list(range(1, 52))})
    assert too_many.status_code == 422


def test_detail_uses_governance_as_single_price_gate_and_keeps_employees(db, priced_pool):
    params = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat()}
    gid = priced_pool["gid"]
    cost_blind = _client(db, "detail_cost_blind", "readonly", {"data_purchase_cost": False})
    body = cost_blind.get(f"/api/pool-analysis/pools/{gid}", params=params).json()
    assert body["purchase_reference"]["restricted"] is False
    assert body["sales_reference"]["restricted"] is False
    assert body["purchase_reference"]["pool_stats"]["weighted_avg"] == 133.33
    assert body["sales_reference"]["pool_stats"]["weighted_avg"] == 133.33
    # 池历史价不是利润成本；data_purchase_cost=false 仍能看采购/销售价格事实。
    assert body["sales_transactions"]["items"][0]["sale_unit_price_ex_tax"] is not None
    assert body["sales_orders"]["items"][0]["sale_unit_price_ex_tax"] is not None
    assert "unit_price_ex_tax" not in body["sales_orders"]["items"][0]
    assert all(row["purchase_unit_price_ex_tax"] is not None
               for row in body["purchase_transactions"]["items"])
    assert body["purchase_metrics"]["weighted_avg_unit_price"] == 133.33

    governance_blind = _client(
        db, "detail_governance_blind", "readonly",
        {"data_pool_price_governance": False})
    hidden = governance_blind.get(f"/api/pool-analysis/pools/{gid}", params=params).json()
    for key in ("purchase_reference", "sales_reference"):
        assert hidden[key]["restricted"] is True
        assert hidden[key]["pool_stats"] is None
        assert hidden[key]["part_stats"] is None
        assert hidden[key]["constraint"]["status"] == "restricted"
        assert hidden[key]["constraint"]["value"] is None
        assert hidden[key]["delta_to_constraint"] is None
        assert hidden[key]["relation_to_constraint"] is None
    assert hidden["max_purchase_price"] is None
    assert hidden["min_sale_price"] is None
    assert hidden["purchase_metrics"] is None
    assert hidden["sales_metrics"] is None
    assert all(row["purchase_unit_price_ex_tax"] is None
               for row in hidden["purchase_transactions"]["items"])
    assert all(row["sale_unit_price_ex_tax"] is None
               for row in hidden["sales_transactions"]["items"])
    assert {row["purchaser"] for row in hidden["purchase_transactions"]["items"]} == {
        "采购张", "采购李"}
    assert {row["salesperson"] for row in hidden["sales_transactions"]["items"]} == {
        "销售王", "销售赵"}


def test_reference_purchase_scope_includes_all_real_active_types_and_can_filter_type(
        db, priced_pool):
    """价格纪律观察全部真实采购，不能把利润成本池类型白名单误当分析范围。"""
    batch = SysImportBatch(filename="other-type.xlsx", file_type="purchase", file_hash="other-type")
    db.add(batch); db.flush()
    head = {"OTHER": f.purchase_head(
        "OTHER", on=date(2026, 4, 1), source_type="补库", is_tax_inclusive=False)}
    line = [f.purchase_line("OTHER", "OTHER-L", "PA-A", qty="10", price="9999")]
    loader.load(db, f.purchase_result(head, line), batch.id, AS_OF)
    db.commit()
    client = _client(db, "scope_reader", "readonly")
    body = client.get(f"/api/parts/{priced_pool['a']}/pool-reference", params={
        "date_from": "2026-01-01", "date_to": AS_OF.isoformat()}).json()
    assert body["purchase_reference"]["pool_stats"]["weighted_avg"] == 7722.31
    assert body["purchase_reference"]["pool_stats"]["total_amount"] == 100390.0

    replenishment = client.get(
        f"/api/parts/{priced_pool['a']}/pool-reference",
        params={"date_from": "2026-01-01", "date_to": AS_OF.isoformat(),
                "purchase_type": "补库"}).json()
    assert replenishment["purchase_reference"]["pool_stats"]["weighted_avg"] == 9999.0
    assert replenishment["purchase_reference"]["part_stats"]["order_count"] == 1

    pool_list = client.get("/api/pool-analysis/pools", params={
        "date_from": "2026-01-01", "date_to": AS_OF.isoformat(),
        "purchase_type": "补库"}).json()
    assert pool_list["purchase_type"] == "补库"
    assert pool_list["items"][0]["purchase_reference"]["pool_stats"]["weighted_avg"] == 9999.0

    detail = client.get(f"/api/pool-analysis/pools/{priced_pool['gid']}", params={
        "date_from": "2026-01-01", "date_to": AS_OF.isoformat(),
        "purchase_type": "补库"}).json()
    assert detail["purchase_reference"]["pool_stats"]["weighted_avg"] == 9999.0
    assert detail["purchase_metrics"]["weighted_avg_unit_price"] == 9999.0
    assert detail["purchase_violation_count"] == 1
    member_a = next(m for m in detail["members"] if m["part_id"] == priced_pool["a"])
    assert member_a["purchase_metrics"]["weighted_avg_unit_price"] == 9999.0
    assert {row["source_type"] for row in detail["purchase_transactions"]["items"]} == {"补库"}


def test_pool_detail_preserves_member_stats_and_preset_or_all_window(db, priced_pool):
    client = _client(db, "detail_window_reader", "readonly")
    gid = priced_pool["gid"]

    preset = client.get(f"/api/pool-analysis/pools/{gid}", params={"range": "365d"})
    assert preset.status_code == 200
    assert preset.json()["window"]["range"] == "365d"
    assert {member["window"]["range"] for member in preset.json()["members"]} == {"365d"}
    assert all(member["purchase_reference"]["part_stats"] is not None
               for member in preset.json()["members"])

    all_time = client.get(f"/api/pool-analysis/pools/{gid}", params={"range": "all"})
    assert all_time.status_code == 200
    assert all_time.json()["window"]["range"] == "all"
    assert all_time.json()["window"]["date_from"] is None


def test_detail_masks_customer_and_supplier_but_keeps_employee_names_when_both_hidden(
        db, priced_pool):
    client = _client(
        db, "detail_both_party_blind", "readonly",
        {"data_customer": False, "data_supplier": False})
    body = client.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}",
        params={"date_from": "2026-01-01", "date_to": AS_OF.isoformat()},
    ).json()
    assert all(row["supplier"] is None for row in body["purchase_transactions"]["items"])
    assert all(row["customer"] is None for row in body["sales_transactions"]["items"])
    assert {row["purchaser"] for row in body["purchase_transactions"]["items"]} == {
        "采购张", "采购李"}
    assert {row["salesperson"] for row in body["sales_transactions"]["items"]} == {
        "销售王", "销售赵"}


def test_pool_order_detail_rejects_purchase_order_without_active_pool_member(
        db, priced_pool, non_pool_only_orders):
    client = _client(db, "non_pool_purchase_order_reader", "readonly")
    response = client.get(
        f"/api/pool-analysis/orders/purchase/{non_pool_only_orders['purchase_id']}")
    assert response.status_code == 404


def test_pool_order_detail_rejects_sales_order_without_active_pool_member(
        db, priced_pool, non_pool_only_orders):
    client = _client(db, "non_pool_sales_order_reader", "readonly")
    response = client.get(
        f"/api/pool-analysis/orders/sales/{non_pool_only_orders['sales_id']}")
    assert response.status_code == 404


def test_pool_order_detail_keeps_every_line_when_order_contains_pool_and_non_pool_parts(
        db, priced_pool):
    client = _client(db, "mixed_pool_order_reader", "readonly")
    window = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat()}
    detail = client.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}", params=window).json()
    order_id = next(row["order_id"] for row in detail["purchase_transactions"]["items"]
                    if row["order_no"] == "P1")

    response = client.get(f"/api/pool-analysis/orders/purchase/{order_id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["part_id"] for item in items} == {
        priced_pool["a"], priced_pool["outside"],
    }
    assert next(item for item in items if item["part_id"] == priced_pool["a"])[
        "pool_group_id"] == priced_pool["gid"]
    assert next(item for item in items if item["part_id"] == priced_pool["outside"])[
        "pool_group_id"] is None


def test_pool_order_detail_uses_unique_id_returns_complete_order_and_own_permissions(
        db, priced_pool):
    window = {"date_from": "2026-01-01", "date_to": AS_OF.isoformat()}
    reader = _client(db, "order_detail_reader", "readonly", {"data_purchase_cost": False})
    pool_detail = reader.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}", params=window).json()
    paged = reader.get(
        f"/api/pool-analysis/pools/{priced_pool['gid']}",
        params={**window, "purchase_page": 2, "orders_page_size": 1}).json()
    assert paged["purchase_transactions"]["page"] == 2
    assert paged["purchase_transactions"]["page_size"] == 1
    assert len(paged["purchase_transactions"]["items"]) == 1
    clicked = next(row for row in pool_detail["purchase_transactions"]["items"]
                   if row["order_no"] == "P1")
    assert isinstance(clicked["order_id"], int)

    response = reader.get(f"/api/pool-analysis/orders/purchase/{clicked['order_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["side"] == "purchase"
    assert body["order"]["order_id"] == clicked["order_id"]
    assert body["order"]["order_no"] == "P1"
    assert body["order"]["purchaser"] == "采购张"
    assert body["order"]["source_type"] == "销售订单"
    assert body["price_restricted"] is False
    assert {row["part_id"] for row in body["items"]} == {
        priced_pool["a"], priced_pool["outside"]}
    assert all("purchase_unit_price_ex_tax" in row for row in body["items"])
    assert all("unit_price_ex_tax" not in row for row in body["items"])
    # data_purchase_cost=false 不影响池历史价格。
    assert all(row["purchase_unit_price_ex_tax"] is not None for row in body["items"])
    assert next(row for row in body["items"] if row["part_id"] == priced_pool["a"])[
        "pool_group_id"] == priced_pool["gid"]
    assert next(row for row in body["items"] if row["part_id"] == priced_pool["outside"])[
        "pool_group_id"] is None

    sale_clicked = next(row for row in pool_detail["sales_transactions"]["items"]
                        if row["order_no"] == "S1")
    sale = reader.get(
        f"/api/pool-analysis/orders/sales/{sale_clicked['order_id']}").json()
    assert sale["order"]["order_no"] == "S1"
    assert sale["order"]["salesperson"] == "销售王"
    assert sale["order"]["customer"] == "客户甲"
    assert sale["items"][0]["sale_unit_price_ex_tax"] == 200.0
    assert "unit_price_ex_tax" not in sale["items"][0]

    price_blind = _client(
        db, "order_detail_price_blind", "readonly",
        {"data_pool_price_governance": False})
    hidden = price_blind.get(
        f"/api/pool-analysis/orders/purchase/{clicked['order_id']}").json()
    assert hidden["price_restricted"] is True
    assert hidden["order"]["order_no"] == "P1"
    assert hidden["order"]["purchaser"] == "采购张"
    assert all(row["purchase_unit_price_ex_tax"] is None for row in hidden["items"])
    assert all(row["purchase_line_value_ex_tax"] is None for row in hidden["items"])
    sale_hidden = price_blind.get(
        f"/api/pool-analysis/orders/sales/{sale_clicked['order_id']}").json()
    assert sale_hidden["price_restricted"] is True
    assert sale_hidden["order"]["salesperson"] == "销售王"
    assert all(row["sale_unit_price_ex_tax"] is None for row in sale_hidden["items"])

    party_blind = _client(
        db, "order_detail_party_blind", "readonly",
        {"data_supplier": False, "data_customer": False})
    purchase_party_hidden = party_blind.get(
        f"/api/pool-analysis/orders/purchase/{clicked['order_id']}").json()
    sales_party_hidden = party_blind.get(
        f"/api/pool-analysis/orders/sales/{sale_clicked['order_id']}").json()
    assert purchase_party_hidden["supplier_restricted"] is True
    assert purchase_party_hidden["order"]["supplier"] is None
    assert purchase_party_hidden["order"]["purchaser"] == "采购张"
    assert sales_party_hidden["customer_restricted"] is True
    assert sales_party_hidden["order"]["customer"] is None
    assert sales_party_hidden["order"]["salesperson"] == "销售王"

    denied = _client(db, "order_detail_denied", "readonly", {"page_pool_analysis": False})
    assert denied.get(
        f"/api/pool-analysis/orders/purchase/{clicked['order_id']}").status_code == 403


def test_sales_reference_total_uses_stored_line_revenue_rounding(db):
    a = DimPart(pn_std="ROUND-A"); b = DimPart(pn_std="ROUND-B")
    db.add_all([a, b]); db.flush()
    pool_catalog.create_pool(db, name="销售舍入池", member_part_ids=[a.id, b.id], operated_by="seed")
    batch = SysImportBatch(filename="rounding.xlsx", file_type="sales", file_hash="rounding")
    db.add(batch); db.flush()
    heads = {
        "R1": f.sales_head("R1", on=date(2026, 5, 1)),
        "R2": f.sales_head("R2", on=date(2026, 5, 2)),
    }
    lines = [
        f.sales_line("R1", "RL1", "ROUND-A", qty="1", price="1"),
        f.sales_line("R2", "RL2", "ROUND-B", qty="1", price="1"),
    ]
    loader.load(db, f.sales_result(heads, lines), batch.id, AS_OF)
    db.commit(); profit.recompute(db)
    client = _client(db, "round_reader", "readonly")
    body = client.get(f"/api/parts/{a.id}/pool-reference", params={
        "date_from": "2026-05-01", "date_to": "2026-05-31"}).json()
    # 每行 round(1/1.13, 2)=0.88；正式落库两行合计 1.76，而非聚合后再舍入的 1.77。
    assert body["sales_reference"]["pool_stats"]["total_amount"] == 1.76
    assert body["sales_reference"]["pool_stats"]["weighted_avg"] == 0.88
