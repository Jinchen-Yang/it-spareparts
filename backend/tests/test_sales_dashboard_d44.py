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
- 订单详情明细行与脱敏；options 新候选、客户补全与金额上限遮蔽；
- 本轮返工：第四对账列 return_reconciled_amount 排序、分组 lines / avg_order /
  真实同期变化 basis_change 排序、三口径区间 / options / 直方图共用整单基准。
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, permissions
from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimCustomer, DimPart
from app.models.sales import (FSalesLine, FSalesOrder,
                              FSalesReconciliationSnapshot)
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
    """Q-A-04：被遮蔽排序键回退**公开口径 order_date + id**，不退回 revenue——
    营收行序同样是排名信息。"""
    client = _client(db, "d44-noprofit", role="boss", overrides={"data_profit": False})
    data = client.get(ORDERS, params={"sort": "gross_profit"}).json()
    assert data["sort"] == "order_date"
    assert [i["order_no"] for i in data["items"]][0] == "SO-A6"   # order_date desc
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
    """Q-A-03/A-AC03 修订：金额区间信息整体走成本权限，三口径一视同仁——营收口径
    虽公开，区间筛选结果的单数同样泄漏金额分布，无例外。"""
    client = _client(db, "d44-range-mask", role="boss",
                     overrides={"data_purchase_cost": False})
    for params in ({"amount_min": "1"}, {"include_negative": "false"},
                   {"include_empty": "false", "amount_basis": "amount_inc_tax"},
                   {"amount_basis": "revenue", "amount_min": "1000"}):
        assert client.get(ORDERS, params=params).status_code == 403, params
        assert client.get(SUMMARY, params=params).status_code == 403, params
    # 无金额筛选时照常访问（营收口径可见）
    assert client.get(ORDERS, params={"amount_basis": "revenue", "sort": "revenue",
                                      "order": "desc"}).status_code == 200


def test_amount_range_intersects_permission_toggles(db, seeded):
    """Q-A-06 修订：许可开关与有限区间**同时满足（交集）**——include_negative=false
    时负区间内的负单同样排除（区间不绕过开关）；无金额单无法与有限区间比较，
    自然不入（NULL 比较不为真，include_empty 保留不了它）。开关单独（不限区间）
    语义不变。区间恒比整单口径（Q-A-02）。"""
    client = _client(db, "d44-range-prec", role="boss")
    # 负区间 ∩ 含负开关（默认 true）→ 区间内负单入选
    neg = client.get(ORDERS, params={"amount_min": "-600", "amount_max": "-400"})
    assert _nos(neg) == ["SO-A4"]
    # 同区间 + include_negative=false → 开关排除负单：交集为空（不再被区间绕过）
    strict = client.get(ORDERS, params={"amount_min": "-600", "amount_max": "-400",
                                        "include_negative": "false"})
    assert strict.status_code == 200 and strict.json()["items"] == []
    # 有限区间下无金额单（A5）无法比较；include_empty=true 也保留不了它
    wide = _nos(client.get(ORDERS, params={"amount_min": "-10000",
                                           "amount_max": "99999"}))
    assert sorted(wide) == ["SO-A1", "SO-A2", "SO-A3", "SO-A4", "SO-A6"]
    # 开关单独（不限区间）语义不变：负开关不含负单、无金额单保留
    no_neg = _nos(client.get(ORDERS, params={"include_negative": "false"}))
    assert "SO-A4" not in no_neg and "SO-A5" in no_neg
    # 0 起点区间：开关组合不改变结果（负单 / 无金额单本就不在 [0, ∞)）
    both = _nos(client.get(ORDERS, params={"amount_min": "0",
                                           "include_negative": "false",
                                           "include_empty": "false"}))
    assert sorted(both) == ["SO-A1", "SO-A2", "SO-A3", "SO-A6"]


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
    """data_profit 不可见：毛利键（登记）置空；revenue_costed / cost_coverage 不做
    全局登记（老板看板同名键公开），由本模块结构化置 null（先例 /api/profit）。"""
    client = _client(db, "d44-kpi-noprofit", role="boss", overrides={"data_profit": False})
    data = client.get(SUMMARY).json()
    kpi = data["kpi"]
    assert kpi["gross_profit"] is None and kpi["gross_margin"] is None
    assert kpi["revenue_costed"] is None and kpi["cost_coverage"] is None
    assert kpi["revenue_total"] == pytest.approx(9600.0)   # 营收公开不受影响
    # 对比期 KPI 同样收敛（递归）
    cmp = client.get(SUMMARY, params={"date_from": "2026-03-01", "date_to": "2026-03-05",
                                      "compare": "true"}).json()["compare"]
    assert cmp["kpi"]["revenue_costed"] is None and cmp["kpi"]["cost_coverage"] is None


def test_summary_coverage_zero_when_revenue_positive_but_uncosted(db, seeded):
    """Q-A-05：营收 > 0 且全未配成本 → 覆盖率 0%（确无已配事实），毛利待配为 null；
    无订单时不伪造 0 元业绩（合计 None）。"""
    client = _client(db, "d44-cov0", role="boss")
    kpi = client.get(SUMMARY, params={"customer_name": "__empty__"}).json()["kpi"]
    assert kpi["orders"] == 1                       # 只有 SO-A5（无客户）
    assert kpi["revenue_total"] == pytest.approx(100.0)
    assert kpi["revenue_costed"] is None            # 全未配成本
    assert kpi["gross_profit"] is None              # 待配成本，不当 0
    assert kpi["cost_coverage"] == 0.0              # 营收正数 → 覆盖率 0%
    empty = client.get(SUMMARY, params={"date_from": "2030-01-01",
                                        "date_to": "2030-12-31"}).json()["kpi"]
    assert empty["orders"] == 0
    assert empty["revenue_total"] is None and empty["cost_coverage"] is None


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
    """A-AC01/DL-006 修订：无成本权时行级**营收组合**随成交价一起遮蔽——
    qty / unit_price / line_amount / revenue 全空（阻断 营收 ÷ 数量 × 1.13 重建成交
    单价，反例 2000×1.13÷2=1130），只留身份与分类；单头聚合营收沿公开口径。"""
    a1 = seeded["a"][0]
    client = _client(db, "d44-detail-mask", role="boss",
                     overrides={"data_purchase_cost": False, "data_profit": False})
    data = client.get(f"{ORDERS}/{a1.id}").json()
    ln = data["lines"][0]
    assert ln["unit_price"] is None and ln["line_amount"] is None
    assert ln["qty"] is None and ln["revenue"] is None
    assert ln["gross_profit"] is None and ln["gross_margin"] is None
    assert ln["pn"] == "PN-D44" and ln["category_major"] == "内存"
    assert data["revenue"] == pytest.approx(900.0)     # 单头聚合营收公开口径不变


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
    # Q-A-03：三上限全部 null（营收上限也走成本权限），双保险见 config 登记
    assert data["amount_range"] == {
        "amount_ex_tax_max": None, "amount_inc_tax_max": None, "revenue_max": None}
    assert data["salespeople"]


def test_new_keys_registered_in_field_groups():
    groups = config.FIELD_GROUPS
    assert "customer_type" in groups["customer_info"]
    assert "customer_source" in groups["customer_info"]
    for key in ("amount_ex_tax_max", "amount_inc_tax_max", "revenue_max"):
        assert key in groups["purchase_cost"], key
    assert "gross_profit" in groups["profit_amount"]
    assert "gross_margin" in groups["profit_rate"]
    # revenue_costed / cost_coverage 刻意不全局登记（老板看板同名键公开），
    # 由销售分析 API 做结构收敛——见 test_summary_profit_masked_without_data_profit
    assert "revenue_costed" not in groups["profit_amount"]


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
    """A-AC06/DL-020：缺失组键为 null + is_empty（不用「未填」字面值），按指标排序
    时空指标沉底（A-AC08）。"""
    client = _client(db, "d44-grp-null", role="boss")
    data = client.get(GROUPS, params={"group_by": "customer", "top": 0, "order": "asc"}).json()
    keys = [g["key"] for g in data["groups"]]
    assert keys == ["C公司", "A公司", "B公司", None]     # 缺失组（无客户）ex 为空 → 沉底
    assert data["groups"][-1]["is_empty"] is True
    assert all(g["is_empty"] is False for g in data["groups"][:-1])
    assert data["other"] is None
    desc = client.get(GROUPS, params={"group_by": "customer", "top": 0,
                                      "order": "desc"}).json()
    assert [g["key"] for g in desc["groups"]][-1] is None   # 降序空指标同样沉底


