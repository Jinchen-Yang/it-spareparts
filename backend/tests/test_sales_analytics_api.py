"""销售分析接口（D-41 §1 / D-40 §4-§6，PR-3b）：权限矩阵、三金额口径、大单排序、对账展示。

覆盖面（对照 PR-3b 验收）：
- 权限矩阵 admin/boss 200，sales/purchaser/readonly 403，匿名 401；受限销售堵漏 403；
- 三金额排序 DESC NULLS LAST + id DESC 破并列，NULL 不当 0（列表值与 KPI 缺失计数）；
- business_type 多选/空/未知值；客户/销售员筛选与 data_customer 403 先例；
- 对账：无快照 / 多快照取最新（修改时间 → 批次 → id）/ 修改时间为 NULL / include_returns
  两态与默认 / 对账字段脱敏（data_reconciliation）；
- KPI = 列表合计；日期闭区间边界与 from>to 422；营收口径与老板看板 /dashboard/sales 回归一致。
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import config, permissions, security
from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimCustomer, DimPart
from app.models.sales import FSalesLine, FSalesOrder, FSalesReconciliationSnapshot
from app.models.system import SysImportBatch, SysUser
from app.services import dashboard, sales_analytics
from app.services.sales_reconciliation import snapshot_content_hash

PASSWORD = "synthetic-sales-analytics-1"
OPTIONS = "/api/sales/analytics/options"
SUMMARY = "/api/sales/analytics/summary"
ORDERS = "/api/sales/analytics/orders"

UTC = timezone.utc


def _client(db, username, *, role="readonly", overrides=None) -> TestClient:
    """实名账号 + 登录 token（boss_board_helpers.client_for 同款，本文件自带精简版）。"""
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


def _snapshot(db, batch_id, raw_order_id, *, modified, recon=None, pending=None,
              returns=None, order_amount=None, source="import"):
    snap = FSalesReconciliationSnapshot(
        raw_order_id=raw_order_id, import_batch_id=batch_id, source=source,
        source_modified_at=modified, order_amount=(
            Decimal(order_amount) if order_amount is not None else None),
        reconciled_amount=Decimal(recon) if recon is not None else None,
        pending_amount=Decimal(pending) if pending is not None else None,
        return_reconciled_amount=Decimal(returns) if returns is not None else None,
        content_hash=snapshot_content_hash(
            raw_order_id=raw_order_id, source_modified_at=modified,
            order_amount=Decimal(order_amount) if order_amount is not None else None,
            reconciled_amount=Decimal(recon) if recon is not None else None,
            pending_amount=Decimal(pending) if pending is not None else None,
            return_reconciled_amount=Decimal(returns) if returns is not None else None,
        ),
    )
    db.add(snap)
    return snap


@pytest.fixture()
def seeded(db):
    """5 张已生效单 + 1 张已取消单（O6，一切口径都必须排除）。

    - O2/O4/O5 缺含税金额（NULL 不当 0）；O5 连不含税金额也缺（排序沉底）；
    - O2/O3 不含税金额并列 2000（id 破并列）；O3 无明细行（营收 NULL）；
    - O4 的明细行 counts_revenue=False（营收被 REVENUE_BUSINESS_TYPES 口径排除）；
    - 对账快照：O1 三条（同修改时间比批次 / NULL 修改时间让位）、O3 一条带退货、
      O4 一条全空金额（has_data=True 但值全 None）。
    """
    b1 = SysImportBatch(filename="sa1.xlsx", file_type="sales", file_hash="sa-h1")
    b2 = SysImportBatch(filename="sa2.xlsx", file_type="sales", file_hash="sa-h2")
    b3 = SysImportBatch(filename="sa3.xlsx", file_type="sales", file_hash="sa-h3")
    db.add_all([b1, b2, b3])
    db.flush()
    part = DimPart(pn_std="PN-SA", brand="B")
    db.add(part)
    db.flush()
    c1 = DimCustomer(name_raw="客户甲", name_normalized="客户甲")
    c2 = DimCustomer(name_raw="客户乙", name_normalized="客户乙")
    db.add_all([c1, c2])
    db.flush()

    def order(raw, no, on, *, ex=None, inc=None, bt="备件销售", cust=c1.id,
              sp="张三", status="已生效"):
        o = FSalesOrder(
            raw_order_id=raw, order_no=no, order_date=on, salesperson=sp,
            customer_id=cust, business_type=bt,
            amount_ex_tax=Decimal(ex) if ex is not None else None,
            amount_inc_tax=Decimal(inc) if inc is not None else None,
            data_status=status, import_batch_id=b1.id,
        )
        db.add(o)
        db.flush()
        return o

    def line(o, raw, *, revenue=None, counts=True):
        db.add(FSalesLine(
            raw_line_id=raw, order_id=o.id, part_id=part.id, qty=Decimal("1"),
            revenue_amount=Decimal(revenue) if revenue is not None else None,
            counts_revenue=counts, import_batch_id=b1.id,
        ))

    o1 = order("R1", "SO-1", date(2026, 1, 10), ex="1000", inc="1130")
    line(o1, "L1", revenue="900")
    o2 = order("R2", "SO-2", date(2026, 1, 11), ex="2000", inc=None)
    o3 = order("R3", "SO-3", date(2026, 2, 5), ex="2000", inc="2260",
               bt="整机销售", cust=c2.id, sp="李四")
    o4 = order("R4", "SO-4", date(2026, 2, 20), ex="500", inc=None)
    line(o4, "L4", revenue="700", counts=False)
    o5 = order("R5", "SO-5", date(2026, 3, 1), ex=None, inc=None)
    order("R6", "SO-6", date(2026, 3, 2), ex="300", inc="339",
          bt="整机销售", status="已取消")

    # 对账快照（D-40 排序键：修改时间 DESC NULLS LAST → 批次 DESC → id DESC）
    _snapshot(db, b1.id, "R1", modified=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
              recon="100", pending="50", returns="10", order_amount="1130")
    _snapshot(db, b2.id, "R1", modified=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
              recon="110", pending="40", returns=None, order_amount="1130")
    _snapshot(db, b3.id, "R1", modified=None, recon="999")  # NULL 修改时间永远让位
    _snapshot(db, b2.id, "R3", modified=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
              recon="200", pending="60", returns="30", order_amount="2260")
    _snapshot(db, b1.id, "R4", modified=datetime(2026, 8, 2, 9, 0, tzinfo=UTC))  # 全空金额
    db.commit()
    return {"orders": (o1, o2, o3, o4, o5), "batches": (b1, b2, b3)}


# ---------- 权限矩阵 ----------

@pytest.mark.parametrize("path", [OPTIONS, SUMMARY, ORDERS, ORDERS + "/1"])
def test_anonymous_is_401(db, seeded, path):
    assert TestClient(app).get(path).status_code == 401


@pytest.mark.parametrize("role,expected", [
    ("admin", 200), ("boss", 200),
    ("sales", 403), ("purchaser", 403), ("readonly", 403),
])
def test_permission_matrix(db, seeded, role, expected):
    client = _client(db, f"sa-{role}", role=role)
    for path in (OPTIONS, SUMMARY, ORDERS, ORDERS + "/1"):
        assert client.get(path).status_code == expected, (role, path)


def test_scoped_sales_with_page_grant_still_403(db, seeded):
    """堵漏：即便逐账号授予页面键，受限销售（own_customers_only）也不得看全公司大单。"""
    client = _client(db, "sa-scoped", role="sales",
                     overrides={"page_sales_analysis": True})
    for path in (OPTIONS, SUMMARY, ORDERS, ORDERS + "/1"):
        assert client.get(path).status_code == 403, path


def test_customer_filter_without_customer_permission_403(db, seeded):
    """客户类筛选硬界：关客户数据组（boss 模板 overrides）按客户筛选 403；不筛选
    可访问且客户字段被脱敏。角色硬界（非 admin/boss 一律 403）另见
    test_permission_matrix 与 D-44 test_hard_role_check—— purchaser/readonly
    即便显式授予页面键也进不了本页，不再作客户筛选反例。"""
    client = _client(db, "sa-nocust", role="boss",
                     overrides={"data_customer": False})
    assert client.get(SUMMARY, params={"customer": "客户甲"}).status_code == 403
    assert client.get(ORDERS, params={"customer": "客户甲"}).status_code == 403
    # 不按客户筛选可正常访问（客户字段本身被 data_customer 脱敏）
    ok = client.get(ORDERS)
    assert ok.status_code == 200
    assert ok.json()["items"][0]["customer"] is None


# ---------- options ----------

def test_options_values_and_counts_active_only(db, seeded):
    client = _client(db, "sa-opt", role="boss")
    data = client.get(OPTIONS).json()
    assert data["business_types"] == [
        {"value": "备件销售", "orders": 4},   # O1/O2/O4/O5
        {"value": "整机销售", "orders": 1},   # O3（O6 已取消不计）
    ]


# ---------- summary / KPI ----------

def test_summary_kpi_full_window(db, seeded):
    client = _client(db, "sa-sum", role="boss")
    kpi = client.get(SUMMARY).json()["kpi"]
    assert kpi["orders"] == 5
    assert kpi["amount_ex_tax_total"] == pytest.approx(5500.0)   # NULL(O5) 跳过
    assert kpi["amount_inc_tax_total"] == pytest.approx(3390.0)  # 1130 + 2260
    assert kpi["amount_inc_tax_missing"] == 3                    # O2/O4/O5
    assert kpi["revenue_total"] == pytest.approx(900.0)          # O4 行不计营收


def test_summary_empty_window_returns_none_not_zero(db, seeded):
    client = _client(db, "sa-empty", role="boss")
    kpi = client.get(SUMMARY, params={
        "date_from": "2025-01-01", "date_to": "2025-01-31"}).json()["kpi"]
    assert kpi["orders"] == 0
    assert kpi["amount_ex_tax_total"] is None    # 查无数据 ≠ 合计为 0
    assert kpi["amount_inc_tax_total"] is None
    assert kpi["revenue_total"] is None


def test_summary_date_boundaries_closed_interval(db, seeded):
    client = _client(db, "sa-date", role="boss")
    # 单日：from=to 闭区间两端都含
    single = client.get(SUMMARY, params={
        "date_from": "2026-02-05", "date_to": "2026-02-05"}).json()["kpi"]
    assert single["orders"] == 1
    assert single["amount_ex_tax_total"] == pytest.approx(2000.0)
    # 跨月：1 月中 → 2 月中（两端边界日都计入）
    cross = client.get(SUMMARY, params={
        "date_from": "2026-01-11", "date_to": "2026-02-20"}).json()["kpi"]
    assert cross["orders"] == 3                  # O2/O3/O4
    assert cross["amount_ex_tax_total"] == pytest.approx(4500.0)


@pytest.mark.parametrize("path", [SUMMARY, ORDERS])
def test_date_from_after_date_to_422(db, seeded, path):
    client = _client(db, "sa-422", role="boss")
    r = client.get(path, params={"date_from": "2026-02-01", "date_to": "2026-01-01"})
    assert r.status_code == 422


def test_summary_business_type_multi_empty_unknown(db, seeded):
    client = _client(db, "sa-bt", role="boss")
    # 多选
    multi = client.get(SUMMARY, params=[
        ("business_type", "备件销售"), ("business_type", "整机销售")]).json()["kpi"]
    assert multi["orders"] == 5
    # 单选
    one = client.get(SUMMARY, params={"business_type": "整机销售"}).json()["kpi"]
    assert one["orders"] == 1
    assert one["amount_ex_tax_total"] == pytest.approx(2000.0)
    # 未知值：空结果（0 单、合计 None），不是报错也不是全量
    unknown = client.get(SUMMARY, params={"business_type": "不存在的类型"}).json()["kpi"]
    assert unknown["orders"] == 0
    assert unknown["amount_ex_tax_total"] is None
    # 空白值：静默丢弃 = 不筛选（绝不能变成 IN ('') 的 0 单）
    blank = client.get(SUMMARY, params=[
        ("business_type", ""), ("business_type", " ")]).json()["kpi"]
    assert blank["orders"] == 5


def test_summary_customer_and_salesperson_filters(db, seeded):
    client = _client(db, "sa-cust", role="boss")
    # O1/O2/O4/O5 默认挂客户甲，O3 挂客户乙
    assert client.get(SUMMARY, params={"customer": "客户甲"}).json()["kpi"]["orders"] == 4
    assert client.get(SUMMARY, params={"customer": "客户乙"}).json()["kpi"]["orders"] == 1
    assert client.get(SUMMARY, params={"customer": "客户"}).json()["kpi"]["orders"] == 5  # ILIKE
    assert client.get(SUMMARY, params={"salesperson": "李四"}).json()["kpi"]["orders"] == 1


def test_summary_kpi_amount_totals_masked_without_purchase_cost(db, seeded):
    """KPI 金额合计随 data_purchase_cost 遮蔽（#397 (4)）：合计键与行金额同组，
    关闭时 amount_*_total 置 null；计数类（orders / missing）与营收合计保留。"""
    client = _client(db, "sa-mask-kpi", role="boss",
                     overrides={"data_purchase_cost": False})
    kpi = client.get(SUMMARY).json()["kpi"]
    assert kpi["orders"] == 5
    assert kpi["amount_inc_tax_missing"] == 3
    assert kpi["amount_ex_tax_total"] is None
    assert kpi["amount_inc_tax_total"] is None
    assert kpi["revenue_total"] == pytest.approx(900.0)   # 营收口径公开


def test_customer_and_salesperson_wildcards_literal_only(db, seeded):
    """ILIKE 通配符转义（#397 (5)）：% / _ / \\ 按字面匹配，不再当通配符。

    子串匹配照常（%客户% 命中双方），但裸 % 不能再当「任意串」全命中、
    _ 不能再当单字符通配（旧实现客户_ / 张_ 会分别命中 5 / 4 单）。"""
    client = _client(db, "sa-esc", role="boss")
    kpi = lambda **p: client.get(SUMMARY, params=p).json()["kpi"]  # noqa: E731
    assert kpi(customer="客户")["orders"] == 5            # 子串 ILIKE 不受影响
    assert kpi(customer="%")["orders"] == 0               # 裸 % = 字面百分号
    assert kpi(customer="_")["orders"] == 0
    assert kpi(customer="客户_")["orders"] == 0           # 旧：单字符通配命中 5
    assert kpi(salesperson="张_")["orders"] == 0          # 旧：命中张三的 4 单
    assert kpi(salesperson="李四")["orders"] == 1         # 精确子串不受影响
    assert kpi(customer="客户甲\\%")["orders"] == 0       # 转义写法本身也是字面


@pytest.mark.parametrize("term", ["%", "_", "\\"])
@pytest.mark.parametrize("field", ["customer", "salesperson"])
def test_customer_and_salesperson_literal_characters_match(db, seeded, field, term):
    """字面特殊字符必须命中实际含该字符的名称，且排除普通名称与其他字符。"""
    batch = seeded["batches"][0]
    expected_no = None
    for index, char in enumerate(["%", "_", "\\", "普通"]):
        name = f"字面{char}名称"
        customer = DimCustomer(name_raw=name, name_normalized=name)
        db.add(customer)
        db.flush()
        order_no = f"SO-LITERAL-{index}"
        db.add(FSalesOrder(
            raw_order_id=f"R-LITERAL-{index}", order_no=order_no,
            order_date=date(2026, 1, 10), customer_id=customer.id,
            salesperson=name, business_type="备件销售", data_status="已生效",
            import_batch_id=batch.id,
        ))
        if char == term:
            expected_no = order_no
    db.commit()
    client = _client(db, "sa-literal-positive", role="boss")
    params = {field: term}
    summary = client.get(SUMMARY, params=params)
    assert summary.status_code == 200, summary.text
    assert summary.json()["kpi"]["orders"] == 1
    orders = client.get(ORDERS, params=params)
    assert orders.status_code == 200, orders.text
    assert [item["order_no"] for item in orders.json()["items"]] == [expected_no]


def test_reconciliation_timestamps_independent_of_session_timezone(db, seeded):
    """timestamptz 输出统一 UTC 带偏移（#397 carry-over）：会话时区切到
    Asia/Shanghai 后，reconciled_as_of / created_at 与 UTC 会话逐字一致——
    PG 会话时区原本会改变 aware datetime 的原生 isoformat 偏移。"""
    o1 = seeded["orders"][0]
    as_of = date(2026, 10, 17)
    orders_utc = sales_analytics.orders(db, as_of=as_of)
    detail_utc = sales_analytics.order_detail(db, o1.id, as_of=as_of)
    original_tz = db.execute(text("SHOW TIME ZONE")).scalar()
    db.execute(text("SET TIME ZONE 'Asia/Shanghai'"))
    try:
        orders_sh = sales_analytics.orders(db, as_of=as_of)
        detail_sh = sales_analytics.order_detail(db, o1.id, as_of=as_of)
    finally:
        db.execute(text(f"SET TIME ZONE '{original_tz}'"))
    r_utc = next(i for i in orders_utc["items"] if i["order_no"] == "SO-1")
    r_sh = next(i for i in orders_sh["items"] if i["order_no"] == "SO-1")
    assert r_sh["reconciliation"] == r_utc["reconciliation"]
    assert r_utc["reconciliation"]["reconciled_as_of"].endswith("+00:00")
    assert r_utc["reconciliation"]["reconciled_as_of"] == \
        datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc).isoformat()
    hist_utc = detail_utc["reconciliation_history"]
    hist_sh = detail_sh["reconciliation_history"]
    assert len(hist_utc) == 3
    assert [h["created_at"] for h in hist_sh] == [h["created_at"] for h in hist_utc]
    assert all(h["created_at"].endswith("+00:00") for h in hist_sh)
    assert [h["reconciled_as_of"] for h in hist_sh] == \
        [h["reconciled_as_of"] for h in hist_utc]


def test_kpi_equals_list_totals(db, seeded):
    """D-41 验收钩子：KPI 合计 = 大单列表逐页累计（同一 SQL 口径）。"""
    client = _client(db, "sa-kpi", role="boss")
    orders_count = ex_total = inc_total = inc_missing = rev_total = 0
    page = 1
    while True:
        data = client.get(ORDERS, params={
            "sort": "amount_ex_tax", "page": page, "page_size": 2}).json()
        for it in data["items"]:
            ex_total += it["amount_ex_tax"] or 0
            inc_total += it["amount_inc_tax"] or 0
            inc_missing += it["amount_inc_tax"] is None
            rev_total += it["revenue"] or 0
        orders_count += len(data["items"])
        if page * 2 >= data["total"]:
            break
        page += 1
    kpi = client.get(SUMMARY).json()["kpi"]
    assert orders_count == kpi["orders"] == 5
    assert ex_total == pytest.approx(kpi["amount_ex_tax_total"])
    assert inc_total == pytest.approx(kpi["amount_inc_tax_total"])
    assert inc_missing == kpi["amount_inc_tax_missing"]
    assert rev_total == pytest.approx(kpi["revenue_total"])


# ---------- orders：三金额排序 / 分页 ----------

def test_orders_sort_amount_ex_tax_desc_nulls_last(db, seeded):
    client = _client(db, "sa-s1", role="boss")
    items = client.get(ORDERS, params={"sort": "amount_ex_tax"}).json()["items"]
    assert [i["order_no"] for i in items] == ["SO-3", "SO-2", "SO-1", "SO-4", "SO-5"]
    # SO-3/SO-2 并列 2000 → id 大者（后建）在前；SO-5 NULL 沉底


def test_orders_sort_amount_inc_tax_nulls_last(db, seeded):
    client = _client(db, "sa-s2", role="boss")
    items = client.get(ORDERS, params={"sort": "amount_inc_tax"}).json()["items"]
    assert [i["order_no"] for i in items] == ["SO-3", "SO-1", "SO-5", "SO-4", "SO-2"]
    # 值域 2260/1130 之后，三个 NULL 按 id DESC 破并列（5>4>2）


def test_orders_sort_revenue(db, seeded):
    client = _client(db, "sa-s3", role="boss")
    items = client.get(ORDERS, params={"sort": "revenue"}).json()["items"]
    assert [i["order_no"] for i in items] == ["SO-1", "SO-5", "SO-4", "SO-3", "SO-2"]
    # O1 计营收 900 在前；O3 无明细行、O4 行被 counts_revenue 排除 → NULL 沉底


def test_orders_sort_degrades_to_public_date_when_amount_masked(db, seeded):
    """Q-A-04：data_purchase_cost=False 的账号按被遮金额键排序 → 行序泄漏金额
    排名，退回公开口径 **order_date + id**（不退回 revenue——营收行序同样是
    排名信息；dashboard ranking_restricted 同款先例）。默认排序与显式
    amount_ex_tax/amount_inc_tax 都降级，响应回显实际排序键。

    旧用例走 maintenance_manager + 页面键授予；本轮角色硬界后非 admin/boss
    一律 403，改用 boss 模板关成本组做同一反例。
    """
    client = _client(db, "sa-s4", role="boss",
                     overrides={"data_purchase_cost": False})
    for params in ({}, {"sort": "amount_ex_tax"}, {"sort": "amount_inc_tax"}):
        data = client.get(ORDERS, params=params).json()
        assert data["sort"] == "order_date", params
        assert [i["order_no"] for i in data["items"]] == [
            "SO-5", "SO-4", "SO-3", "SO-2", "SO-1"]      # order_date desc + id desc
        assert data["items"][0]["amount_ex_tax"] is None  # 值本身仍被遮蔽
    # revenue 公开口径不受降级影响
    data = client.get(ORDERS, params={"sort": "revenue"}).json()
    assert data["items"][0]["revenue"] == pytest.approx(900.0)


def test_orders_pagination_stable_no_dup_no_loss(db, seeded):
    client = _client(db, "sa-page", role="boss")
    seen = []
    for page in (1, 2, 3):
        data = client.get(ORDERS, params={
            "sort": "amount_ex_tax", "page": page, "page_size": 2}).json()
        assert data["total"] == 5
        seen += [i["order_no"] for i in data["items"]]
    assert len(seen) == len(set(seen)) == 5
    assert seen == ["SO-3", "SO-2", "SO-1", "SO-4", "SO-5"]


def test_orders_business_type_multi(db, seeded):
    client = _client(db, "sa-bt2", role="boss")
    data = client.get(ORDERS, params=[
        ("business_type", "整机销售"), ("business_type", "回收销售")]).json()
    assert [i["order_no"] for i in data["items"]] == ["SO-3"]   # O6 已取消不计


# ---------- 对账块 ----------

def test_reconciliation_latest_snapshot_selection(db, seeded):
    """多快照取最新：修改时间 DESC NULLS LAST → 批次 DESC（R1 三条快照）。"""
    client = _client(db, "sa-recon", role="boss")
    items = client.get(ORDERS).json()["items"]
    by_no = {i["order_no"]: i["reconciliation"] for i in items}
    # R1：b2(07-01, recon=110) 胜 b1(07-01, recon=100)（同修改时间批次大者胜），
    # b3(NULL 修改时间, recon=999) 永远让位
    r1 = by_no["SO-1"]
    assert r1["has_data"] is True
    assert r1["reconciled_amount"] == pytest.approx(110.0)
    assert r1["pending_amount"] == pytest.approx(40.0)
    assert r1["order_amount_at_snapshot"] == pytest.approx(1130.0)
    assert r1["reconciled_as_of"] == datetime(2026, 7, 1, 9, 0, tzinfo=UTC).isoformat()


def test_reconciliation_no_snapshot_and_all_null(db, seeded):
    client = _client(db, "sa-recon2", role="boss")
    items = client.get(ORDERS).json()["items"]
    by_no = {i["order_no"]: i["reconciliation"] for i in items}
    # 无快照：has_data=False，金额全 null（不是 0）
    r2 = by_no["SO-2"]
    assert r2 == {"has_data": False, "reconciled_as_of": None,
                  "order_amount_at_snapshot": None, "reconciled_amount": None,
                  "pending_amount": None, "return_reconciled_amount": None,
                  "reconciled_display": None}
    # 有快照但金额全空单元格：has_data=True、值仍全 null
    r4 = by_no["SO-4"]
    assert r4["has_data"] is True
    assert r4["reconciled_amount"] is None
    assert r4["reconciled_display"] is None


def test_include_returns_default_false_and_true(db, seeded):
    client = _client(db, "sa-ret", role="boss")
    # 默认不含退货：display = 已对账
    items = client.get(ORDERS).json()["items"]
    by_no = {i["order_no"]: i["reconciliation"] for i in items}
    assert by_no["SO-3"]["reconciled_display"] == pytest.approx(200.0)
    assert by_no["SO-1"]["reconciled_display"] == pytest.approx(110.0)  # 退货 NULL 不补 0
    # 含退货：display = 已对账 + 退货已对账（不写死扣减）
    items = client.get(ORDERS, params={"include_returns": "true"}).json()["items"]
    by_no = {i["order_no"]: i["reconciliation"] for i in items}
    assert by_no["SO-3"]["reconciled_display"] == pytest.approx(230.0)
    assert by_no["SO-1"]["reconciled_display"] == pytest.approx(110.0)
    # 订单详情端点同开关（SO-3 最新快照 recon=200/ret=30；SO-1 双向同值不可作证）
    o3 = seeded["orders"][2]
    d_default = client.get(f"{ORDERS}/{o3.id}").json()
    d_true = client.get(
        f"{ORDERS}/{o3.id}", params={"include_returns": "true"}).json()
    assert d_default["reconciliation"]["reconciled_display"] == pytest.approx(200.0)
    assert d_true["reconciliation"]["reconciled_display"] == pytest.approx(230.0)


def test_order_detail_history_and_latest(db, seeded):
    client = _client(db, "sa-det", role="boss")
    o1 = seeded["orders"][0]
    detail = client.get(f"{ORDERS}/{o1.id}").json()
    # 历史每个快照一条，旧→新（NULL 修改时间最旧），末条 = 最新块
    history = detail["reconciliation_history"]
    assert len(history) == 3
    assert [h["import_batch_id"] for h in history] == [
        seeded["batches"][2].id,   # b3：NULL 修改时间排最旧
        seeded["batches"][0].id,   # b1：07-01 批次小
        seeded["batches"][1].id,   # b2：07-01 批次大 = 最新
    ]
    assert history[-1]["reconciled_amount"] == pytest.approx(110.0)
    assert detail["reconciliation"]["reconciled_amount"] == pytest.approx(110.0)
    assert detail["amount_inc_tax"] == pytest.approx(1130.0)
    assert detail["customer"] == "客户甲"


def test_order_detail_404(db, seeded):
    client = _client(db, "sa-404", role="boss")
    assert client.get(f"{ORDERS}/999999").status_code == 404
    o6_id = db.query(FSalesOrder.id).filter_by(order_no="SO-6").scalar()
    assert client.get(f"{ORDERS}/{o6_id}").status_code == 404   # 已取消单与列表同口径


# ---------- 脱敏 ----------

def test_data_reconciliation_hidden_masks_reconciliation_fields(db, seeded):
    """data_reconciliation 关闭 → 对账**整块**按「无快照」形态输出（#397 (2)）：
    has_data=False、块内全 null、history=[]——与真实无快照单（SO-2）完全一致，
    连 has_data / reconciled_as_of / import_batch_id 这类非金额字段也不泄漏
    「该单有对账数据」。（角色硬界后 readonly 即便授予页面键也 403，反例改用
    boss 模板关对账组。）"""
    client = _client(db, "sa-mask", role="boss",
                     overrides={"data_reconciliation": False})
    items = client.get(ORDERS).json()["items"]
    by_no = {i["order_no"]: i["reconciliation"] for i in items}
    empty = {"has_data": False, "reconciled_as_of": None,
             "order_amount_at_snapshot": None, "reconciled_amount": None,
             "pending_amount": None, "return_reconciled_amount": None,
             "reconciled_display": None}
    assert by_no["SO-1"] == empty        # 有三条快照也整块置空
    assert by_no["SO-2"] == empty        # 真实无快照：两种成因形态一致、不可区分
    o1 = seeded["orders"][0]
    detail = client.get(f"{ORDERS}/{o1.id}").json()
    assert detail["reconciliation"] == empty
    assert detail["reconciliation_history"] == []


def test_service_level_masking_direct(db, seeded):
    """服务层直调 + apply_field_visibility（test_dashboard_masking 同款先例）。"""
    ctx = security.UserContext(
        user_id="u", role="custom",
        permissions={**permissions._full(), "data_reconciliation": False},
        is_authenticated=True)
    # 新契约（#397 (2)）：服务层带 ctx 直调 → 无权限时整块「无快照」形态（快照表都不查）
    hidden = sales_analytics.orders(db, ctx, as_of=date(2026, 10, 17))
    so1h = next(i for i in hidden["items"] if i["order_no"] == "SO-1")
    assert so1h["reconciliation"]["has_data"] is False
    assert so1h["reconciliation"]["reconciled_amount"] is None
    # 不带 ctx 的旧路径：服务层照常取数，遮蔽交给 apply_field_visibility 这一层
    data = sales_analytics.orders(db, as_of=date(2026, 10, 17))
    masked = security.apply_field_visibility(data, ctx)
    so1 = next(i for i in masked["items"] if i["order_no"] == "SO-1")
    assert so1["reconciliation"]["reconciled_amount"] is None
    assert so1["reconciliation"]["has_data"] is True
    # 开关打开时原值可见
    ctx_on = security.UserContext(
        user_id="u", role="custom", permissions=permissions._full(),
        is_authenticated=True)
    visible = security.apply_field_visibility(
        sales_analytics.orders(db, ctx_on, as_of=date(2026, 10, 17)), ctx_on)
    so1v = next(i for i in visible["items"] if i["order_no"] == "SO-1")
    assert so1v["reconciliation"]["reconciled_amount"] == pytest.approx(110.0)


# ---------- 营收口径回归（与老板看板同源） ----------

@pytest.fixture()
def seeded_via_loader(db):
    """走真实导入 + 利润引擎（revenue_amount/counts_revenue 由 profit.recompute 落库）。"""
    from app.etl import loader
    from app.services import profit
    from tests import factories as f

    b = SysImportBatch(filename="sa.xlsx", file_type="sales", file_hash="sa-load")
    db.add(b)
    db.flush()
    sorders = {
        "S1": f.sales_head("S1", order_no="SO-L1", on=date(2026, 2, 1)),
        "S2": f.sales_head("S2", order_no="SO-L2", on=date(2026, 2, 2),
                           business_type="整机销售"),
    }
    slines = [
        f.sales_line("S1", "SL1", "PN-A", qty="1", price="226"),
        f.sales_line("S1", "SL2", "PN-B", qty="2", price="113"),
        f.sales_line("S2", "SL3", "PN-A", qty="1", price="226"),
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    return b


def test_revenue_caliber_matches_dashboard(db, seeded_via_loader):
    """营收 = 计营收行 revenue_amount 之和（REVENUE_BUSINESS_TYPES 口径落库的
    counts_revenue），与 dashboard.sales_orders 逐单一致——本模块只消费不重算。"""
    as_of = date(2027, 1, 1)   # 不设未来上界的对照口径
    mine = sales_analytics.orders(db, as_of=as_of)["items"]
    board = dashboard.sales_orders(db, as_of=as_of)["items"]
    board_by_no = {i["order_no"]: i for i in board}
    assert {i["order_no"] for i in mine} == set(board_by_no) == {"SO-L1", "SO-L2"}
    for item in mine:
        ref = board_by_no[item["order_no"]]
        assert item["revenue"] == pytest.approx(ref["total_revenue"]), item["order_no"]
    # SO-L1: A 1*226/1.13 + B 2*113/1.13 = 200 + 200；SO-L2 整机销售不计营收 → None
    by_no = {i["order_no"]: i for i in mine}
    assert by_no["SO-L1"]["revenue"] == pytest.approx(400.0)
    assert by_no["SO-L2"]["revenue"] is None


# ---------- FIELD_GROUPS / 权限键登记契约 ----------

def test_reconciliation_keys_registered_in_field_groups():
    """对账字段名逐字登记（漏登记 = 绕过 data_reconciliation 泄漏，config.py:687 前车之鉴）。"""
    group = set(config.FIELD_GROUPS["sales_reconciliation"])
    emitted = {
        "reconciled_amount", "pending_amount", "return_reconciled_amount",
        "order_amount_at_snapshot", "reconciled_as_of", "reconciled_display",
    }
    assert emitted <= group
    # 通用键名绝不入组：会把其他接口同名/as_of 等字段一并误遮
    for generic in ("as_of", "order_amount", "has_data", "created_at",
                    "source", "import_batch_id", "revenue"):
        assert generic not in group, generic
    # 权限开关 ↔ 字段组一一对应
    assert permissions.DATA_GROUPS["data_reconciliation"] == ["sales_reconciliation"]


def test_public_kpi_keys_not_accidentally_masked():
    """KPI 营收口径键保持公开（营收是经营报表公开口径，DATA_DATA_DEPENDENCIES 注）；
    销售单两金额沿用 purchase_cost 既有键名（保守遮蔽）。"""
    all_fields = {f for fields in config.FIELD_GROUPS.values() for f in fields}
    for public_key in ("revenue", "revenue_total", "orders",
                       "amount_inc_tax_missing", "window", "filters_echo"):
        assert public_key not in all_fields, public_key
    # 两金额合计键与列表行金额同组（#397 (4)）：合计值也是金额，
    # 漏登记会绕过 data_purchase_cost 泄漏采购/销售总额
    assert {"amount_ex_tax", "amount_inc_tax",
            "amount_ex_tax_total", "amount_inc_tax_total"} <= set(
                config.FIELD_GROUPS["purchase_cost"])
    assert "customer" in set(config.FIELD_GROUPS["customer_info"])


def test_permission_keys_registered():
    assert "page_sales_analysis" in permissions.PAGE_KEYS
    assert "data_reconciliation" in permissions.DATA_GROUPS
    for key in ("page_sales_analysis", "data_reconciliation"):
        assert permissions.LABELS[key]
        meta = permissions.PERMISSION_META[key]
        for field in ("label", "summary", "can", "cannot", "typical",
                      "sensitivity", "risk"):
            assert meta.get(field), (key, field)
    # UI 矩阵自动收录（page/data 组按 PAGE_KEYS/DATA_GROUPS 生成）
    page_group = next(g for g in permissions.UI_GROUPS if g["key"] == "page")
    data_group = next(g for g in permissions.UI_GROUPS if g["key"] == "data")
    assert "page_sales_analysis" in page_group["keys"]
    assert "data_reconciliation" in data_group["keys"]


def test_role_template_defaults():
    """admin 恒放行、boss 默认开、其余角色（含 guest 兜底）失败关闭。"""
    assert permissions.effective("admin", None)["page_sales_analysis"] is True
    assert permissions.effective("admin", None)["data_reconciliation"] is True
    assert permissions.effective("boss", None)["page_sales_analysis"] is True
    assert permissions.effective("boss", None)["data_reconciliation"] is True
    for role in ("sales", "purchaser", "readonly", "guest", "maintenance_manager"):
        eff = permissions.effective(role, None)
        assert eff["page_sales_analysis"] is False, role
        assert eff["data_reconciliation"] is False, role
    # readonly 是匿名兜底模板：两键必须显式 False（防 _full() 继承 True）
    assert permissions.ROLE_TEMPLATES["readonly"]["page_sales_analysis"] is False
    assert permissions.ROLE_TEMPLATES["readonly"]["data_reconciliation"] is False


# ---------------------------------------------------------- 整数参数上界（编码检察官 #398 同类排查）

from app.api.query_bounds import ANALYTICS_PAGE_MAX, PG_INT_MAX  # noqa: E402


@pytest.mark.parametrize("page", [0, ANALYTICS_PAGE_MAX + 1, 10**18, 2**63])
def test_orders_page_out_of_bounds_422(db, seeded, page):
    client = _client(db, "sa-page-bound", role="boss")
    r = client.get(ORDERS, params={"page": page})
    assert r.status_code == 422, r.text


def test_orders_page_at_upper_bound_ok(db, seeded):
    client = _client(db, "sa-page-max", role="boss")
    r = client.get(ORDERS, params={"page": ANALYTICS_PAGE_MAX, "page_size": 200})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("oid", [0, -1, PG_INT_MAX + 1, 10**20])
def test_order_detail_id_out_of_bounds_422(db, seeded, oid):
    client = _client(db, "sa-id-bound", role="boss")
    r = client.get(f"{ORDERS}/{oid}")
    assert r.status_code == 422, r.text


def test_order_detail_id_at_upper_bound_404(db, seeded):
    client = _client(db, "sa-id-max", role="boss")
    r = client.get(f"{ORDERS}/{PG_INT_MAX}")
    assert r.status_code == 404, r.text
