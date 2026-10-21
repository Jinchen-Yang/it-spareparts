"""销售看板扩展（D-44，PR-1 后端）：多选筛选、金额区间、任意列升降序、毛利、明细行、候选。

覆盖面：
- 升序 / 降序：三金额、毛利、日期、客户、销售；空值升降都沉底；id 同向破并列；
- 被遮蔽键排序回退（金额 / 毛利 → revenue，客户 → order_date）；
- 客户规范名多选（同名多 customer_id 合并，D-44 §12）、客户类型、销售人员多选；
- 无客户权限按客户类型筛选 403（D-44 §11）；
- 金额区间闭区间、口径切换、负金额 / 无金额开关（D-44 §7/§10），min>max 422，
  被遮蔽口径 403、公开口径 revenue 放行；
- KPI 客户数 / 销售人数 / 毛利 / 毛利率 / 成本覆盖率，毛利与 dashboard.kpi 同口径；
- KPI = 列表合计（金额区间筛选下仍成立）；
- 订单详情明细行与脱敏；options 新候选、客户补全与金额上限遮蔽。
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import config, permissions
from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimCustomer, DimPart
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch, SysUser
from app.services import dashboard

PASSWORD = "synthetic-sales-d44-1"
OPTIONS = "/api/sales/analytics/options"
SUMMARY = "/api/sales/analytics/summary"
ORDERS = "/api/sales/analytics/orders"


def _client(db, username, *, role="readonly", overrides=None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password(PASSWORD), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {}, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


@pytest.fixture()
def seeded(db):
    """6 张已生效单 + 1 张已取消单。

    | 单 | 日期 | 客户（规范名/类型） | 销售 | 不含税 | 含税 | 营收 | 已配成本营收 / 成本 |
    |---|---|---|---|---|---|---|---|
    | A1 | 01-10 | 甲（A 公司#1，渠道） | 张三 | 1000 | 1130 | 900 | 900 / 600 |
    | A2 | 01-11 | 甲（A 公司#2，渠道） | 李四 | 3000 | 空 | 2000 | 1500 / 1000（另 500 未配成本） |
    | A3 | 02-05 | 乙（直客） | 张三 | 2000 | 2260 | 空（无明细） | — |
    | A4 | 02-20 | 丙（类型空） | 王五 | -500 | -565 | -400 | -400 / -300 |
    | A5 | 03-01 | 无客户 | 空 | 空 | 空 | 100 | 未配成本 |
    | A6 | 03-02 | 乙（直客） | 李四 | 8000 | 9040 | 7000 | 7000 / 5000 |
    | X7 | 03-03 | 甲 | 张三 | 9999 | — | 已取消，一切口径排除 |

    甲的两个 customer_id 规范名相同（D-44 §12 合并）。
    """
    b = SysImportBatch(filename="d44.xlsx", file_type="sales", file_hash="d44-h1")
    db.add(b)
    db.flush()
    part = DimPart(pn_std="PN-D44", brand="B")
    db.add(part)
    db.flush()
    ca1 = DimCustomer(name_raw="A公司（质保）", name_normalized="A公司", customer_type="渠道")
    ca2 = DimCustomer(name_raw="A公司", name_normalized="A公司", customer_type="渠道")
    cb = DimCustomer(name_raw="B公司", name_normalized="B公司", customer_type="直客")
    cc = DimCustomer(name_raw="C公司", name_normalized="C公司", customer_type=None)
    db.add_all([ca1, ca2, cb, cc])
    db.flush()

    def order(raw, on, cust, sp, ex, inc, status="已生效"):
        o = FSalesOrder(
            raw_order_id=raw, order_no=f"SO-{raw}", order_date=on, salesperson=sp,
            customer_id=cust, business_type="备件销售",
            amount_ex_tax=Decimal(ex) if ex is not None else None,
            amount_inc_tax=Decimal(inc) if inc is not None else None,
            data_status=status, import_batch_id=b.id,
        )
        db.add(o)
        db.flush()
        return o

    def line(o, raw, revenue, cost=None, *, line_no=1, unit_price=None):
        db.add(FSalesLine(
            raw_line_id=raw, order_id=o.id, part_id=part.id, line_no=line_no,
            pn_std="PN-D44", category_major="内存", brand="B", qty=Decimal("1"),
            unit_price=Decimal(unit_price) if unit_price is not None else None,
            line_amount=Decimal(unit_price) if unit_price is not None else None,
            revenue_amount=Decimal(revenue), counts_revenue=True,
            cost_moving_avg=Decimal(cost) if cost is not None else None,
            cost_amount=Decimal(cost) if cost is not None else None,
            import_batch_id=b.id,
        ))

    a1 = order("A1", date(2026, 1, 10), ca1.id, "张三", "1000", "1130")
    line(a1, "LA1", "900", "600", unit_price="1017")
    a2 = order("A2", date(2026, 1, 11), ca2.id, "李四", "3000", None)
    line(a2, "LA2a", "1500", "1000", line_no=1)
    line(a2, "LA2b", "500", None, line_no=2)
    a3 = order("A3", date(2026, 2, 5), cb.id, "张三", "2000", "2260")
    a4 = order("A4", date(2026, 2, 20), cc.id, "王五", "-500", "-565")
    line(a4, "LA4", "-400", "-300")
    a5 = order("A5", date(2026, 3, 1), None, None, None, None)
    line(a5, "LA5", "100", None)
    a6 = order("A6", date(2026, 3, 2), cb.id, "李四", "8000", "9040")
    line(a6, "LA6", "7000", "5000")
    x7 = order("X7", date(2026, 3, 3), ca1.id, "张三", "9999", None, status="已取消")
    line(x7, "LX7", "9999", "1")
    db.commit()
    return {"a": (a1, a2, a3, a4, a5, a6)}


def _nos(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [i["order_no"] for i in resp.json()["items"]]


# ---------- 升序 / 降序（D-44 §4） ----------

@pytest.mark.parametrize("sort,order,expected", [
    ("amount_ex_tax", "desc", ["SO-A6", "SO-A2", "SO-A3", "SO-A1", "SO-A4", "SO-A5"]),
    ("amount_ex_tax", "asc", ["SO-A4", "SO-A1", "SO-A3", "SO-A2", "SO-A6", "SO-A5"]),
    # A2/A5 含税为空：升降都沉底，id 同向破并列
    ("amount_inc_tax", "desc", ["SO-A6", "SO-A3", "SO-A1", "SO-A4", "SO-A5", "SO-A2"]),
    ("amount_inc_tax", "asc", ["SO-A4", "SO-A1", "SO-A3", "SO-A6", "SO-A2", "SO-A5"]),
    ("revenue", "asc", ["SO-A4", "SO-A5", "SO-A1", "SO-A2", "SO-A6", "SO-A3"]),
    # 毛利：A6 2000 / A2 500 / A1 300 / A4 -100；A3/A5 无已配成本 → NULL 沉底
    ("gross_profit", "desc", ["SO-A6", "SO-A2", "SO-A1", "SO-A4", "SO-A5", "SO-A3"]),
    ("gross_profit", "asc", ["SO-A4", "SO-A1", "SO-A2", "SO-A6", "SO-A3", "SO-A5"]),
    ("order_date", "asc", ["SO-A1", "SO-A2", "SO-A3", "SO-A4", "SO-A5", "SO-A6"]),
    ("order_date", "desc", ["SO-A6", "SO-A5", "SO-A4", "SO-A3", "SO-A2", "SO-A1"]),
    ("customer", "asc", ["SO-A1", "SO-A2", "SO-A3", "SO-A6", "SO-A4", "SO-A5"]),
])
def test_orders_sort_both_directions_nulls_last(db, seeded, sort, order, expected):
    client = _client(db, f"d44-sort-{sort}-{order}", role="boss")
    resp = client.get(ORDERS, params={"sort": sort, "order": order})
    assert _nos(resp) == expected
    assert resp.json()["order"] == order


def test_salesperson_sort_reverses_and_null_stays_last(db, seeded):
    """中文排序依赖库排序规则，只断言升降互逆（同名按 id 同向）与空值沉底。"""
    client = _client(db, "d44-sort-sp", role="boss")
    asc = _nos(client.get(ORDERS, params={"sort": "salesperson", "order": "asc"}))
    desc = _nos(client.get(ORDERS, params={"sort": "salesperson", "order": "desc"}))
    assert asc[-1] == desc[-1] == "SO-A5"
    assert asc[:-1] == list(reversed(desc[:-1]))


def test_orders_order_param_validated(db, seeded):
    client = _client(db, "d44-order422", role="boss")
    assert client.get(ORDERS, params={"order": "up"}).status_code == 422
    assert client.get(ORDERS, params={"sort": "cost"}).status_code == 422


def test_ascending_pagination_no_dup_no_loss(db, seeded):
    client = _client(db, "d44-page", role="boss")
    seen = []
    for page in (1, 2, 3):
        seen += _nos(client.get(ORDERS, params={
            "sort": "amount_inc_tax", "order": "asc", "page": page, "page_size": 2}))
    assert sorted(seen) == sorted(f"SO-A{i}" for i in range(1, 7))
    assert len(seen) == len(set(seen))


def test_gross_profit_sort_degrades_when_profit_hidden(db, seeded):
    client = _client(db, "d44-noprofit", role="boss", overrides={"data_profit": False})
    data = client.get(ORDERS, params={"sort": "gross_profit"}).json()
    assert data["sort"] == "revenue"
    assert all(i["gross_profit"] is None and i["gross_margin"] is None
               for i in data["items"])
    # 金额本身仍可见（只关了毛利）
    assert data["items"][0]["amount_ex_tax"] is not None


def test_customer_sort_degrades_when_customer_hidden(db, seeded):
    client = _client(db, "d44-nocust-sort", role="boss", overrides={"data_customer": False})
    data = client.get(ORDERS, params={"sort": "customer", "order": "asc"}).json()
    assert data["sort"] == "order_date"
    assert [i["order_no"] for i in data["items"]][0] == "SO-A1"
    assert all(i["customer"] is None and i["customer_type"] is None for i in data["items"])


# ---------- 多选筛选（D-44 §2/§11/§12） ----------

def test_customer_name_multi_merges_same_normalized_name(db, seeded):
    client = _client(db, "d44-cust", role="boss")
    assert sorted(_nos(client.get(ORDERS, params={"customer_name": "A公司"}))) == [
        "SO-A1", "SO-A2"]
    both = client.get(ORDERS, params=[("customer_name", "A公司"), ("customer_name", "C公司")])
    assert sorted(_nos(both)) == ["SO-A1", "SO-A2", "SO-A4"]
    # 精确匹配：子串不命中
    assert _nos(client.get(ORDERS, params={"customer_name": "A"})) == []


def test_customer_type_and_salesperson_multi(db, seeded):
    client = _client(db, "d44-ctype", role="boss")
    assert sorted(_nos(client.get(ORDERS, params={"customer_type": "直客"}))) == [
        "SO-A3", "SO-A6"]
    resp = client.get(ORDERS, params=[("salesperson_name", "张三"), ("salesperson_name", "王五")])
    assert sorted(_nos(resp)) == ["SO-A1", "SO-A3", "SO-A4"]
    kpi = client.get(SUMMARY, params={"customer_type": "渠道"}).json()["kpi"]
    assert kpi["orders"] == 2 and kpi["customers"] == 1   # 甲两个 id 合并为一个客户


@pytest.mark.parametrize("params", [
    {"customer_type": "直客"}, {"customer_name": "A公司"}, {"customer": "A"},
])
def test_customer_class_filters_403_without_customer_permission(db, seeded, params):
    client = _client(db, f"d44-nocust-{sorted(params)[0]}", role="boss",
                     overrides={"data_customer": False})
    for path in (SUMMARY, ORDERS, OPTIONS):
        assert client.get(path, params=params).status_code == 403, (path, params)


# ---------- 金额区间（D-44 §7/§10） ----------

def test_amount_range_closed_interval_and_basis(db, seeded):
    client = _client(db, "d44-range", role="boss")
    resp = client.get(ORDERS, params={"amount_min": "1000", "amount_max": "3000"})
    assert sorted(_nos(resp)) == ["SO-A1", "SO-A2", "SO-A3"]      # 两端闭区间
    inc = client.get(ORDERS, params={
        "amount_basis": "amount_inc_tax", "amount_min": "1000", "amount_max": "3000"})
    assert sorted(_nos(inc)) == ["SO-A1", "SO-A3"]                # A2 含税为空不落区间
    rev = client.get(ORDERS, params={"amount_basis": "revenue", "amount_min": "1000"})
    assert sorted(_nos(rev)) == ["SO-A2", "SO-A6"]                # 营收为聚合值


def test_negative_and_empty_toggles(db, seeded):
    client = _client(db, "d44-toggle", role="boss")
    assert len(_nos(client.get(ORDERS))) == 6                      # 默认都含
    no_neg = _nos(client.get(ORDERS, params={"include_negative": "false"}))
    assert "SO-A4" not in no_neg and "SO-A5" in no_neg             # 空值不因负数开关被排除
    no_empty = _nos(client.get(ORDERS, params={"include_empty": "false"}))
    assert "SO-A5" not in no_empty and "SO-A4" in no_empty
    no_empty_inc = _nos(client.get(ORDERS, params={
        "include_empty": "false", "amount_basis": "amount_inc_tax"}))
    assert sorted(no_empty_inc) == ["SO-A1", "SO-A3", "SO-A4", "SO-A6"]


def test_amount_min_greater_than_max_422(db, seeded):
    client = _client(db, "d44-range422", role="boss")
    for path in (SUMMARY, ORDERS):
        resp = client.get(path, params={"amount_min": "5", "amount_max": "1"})
        assert resp.status_code == 422, path
    assert client.get(ORDERS, params={"amount_basis": "cost"}).status_code == 422


def test_amount_range_403_when_basis_masked_but_revenue_allowed(db, seeded):
    client = _client(db, "d44-range-mask", role="boss",
                     overrides={"data_purchase_cost": False})
    for params in ({"amount_min": "1"}, {"include_negative": "false"},
                   {"include_empty": "false", "amount_basis": "amount_inc_tax"}):
        assert client.get(ORDERS, params=params).status_code == 403, params
    ok = client.get(ORDERS, params={"amount_basis": "revenue", "amount_min": "1000"})
    assert sorted(_nos(ok)) == ["SO-A2", "SO-A6"]


def test_kpi_equals_list_totals_under_amount_filter(db, seeded):
    client = _client(db, "d44-kpi-eq", role="boss")
    params = {"amount_min": "0", "include_empty": "false"}
    kpi = client.get(SUMMARY, params=params).json()["kpi"]
    items = client.get(ORDERS, params={**params, "page_size": 200}).json()["items"]
    assert kpi["orders"] == len(items) == 4
    assert kpi["amount_ex_tax_total"] == pytest.approx(
        sum(i["amount_ex_tax"] for i in items))
    assert kpi["revenue_total"] == pytest.approx(
        sum(i["revenue"] or 0 for i in items))
    assert kpi["gross_profit"] == pytest.approx(
        sum(i["gross_profit"] or 0 for i in items))


# ---------- KPI 扩展与毛利口径（D-44 §5） ----------

def test_summary_new_kpis(db, seeded):
    client = _client(db, "d44-kpi", role="boss")
    kpi = client.get(SUMMARY).json()["kpi"]
    assert kpi["orders"] == 6
    assert kpi["customers"] == 3          # A公司（两 id 合并）/ B公司 / C公司；无客户不计
    assert kpi["salespeople"] == 3        # 张三 / 李四 / 王五；空不计
    assert kpi["revenue_total"] == pytest.approx(9600.0)
    assert kpi["revenue_costed"] == pytest.approx(9000.0)      # 900+1500-400+7000
    assert kpi["gross_profit"] == pytest.approx(2700.0)        # 9000 - 6300
    assert kpi["gross_margin"] == pytest.approx(0.3)
    assert kpi["cost_coverage"] == pytest.approx(round(9000 / 9600, 4))


def test_gross_profit_matches_dashboard_kpi(db, seeded):
    client = _client(db, "d44-vs-dash", role="boss")
    params = {"date_from": "2026-01-01", "date_to": "2026-03-31"}
    kpi = client.get(SUMMARY, params=params).json()["kpi"]
    ref = dashboard.kpi(db, date(2026, 1, 1), date(2026, 3, 31), as_of=date(2026, 10, 21))
    assert kpi["gross_profit"] == pytest.approx(ref["gross_profit"])
    assert kpi["gross_margin"] == pytest.approx(ref["gross_margin"])
    assert kpi["cost_coverage"] == pytest.approx(ref["cost_coverage"])
    assert kpi["revenue_total"] == pytest.approx(ref["sales_ex_tax"])


def test_summary_profit_masked_without_data_profit(db, seeded):
    client = _client(db, "d44-kpi-noprofit", role="boss", overrides={"data_profit": False})
    kpi = client.get(SUMMARY).json()["kpi"]
    assert kpi["gross_profit"] is None and kpi["gross_margin"] is None
    assert kpi["revenue_total"] == pytest.approx(9600.0)


# ---------- 订单详情明细行 ----------

def test_order_detail_lines(db, seeded):
    a2 = seeded["a"][1]
    client = _client(db, "d44-detail", role="boss")
    data = client.get(f"{ORDERS}/{a2.id}").json()
    assert data["gross_profit"] == pytest.approx(500.0)
    assert data["customer_type"] == "渠道"
    lines = data["lines"]
    assert [ln["line_no"] for ln in lines] == [1, 2]
    assert lines[0]["gross_profit"] == pytest.approx(500.0)
    assert lines[0]["gross_margin"] == pytest.approx(round(500 / 1500, 4))
    assert lines[1]["gross_profit"] is None            # 未配成本不当 0
    assert lines[1]["revenue"] == pytest.approx(500.0)
    assert lines[0]["category_major"] == "内存" and lines[0]["pn"] == "PN-D44"


def test_order_detail_lines_masked(db, seeded):
    a1 = seeded["a"][0]
    client = _client(db, "d44-detail-mask", role="boss",
                     overrides={"data_purchase_cost": False, "data_profit": False})
    ln = client.get(f"{ORDERS}/{a1.id}").json()["lines"][0]
    assert ln["unit_price"] is None and ln["line_amount"] is None
    assert ln["gross_profit"] is None and ln["gross_margin"] is None
    assert ln["revenue"] == pytest.approx(900.0)       # 营收公开口径


# ---------- options ----------

def test_options_new_candidates(db, seeded):
    client = _client(db, "d44-opt", role="boss")
    data = client.get(OPTIONS).json()
    # 同计数按值排序依赖库排序规则，这里只断言集合与“空值排最后”
    assert sorted(data["salespeople"][:2], key=lambda x: x["value"]) == sorted([
        {"value": "张三", "orders": 2}, {"value": "李四", "orders": 2}],
        key=lambda x: x["value"])
    assert data["salespeople"][-1] == {"value": None, "orders": 1}
    assert {"value": "渠道", "orders": 2} in data["customer_types"]
    assert {"value": None, "orders": 2} in data["customer_types"]   # 类型空 + 无客户
    assert data["customers"][0] == {"value": "A公司", "orders": 2}   # 两 id 合并计数
    assert data["amount_range"] == {
        "amount_ex_tax_max": 8000.0, "amount_inc_tax_max": 9040.0, "revenue_max": 7000.0}
    q = client.get(OPTIONS, params={"customer_q": "B"}).json()["customers"]
    assert q == [{"value": "B公司", "orders": 2}]
    # 金额上限随其他筛选变化，但不受金额区间本身约束
    scoped = client.get(OPTIONS, params={"customer_name": "A公司", "amount_max": "1"}).json()
    assert scoped["amount_range"]["amount_ex_tax_max"] == 3000.0


def test_options_masking(db, seeded):
    client = _client(db, "d44-opt-mask", role="boss",
                     overrides={"data_purchase_cost": False, "data_customer": False})
    data = client.get(OPTIONS).json()
    assert data["customers"] == [] and data["customer_types"] == []
    assert data["amount_range"]["amount_ex_tax_max"] is None
    assert data["amount_range"]["amount_inc_tax_max"] is None
    assert data["amount_range"]["revenue_max"] == 7000.0
    assert data["salespeople"]


def test_new_keys_registered_in_field_groups():
    groups = config.FIELD_GROUPS
    assert "customer_type" in groups["customer_info"]
    for key in ("amount_ex_tax_max", "amount_inc_tax_max"):
        assert key in groups["purchase_cost"], key
    assert "gross_profit" in groups["profit_amount"]
    assert "gross_margin" in groups["profit_rate"]


# ================= PR-2：分组排行 / 趋势 / 对比上月 / 新老客 =================

GROUPS = "/api/sales/analytics/groups"
TREND = "/api/sales/analytics/trend"


def test_groups_customer_ranking_other_and_shares(db, seeded):
    client = _client(db, "d44-grp", role="boss")
    data = client.get(GROUPS, params={"group_by": "customer", "top": 2}).json()
    assert [g["key"] for g in data["groups"]] == ["B公司", "A公司"]
    b, a = data["groups"]
    assert b["orders"] == 2 and b["amount_ex_tax"] == pytest.approx(10000.0)
    assert a["revenue"] == pytest.approx(2900.0) and a["gross_profit"] == pytest.approx(800.0)
    assert b["share"] == pytest.approx(round(10000 / 13500, 4))
    assert a["cumulative_share"] == pytest.approx(round(14000 / 13500, 4))
    other = data["other"]
    assert other["groups"] == 2 and other["orders"] == 2      # C公司 + 未填
    assert other["amount_ex_tax"] == pytest.approx(-500.0)
    assert other["gross_profit"] == pytest.approx(-100.0)
    assert data["total_groups"] == 4
    assert data["concentration"]["top1_share"] == pytest.approx(round(10000 / 13500, 4))
    # 分组合计（含“其他”）= KPI 合计
    kpi = client.get(SUMMARY).json()["kpi"]
    assert sum(g["orders"] for g in data["groups"]) + other["orders"] == kpi["orders"]
    assert (sum(g["revenue"] for g in data["groups"]) + other["revenue"]
            == pytest.approx(kpi["revenue_total"]))


def test_groups_empty_key_and_null_sort_last(db, seeded):
    client = _client(db, "d44-grp-null", role="boss")
    data = client.get(GROUPS, params={"group_by": "customer", "top": 0, "order": "asc"}).json()
    keys = [g["key"] for g in data["groups"]]
    assert keys == ["C公司", "A公司", "B公司", "未填"]          # 未填 ex 为空 → 沉底
    assert data["groups"][-1]["is_empty"] is True
    assert data["other"] is None


def test_groups_sort_orders_asc_ties_by_key(db, seeded):
    client = _client(db, "d44-grp-sp", role="boss")
    data = client.get(GROUPS, params={
        "group_by": "salesperson", "sort": "orders", "order": "asc", "top": 0}).json()
    assert [(g["key"], g["orders"]) for g in data["groups"]] == [
        ("未填", 1), ("王五", 1), ("张三", 2), ("李四", 2)]


def test_groups_validation(db, seeded):
    client = _client(db, "d44-grp-422", role="boss")
    assert client.get(GROUPS).status_code == 422
    assert client.get(GROUPS, params={"group_by": "supplier"}).status_code == 422
    assert client.get(GROUPS, params={"group_by": "customer", "sort": "cost"}).status_code == 422


@pytest.mark.parametrize("dim", ["customer", "customer_type", "customer_city", "customer_source"])
def test_groups_customer_dims_403_without_customer_permission(db, seeded, dim):
    client = _client(db, f"d44-grp-nocust-{dim}", role="boss", overrides={"data_customer": False})
    assert client.get(GROUPS, params={"group_by": dim}).status_code == 403
    assert client.get(GROUPS, params={"group_by": "salesperson"}).status_code == 200


def test_groups_masked_sort_and_basis_fallback(db, seeded):
    client = _client(db, "d44-grp-mask", role="boss",
                     overrides={"data_purchase_cost": False, "data_profit": False})
    data = client.get(GROUPS, params={"group_by": "salesperson", "top": 0}).json()
    assert data["sort"] == "orders" and data["amount_basis"] == "revenue"
    assert all(g["amount_ex_tax"] is None and g["gross_profit"] is None for g in data["groups"])
    li = next(g for g in data["groups"] if g["key"] == "李四")
    assert li["share"] == pytest.approx(round(9000 / 9600, 4))  # 按公开营收计占比
    data = client.get(GROUPS, params={"group_by": "salesperson", "sort": "gross_profit"}).json()
    assert data["sort"] == "orders"


def test_trend_month_buckets_sum_to_kpi(db, seeded):
    client = _client(db, "d44-trend-m", role="boss")
    params = {"date_from": "2026-01-01", "date_to": "2026-03-31", "granularity": "month"}
    data = client.get(TREND, params=params).json()
    assert [(b["bucket"], b["orders"]) for b in data["buckets"]] == [
        ("2026-01-01", 2), ("2026-02-01", 2), ("2026-03-01", 2)]
    assert [b["amount_ex_tax"] for b in data["buckets"]] == [4000.0, 1500.0, 8000.0]
    assert data["compare_available"] is False and data["compare"] is None


def test_trend_day_fills_empty_buckets_with_zero(db, seeded):
    client = _client(db, "d44-trend-d", role="boss")
    data = client.get(TREND, params={"date_from": "2026-01-09", "date_to": "2026-01-12"}).json()
    assert [(b["bucket"], b["orders"], b["amount_ex_tax"]) for b in data["buckets"]] == [
        ("2026-01-09", 0, 0.0), ("2026-01-10", 1, 1000.0),
        ("2026-01-11", 1, 3000.0), ("2026-01-12", 0, 0.0)]
    # 有成交但含税全缺失的桶为 null，不当 0
    assert data["buckets"][2]["amount_inc_tax"] is None


def test_trend_week_starts_monday_and_all_range(db, seeded):
    client = _client(db, "d44-trend-w", role="boss")
    data = client.get(TREND, params={"granularity": "week"}).json()   # 全部：取最早/最晚单
    assert data["buckets"][0]["bucket"] == "2026-01-05"               # 01-10 所在周的周一
    assert sum(b["orders"] for b in data["buckets"]) == 6
    assert [w["orders"] for w in data["weekday"]] == [1, 0, 0, 1, 1, 1, 2]


def test_trend_compare_previous_month(db, seeded):
    client = _client(db, "d44-trend-c", role="boss")
    data = client.get(TREND, params={
        "date_from": "2026-03-01", "date_to": "2026-03-05", "compare": "true"}).json()
    assert data["compare_window"] == {"date_from": "2026-02-01", "date_to": "2026-02-05"}
    assert [b["orders"] for b in data["compare"]] == [0, 0, 0, 0, 1]
    assert [b["orders"] for b in data["buckets"]] == [1, 1, 0, 0, 0]


def test_trend_too_many_buckets_422(db, seeded):
    client = _client(db, "d44-trend-422", role="boss")
    resp = client.get(TREND, params={"date_from": "2020-01-01", "date_to": "2026-01-01"})
    assert resp.status_code == 422
    assert client.get(TREND, params={"granularity": "year"}).status_code == 422


def test_summary_compare_and_lifecycle(db, seeded):
    client = _client(db, "d44-sum-cmp", role="boss")
    data = client.get(SUMMARY, params={
        "date_from": "2026-03-01", "date_to": "2026-03-05", "compare": "true"}).json()
    assert data["compare_available"] is True
    assert data["compare"]["date_from"] == "2026-02-01"
    assert data["compare"]["kpi"]["orders"] == 1
    assert data["compare"]["kpi"]["amount_ex_tax_total"] == pytest.approx(2000.0)
    long = client.get(SUMMARY, params={
        "date_from": "2026-01-01", "date_to": "2026-03-31", "compare": "true"}).json()
    assert long["compare"] is None and long["compare_available"] is False

    kpi = client.get(SUMMARY, params={"date_from": "2026-02-01", "date_to": "2026-03-31"}).json()["kpi"]
    assert (kpi["new_customers"], kpi["returning_customers"]) == (2, 0)
    kpi = client.get(SUMMARY, params={"date_from": "2026-01-11", "date_to": "2026-03-31"}).json()["kpi"]
    assert (kpi["new_customers"], kpi["returning_customers"]) == (2, 1)   # A公司首单 01-10


@pytest.mark.parametrize("d,months,expected", [
    (date(2026, 3, 31), -1, date(2026, 2, 28)),
    (date(2024, 3, 31), -1, date(2024, 2, 29)),
    (date(2026, 1, 15), -1, date(2025, 12, 15)),
    (date(2026, 12, 31), 1, date(2027, 1, 31)),
])
def test_shift_month(d, months, expected):
    from app.services.sales_dashboard import shift_month
    assert shift_month(d, months) == expected


@pytest.mark.parametrize("window,expected", [
    ((date(2026, 10, 21), date(2026, 10, 21)), (date(2026, 9, 21), date(2026, 9, 21))),
    ((date(2026, 10, 1), date(2026, 10, 31)), (date(2026, 9, 1), date(2026, 9, 30))),
    ((date(2026, 9, 30), date(2026, 10, 30)), None),          # 长于一个月（重叠）
    ((None, date(2026, 10, 1)), None),
])
def test_compare_window(window, expected):
    from app.services.sales_dashboard import compare_window
    assert compare_window(*window) == expected


# ================= PR-3：行级口径（商品大类 / 品牌，D-44 §8） =================

@pytest.fixture()
def lines_seeded(db, seeded):
    """在 seeded 基础上：A6 原行改为 硬盘/C（含税 9040），A6 再加一行 内存/B
    （数量 2 × 单价 100，行金额为空 → 含税 200；营收 177 成本 100）；A5 行大类置空。"""
    from sqlalchemy import select as _select
    a1, a2, a3, a4, a5, a6 = seeded["a"]
    la6 = db.scalar(_select(FSalesLine).where(FSalesLine.raw_line_id == "LA6"))
    la6.category_major, la6.brand = "硬盘", "C"
    la6.unit_price = la6.line_amount = Decimal("9040")
    la5 = db.scalar(_select(FSalesLine).where(FSalesLine.raw_line_id == "LA5"))
    la5.category_major = None
    db.add(FSalesLine(
        raw_line_id="LA6b", order_id=a6.id, part_id=la6.part_id, line_no=2,
        pn_std="PN-D44", category_major="内存", brand="B", qty=Decimal("2"),
        unit_price=Decimal("100"), line_amount=None, revenue_amount=Decimal("177"),
        counts_revenue=True, cost_moving_avg=Decimal("50"), cost_amount=Decimal("100"),
        import_batch_id=la6.import_batch_id,
    ))
    db.commit()
    return seeded


def test_line_mode_category_filter_only_counts_matching_lines(db, lines_seeded):
    client = _client(db, "d44-line-cat", role="boss")
    data = client.get(SUMMARY, params={"category": "硬盘"}).json()
    assert data["line_mode"] is True
    kpi = data["kpi"]
    assert kpi["orders"] == 1
    assert kpi["amount_inc_tax_total"] == pytest.approx(9040.0)
    assert kpi["amount_ex_tax_total"] == pytest.approx(8000.0)       # ÷1.13 名义
    assert kpi["revenue_total"] == pytest.approx(7000.0)
    assert kpi["gross_profit"] == pytest.approx(2000.0)
    items = client.get(ORDERS, params={"category": "内存", "sort": "order_date",
                                       "order": "asc"}).json()["items"]
    assert [i["order_no"] for i in items] == ["SO-A1", "SO-A2", "SO-A4", "SO-A6"]
    a6 = items[-1]                                                    # 只计 内存 那一行
    assert a6["amount_inc_tax"] == pytest.approx(200.0)
    assert a6["amount_ex_tax"] == pytest.approx(176.99)
    assert a6["revenue"] == pytest.approx(177.0)
    assert a6["gross_profit"] == pytest.approx(77.0)


def test_line_mode_uncategorized_and_brand(db, lines_seeded):
    client = _client(db, "d44-line-uncat", role="boss")
    assert _nos(client.get(ORDERS, params={"category": "未分类"})) == ["SO-A5"]
    assert _nos(client.get(ORDERS, params={"brand": "C"})) == ["SO-A6"]
    # 未选大类 / 品牌时仍是单头口径
    data = client.get(SUMMARY).json()
    assert data["line_mode"] is False and data["kpi"]["orders"] == 6


def test_group_by_category_line_level_sums(db, lines_seeded):
    client = _client(db, "d44-grp-cat", role="boss")
    data = client.get(GROUPS, params={"group_by": "category", "sort": "revenue",
                                      "top": 0}).json()
    assert data["line_level"] is True
    got = {g["key"]: (g["orders"], g["lines"], g["revenue"]) for g in data["groups"]}
    assert got == {"硬盘": (1, 1, 7000.0), "内存": (4, 5, 2677.0), "未分类": (1, 1, 100.0)}
    assert [g["key"] for g in data["groups"]] == ["硬盘", "内存", "未分类"]
    kpi = client.get(SUMMARY).json()["kpi"]
    assert sum(g["revenue"] for g in data["groups"]) == pytest.approx(kpi["revenue_total"])


def test_group_by_brand_with_category_filter_and_pn(db, lines_seeded):
    client = _client(db, "d44-grp-brand", role="boss")
    data = client.get(GROUPS, params={"group_by": "brand", "category": "内存", "top": 0}).json()
    assert [(g["key"], g["lines"]) for g in data["groups"]] == [("B", 5)]
    pn = client.get(GROUPS, params={"group_by": "pn", "top": 0}).json()
    assert [(g["key"], g["orders"]) for g in pn["groups"]] == [("PN-D44", 5)]  # A3 无明细


def test_options_categories_and_brands(db, lines_seeded):
    client = _client(db, "d44-opt-cat", role="boss")
    data = client.get(OPTIONS).json()
    assert {c["value"]: c["orders"] for c in data["categories"]} == {
        "内存": 4, "硬盘": 1, "未分类": 1}
    assert {b["value"]: b["orders"] for b in data["brands"]} == {"B": 5, "C": 1}