def test_groups_real_empty_label_name_not_colliding(db, seeded):
    """A-AC06：真实客户名就叫「未填」时是独立一组（字符串键），与缺失组（null 键）
    分开；哨兵 __empty__ 只筛到真缺失，不误伤真实「未填」。"""
    a1 = seeded["a"][0]
    cw = DimCustomer(name_raw="未填", name_normalized="未填", customer_type="直客")
    db.add(cw)
    db.flush()
    db.add(FSalesOrder(
        raw_order_id="A8", order_no="SO-A8", order_date=date(2026, 3, 5),
        salesperson="张三", customer_id=cw.id, business_type="备件销售",
        amount_ex_tax=Decimal("500"), amount_inc_tax=Decimal("565"),
        data_status="已生效", import_batch_id=a1.import_batch_id))
    db.commit()
    client = _client(db, "d44-grp-weitian", role="boss")
    data = client.get(GROUPS, params={"group_by": "customer", "top": 0}).json()
    by_key = {g["key"]: g for g in data["groups"]}
    assert by_key["未填"]["is_empty"] is False and by_key["未填"]["orders"] == 1
    assert by_key[None]["is_empty"] is True and by_key[None]["orders"] == 1
    assert sorted(_nos(client.get(ORDERS, params={"customer_name": "__empty__"}))) == [
        "SO-A5"]                                     # 哨兵只匹配真缺失
    assert _nos(client.get(ORDERS, params={"customer_name": "未填"})) == ["SO-A8"]


def test_groups_sort_orders_asc_ties_by_key(db, seeded):
    client = _client(db, "d44-grp-sp", role="boss")
    data = client.get(GROUPS, params={
        "group_by": "salesperson", "sort": "orders", "order": "asc", "top": 0}).json()
    # 缺失组按 orders=1 正常参与排名（并列按键升序，None → "" 排最前）
    assert [(g["key"], g["orders"]) for g in data["groups"]] == [
        (None, 1), ("王五", 1), ("张三", 2), ("李四", 2)]


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
    # 未分类 = 缺失组（null 键 + is_empty，A-AC06），显示标签由前端按维度给
    assert got == {"硬盘": (1, 1, 7000.0), "内存": (4, 5, 2677.0), None: (1, 1, 100.0)}
    assert [g["key"] for g in data["groups"]] == ["硬盘", "内存", None]
    assert data["groups"][-1]["is_empty"] is True
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


# ================= PR-3：金额直方图 / 订单导出 =================

HIST = "/api/sales/analytics/amount-histogram"
EXPORT = "/api/sales/analytics/orders/export"


def test_amount_histogram_bins_align_to_1k(db, seeded):
    client = _client(db, "d44-hist", role="boss")
    data = client.get(HIST, params={"amount_max": "1"}).json()   # 金额区间本身不约束分布
    assert data["max"] == 8000.0
    assert [(b["lo"], b["hi"]) for b in data["bins"]][:2] == [(0.0, 1000.0), (1000.0, 2000.0)]
    # A-AC09 右端闭：8000 恰落段界，归入末段 [8000, 9000)，不丢单
    assert [b["orders"] for b in data["bins"]] == [0, 1, 1, 1, 0, 0, 0, 0, 1]
    assert data["negative_orders"] == 1 and data["empty_orders"] == 1
    assert data["total"] == 6
    assert sum(b["orders"] for b in data["bins"]) + 2 == data["total"]   # 守恒


@pytest.fixture()
def hist_seeded(db, seeded):
    """A-AC09 边界数据（2027 年隔离）：0 / 8001 / 8501 / 负 / 空金额各一张。"""
    a1 = seeded["a"][0]

    def add(raw, on, ex):
        db.add(FSalesOrder(
            raw_order_id=raw, order_no=f"SO-{raw}", order_date=on, salesperson="张三",
            business_type="备件销售", amount_ex_tax=Decimal(ex) if ex is not None else None,
            data_status="已生效", import_batch_id=a1.import_batch_id))
    add("H1", date(2027, 1, 1), "0")
    add("H2", date(2027, 1, 2), "8001")
    add("H3", date(2027, 1, 3), "8501")
    add("H4", date(2027, 1, 4), "-10")
    add("H5", date(2027, 1, 5), None)
    db.commit()
    return seeded


def test_amount_histogram_zero_boundary_negative_empty(db, hist_seeded):
    """A-AC09：0 / 8001 / 8501 / 负 / 空金额 / 空集全覆盖，逐单守恒。"""
    client = _client(db, "d44-hist-edge", role="boss")
    data = client.get(HIST, params={"date_from": "2027-01-01",
                                    "date_to": "2027-01-31"}).json()
    assert data["max"] == 8501.0
    # width = 1000（ceil(8501/20)→425.05→1k 整数倍）；n = floor(8501/1000)+1 = 9
    counts = [b["orders"] for b in data["bins"]]
    assert counts == [1, 0, 0, 0, 0, 0, 0, 0, 2]    # 0→[0,1000)；8001/8501→[8000,9000)
    assert data["negative_orders"] == 1 and data["empty_orders"] == 1
    assert data["total"] == 5
    assert sum(counts) + 2 == data["total"]
    empty = client.get(HIST, params={"date_from": "2028-01-01",
                                     "date_to": "2028-01-31"}).json()
    assert empty["bins"] == [] and empty["total"] == 0 and empty["max"] is None
    # 纯负 + 空金额（max < 0 不给段，单独计数）
    neg = client.get(HIST, params={"date_from": "2027-01-04",
                                   "date_to": "2027-01-05"}).json()
    assert neg["bins"] == [] and neg["negative_orders"] == 1 and neg["empty_orders"] == 1


def test_amount_histogram_masked_basis_403(db, seeded):
    """Q-A-03/A-AC03：直方图是金额分布信息，无成本权限三口径一律 403。"""
    client = _client(db, "d44-hist-mask", role="boss", overrides={"data_purchase_cost": False})
    assert client.get(HIST).status_code == 403
    assert client.get(HIST, params={"amount_basis": "revenue"}).status_code == 403
    assert client.get(HIST, params={"amount_basis": "amount_inc_tax"}).status_code == 403


def _csv_rows(resp):
    import csv
    import io
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))


def test_orders_export_matches_list_order(db, seeded):
    client = _client(db, "d44-export", role="boss")
    rows = _csv_rows(client.get(EXPORT, params={"sort": "order_date", "order": "asc"}))
    assert rows[0] == ["订单号", "订单日期", "客户", "客户类型", "销售人员", "业务类型",
                       "不含税金额", "含税订单金额", "营收", "毛利", "毛利率"]
    assert [r[0] for r in rows[1:]] == [f"SO-A{i}" for i in range(1, 7)]
    a4 = rows[4]
    assert a4[6] == "-500.0"                       # 负数是数值，不被当公式加撇号
    assert rows[5][2] == ""                        # A5 无客户


def test_orders_export_drops_masked_columns(db, seeded):
    client = _client(db, "d44-export-mask", role="boss", overrides={
        "data_purchase_cost": False, "data_profit": False, "data_customer": False})
    rows = _csv_rows(client.get(EXPORT))
    assert rows[0] == ["订单号", "订单日期", "销售人员", "业务类型", "营收"]
    # 默认按不含税排序被遮蔽 → 退回营收降序
    assert rows[1][0] == "SO-A6"


def test_orders_export_row_limit_413(db, seeded, monkeypatch):
    from app.services import sales_dashboard
    monkeypatch.setattr(sales_dashboard, "EXPORT_MAX_ROWS", 2)
    client = _client(db, "d44-export-413", role="boss")
    assert client.get(EXPORT).status_code == 413
    assert client.get(EXPORT, params={"customer_type": "直客"}).status_code == 200


# ================= 2026-10-22 轮复审新增验收 =================

def test_orders_export_formula_safety(db, seeded):
    """D-33（独立模块 sales_export_text）：= / @ / +/- 公式形态加撇号；负数、
    负 PN、普通文本不误伤。"""
    a1 = seeded["a"][0]
    db.add(FSalesOrder(
        raw_order_id="INJ", order_no="=SUM(A1:A9)", order_date=date(2026, 3, 6),
        salesperson="+HYPERLINK(\"http://x\")", business_type="@cmd|'/c calc'!A0",
        amount_ex_tax=Decimal("1"), amount_inc_tax=Decimal("1.13"),
        data_status="已生效", import_batch_id=a1.import_batch_id))
    db.commit()
    client = _client(db, "d44-export-inj", role="boss")
    rows = _csv_rows(client.get(EXPORT, params={"sort": "order_date", "order": "asc"}))
    inj = rows[-1]
    assert inj[0] == "'=SUM(A1:A9)"
    assert inj[4] == "'+HYPERLINK(\"http://x\")"
    assert inj[5] == "'@cmd|'/c calc'!A0"
    a4 = rows[4]                                       # SO-A4：-500 / -565 负数
    assert a4[6] == "-500.0" and a4[7] == "-565.0"     # 数值不被当公式


def test_orders_export_too_long_cell_413(db, seeded, monkeypatch):
    """单元格 32,767 上限 → 413（绝不 500）。库列 varchar 有限长，超长对抗串
    由 sales_export_text 抛 ExportTextTooLong（单测见 test_sales_export_text.py），
    这里验证 API 层 413 转换链路。"""
    from app.api import sales_analytics as api
    from app.services.sales_export_text import ExportTextTooLong

    def boom(v):
        if isinstance(v, str) and v.startswith("SO-A1"):
            raise ExportTextTooLong("CSV 单元格超过 32767 字符安全上限")
        return v
    monkeypatch.setattr(api, "safe_formula_text", boom)
    client = _client(db, "d44-export-413cell", role="boss")
    resp = client.get(EXPORT, params={"sort": "order_date", "order": "asc"})
    assert resp.status_code == 413


@pytest.mark.parametrize("role", ["readonly", "purchaser", "sales"])
def test_hard_role_check_rejects_non_admin_boss(db, seeded, role):
    """角色硬界（2026-10-22 轮复审）：purchaser / readonly / sales 即使被显式授予
    page_sales_analysis 也 403——页面键控制菜单，角色硬界控制数据面。"""
    client = _client(db, f"d44-role-{role}", role=role,
                     overrides={"page_sales_analysis": True})
    a1 = seeded["a"][0]
    paths = (SUMMARY, ORDERS, OPTIONS,
             f"{GROUPS}?group_by=salesperson", TREND, HIST, EXPORT, f"{ORDERS}/{a1.id}")
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 403, (path, resp.status_code)


def test_new_sort_columns_order_no_business_type(db, seeded):
    """A-AC07：所有可见数据列双向排序——订单号 / 业务类型 / 客户类型。"""
    client = _client(db, "d44-sort-new", role="boss")
    asc = _nos(client.get(ORDERS, params={"sort": "order_no", "order": "asc"}))
    assert asc == [f"SO-A{i}" for i in range(1, 7)]
    desc = _nos(client.get(ORDERS, params={"sort": "order_no", "order": "desc"}))
    assert desc == list(reversed(asc))
    bt = client.get(ORDERS, params={"sort": "business_type", "order": "desc"}).json()
    assert bt["sort"] == "business_type" and len(bt["items"]) == 6
    ct_asc = _nos(client.get(ORDERS, params={"sort": "customer_type", "order": "asc"}))
    ct_desc = _nos(client.get(ORDERS, params={"sort": "customer_type", "order": "desc"}))
    # 渠道(A1/A2) → 直客(A3/A6) → 类型空(A4) → 无客户(A5)；空值升降都沉底（id 同向）
    assert ct_asc[:2] == ["SO-A1", "SO-A2"] and ct_asc[-2:] == ["SO-A4", "SO-A5"]
    assert ct_desc[0] in ("SO-A3", "SO-A6") and ct_desc[-2:] == ["SO-A5", "SO-A4"]


@pytest.fixture()
def recon_seeded(db, seeded):
    """对账快照：A1 两条（旧 pending 800 → 新 pending 100，最新取新）；A6 一条
    （已对账 5000 / 退货已对账 300 / 待对账 4000）。"""
    a1 = seeded["a"][0]
    b2 = SysImportBatch(filename="d44-recon.xlsx", file_type="sales", file_hash="d44-h2")
    db.add(b2)
    db.flush()

    def snap(raw, batch, modified, reconciled, pending, returned=None):
        db.add(FSalesReconciliationSnapshot(
            raw_order_id=raw, order_no=f"SO-{raw}", import_batch_id=batch,
            source="import", source_modified_at=modified,
            reconciled_amount=Decimal(reconciled), pending_amount=Decimal(pending),
            return_reconciled_amount=Decimal(returned) if returned else None,
            content_hash=f"{raw}-{batch}-{modified:%Y%m%d%H%M}",
        ))

    snap("A1", a1.import_batch_id, datetime(2026, 3, 1, tzinfo=timezone.utc), "300", "800")
    snap("A1", b2.id, datetime(2026, 3, 5, tzinfo=timezone.utc), "350", "100")
    snap("A6", b2.id, datetime(2026, 3, 6, tzinfo=timezone.utc), "5000", "4000", "300")
    db.commit()
    return seeded


def test_recon_sort_columns_on_latest_snapshot(db, recon_seeded):
    """预审线索 1 反例（uv.lock 锁定 SQLAlchemy 2.0.49 无 ``Select.distinct_on``，
    旧实现组 SQL 前抛 AttributeError → 500）：对账三列排序必须经 DISTINCT ON
    最新快照子查询真实跑通——列表、include_returns 组合、导出同一 join。"""
    client = _client(db, "d44-recon-sort", role="boss")
    data = client.get(ORDERS, params={"sort": "pending_amount", "order": "desc",
                                      "page_size": 2}).json()
    assert data["sort"] == "pending_amount"
    assert data["items"][0]["order_no"] == "SO-A6"          # 待对账 4000 最大
    a1 = next(i for i in data["items"] if i["order_no"] == "SO-A1")
    assert a1["reconciliation"]["pending_amount"] == pytest.approx(100.0)  # 最新快照
    # reconciled_display 排序随定义：不含退货 = 已对账；含退货 = 已对账+退货已对账
    plain = client.get(ORDERS, params={"sort": "reconciled_display", "order": "desc",
                                       "page_size": 2}).json()
    assert plain["items"][0]["order_no"] == "SO-A6"         # 5000 > 350
    with_ret = client.get(ORDERS, params={"sort": "reconciled_display",
                                          "order": "desc", "page_size": 2,
                                          "include_returns": "true"}).json()
    assert with_ret["items"][0]["order_no"] == "SO-A6"      # 5300 > 350
    # 更新时间排序（无快照单 NULLS LAST）
    asof = client.get(ORDERS, params={"sort": "reconciled_as_of", "order": "desc",
                                      "page_size": 2}).json()
    assert asof["items"][0]["order_no"] == "SO-A6"          # 03-06 晚于 A1 03-05
    # 导出同排序键走同一子查询 join，不 500
    exp = client.get(EXPORT, params={"sort": "pending_amount"})
    assert exp.status_code == 200
    assert exp.text.splitlines()[1].split(",")[0] == "SO-A6"


def test_line_mode_range_filters_on_header_amounts(db, lines_seeded):
    """Q-A-02「整单入选、匹配行计额」：金额区间恒比原单头金额；行级筛选只改展示
    金额。A6 单头 8000 ≥ 8000 入选，展示金额 = 内存行合计 200。"""
    client = _client(db, "d44-line-range", role="boss")
    data = client.get(ORDERS, params={"category": "内存",
                                      "amount_min": "8000"}).json()
    assert [i["order_no"] for i in data["items"]] == ["SO-A6"]
    assert data["items"][0]["amount_inc_tax"] == pytest.approx(200.0)
    assert data["items"][0]["amount_ex_tax"] == pytest.approx(176.99)
    kpi = client.get(SUMMARY, params={"category": "内存",
                                      "amount_min": "8000"}).json()["kpi"]
    assert kpi["orders"] == 1 and kpi["amount_inc_tax_total"] == pytest.approx(200.0)


def test_line_mode_revenue_range_selects_on_whole_order_revenue(db, lines_seeded):
    """预审线索 2 / Q-A-02（revenue 修订）：营收口径区间比较用**整单营收**
    （header_revenue 相关子查询，与行级 join 无关）——先按整单营收入选，再算
    匹配行贡献。A6 整单营收 7177（硬盘 7000 + 内存 177）≥ 5000 入选、展示营收
    = 内存行 177；旧实现拿匹配行营收（177 < 5000）会把 A6 错排除。options 的
    金额上限同一整单比较域（行级筛选不取匹配行值）。"""
    client = _client(db, "d44-line-rev-range", role="boss")
    data = client.get(ORDERS, params={
        "category": "内存", "amount_basis": "revenue", "amount_min": "5000"}).json()
    assert [i["order_no"] for i in data["items"]] == ["SO-A6"]
    assert data["items"][0]["revenue"] == pytest.approx(177.0)   # 展示=匹配行贡献
    kpi = client.get(SUMMARY, params={"category": "内存", "amount_basis": "revenue",
                                      "amount_min": "5000"}).json()["kpi"]
    assert kpi["orders"] == 1 and kpi["revenue_total"] == pytest.approx(177.0)
    opt = client.get(OPTIONS, params={"category": "内存"}).json()["amount_range"]
    assert opt["revenue_max"] == pytest.approx(7177.0)           # 整单口径上限
    assert opt["amount_ex_tax_max"] == pytest.approx(8000.0)


@pytest.fixture()
def leaf_seeded(db, seeded):
    """A-AC04 叶子舍入：A1 追加两行 56.55（电容）——叶子上各自 ÷1.13 舍到分
    = 50.04 × 2 = 100.08；旧行为整单舍入（113.10 ÷ 1.13 = 100.00）会差 8 分。"""
    a1 = seeded["a"][0]
    part = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-D44"))
    for i, amt in enumerate(("56.55", "56.55"), 1):
        db.add(FSalesLine(
            raw_line_id=f"LL{i}", order_id=a1.id, part_id=part.id, line_no=10 + i,
            pn_std="PN-D44", category_major="电容", brand="B", qty=Decimal("1"),
            unit_price=Decimal(amt), line_amount=Decimal(amt),
            revenue_amount=Decimal("10"), counts_revenue=True,
            import_batch_id=a1.import_batch_id))
    db.commit()
    return seeded


def test_leaf_rounding_conservation_across_views(db, leaf_seeded):
    """A-AC04/DL-018：订单列表 / KPI / 分组对同一批叶子值求和——逐分守恒。"""
    client = _client(db, "d44-leaf", role="boss")
    p = {"category": "电容"}
    items = client.get(ORDERS, params={**p, "sort": "order_date"}).json()["items"]
    assert len(items) == 1
    assert items[0]["amount_inc_tax"] == pytest.approx(113.10)
    assert items[0]["amount_ex_tax"] == pytest.approx(100.08)   # 50.04 + 50.04
    kpi = client.get(SUMMARY, params=p).json()["kpi"]
    assert kpi["amount_ex_tax_total"] == pytest.approx(100.08)
    assert kpi["amount_inc_tax_total"] == pytest.approx(113.10)
    grp = client.get(GROUPS, params={**p, "group_by": "category", "top": 0}).json()
    cap = next(g for g in grp["groups"] if g["key"] == "电容")
    assert cap["amount_ex_tax"] == pytest.approx(100.08)
    assert cap["amount_inc_tax"] == pytest.approx(113.10)
    # 趋势（月桶）同一批叶子
    tr = client.get(TREND, params={**p, "date_from": "2026-01-01",
                                   "date_to": "2026-01-31",
                                   "granularity": "month"}).json()
    assert tr["buckets"][0]["amount_ex_tax"] == pytest.approx(100.08)


@pytest.fixture()
def nodate_seeded(db, seeded):
    a1 = seeded["a"][0]
    db.add(FSalesOrder(
        raw_order_id="ND1", order_no="SO-ND1", order_date=None, salesperson="张三",
        business_type="备件销售", amount_ex_tax=Decimal("42"),
        amount_inc_tax=Decimal("47.46"), data_status="已生效",
        import_batch_id=a1.import_batch_id))
    db.commit()
    return seeded


def test_trend_no_date_orders_counted_not_dropped(db, nodate_seeded):
    """A-AC05/DL-019：无日期单不入日期桶（无日期不可定位），但「全部」口径追加
    显式无日期桶（bucket=null + no_date=true，金额同口径），桶合计（含无日期桶）
    = KPI 合计；有日期区间时以 no_date_orders 计数提示，不静默消失。"""
    client = _client(db, "d44-nodate", role="boss")
    alld = client.get(TREND, params={"granularity": "month"}).json()
    assert alld["has_no_date"] is True and alld["no_date_orders"] == 1
    assert alld["no_date_amount_ex_tax"] == pytest.approx(42.0)
    assert alld["no_date_amount_inc_tax"] == pytest.approx(47.46)
    assert alld["no_date_revenue"] is None                # ND1 无明细行
    date_buckets = [b for b in alld["buckets"] if not b.get("no_date")]
    nd_bucket = [b for b in alld["buckets"] if b.get("no_date")]
    assert len(nd_bucket) == 1 and nd_bucket[0]["bucket"] is None
    assert nd_bucket[0]["orders"] == 1
    assert nd_bucket[0]["amount_ex_tax"] == pytest.approx(42.0)
    assert sum(b["orders"] for b in date_buckets) == 6
    kpi_all = client.get(SUMMARY).json()["kpi"]
    assert (sum(b["orders"] for b in alld["buckets"])
            == kpi_all["orders"] == 7)                    # 全部：含无日期桶守恒
    # 金额同样守恒（不只计数说明）：桶金额合计（含无日期桶）= KPI 金额合计
    assert (sum(b["amount_ex_tax"] or 0 for b in alld["buckets"])
            == pytest.approx(kpi_all["amount_ex_tax_total"]))
    assert (sum(b["revenue"] or 0 for b in alld["buckets"])
            == pytest.approx(kpi_all["revenue_total"]))
    assert (sum(b["amount_inc_tax"] or 0 for b in alld["buckets"])
            == pytest.approx(kpi_all["amount_inc_tax_total"]))
    bounded = client.get(TREND, params={"date_from": "2026-01-01",
                                        "date_to": "2026-03-31",
                                        "granularity": "month"}).json()
    assert bounded["no_date_orders"] == 1                 # 区间口径仍显式计数
    assert bounded["no_date_amount_ex_tax"] == pytest.approx(42.0)
    assert bounded["no_date_revenue"] is None
    assert all(not b.get("no_date") for b in bounded["buckets"])
    assert sum(b["orders"] for b in bounded["buckets"]) == 6
    kpi_b = client.get(SUMMARY, params={"date_from": "2026-01-01",
                                        "date_to": "2026-03-31"}).json()["kpi"]
    assert (sum(b["orders"] for b in bounded["buckets"]) == kpi_b["orders"] == 6)


def test_extreme_dates_never_500(db, seeded):
    """A-AC10：日历极值要么 4xx 要么正常，绝不 500；对比期越界按不可得处理。"""
    client = _client(db, "d44-extreme", role="boss")
    for g in ("day", "week", "month"):        # 超长区间 → 桶数超限 422
        resp = client.get(TREND, params={"date_from": "2026-01-01",
                                         "date_to": "9999-12-31", "granularity": g})
        assert resp.status_code == 422, (g, resp.status_code)
    # 极值月本身正常：9999-12 一个桶（步进越界在含 end 的桶后收尾）；上月可得
    for g in ("day", "month"):
        m = client.get(TREND, params={"date_from": "9999-12-01", "date_to": "9999-12-31",
                                      "granularity": g, "compare": "true"})
        assert m.status_code == 200, g
        body = m.json()
        assert body["buckets"], g
        assert body["compare_available"] is True
        assert body["compare_window"] == {"date_from": "9999-11-01",
                                          "date_to": "9999-11-30"}
    # 0001-01 无上一月：对比不可得，本体正常
    first = client.get(TREND, params={"date_from": "0001-01-01", "date_to": "0001-01-31",
                                      "granularity": "month", "compare": "true"})
    assert first.status_code == 200
    assert first.json()["compare_available"] is False
    s = client.get(SUMMARY, params={"date_from": "9999-12-01", "date_to": "9999-12-31"})
    assert s.status_code == 200 and s.json()["kpi"]["orders"] == 0
    g = client.get(GROUPS, params={"group_by": "salesperson",
                                   "date_from": "9999-12-01", "date_to": "9999-12-31"})
    assert g.status_code == 200 and g.json()["groups"] == []


def test_lifecycle_full_range_no_split(db, seeded):
    """Q-A-10：全范围（无下端点）不拆新老客——基线缺失，拆分无意义；有下端点时
    各计数就位（undetermined 防御位恒 0：在筛客户必有带日期单）。"""
    client = _client(db, "d44-lc-full", role="boss")
    kpi = client.get(SUMMARY).json()["kpi"]
    assert kpi["new_customers"] is None and kpi["returning_customers"] is None
    assert kpi["undetermined"] is None
    kpi2 = client.get(SUMMARY, params={"date_from": "2026-02-01",
                                       "date_to": "2026-03-31"}).json()["kpi"]
    assert kpi2["undetermined"] == 0 and kpi2["new_customers"] >= 0


def test_customer_aggregates_structurally_hidden_without_data_customer(db, seeded):
    """预审线索 6：data_customer 不可见时，客户派生聚合（customers / new /
    returning / undetermined）结构化置 null——count(distinct customer) 与新老客
    拆分本身是客户情报（基数 / 结构）；salespeople 等非客户聚合不受影响，
    有权限账号照常（对照，防结构收敛误伤）。"""
    client = _client(db, "d44-nocust-agg", role="boss", overrides={"data_customer": False})
    kpi = client.get(SUMMARY, params={"date_from": "2026-02-01",
                                      "date_to": "2026-03-31"}).json()["kpi"]
    assert kpi["customers"] is None
    assert kpi["new_customers"] is None and kpi["returning_customers"] is None
    assert kpi["undetermined"] is None
    assert kpi["salespeople"] == 3                      # 张三/王五/李四，非客户聚合
    grp = client.get(GROUPS, params={"group_by": "salesperson", "top": 0}).json()
    assert all(g["customers"] is None for g in grp["groups"])
    tr = client.get(TREND, params={"date_from": "2026-02-01", "date_to": "2026-03-31",
                                   "granularity": "month"}).json()
    assert all(b["customers"] is None for b in tr["buckets"])
    ok = _client(db, "d44-cust-agg-ok", role="boss")
    kpi2 = ok.get(SUMMARY, params={"date_from": "2026-02-01",
                                   "date_to": "2026-03-31"}).json()["kpi"]
    assert kpi2["customers"] == 2 and kpi2["salespeople"] == 3   # B公司/C公司


# ================= 本轮返工：第四对账列 / 分组排序补齐 / 整单基准同域 =================

@pytest.fixture()
def recon4_seeded(db, seeded):
    """第四对账列排序反例（反馈§2 冻结数据，2027 年隔离）：R1/R2/R3 各有有效最新
    快照（已对账 300/100/200、待对账 700/900/800、退货已对账 150/50/100、源修改
    时间 06-19/20/21）；R1 另有旧快照（999 系列）、R3 另有 source_modified_at 空
    且 id 更高的快照（排序 NULLS LAST 不得当选）；R4 无快照。"""
    a1 = seeded["a"][0]
    b4 = SysImportBatch(filename="d44-recon4.xlsx", file_type="sales", file_hash="d44-h4")
    b5 = SysImportBatch(filename="d44-recon4-old.xlsx", file_type="sales", file_hash="d44-h5")
    db.add_all([b4, b5])
    db.flush()

    for raw, on in (("R1", date(2027, 6, 10)), ("R2", date(2027, 6, 11)),
                    ("R3", date(2027, 6, 12)), ("R4", date(2027, 6, 13))):
        db.add(FSalesOrder(
            raw_order_id=raw, order_no=f"SO-{raw}", order_date=on, salesperson="张三",
            business_type="备件销售", amount_ex_tax=Decimal("1000"),
            data_status="已生效", import_batch_id=a1.import_batch_id))

    def snap(raw, batch, modified, reconciled, pending, returned):
        db.add(FSalesReconciliationSnapshot(
            raw_order_id=raw, order_no=f"SO-{raw}", import_batch_id=batch,
            source="import", source_modified_at=modified,
            reconciled_amount=Decimal(reconciled), pending_amount=Decimal(pending),
            return_reconciled_amount=Decimal(returned) if returned is not None else None,
            content_hash=(f"{raw}-{batch}-{modified:%Y%m%d%H%M}"
                          if modified is not None else f"{raw}-{batch}-null"),
        ))

    snap("R1", b4.id, datetime(2027, 6, 19, tzinfo=timezone.utc), "300", "700", "150")
    snap("R2", b4.id, datetime(2027, 6, 20, tzinfo=timezone.utc), "100", "900", "50")
    snap("R3", b4.id, datetime(2027, 6, 21, tzinfo=timezone.utc), "200", "800", "100")
    # R1 旧快照（06-01）：排序必须取最新 06-19 的值，不取 999
    snap("R1", b5.id, datetime(2027, 6, 1, tzinfo=timezone.utc), "999", "999", "999")
    # R3 空修改时间快照（id 更高）：source_modified_at NULLS LAST，不得当选
    snap("R3", b5.id, None, "888", "888", "888")
    db.commit()
    return seeded


def test_recon_return_amount_sort_fourth_column(db, recon4_seeded):
    """A-AC07 返工（反馈§2）：第四个对账可见列「退货已对账金额」双向排序——经
    DISTINCT ON 最新快照子查询取值（旧快照 / 空修改时间不得当选）、NULLS LAST
    两向、id 同向破并列翻页不漏不重；无对账权限回退 order_date（行序防泄漏）；
    导出同键走同一子查询 join 不 500。"""
    client = _client(db, "d44-recon4", role="boss")
    p = {"date_from": "2027-06-01", "date_to": "2027-06-30",
         "sort": "return_reconciled_amount"}
    desc = client.get(ORDERS, params={**p, "order": "desc", "page_size": 10}).json()
    assert desc["sort"] == "return_reconciled_amount"
    assert [i["order_no"] for i in desc["items"]] == ["SO-R1", "SO-R3", "SO-R2", "SO-R4"]
    r1 = desc["items"][0]["reconciliation"]          # 最新快照：150/700，非旧快照 999
    assert r1["return_reconciled_amount"] == pytest.approx(150.0)
    assert r1["pending_amount"] == pytest.approx(700.0)
    r3 = desc["items"][1]["reconciliation"]          # 空修改时间快照 888 不当选
    assert r3["return_reconciled_amount"] == pytest.approx(100.0)
    asc = client.get(ORDERS, params={**p, "order": "asc", "page_size": 10}).json()
    assert [i["order_no"] for i in asc["items"]] == ["SO-R2", "SO-R3", "SO-R1", "SO-R4"]
    # 无快照 R4 两向恒末尾；include_returns 只改 reconciled_display 展示，不改本列键
    with_ret = client.get(ORDERS, params={**p, "order": "desc", "page_size": 10,
                                          "include_returns": "true"}).json()
    assert [i["order_no"] for i in with_ret["items"]] == ["SO-R1", "SO-R3", "SO-R2", "SO-R4"]
    pages = []
    for pg in (1, 2):                                 # page_size=2 × 2 页不漏不重
        pages += [i["order_no"] for i in client.get(
            ORDERS, params={**p, "order": "desc", "page_size": 2, "page": pg}
        ).json()["items"]]
    assert pages == [i["order_no"] for i in desc["items"]]
    masked = _client(db, "d44-recon4-mask", role="boss",
                     overrides={"data_reconciliation": False})
    fell = masked.get(ORDERS, params={**p, "order": "desc"}).json()
    assert fell["sort"] == "order_date"
    assert [i["order_no"] for i in fell["items"]] == ["SO-R4", "SO-R3", "SO-R2", "SO-R1"]
    exp = client.get(EXPORT, params=p)
    assert exp.status_code == 200
    assert exp.text.splitlines()[1].split(",")[0] == "SO-R1"


@pytest.fixture()
def gsort_seeded(db, seeded):
    """分组排序反例数据（2027 年隔离，反馈§3）；销售用 ASCII 名保证并列破并列
    （组键升序）断言确定。当期 2027-03 / 对比期 2027-02：

    | 销售 | 当期 ex（单数） | 上期 ex | 变化率 |
    |---|---|---|---|
    | UP-A | 1000（1） | 900 | ≈+11.11% |
    | UP-B | 500（1） | 100 | +400% |
    | AVG-C | 300+300（2） | 240 | +150%，单均 300 |
    | TIE-A | 200（1） | 100 | +100% |
    | TIE-B | 400（1） | 200 | +100% |
    | ZERO | 300（1） | 0 | 基期 0 不可比 |
    | NEG | 200（1） | -100 | 基期负不可比 |
    | NEW | 100（1） | 上月无 | 不可比 |
    | NULLC | 空（1） | 400 | 本期未知不可比 |
    """
    a1 = seeded["a"][0]
    rows = [
        ("G1", date(2027, 3, 1), "UP-A", "1000", date(2027, 2, 1), "900"),
        ("G2", date(2027, 3, 2), "UP-B", "500", date(2027, 2, 2), "100"),
        ("G3", date(2027, 3, 3), "AVG-C", "300", date(2027, 2, 3), "240"),
        ("G4", date(2027, 3, 4), "AVG-C", "300", None, None),
        ("G5", date(2027, 3, 5), "TIE-A", "200", date(2027, 2, 5), "100"),
        ("G6", date(2027, 3, 6), "TIE-B", "400", date(2027, 2, 6), "200"),
        ("G7", date(2027, 3, 7), "ZERO", "300", date(2027, 2, 7), "0"),
        ("G8", date(2027, 3, 8), "NEG", "200", date(2027, 2, 8), "-100"),
        ("G9", date(2027, 3, 9), "NEW", "100", None, None),
        ("G10", date(2027, 3, 10), "NULLC", None, date(2027, 2, 10), "400"),
    ]
    for raw, on, sp, ex, pon, pex in rows:
        db.add(FSalesOrder(
            raw_order_id=raw, order_no=f"SO-{raw}", order_date=on, salesperson=sp,
            business_type="备件销售", amount_ex_tax=Decimal(ex) if ex is not None else None,
            data_status="已生效", import_batch_id=a1.import_batch_id))
        if pon is not None:
            db.add(FSalesOrder(
                raw_order_id=f"{raw}P", order_no=f"SO-{raw}P", order_date=pon,
                salesperson=sp, business_type="备件销售",
                amount_ex_tax=Decimal(pex) if pex is not None else None,
                data_status="已生效", import_batch_id=a1.import_batch_id))
    db.commit()
    return seeded


def test_groups_basis_change_sort_is_real_growth_not_amount(db, gsort_seeded):
    """反馈§3 数学反例：真实同期变化排序不得用当期金额冒充——UP-A 当期 1000
    （上期 900，≈+11.11%）与 UP-B 当期 500（上期 100，+400%）：金额降序 UP-A 先，
    变化降序 UP-B 先。基期 0 / 负 / 未知与本期未知 → 不可比较（Q-A-07，不用
    abs(基期) 造增长），两向空值恒末尾；同率并列按组键升序。"""
    client = _client(db, "d44-gch", role="boss")
    p = {"group_by": "salesperson", "top": 0, "compare": "true",
         "date_from": "2027-03-01", "date_to": "2027-03-31"}

    def keys(d):
        return [g["key"] for g in d["groups"]]

    amount = client.get(GROUPS, params={**p, "sort": "amount_ex_tax",
                                        "order": "desc"}).json()
    assert keys(amount) == ["UP-A", "AVG-C", "UP-B", "TIE-B", "ZERO",
                            "NEG", "TIE-A", "NEW", "NULLC"]
    change = client.get(GROUPS, params={**p, "sort": "basis_change",
                                        "order": "desc"}).json()
    assert change["sort"] == "basis_change"
    assert change["compare_window"] == {"date_from": "2027-02-01",
                                        "date_to": "2027-02-28"}
    assert keys(change) == ["UP-B", "AVG-C", "TIE-A", "TIE-B", "UP-A",
                            "NEG", "NEW", "NULLC", "ZERO"]
    assert keys(amount)[0] == "UP-A" and keys(change)[0] == "UP-B"   # 冒充反例
    assert keys(amount).index("UP-B") > keys(change).index("UP-B")
    upb = next(g for g in change["groups"] if g["key"] == "UP-B")
    assert upb["compare"]["amount_ex_tax"] == pytest.approx(100.0)   # compare 同源
    asc = client.get(GROUPS, params={**p, "sort": "basis_change",
                                     "order": "asc"}).json()
    assert keys(asc) == ["UP-A", "TIE-A", "TIE-B", "AVG-C", "UP-B",
                         "NEG", "NEW", "NULLC", "ZERO"]
    # 未开 compare：该列不可见，排序退化为组键序（全部不可比较），静默不报错
    nocmp = client.get(GROUPS, params={**p, "sort": "basis_change",
                                       "compare": "false"}).json()
    assert keys(nocmp) == sorted(keys(amount))


def test_groups_avg_order_sort_both_directions(db, gsort_seeded):
    """A-AC07 返工：单均（avg_order = 当前 amount_basis 口径 ÷ 组内去重单数）双向
    排序；AVG-C 总 600 / 2 单 = 300 与 ZERO（1 单 300）同值并列 → 组键升序破并列；
    NULLC 本期无金额单均未知 → 两向恒末尾。"""
    client = _client(db, "d44-avg", role="boss")
    p = {"group_by": "salesperson", "top": 0,
         "date_from": "2027-03-01", "date_to": "2027-03-31"}
    desc = client.get(GROUPS, params={**p, "sort": "avg_order", "order": "desc"}).json()
    assert [g["key"] for g in desc["groups"]] == ["UP-A", "UP-B", "TIE-B", "AVG-C",
                                                  "ZERO", "NEG", "TIE-A", "NEW", "NULLC"]
    assert desc["groups"][3]["avg_order"] == pytest.approx(300.0)    # AVG-C 600/2
    asc = client.get(GROUPS, params={**p, "sort": "avg_order", "order": "asc"}).json()
    assert [g["key"] for g in asc["groups"]] == ["NEW", "NEG", "TIE-A", "AVG-C",
                                                 "ZERO", "TIE-B", "UP-B", "UP-A", "NULLC"]


def test_groups_lines_sort_line_dims_only(db, lines_seeded):
    """A-AC07 返工：行级维度的「明细行」列可排序（按当前匹配行集合）；单头维度
    没有该列 → 422（服务层组合校验，可读报错不 500）。"""
    client = _client(db, "d44-lines", role="boss")
    desc = client.get(GROUPS, params={"group_by": "category", "sort": "lines",
                                      "order": "desc", "top": 0}).json()
    # 内存 5 行；未分类与硬盘各 1 行并列 → 组键升序（缺失组键 → "" 最前）
    assert [(g["key"], g["lines"]) for g in desc["groups"]] == [
        ("内存", 5), (None, 1), ("硬盘", 1)]
    asc = client.get(GROUPS, params={"group_by": "category", "sort": "lines",
                                     "order": "asc", "top": 0}).json()
    assert [(g["key"], g["lines"]) for g in asc["groups"]] == [
        (None, 1), ("硬盘", 1), ("内存", 5)]
    # 行级筛选下 lines 只数匹配行（反馈§3：按当前匹配行集合）
    filt = client.get(GROUPS, params={"group_by": "brand", "sort": "lines",
                                      "category": "内存", "top": 0}).json()
    assert [(g["key"], g["lines"]) for g in filt["groups"]] == [("B", 5)]
    guard = client.get(GROUPS, params={"group_by": "salesperson", "sort": "lines"})
    assert guard.status_code == 422
    assert "行级" in guard.json()["detail"]


def test_amount_range_and_histogram_share_whole_order_basis(db, lines_seeded):
    """反馈§1 冻结反例（三口径区间 / options / 直方图同域）：内存行匹配贡献
    inc 200 / ex 176.99 / 营收 177 远低于阈值，但整单 ex 8000 / inc 9040 /
    营收 7177——三口径有限区间入选、options 上限、直方图 max 与落桶全部按整单
    比较（旧实现取匹配行贡献会错排除 / 错分桶）。"""
    client = _client(db, "d44-whole", role="boss")
    p = {"category": "内存"}
    for basis, lo, display_key, display in (
        ("amount_ex_tax", "8000", "amount_ex_tax", 176.99),
        ("amount_inc_tax", "9040", "amount_inc_tax", 200.0),
        ("revenue", "7177", "revenue", 177.0),
    ):
        data = client.get(ORDERS, params={**p, "amount_basis": basis,
                                          "amount_min": lo}).json()
        assert [i["order_no"] for i in data["items"]] == ["SO-A6"], basis
        assert data["items"][0][display_key] == pytest.approx(display), basis
    opt = client.get(OPTIONS, params=p).json()["amount_range"]
    assert opt["amount_ex_tax_max"] == pytest.approx(8000.0)
    assert opt["amount_inc_tax_max"] == pytest.approx(9040.0)
    assert opt["revenue_max"] == pytest.approx(7177.0)
    hist = client.get(HIST, params=p).json()
    assert hist["max"] == 8000.0                       # 整单 ex，非匹配行 176.99
    assert hist["total"] == 4                          # A1/A2/A4/A6
    assert hist["negative_orders"] == 1 and hist["empty_orders"] == 0
    assert sum(b["orders"] for b in hist["bins"]) + 1 == hist["total"]
    a6_bin = next(b for b in hist["bins"] if b["lo"] == 8000.0)
    assert a6_bin["orders"] == 1                       # A6 整单 8000 落 [8000,9000)
    rev_hist = client.get(HIST, params={**p, "amount_basis": "revenue"}).json()
    assert rev_hist["max"] == 7177.0                   # 整单营收（A1 整单 900 次之）


# ---------- 客户数排序侧信道 + 派生排序权限核对（反馈§4A / Q-A-04） ----------

@pytest.fixture()
def cside_seeded(db, seeded):
    """客户数排序侧信道反例（反馈§4A 冻结数据，2027-04 隔离）：SA 三张单一规范
    客户（orders=3 / customers=1），SB 两张两个规范客户（orders=2 / customers=2）
    ——单数序与客户数序**两向都相反**，任何方向按隐藏客户数排都会被抓住。"""
    a1 = seeded["a"][0]
    ca = DimCustomer(name_raw="CS-A", name_normalized="CS-A")
    cb1 = DimCustomer(name_raw="CS-B1", name_normalized="CS-B1")
    cb2 = DimCustomer(name_raw="CS-B2", name_normalized="CS-B2")
    db.add_all([ca, cb1, cb2])
    db.flush()
    for i, cust in enumerate((ca, ca, ca, cb1, cb2)):
        db.add(FSalesOrder(
            raw_order_id=f"CS{i}", order_no=f"SO-CS{i}",
            order_date=date(2027, 4, 1 + i), salesperson="SA" if i < 3 else "SB",
            customer_id=cust.id, business_type="备件销售",
            amount_ex_tax=Decimal("100"), data_status="已生效",
            import_batch_id=a1.import_batch_id))
    db.commit()
    return seeded


def test_groups_customers_sort_no_side_channel_without_data_customer(db, cside_seeded):
    """反馈§4A / Q-A-04：无 data_customer 时 sort=customers 不得形成侧信道——计数
    值已在 _finish 置 null，但行序仍按真实客户数泄漏相对规模同样算泄露。退回公开
    单数并如实回显 orders（静默回退与被遮金额排序同风格）：降序 SA（3 单）先而非
    SB（2 客户）先，升序 SB 先而非 SA（1 客户）先——两向都与隐藏客户数序相反。"""
    client = _client(db, "d44-cside", role="boss", overrides={"data_customer": False})
    p = {"group_by": "salesperson", "sort": "customers", "top": 0,
         "date_from": "2027-04-01", "date_to": "2027-04-30"}
    desc = client.get(GROUPS, params={**p, "order": "desc"}).json()
    assert desc["sort"] == "orders"                    # 回退如实回显，不谎称已按客户数排
    assert [g["key"] for g in desc["groups"]] == ["SA", "SB"]   # 单数 3>2，非客户数 2>1
    assert all(g["customers"] is None for g in desc["groups"])  # 计数仍置 null（线索 6）
    asc = client.get(GROUPS, params={**p, "order": "asc"}).json()
    assert asc["sort"] == "orders"
    assert [g["key"] for g in asc["groups"]] == ["SB", "SA"]    # 单数 2<3，非客户数 1<2
    # 客户类属分组本身仍 403（组键是客户信息），回退不放宽那条硬界
    assert client.get(GROUPS, params={
        "group_by": "customer", "sort": "customers",
        "date_from": "2027-04-01", "date_to": "2027-04-30"}).status_code == 403


def test_groups_customers_sort_real_with_data_customer(db, cside_seeded):
    """有权对照组：data_customer 可见时客户数排序真实（降序 SB 2 客户先、计数
    可见、回显 customers）——回退守卫不吞权限内语义。"""
    client = _client(db, "d44-cside-ok", role="boss")
    data = client.get(GROUPS, params={
        "group_by": "salesperson", "sort": "customers", "order": "desc", "top": 0,
        "date_from": "2027-04-01", "date_to": "2027-04-30"}).json()
    assert data["sort"] == "customers"
    assert [(g["key"], g["customers"]) for g in data["groups"]] == [("SB", 2), ("SA", 1)]


@pytest.fixture()
def pubbasis_seeded(db, seeded):
    """派生金融排序公开口径反例（反馈§4A 顺带核对，2027-05 当期 / 2027-04 对比期）：
    营收序与不含税序**相反**——

    | 销售 | 当期 ex / 营收 | 上期 ex / 营收 | ex 单均 / 营收单均 | ex 变化 / 营收变化 |
    |---|---|---|---|---|
    | HI-R | 1000 / 5000 | 100 / 4000 | 1000 / 5000 | +900% / +25% |
    | LO-R | 5000 / 1000 | 4900 / 500 | 5000 / 1000 | ≈+2% / +100% |

    金额全遮（data_purchase_cost / data_profit 关）时 avg_order / basis_change 若按
    被遮 ex 口径排序，两向都会与公开营收口径反序——可被逐项抓住。"""
    a1 = seeded["a"][0]
    part = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-D44"))

    def order(raw, on, sp, ex, revenue):
        o = FSalesOrder(
            raw_order_id=raw, order_no=f"SO-{raw}", order_date=on, salesperson=sp,
            business_type="备件销售", amount_ex_tax=Decimal(ex),
            data_status="已生效", import_batch_id=a1.import_batch_id)
        db.add(o)
        db.flush()
        db.add(FSalesLine(
            raw_line_id=f"L{raw}", order_id=o.id, line_no=1, part_id=part.id,
            pn_std="PN-D44", category_major="内存", brand="B", qty=Decimal("1"),
            revenue_amount=Decimal(revenue), counts_revenue=True,
            import_batch_id=a1.import_batch_id))

    order("PB1", date(2027, 5, 1), "HI-R", "1000", "5000")
    order("PB1P", date(2027, 4, 1), "HI-R", "100", "4000")
    order("PB2", date(2027, 5, 2), "LO-R", "5000", "1000")
    order("PB2P", date(2027, 4, 2), "LO-R", "4900", "500")
    db.commit()
    return seeded


def test_groups_derived_sorts_use_public_basis_when_amounts_masked(db, pubbasis_seeded):
    """反馈§4A 顺带核对：金额全遮时 avg_order / basis_change 两个派生金融排序不得
    按被遮口径泄漏——口径先退回公开营收，排序值 = 展示值（不只遮输出键）。
    有权对照组按 ex 口径两向反序，证明遮蔽下断言不是数据巧合。"""
    masked = _client(db, "d44-pub-mask", role="boss",
                     overrides={"data_purchase_cost": False, "data_profit": False})
    p = {"group_by": "salesperson", "top": 0, "compare": "true",
         "date_from": "2027-05-01", "date_to": "2027-05-31"}
    avg = masked.get(GROUPS, params={**p, "sort": "avg_order", "order": "desc"}).json()
    assert avg["sort"] == "avg_order" and avg["amount_basis"] == "revenue"
    assert [(g["key"], g["avg_order"]) for g in avg["groups"]] == [
        ("HI-R", pytest.approx(5000.0)), ("LO-R", pytest.approx(1000.0))]
    assert all(g["amount_ex_tax"] is None for g in avg["groups"])   # 被遮键仍不可见
    chg = masked.get(GROUPS, params={**p, "sort": "basis_change", "order": "desc"}).json()
    assert chg["sort"] == "basis_change" and chg["amount_basis"] == "revenue"
    assert [g["key"] for g in chg["groups"]] == ["LO-R", "HI-R"]    # 营收 +100% > +25%
    hi = next(g for g in chg["groups"] if g["key"] == "HI-R")
    assert hi["compare"]["revenue"] == pytest.approx(4000.0)         # 对比期同公开口径
    # 有权对照组：ex 口径单均与变化序都与公开营收口径相反
    full = _client(db, "d44-pub-full", role="boss")
    avg_ex = full.get(GROUPS, params={**p, "sort": "avg_order", "order": "desc"}).json()
    assert avg_ex["amount_basis"] == "amount_ex_tax"
    assert [g["key"] for g in avg_ex["groups"]] == ["LO-R", "HI-R"]     # ex 单均 5000 先
    chg_ex = full.get(GROUPS, params={**p, "sort": "basis_change", "order": "desc"}).json()
    assert [g["key"] for g in chg_ex["groups"]] == ["HI-R", "LO-R"]     # ex +900% 先


def test_groups_service_sort_guards_direct(db, cside_seeded):
    """服务层双保险（与 orders() 同款先例，反馈§4A「服务/API 同源有效排序语义」）：
    groups 直调带 user_ctx 也不按被遮键排客户数——退回单数；不带 ctx 的内部取数
    路径行为不变（照排客户数，遮蔽交给调用方）。"""
    from app import security
    from app.services import sales_dashboard
    ctx = security.UserContext(
        user_id="u", role="custom",
        permissions={**permissions._full(), "data_customer": False},
        is_authenticated=True)
    data = sales_dashboard.groups(
        db, user_ctx=ctx, group_by="salesperson", sort="customers",
        order="desc", top=0, date_from=date(2027, 4, 1), date_to=date(2027, 4, 30))
    assert data["sort"] == "orders"
    assert [g["key"] for g in data["groups"]] == ["SA", "SB"]
    noc = sales_dashboard.groups(
        db, group_by="salesperson", sort="customers",
        order="desc", top=0, date_from=date(2027, 4, 1), date_to=date(2027, 4, 30))
    assert noc["sort"] == "customers"
    assert [g["key"] for g in noc["groups"]] == ["SB", "SA"]        # 内部路径照常


def test_groups_service_public_basis_direct(db, pubbasis_seeded):
    """服务层双保险（口径半边）：直调带 user_ctx 且请求被遮口径时，basis 同样
    退回公开营收——avg_order / basis_change 的排序值与展示值同源，不按被遮 ex 泄漏。"""
    from app import security
    from app.services import sales_dashboard
    ctx = security.UserContext(
        user_id="u", role="custom",
        permissions={**permissions._full(), "data_purchase_cost": False,
                     "data_profit": False},
        is_authenticated=True)
    data = sales_dashboard.groups(
        db, user_ctx=ctx, group_by="salesperson", sort="avg_order",
        order="desc", top=0, amount_basis="amount_ex_tax",
        date_from=date(2027, 5, 1), date_to=date(2027, 5, 31))
    assert data["amount_basis"] == "revenue"
    assert [g["key"] for g in data["groups"]] == ["HI-R", "LO-R"]


# ---------- 归一分组返工（A1 阻断：行级维度归一后重复 key / TopN+其他重复计额） ----------

@pytest.fixture()
def norm_seeded(db, seeded):
    """归一分组反例（A1 冻结场景，2027-06 当期 / 2027-05 对比期）：行级维度取
    NULL / 空串 / 半角双空格 / 全角空格 / 首尾空白变体，同一订单 3 行同组、
    同规范 PN 配不同 raw 别名、真实名「未填」——归一后必须各自合并为唯一 key。

    | 单 | 日期 | category | brand | pn_std / pn_raw | 行数 | 行金额 / 营收 |
    |---|---|---|---|---|---|---|
    | NA1 | 06-01 | NULL | NULL | NULL / NULL | 1 | 1130 / 900 |
    | NA2 | 06-02 | '' | '' | '' / '' | 1 | 1130 / 900 |
    | NA3 | 06-03 | '  ' | '　' | '  ' / '  ' | 1 | 1130 / 900 |
    | NB1 | 06-10 | ' 内存 ' | ' B牌 ' | ' PN-A ' / NULL | 1 | 1130 / 900 |
    | NB2 | 06-11 | 内存 | B牌 | PN-A / PN-RAW-X | 1 | 1130 / 900 |
    | NB3 | 06-12 | 内存 | B牌 | PN-A / PN-RAW-Y | 3 | 1130 / 900 |
    | NB4 | 06-13 | 内存 | B牌 | NULL / ' PN-B ' | 1 | 1130.55 / 900 |
    | NC1 | 06-20 | 未填 | 未填 | 未填 / 未填 | 1 | 1130.55 / 900 |
    | NM1 | 05-10 | ' 内存 ' | ' B牌 ' | ' PN-A ' / NULL | 1 | 1130 / 900 |

    单头 ex 1000 / inc 1130；行含税 = 行金额（叶子舍入），不含税 = ÷1.13 名义。
    """
    a1 = seeded["a"][0]
    part = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-D44"))
    rows = [
        ("NA1", date(2027, 6, 1), None, None, None, None, 1, "1130"),
        ("NA2", date(2027, 6, 2), "", "", "", "", 1, "1130"),
        ("NA3", date(2027, 6, 3), "  ", "　", "  ", "  ", 1, "1130"),
        ("NB1", date(2027, 6, 10), " 内存 ", " B牌 ", " PN-A ", None, 1, "1130"),
        ("NB2", date(2027, 6, 11), "内存", "B牌", "PN-A", "PN-RAW-X", 1, "1130"),
        ("NB3", date(2027, 6, 12), "内存", "B牌", "PN-A", "PN-RAW-Y", 3, "1130"),
        ("NB4", date(2027, 6, 13), "内存", "B牌", None, " PN-B ", 1, "1130.55"),
        ("NC1", date(2027, 6, 20), "未填", "未填", "未填", "未填", 1, "1130.55"),
        ("NM1", date(2027, 5, 10), " 内存 ", " B牌 ", " PN-A ", None, 1, "1130"),
    ]
    for raw, on, cat, brand, pn_std, pn_raw, nlines, amt in rows:
        o = FSalesOrder(
            raw_order_id=raw, order_no=f"SO-{raw}", order_date=on,
            salesperson="张三", business_type="备件销售",
            amount_ex_tax=Decimal("1000"), amount_inc_tax=Decimal("1130"),
            data_status="已生效", import_batch_id=a1.import_batch_id)
        db.add(o)
        db.flush()
        for i in range(1, nlines + 1):
            db.add(FSalesLine(
                raw_line_id=f"L{raw}-{i}", order_id=o.id, line_no=i, part_id=part.id,
                pn_std=pn_std, pn_raw=pn_raw, category_major=cat, brand=brand,
                qty=Decimal("1"), line_amount=Decimal(amt),
                revenue_amount=Decimal("900"), counts_revenue=True,
                import_batch_id=a1.import_batch_id))
    db.commit()
    return seeded


@pytest.mark.parametrize("dim", ["category", "brand", "pn"])
def test_line_groups_missing_variants_merge_to_single_null_group(db, norm_seeded, dim):
    """A1 反例①：NULL / 空串 / 半角双空格（brand 另有全角空格变体）归一后是
    同一个缺失组——1 个 key=null 组（orders=3 / lines=3），不是 3 个重复 null 组
    （旧实现 SELECT 归一、GROUP BY 原始列 → 同 key 多行）。"""
    client = _client(db, f"d44-norm-miss-{dim}", role="boss")
    data = client.get(GROUPS, params={
        "group_by": dim, "top": 0, "sort": "orders",
        "date_from": "2027-06-01", "date_to": "2027-06-05"}).json()
    assert data["total_groups"] == 1
    g = data["groups"][0]
    assert g["key"] is None and g["is_empty"] is True
    assert g["orders"] == 3 and g["lines"] == 3
    assert g["amount_ex_tax"] == pytest.approx(3000.0)
    assert g["amount_inc_tax"] == pytest.approx(3390.0)
    assert g["revenue"] == pytest.approx(2700.0)


@pytest.mark.parametrize("dim,other_shape", [
    ("category", (2, 4)), ("brand", (2, 4)), ("pn", (3, 5))])
def test_line_groups_topn_other_conserves_kpi_and_totals(db, norm_seeded, dim, other_shape):
    """A1 反例②（KPI 3000/3390/2700 vs 旧合计 4000/4520/3600）：A 窗口
    KPI = 分组（含其他）逐分守恒；全月 top=1 的 Top1 +「其他」= top=0 全组合
    计（三口径）——重复 key 时「其他」按 rest_keys 匹配会把头部同 key 行再计
    一遍（Top1 1000 + 其他 3000 = 4000 ≠ KPI 3000）。"""
    client = _client(db, f"d44-norm-cons-{dim}", role="boss")
    awin = {"date_from": "2027-06-01", "date_to": "2027-06-05"}
    kpi = client.get(SUMMARY, params=awin).json()["kpi"]
    assert kpi["amount_ex_tax_total"] == pytest.approx(3000.0)
    assert kpi["amount_inc_tax_total"] == pytest.approx(3390.0)
    assert kpi["revenue_total"] == pytest.approx(2700.0)
    for top in (0, 1):
        data = client.get(GROUPS, params={**awin, "group_by": dim, "top": top}).json()
        assert data["other"] is None                     # 只有一个组，无「其他」
        assert data["groups"][0]["amount_ex_tax"] == pytest.approx(3000.0)
        assert data["groups"][0]["amount_inc_tax"] == pytest.approx(3390.0)
        assert data["groups"][0]["revenue"] == pytest.approx(2700.0)
    # 全月：Top1 +「其他」= 全组合计（叶子舍入含 1130.55 → 1000.49 名义不含税）
    june = {"date_from": "2027-06-01", "date_to": "2027-06-30"}
    full = client.get(GROUPS, params={**june, "group_by": dim, "top": 0}).json()
    top1 = client.get(GROUPS, params={**june, "group_by": dim, "top": 1}).json()
    other = top1["other"]
    assert other is not None
    assert (other["groups"], other["orders"]) == other_shape
    for key in ("amount_ex_tax", "amount_inc_tax", "revenue"):
        assert (sum(g[key] for g in top1["groups"]) + other[key]
                == pytest.approx(sum(g[key] for g in full["groups"]))), key


@pytest.mark.parametrize("dim,expect", [
    ("category", [("内存", 4, 6, 6000.49, 6780.55, 5400.0),
                  (None, 3, 3, 3000.0, 3390.0, 2700.0),
                  ("未填", 1, 1, 1000.49, 1130.55, 900.0)]),
    ("brand", [("B牌", 4, 6, 6000.49, 6780.55, 5400.0),
               (None, 3, 3, 3000.0, 3390.0, 2700.0),
               ("未填", 1, 1, 1000.49, 1130.55, 900.0)]),
    ("pn", [("PN-A", 3, 5, 5000.0, 5650.0, 4500.0),
            (None, 3, 3, 3000.0, 3390.0, 2700.0),
            ("PN-B", 1, 1, 1000.49, 1130.55, 900.0),
            ("未填", 1, 1, 1000.49, 1130.55, 900.0)]),
])
def test_line_groups_trim_alias_dedup_and_unfilled_no_collision(db, norm_seeded, dim, expect):
    """A1 反例③：首尾空白（含全角）同名归同组；同规范 PN 配不同 raw 别名合并
    （pn_std 优先、NULL 才落 pn_raw 并同样归一）；同一订单 3 行同组 orders 按
    单去重（4 单非 6 单 / 3 单非 5 单）；真实名「未填」与缺失 null 不碰撞；
    逐行名义舍入（1130.55 → 不含税 1000.49）；PN-B 与 未填 同值并列按组键升序。"""
    client = _client(db, f"d44-norm-trim-{dim}", role="boss")
    data = client.get(GROUPS, params={
        "group_by": dim, "top": 0, "sort": "amount_ex_tax", "order": "desc",
        "date_from": "2027-06-01", "date_to": "2027-06-30"}).json()
    assert data["total_groups"] == len(expect)
    got = [(g["key"], g["orders"], g["lines"], g["amount_ex_tax"],
            g["amount_inc_tax"], g["revenue"]) for g in data["groups"]]
    assert [g[0] for g in got] == [e[0] for e in expect]      # 唯一 key、排序稳定
    for (k, o, l, ex, inc, rev), g in zip(expect, got):
        assert (k, o, l) == (g[0], g[1], g[2]), k
        assert ex == pytest.approx(g[3]) and inc == pytest.approx(g[4]), k
        assert rev == pytest.approx(g[5]), k
    assert data["groups"][1]["is_empty"] is True              # 缺失组标记


def test_line_groups_compare_and_filter_same_normalization(db, norm_seeded):
    """A1 反例④：对比期与行级筛选和分组同用一套归一——5 月 ' 内存 ' 订单归入
    内存 组的 compare（旧实现对比行也按原始列分组，' 内存 ' ≠ '内存' 对不上）；
    category=内存 精确筛选同样命中 ' 内存 ' 的行。"""
    client = _client(db, "d44-norm-cmp", role="boss")
    data = client.get(GROUPS, params={
        "group_by": "category", "top": 0, "compare": "true",
        "date_from": "2027-06-01", "date_to": "2027-06-30"}).json()
    assert data["compare_window"] == {"date_from": "2027-05-01", "date_to": "2027-05-30"}
    mem = next(g for g in data["groups"] if g["key"] == "内存")
    assert mem["compare"]["orders"] == 1
    assert mem["compare"]["amount_ex_tax"] == pytest.approx(1000.0)
    miss = next(g for g in data["groups"] if g["key"] is None)
    assert miss["compare"] is None                           # 对比期无缺失组行
    filt = client.get(GROUPS, params={
        "group_by": "brand", "category": "内存", "top": 0,
        "date_from": "2027-06-01", "date_to": "2027-06-30"}).json()
    assert [(g["key"], g["orders"], g["lines"]) for g in filt["groups"]] == [("B牌", 4, 6)]
