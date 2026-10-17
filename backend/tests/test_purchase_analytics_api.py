"""采购分析 API（D-39 / 客户需求 Round1 PR-3a）。

覆盖：Q6 税率 SQL CASE 与 Python 参照逐行对拍（6 税组合 + 历史缺口回退）、
D-39 补充决策的单头批次行过滤（11 单残留形态：同行号新旧各一条 / 旧批次多行号 /
单头批次无任何行的边界）、日期闭区间与空区间、各筛选单测+组合+多选、分组=KPI、
下钻=分组、分页排序稳定、权限矩阵（admin/boss 200；sales/purchaser/readonly 403；
显式授权放行；scoped 收紧账号 fail-closed）、金额/客户脱敏、422/404/401。

夹具直接 ORM 插入（不走 loader）：混合批次形态 loader 造不出来——它一次调用
只能整批同 batch，而残留形态恰恰要求同一张单挂两个批次的行。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import (DimCustomer, DimPart, FPurchaseLine, FPurchaseOrder,
                        FSalesOrder)
from app.models.system import SysImportBatch
from app.services import purchase_analysis, purchase_analytics
from app.services.purchase_analytics import (NO_LINKED_LABEL, UNCATEGORIZED_LABEL,
                                             EMPTY_LABEL, _line_amount_exprs,
                                             _order_amount_exprs,
                                             line_amounts_reference,
                                             order_amounts_reference)
from tests.boss_board_helpers import client_for

D = Decimal

# ---------------------------------------------------------------- 夹具


def _batch(db: Session, name: str) -> SysImportBatch:
    b = SysImportBatch(filename=f"{name}.xlsx", file_type="purchase",
                       file_hash=f"analytics-{name}", uploaded_by="analytics", status="success")
    db.add(b)
    db.flush()
    return b


def _part(db: Session, pn: str, category: str | None) -> DimPart:
    p = DimPart(pn_std=pn, category_major=category)
    db.add(p)
    db.flush()
    return p


def _customer(db: Session, name: str) -> DimCustomer:
    c = DimCustomer(name_raw=name, name_normalized=name)
    db.add(c)
    db.flush()
    return c


def _sales(db: Session, raw_id: str, order_no: str, salesperson: str,
           customer: DimCustomer, batch: SysImportBatch) -> FSalesOrder:
    s = FSalesOrder(raw_order_id=raw_id, order_no=order_no, salesperson=salesperson,
                    customer_id=customer.id, data_status="已生效",
                    import_batch_id=batch.id)
    db.add(s)
    db.flush()
    return s


def _po(db: Session, raw_id: str, batch: SysImportBatch, *, on: date,
        source_type: str | None, purchaser: str | None = None,
        linked: str | None = None, is_inc: bool | None = None,
        rate: Decimal | None = None, amount_ex: Decimal | None = None,
        amount_inc: Decimal | None = None,
        status: str = "已生效") -> FPurchaseOrder:
    po = FPurchaseOrder(
        raw_order_id=raw_id, order_no=f"CG-{raw_id}", order_date=on,
        purchaser=purchaser, source_type=source_type, source_type_raw=source_type,
        linked_sales_order_no=linked, is_tax_inclusive=is_inc, tax_rate=rate,
        amount_ex_tax=amount_ex, amount_inc_tax=amount_inc, data_status=status,
        import_batch_id=batch.id)
    db.add(po)
    db.flush()
    return po


def _line(db: Session, raw_id: str, po: FPurchaseOrder, part: DimPart,
          batch: SysImportBatch, *, line_no: int, amount: Decimal,
          qty: str = "1", price: str | None = None) -> FPurchaseLine:
    ln = FPurchaseLine(
        raw_line_id=raw_id, order_id=po.id, line_no=line_no, part_id=part.id,
        pn_std=part.pn_std, qty=D(qty),
        unit_price=D(price) if price else D(amount) / D(qty), line_amount=amount,
        import_batch_id=batch.id)
    db.add(ln)
    db.flush()
    return ln


@pytest.fixture()
def ds(db: Session):
    """核心数据集（返回命名空间；期望值见各断言旁注释）。

    形态覆盖（对齐库里 11 张残留单的实测形态，D-39 补充决策 2026-10-17）：
    - poA：同行号 line_no=1 新旧批次各一条 + 旧批次多出 line_no=3（形态 a+b）
    - poC：同行号残留（形态 a）
    - poD：单头批次无任何行、只有旧批次行（形态 d 边界：计 0 行不报错）
    - poE：已取消（固定已生效口径的排除证据）
    - 税组合：inc×13% / ex×未标 / inc×6% / ex×0 / ex×0.6% / is_inc=None
    """
    nb, ob = _batch(db, "new"), _batch(db, "old")
    p1 = _part(db, "PN-DQ", "电器件")
    p2 = _part(db, "PN-FL", None)          # 未分类
    p3 = _part(db, "PN-JX", "机械件")
    pres = _part(db, "PN-RES", "残留类")    # 只被旧批次残留行引用（钉 options 批次过滤）
    c1, c2, c3 = _customer(db, "华东电力"), _customer(db, "华南钢铁"), _customer(db, "华南钢铁二期")
    s1 = _sales(db, "XS-001", "XS-001", "销售小王", c1, nb)
    s2 = _sales(db, "XS-002-A", "XS-002", "销售小李", c2, nb)
    s2_dup = _sales(db, "XS-002-B", "XS-002", "销售小李", c3, nb)  # 同单号重复（id 更大）
    s3 = _sales(db, "XS-003", "XS-003", "销售小王", c1, nb)
    assert s2_dup.id > s2.id

    poa = _po(db, "PA", nb, on=date(2026, 3, 5), source_type="销售订单", purchaser="采购员甲",
              linked="XS-001", is_inc=True, rate=D("0.13"), amount_ex=D("1000"),
              amount_inc=D("1130"))
    _line(db, "PA-L1", poa, p1, nb, line_no=1, amount=D("500"))
    _line(db, "PA-L2", poa, p2, nb, line_no=2, amount=D("630"))
    r1 = _line(db, "PA-OLD-L1", poa, p1, ob, line_no=1, amount=D("999"))   # 残留：同行号
    r2 = _line(db, "PA-OLD-L3", poa, pres, ob, line_no=3, amount=D("500"))  # 残留：多行号，
    # 分类「残留类」为现行行所无——options/分类下拉若漏了批次过滤就会漏出该取值

    pob = _po(db, "PB", nb, on=date(2026, 3, 20), source_type="指定采购", purchaser=None,
              is_inc=False, rate=None, amount_ex=D("2000"), amount_inc=None)
    _line(db, "PB-L1", pob, p3, nb, line_no=1, amount=D("800"))
    _line(db, "PB-L2", pob, p1, nb, line_no=2, amount=D("1200"))

    poc = _po(db, "PC", nb, on=date(2026, 4, 1), source_type="销售订单", purchaser="采购员乙",
              linked="XS-002", is_inc=True, rate=D("0.06"), amount_ex=None,
              amount_inc=D("1060"))
    _line(db, "PC-L1", poc, p1, nb, line_no=1, amount=D("1060"))
    _line(db, "PC-OLD-L1", poc, p1, ob, line_no=1, amount=D("400"))        # 残留：同行号

    pod = _po(db, "PD", nb, on=date(2026, 4, 10), source_type="维保需求", purchaser="采购员乙",
              is_inc=None, rate=None, amount_ex=D("500"), amount_inc=D("500"))
    _line(db, "PD-OLD-L1", pod, p1, ob, line_no=1, amount=D("500"))        # 只有旧批次行

    poe = _po(db, "PE", nb, on=date(2026, 3, 1), source_type="销售订单", purchaser="采购员甲",
              is_inc=True, rate=D("0.13"), amount_ex=D("100"), amount_inc=D("113"),
              status="已取消")
    _line(db, "PE-L1", poe, p1, nb, line_no=1, amount=D("113"))

    pof = _po(db, "PF", nb, on=date(2026, 5, 15), source_type="销售订单", purchaser="采购员甲",
              linked="XS-001", is_inc=False, rate=D("0"), amount_ex=D("1000"),
              amount_inc=D("1130"))
    _line(db, "PF-L1", pof, p2, nb, line_no=1, amount=D("1000"))

    pog = _po(db, "PG", nb, on=date(2026, 5, 20), source_type="销售订单", purchaser="采购员丙",
              linked="XS-003", is_inc=False, rate=D("0.006"), amount_ex=D("1000"),
              amount_inc=D("1006"))
    _line(db, "PG-L1", pog, p3, nb, line_no=1, amount=D("1000"))

    db.commit()

    class _DS:
        pass

    d = _DS()
    d.db, d.nb, d.ob = db, nb, ob
    d.p1, d.p2, d.p3 = p1, p2, p3
    d.c1, d.c2 = c1, c2
    d.poa, d.pob, d.poc, d.pod, d.poe, d.pof, d.pog = \
        poa, pob, poc, pod, poe, pof, pog
    d.residual_ids = {r1.id, r2.id}
    return d


@pytest.fixture()
def boss(ds, db):
    return client_for(db, username="analytics-boss", role="boss")


def _summary(c, **params):
    r = c.get("/api/purchases/analytics/summary", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _orders(c, **params):
    r = c.get("/api/purchases/analytics/orders", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 税率口径对拍

# (amount_ex, amount_inc, tax_rate, is_inc)：6 税组合 × 含税标记 + 历史缺口
CASES = [
    ("1000", "1130", "0.13", True),     # 含税 13%：inc 权威
    ("1000", "1130", "0.13", False),    # 不含税 13%：ex 权威
    ("1000", "1060", "0.06", True),     # 含税 6%
    ("1000", "1060", "0.06", False),
    ("1000", "1006", "0.006", False),   # 不含税 0.6%
    ("1000", "1130", "0", False),       # 税率 0 → 按 13%
    ("1000", "1130", None, True),       # 未标税率 → 13%
    ("1000", "1130", None, False),
    ("1000", None, "0.13", True),       # 历史缺口：口径侧为空回退另一列
    (None, "1130", "0.06", False),
    (None, None, None, None),           # 双侧全空 → None，不静默当 0
]


@pytest.mark.parametrize("ex,inc,rate,is_inc", CASES)
def test_order_amount_sql_matches_reference(db, ex, inc, rate, is_inc):
    """SQL CASE（summary/orders 用的表达式）与 Python 参照逐组合对拍。"""
    b = _batch(db, "parity")
    po = _po(db, "P-PARITY", b, on=date(2026, 1, 1), source_type="销售订单",
             is_inc=is_inc, rate=D(rate) if rate is not None else None,
             amount_ex=D(ex) if ex is not None else None,
             amount_inc=D(inc) if inc is not None else None)
    db.commit()
    a_ex, a_inc, a_paid, a_nominal = _order_amount_exprs()
    row = db.execute(select(a_ex, a_inc, a_paid, a_nominal).where(FPurchaseOrder.id == po.id)).one()
    r_ex, r_inc = order_amounts_reference(
        D(ex) if ex is not None else None, D(inc) if inc is not None else None,
        rate, is_inc)
    assert row[0] == (r_ex if r_ex is not None else None)
    assert row[1] == (r_inc if r_inc is not None else None)
    assert row[2] == (D(inc) if inc is not None else None)  # 实付=amount_inc_tax 原值
    assert row[3] == (rate is None or D(str(rate)) == 0)     # nominal=未标税率


@pytest.mark.parametrize("amount,rate,is_inc", [
    ("500", "0.13", True), ("500", None, False), ("800", "0.006", False), ("1060", "0.06", True),
])
def test_line_amount_sql_matches_reference(db, amount, rate, is_inc):
    b = _batch(db, "line-parity")
    p = _part(db, f"PN-LP-{rate}-{is_inc}", None)
    po = _po(db, "P-LINE-PARITY", b, on=date(2026, 1, 1), source_type="销售订单",
             is_inc=is_inc, rate=D(rate) if rate is not None else None)
    _line(db, "LP-L1", po, p, b, line_no=1, amount=D(amount))
    db.commit()
    l_ex, l_inc = _line_amount_exprs()
    row = db.execute(
        select(l_ex, l_inc).select_from(FPurchaseOrder)
        .join(FPurchaseLine, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseOrder.id == po.id)).one()
    r_ex, r_inc = line_amounts_reference(D(amount), rate, is_inc)
    assert row[0] == r_ex
    assert row[1] == r_inc


@pytest.mark.parametrize("ex,inc,rate,is_inc", [c for c in CASES if c[2] in (None, "0")])
def test_unmarked_rate_matches_legacy_order_amounts(ex, inc, rate, is_inc):
    """未标/0 税率时与既有 purchase_analysis._order_amounts（固定 13%）口径一致。"""
    r = order_amounts_reference(D(ex) if ex else None, D(inc) if inc else None, rate, is_inc)
    legacy = purchase_analysis._order_amounts(ex, inc, is_inc)
    assert r == legacy


# ---------------------------------------------------------------- KPI（单头口径）

def test_summary_kpi_order_mode(boss, ds):
    """不筛分类：单头口径。inc=7151/ex=6500/paid=4826（poB 实付 None 不计）；
    行数=各单单头批次行之和 7（残留行不计）；nominal 占比=3955/7151。"""
    data = _summary(boss)
    assert data["basis_mode"] == "order"
    kpi = data["kpi"]
    assert kpi["orders"] == 6                      # poE 已取消排除
    assert kpi["lines"] == 7                       # 2+2+1+0+1+1
    assert kpi["amount_inc"] == pytest.approx(7151.0)
    assert kpi["amount_ex"] == pytest.approx(6500.0)
    assert kpi["amount_paid"] == pytest.approx(4826.0)
    assert kpi["nominal_inc_share"] == pytest.approx(round(3955 / 7151, 4))
    # 分组合计 = KPI（实付 None 的组按 0 计入合计）
    assert sum(g["orders"] for g in data["groups"]) == kpi["orders"]
    assert sum(g["lines"] for g in data["groups"]) == kpi["lines"]
    assert sum(g["amount_paid"] or 0 for g in data["groups"]) == pytest.approx(kpi["amount_paid"])


def test_orders_paid_is_header_amount_raw(boss, ds):
    """未按分类筛选时，订单级实付 = 单头「采购金额」原值（含 None，不静默当 0）。"""
    data = _orders(boss, sort="amount_paid", order="desc", page_size=10)
    by_no = {i["order_no"]: i for i in data["items"]}
    assert by_no["CG-PA"]["amount_paid"] == pytest.approx(1130.0)
    assert by_no["CG-PB"]["amount_paid"] is None
    assert by_no["CG-PD"]["amount_paid"] == pytest.approx(500.0)


def test_cancelled_order_excluded_everywhere(boss, ds):
    data = _orders(boss, page_size=100)
    assert all(i["order_no"] != "CG-PE" for i in data["items"])
    assert data["total"] == 6
    r = boss.get(f"/api/purchases/analytics/orders/{ds.poe.id}")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- D-39 单头批次行

def test_residual_lines_never_counted(boss, ds):
    """残留旧批次行：不计行数、不进分类分组、不出现在明细接口（形态 a/b）。"""
    data = _summary(boss)                            # 单头口径：行数只算单头批次行
    assert data["kpi"]["lines"] == 7
    line_mode = _summary(boss, group_by="category")  # 行级口径：残留行不进分类分组
    assert line_mode["kpi"]["lines"] == 7            # 与单头口径同 7 行（poD 本就 0 行）
    assert line_mode["kpi"]["orders"] == 5           # poD（0 匹配行）整个不出现
    assert {g["key"] for g in line_mode["groups"]} == {"电器件", "机械件", UNCATEGORIZED_LABEL}
    # 明细接口：poA 只有 2 行（残留 2 行不出现）
    r = boss.get(f"/api/purchases/analytics/orders/{ds.poa.id}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["lines_count"] == 2
    assert {ln["line_amount"] for ln in detail["lines"]} == {500.0, 630.0}
    assert {ln["line_id"] for ln in detail["lines"]}.isdisjoint(ds.residual_ids)


def test_order_with_no_current_batch_lines_counts_zero(boss, ds):
    """形态 d：单头批次无任何行（只有旧批次行）→ 行数 0、不报错、有可识别 lines=0。"""
    data = _orders(boss, purchaser=["采购员乙"], page_size=10)
    items = {i["order_no"]: i for i in data["items"]}
    assert items["CG-PD"]["lines"] == 0
    r = boss.get(f"/api/purchases/analytics/orders/{ds.pod.id}")
    assert r.status_code == 200, r.text
    assert r.json()["lines_count"] == 0
    assert r.json()["lines"] == []


def test_category_filter_sums_only_matching_lines(boss, ds):
    """分类筛选 → 拆行只汇总匹配行；行级三口径按单头税率换算。"""
    data = _summary(boss, category=["电器件"])
    assert data["basis_mode"] == "line"
    kpi = data["kpi"]
    assert kpi["orders"] == 3                       # poA/poB/poC（poD 残留行不算）
    assert kpi["lines"] == 3
    assert kpi["amount_paid"] == pytest.approx(2760.0)   # 500+1200+1060
    assert kpi["amount_inc"] == pytest.approx(2916.0)    # 500 + 1200×1.13 + 1060
    assert kpi["amount_ex"] == pytest.approx(2642.48)    # 500/1.13 + 1200 + 1060/1.06


def test_category_multi_select_and_uncategorized(boss, ds):
    both = _summary(boss, category=["电器件", "机械件"])
    assert both["kpi"]["orders"] == 4
    assert both["kpi"]["lines"] == 5
    assert both["kpi"]["amount_paid"] == pytest.approx(4560.0)
    un = _summary(boss, category=[UNCATEGORIZED_LABEL])
    assert un["kpi"]["orders"] == 2                 # poA L2 + poF
    assert un["kpi"]["lines"] == 2
    assert un["kpi"]["amount_paid"] == pytest.approx(1630.0)
    assert un["kpi"]["amount_inc"] == pytest.approx(1760.0)   # 630 + 1000×1.13
    assert un["kpi"]["amount_ex"] == pytest.approx(1557.52)   # 630/1.13 + 1000


def test_group_by_category_kpi_equals_group_sums(boss, ds):
    data = _summary(boss, group_by="category")
    groups = {g["key"]: g for g in data["groups"]}
    assert set(groups) == {"电器件", "机械件", UNCATEGORIZED_LABEL}
    assert groups["电器件"]["amount_paid"] == pytest.approx(2760.0)
    assert groups["机械件"]["amount_paid"] == pytest.approx(1800.0)
    assert groups[UNCATEGORIZED_LABEL]["amount_paid"] == pytest.approx(1630.0)
    assert groups["电器件"]["orders"] == 3
    assert groups["电器件"]["lines"] == 3
    # 分组合计 = KPI（行级口径）
    kpi = data["kpi"]
    assert kpi["lines"] == sum(g["lines"] for g in groups.values())
    assert kpi["amount_paid"] == pytest.approx(sum(g["amount_paid"] for g in groups.values()))
    assert kpi["amount_inc"] == pytest.approx(sum(g["amount_inc"] for g in groups.values()))


def test_order_detail_category_matched_block(boss, ds):
    r = boss.get(f"/api/purchases/analytics/orders/{ds.poa.id}",
                 params={"category": ["电器件"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["lines_count"] == 1                    # 展示行也被过滤
    assert d["category_matched"] == {
        "lines": 1, "amount_inc": pytest.approx(500.0),
        "amount_ex": pytest.approx(442.48), "amount_paid": pytest.approx(500.0)}
    assert d["tax_rate_effective"] == pytest.approx(0.13)
    assert d["amount_paid"] == pytest.approx(1130.0)


def test_order_detail_header_amounts(boss, ds):
    r = boss.get(f"/api/purchases/analytics/orders/{ds.poc.id}")
    d = r.json()
    assert d["amount_inc"] == pytest.approx(1060.0)
    assert d["amount_ex"] == pytest.approx(1000.0)   # 1060/1.06
    assert d["customer"] == "华南钢铁"               # XS-002 去重取 id 小的（C2）
    assert d["salesperson"] == "销售小李"


# ---------------------------------------------------------------- 筛选

def test_date_window_closed_interval(boss, ds):
    m = _summary(boss, date_from="2026-03-01", date_to="2026-03-31")
    assert m["kpi"]["orders"] == 2                   # poA+poB（poE 已取消）
    only_from = _summary(boss, date_from="2026-03-10")
    assert only_from["kpi"]["orders"] == 5           # 03-10 之后全部
    only_to = _summary(boss, date_to="2026-04-05")
    assert only_to["kpi"]["orders"] == 3
    both_ends = _summary(boss, date_from="2026-03-05", date_to="2026-05-20")
    assert both_ends["kpi"]["orders"] == 6           # 闭区间含两端


def test_empty_window_returns_zero_not_fake_amounts(boss, ds):
    data = _summary(boss, date_from="2026-01-01", date_to="2026-01-31")
    assert data["kpi"]["orders"] == 0
    assert data["kpi"]["lines"] == 0
    assert data["kpi"]["amount_inc"] is None         # 空区间金额是 None，不是 0
    assert data["kpi"]["amount_paid"] is None
    assert data["kpi"]["nominal_inc_share"] is None
    assert data["groups"] == []


def test_all_time_via_min_date_equals_no_filter(boss, ds):
    """「全部」= 不传（或最小日期）——两种传法结果一致。"""
    no_filter = _summary(boss)
    min_date = _summary(boss, date_from="1900-01-01", date_to="2999-12-31")
    assert no_filter["kpi"] == min_date["kpi"]


def test_date_from_after_to_rejected_422(boss, ds):
    r = boss.get("/api/purchases/analytics/summary",
                 params={"date_from": "2026-05-01", "date_to": "2026-03-01"})
    assert r.status_code == 422, r.text
    r = boss.get("/api/purchases/analytics/orders",
                 params={"date_from": "2026-05-01", "date_to": "2026-03-01"})
    assert r.status_code == 422, r.text


def test_source_type_filter_multi(boss, ds):
    single = _summary(boss, source_type=["指定采购"])
    assert single["kpi"]["orders"] == 1
    assert single["kpi"]["amount_ex"] == pytest.approx(2000.0)
    multi = _summary(boss, source_type=["指定采购", "维保需求"])
    assert multi["kpi"]["orders"] == 2               # poB+poD
    # 类型原值分组：空 purchaser → "(空)"（与 cancellation_stats 同款哨兵）
    groups = {g["key"]: g for g in _summary(boss, group_by="source_type")["groups"]}
    assert set(groups) == {"销售订单", "指定采购", "维保需求"}
    assert groups["销售订单"]["amount_paid"] == pytest.approx(4326.0)  # 1130+1060+1130+1006


def test_purchaser_filter_multi_and_empty_label(boss, ds):
    multi = _summary(boss, purchaser=["采购员甲", "采购员丙"])
    assert multi["kpi"]["orders"] == 3              # poA/poF/poG
    groups = {g["key"]: g for g in _summary(boss, group_by="purchaser")["groups"]}
    assert set(groups) == {"采购员甲", "采购员乙", "采购员丙", EMPTY_LABEL}
    assert groups[EMPTY_LABEL]["orders"] == 1       # poB purchaser=None


def test_salesperson_filter_multi(boss, ds):
    data = _summary(boss, salesperson=["销售小王"])
    assert data["kpi"]["orders"] == 3               # poA/poF/poG（XS-001/XS-003）
    multi = _summary(boss, salesperson=["销售小王", "销售小李"])
    assert multi["kpi"]["orders"] == 4


def test_customer_filter_and_sales_dedup(boss, ds):
    """按客户筛选精确；XS-002 重复销售单若不去重会把 poC 金额翻倍。"""
    data = _summary(boss, customer_id=[ds.c2.id])
    assert data["kpi"]["orders"] == 1
    assert data["kpi"]["amount_paid"] == pytest.approx(1060.0)   # 不去重会是 2120
    multi = _summary(boss, customer_id=[ds.c1.id, ds.c2.id])
    assert multi["kpi"]["orders"] == 4              # C1: poA/poF/poG；C2: poC
    groups = {g["key"]: g for g in _summary(boss, group_by="customer")["groups"]}
    assert set(groups) == {str(ds.c1.id), str(ds.c2.id), NO_LINKED_LABEL}
    assert groups[str(ds.c2.id)]["orders"] == 1
    assert groups[NO_LINKED_LABEL]["orders"] == 2   # poB/poD 无关联销售单不丢


def test_month_day_grouping(boss, ds):
    months = {g["key"]: g for g in _summary(boss, group_by="month")["groups"]}
    assert set(months) == {"2026-03", "2026-04", "2026-05"}
    assert months["2026-03"]["orders"] == 2
    days = {g["key"]: g["orders"] for g in _summary(boss, group_by="day")["groups"]}
    assert days["2026-03-05"] == 1 and days["2026-05-20"] == 1


def test_filter_combination(boss, ds):
    data = _summary(boss, date_from="2026-03-01", date_to="2026-05-31",
                    source_type=["销售订单"], salesperson=["销售小王"])
    assert data["kpi"]["orders"] == 3               # poA/poF（XS-001）+ poG（XS-003 同销售）
    assert data["kpi"]["amount_paid"] == pytest.approx(3266.0)


# ---------------------------------------------------------------- 下钻 = 分组

def test_drilldown_matches_group_for_every_dimension(boss, ds):
    """每个分组的下钻合计 = 分组值（哨兵值/月份键同真值下钻）。"""
    for group_by in ("source_type", "purchaser", "salesperson", "customer", "month"):
        data = _summary(boss, group_by=group_by)
        for g in data["groups"]:
            drill = _orders(boss, group_by=group_by, group_key=g["key"], page_size=100)
            assert drill["total"] == g["orders"], (group_by, g["key"])
            assert {i["order_id"] for i in drill["items"]}
            paid = sum(i["amount_paid"] or 0 for i in drill["items"])
            expect = g["amount_paid"] or 0
            assert paid == pytest.approx(expect), (group_by, g["key"], paid, expect)


def test_drilldown_category_group_is_line_mode(boss, ds):
    """分类分组下钻：行级口径（每单只计匹配行的行数与金额）。"""
    drill = _orders(boss, group_by="category", group_key="电器件", page_size=100)
    assert drill["basis_mode"] == "line"
    items = {i["order_no"]: i for i in drill["items"]}
    assert set(items) == {"CG-PA", "CG-PB", "CG-PC"}
    assert items["CG-PA"]["lines"] == 1             # 只计电器件行
    assert items["CG-PA"]["amount_paid"] == pytest.approx(500.0)
    assert items["CG-PB"]["amount_paid"] == pytest.approx(1200.0)
    assert sum(i["amount_paid"] for i in items.values()) == pytest.approx(2760.0)


def test_line_mode_drill_sort_by_amount(boss, ds):
    """行级口径下按金额排序/翻页：排序键是聚合表达式（回归：排裸行表达式会被
    PG 以 GROUP BY 拒绝，EXPLAIN 脚本抓到）。"""
    drill = _orders(boss, category=["电器件", "机械件", UNCATEGORIZED_LABEL],
                    sort="amount_paid", order="desc", page_size=10)
    assert drill["basis_mode"] == "line"
    assert [i["order_no"] for i in drill["items"]] == \
        ["CG-PB", "CG-PA", "CG-PC", "CG-PG", "CG-PF"]   # 2000/1130/1060/1000/1000（PG/PF 实付并列，按 id 倒序）
    inc_sorted = _orders(boss, category=["电器件", "机械件", UNCATEGORIZED_LABEL],
                         sort="amount_inc", order="desc", page_size=10)
    assert [i["order_no"] for i in inc_sorted["items"]] == \
        ["CG-PB", "CG-PF", "CG-PA", "CG-PC", "CG-PG"]   # 2260；1130 同额按 id 倒序


def test_category_btrim_consistency_across_group_filter_drilldown(db):
    """分类的分组键/筛选值/下钻键同一 btrim 真值（对抗复审确认的 major）：
    ' 电器件 '（首尾空格）进「电器件」组且能被 category=['电器件'] 筛到；
    纯空白归「未分类」组且哨兵筛选/下钻都能命中——三条路径数字一致。"""
    b = _batch(db, "ws")
    pw1 = _part(db, "PN-WS1", "电器件")
    pw2 = _part(db, "PN-WS2", " 电器件 ")    # 首尾空格（Excel 导入常见）
    pw3 = _part(db, "PN-WS3", "   ")          # 纯空白
    for i, (part, amt) in enumerate([(pw1, "100"), (pw2, "200"), (pw3, "113")], 1):
        o = _po(db, f"WS-{i}", b, on=date(2026, 6, i), source_type="销售订单",
                is_inc=False, rate=None, amount_ex=D(amt), amount_inc=D(amt))
        _line(db, f"WS-L{i}", o, part, b, line_no=1, amount=D(amt))
    db.commit()
    c = client_for(db, username="analytics-ws", role="boss")
    groups = {g["key"]: g for g in _summary(c, group_by="category")["groups"]}
    assert set(groups) == {"电器件", UNCATEGORIZED_LABEL}
    assert groups["电器件"]["orders"] == 2                    # pw1 + pw2（空格不影响归组）
    assert groups["电器件"]["amount_paid"] == pytest.approx(300.0)
    assert groups[UNCATEGORIZED_LABEL]["orders"] == 1         # 纯空白归未分类
    # 筛选与分组同一真值
    f = _summary(c, category=["电器件"])
    assert f["kpi"]["orders"] == 2
    assert f["kpi"]["amount_paid"] == pytest.approx(300.0)
    un = _summary(c, category=[UNCATEGORIZED_LABEL])
    assert un["kpi"]["orders"] == 1
    assert un["kpi"]["amount_paid"] == pytest.approx(113.0)
    # 下钻与分组同一真值
    assert _orders(c, group_by="category", group_key="电器件")["total"] == 2
    assert _orders(c, group_by="category",
                   group_key=UNCATEGORIZED_LABEL)["total"] == 1


def test_empty_string_category_elements_are_noop(boss, ds):
    """空串/纯空白分类元素 = 未筛选：不静默翻到行级口径（对抗复审确认项）。"""
    a = _summary(boss, group_by="month")
    b = _summary(boss, group_by="month", **{"category": ["", "  "]})
    assert b["basis_mode"] == "order"
    assert b["filters_echo"]["category"] == []
    assert b["kpi"] == a["kpi"]
    c = _orders(boss, **{"category": [""]})
    assert c["basis_mode"] == "order"
    assert c["total"] == 6


def test_orders_group_by_category_without_key_is_line_mode(boss, ds):
    """orders(group_by=category) 不带 group_key：与 summary 同为行级口径，合计对账
    （对抗复审确认项：单头口径会与同参数 KPI 串台）。"""
    data = _orders(boss, group_by="category", page_size=100)
    assert data["basis_mode"] == "line"
    assert data["total"] == 5                                 # poD（0 匹配行）不出现
    assert sum(i["amount_paid"] for i in data["items"]) == pytest.approx(6190.0)
    kpi = _summary(boss, group_by="category")["kpi"]
    assert data["total"] == kpi["orders"]


def test_drilldown_with_existing_category_filter_is_intersection(boss, ds):
    """多分类筛选下钻单组 = 交集（AND），不是把 group_key 并进 OR 集合
    （对抗复审确认的 major：否则下钻混进另一分类的行、合计虚高）。"""
    drill = _orders(boss, **{"category": ["电器件", "机械件"], "group_by": "category",
                             "group_key": "电器件"}, page_size=100)
    assert drill["total"] == 3                                # 电器件组 3 单，不是 4
    by = {i["order_no"]: i for i in drill["items"]}
    assert set(by) == {"CG-PA", "CG-PB", "CG-PC"}
    assert by["CG-PB"]["amount_paid"] == pytest.approx(1200.0)   # 只计电器件行
    assert by["CG-PA"]["lines"] == 1
    assert sum(i["amount_paid"] for i in drill["items"]) == pytest.approx(2760.0)


def test_drilldown_uncategorized_and_no_linked_sentinels(boss, ds):
    un = _orders(boss, group_by="category", group_key=UNCATEGORIZED_LABEL, page_size=100)
    assert {i["order_no"] for i in un["items"]} == {"CG-PA", "CG-PF"}
    nl = _orders(boss, group_by="salesperson", group_key=NO_LINKED_LABEL, page_size=100)
    assert {i["order_no"] for i in nl["items"]} == {"CG-PB", "CG-PD"}
    assert all(i["salesperson"] is None for i in nl["items"])
    empty = _orders(boss, group_by="purchaser", group_key=EMPTY_LABEL, page_size=100)
    assert {i["order_no"] for i in empty["items"]} == {"CG-PB"}


# ---------------------------------------------------------------- 分页排序

def test_pagination_stable_pages(boss, ds):
    data = _orders(boss, sort="order_date", order="desc", page=1, page_size=2)
    assert data["total"] == 6
    assert [i["order_no"] for i in data["items"]] == ["CG-PG", "CG-PF"]
    page2 = _orders(boss, sort="order_date", order="desc", page=2, page_size=2)
    assert [i["order_no"] for i in page2["items"]] == ["CG-PD", "CG-PC"]
    page3 = _orders(boss, sort="order_date", order="desc", page=3, page_size=2)
    assert [i["order_no"] for i in page3["items"]] == ["CG-PB", "CG-PA"]
    page4 = _orders(boss, sort="order_date", order="desc", page=4, page_size=2)
    assert page4["items"] == []


def test_sort_amount_desc_tie_broken_by_id_desc(boss, ds):
    """同额（1130：poA/poF）按 id 倒序；实付 None（poB）排最后不报错。"""
    data = _orders(boss, sort="amount_paid", order="desc", page_size=10)
    nos = [i["order_no"] for i in data["items"]]
    assert nos == ["CG-PF", "CG-PA", "CG-PC", "CG-PG", "CG-PD", "CG-PB"]
    assert ds.pof.id > ds.poa.id                   # 插入顺序保证 tie 分向


def test_sort_asc_and_bad_params(boss, ds):
    data = _orders(boss, sort="amount_inc", order="asc", page_size=10)
    assert data["items"][0]["order_no"] == "CG-PD"  # 565 最小
    r = boss.get("/api/purchases/analytics/orders", params={"sort": "supplier"})
    assert r.status_code == 422, r.text
    r = boss.get("/api/purchases/analytics/orders", params={"order": "up"})
    assert r.status_code == 422, r.text
    r = boss.get("/api/purchases/analytics/orders", params={"group_key": "x"})
    assert r.status_code == 422, r.text             # group_key 需与 group_by 同传
    r = boss.get("/api/purchases/analytics/summary", params={"group_by": "week"})
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- options

def test_options_lists_distinct_values(boss, ds):
    r = boss.get("/api/purchases/analytics/options")
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data["source_types"]) == {"销售订单", "指定采购", "维保需求"}
    assert set(data["purchasers"]) == {"采购员甲", "采购员乙", "采购员丙"}   # None 不在
    assert set(data["salespersons"]) == {"销售小王", "销售小李"}
    assert set(data["categories"]) == {"电器件", "机械件", UNCATEGORIZED_LABEL}
    assert "残留类" not in data["categories"]   # 旧批次残留行的独有分类不得漏进下拉（D-39 批次过滤）
    assert data["categories"][-1] == UNCATEGORIZED_LABEL   # 未分类固定垫底
    assert data["uncategorized_label"] == UNCATEGORIZED_LABEL


# ---------------------------------------------------------------- 权限矩阵

ENDPOINTS = ("/api/purchases/analytics/options", "/api/purchases/analytics/summary",
             "/api/purchases/analytics/orders", "/api/purchases/analytics/orders/1")


@pytest.mark.parametrize("role,expected", [
    ("boss", 200), ("admin", 200),
    ("sales", 403), ("purchaser", 403), ("readonly", 403),
])
def test_page_permission_matrix(db, ds, role, expected):
    c = client_for(db, username=f"analytics-{role}", role=role)
    for ep in ENDPOINTS:
        r = c.get(ep)
        assert r.status_code == expected, (role, ep, r.status_code, r.text)


def test_anonymous_gets_401(ds):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    for ep in ENDPOINTS:
        assert c.get(ep).status_code == 401, ep


def test_explicit_grant_opens_readonly(db, ds):
    c = client_for(db, username="analytics-granted", role="readonly",
                   overrides={"page_purchase_analytics": True})
    r = c.get("/api/purchases/analytics/summary")
    assert r.status_code == 200, r.text


def test_scoped_account_fail_closed(db, ds):
    """own_customers_only 收紧账号（即便显式给了页面键）→ 403：全公司聚合无按客
    户裁剪的数据面，fail-closed（比 dashboard 更严的先例见路由注释）。"""
    c = client_for(db, username="analytics-scoped", role="readonly",
                   overrides={"page_purchase_analytics": True, "own_customers_only": True})
    for ep in ENDPOINTS:
        assert c.get(ep).status_code == 403, ep


# ---------------------------------------------------------------- 脱敏

def test_cost_masking_nulls_amounts_keeps_counts(db, ds):
    c = client_for(db, username="analytics-nocost", role="boss",
                   overrides={"data_purchase_cost": False})
    data = _summary(c)
    assert data["kpi"]["orders"] == 6               # 计数不遮
    assert data["kpi"]["lines"] == 7
    for key in ("amount_inc", "amount_ex", "amount_paid", "nominal_inc_share"):
        assert data["kpi"][key] is None, key        # 金额键置 null 但键名保留
    for g in data["groups"]:
        assert g["amount_inc"] is None and g["amount_paid"] is None
    lst = _orders(c, page_size=10)
    for i in lst["items"]:
        assert i["amount_inc"] is None and i["amount_paid"] is None
        assert i["lines"] is not None               # 行数仍可见


def test_customer_masking_and_filter_403(db, ds):
    c = client_for(db, username="analytics-nocus", role="boss",
                   overrides={"data_customer": False})
    r = c.get("/api/purchases/analytics/summary",
              params={"customer_id": ds.c1.id})
    assert r.status_code == 403, r.text
    # group_by=customer 同样拒绝：分组键是客户 id 维度，遮了客户数据就不该给客户分组
    r = c.get("/api/purchases/analytics/summary", params={"group_by": "customer"})
    assert r.status_code == 403, r.text
    r = c.get("/api/purchases/analytics/orders",
              params={"group_by": "customer", "group_key": str(ds.c1.id)})
    assert r.status_code == 403, r.text
    # 其余维度正常返回，客户名/id 不泄漏
    data = _summary(c, group_by="salesperson")
    assert data["groups"]                            # salesperson 维度不受影响
    lst = _orders(c, page_size=100)
    assert lst["items"]
    for i in lst["items"]:
        assert i["customer_id"] is None and i["customer"] is None


def test_orders_amount_sort_falls_back_when_masked(db, ds, boss):
    """结构性排序保护（对抗复审确认的 major）：data_purchase_cost=False 的账号
    按金额排序时，行序会泄漏隐藏金额的排名——必须回退 order_date（pool.py 先例）。"""
    c = client_for(db, username="analytics-nocost-sort", role="boss",
                   overrides={"data_purchase_cost": False})
    data = _orders(c, sort="amount_paid", order="desc", page_size=10)
    assert data["ranking_restricted"] is True
    assert data["effective_sort"] == "order_date"          # 回退安全排序键
    assert [i["order_no"] for i in data["items"]] == \
        ["CG-PG", "CG-PF", "CG-PD", "CG-PC", "CG-PB", "CG-PA"]   # order_date 降序
    # 全权账号同参数仍按金额排（默认行为不变），且两者顺序不同
    full = _orders(boss, sort="amount_paid", order="desc", page_size=10)
    assert [i["order_no"] for i in full["items"]] == \
        ["CG-PF", "CG-PA", "CG-PC", "CG-PG", "CG-PD", "CG-PB"]
    assert full["ranking_restricted"] is False and full["effective_sort"] == "amount_paid"


def test_summary_group_order_falls_back_when_masked(db, ds, boss):
    """分组顺序不得泄漏隐藏金额排名（对抗复审确认的 major）：金额组被遮时分组
    按订单数降序 + 键升序，而非实付合计降序（pool.py 结构性保护先例）。"""
    full = _summary(boss, group_by="month")
    assert full["groups_ranking_restricted"] is False
    assert [g["key"] for g in full["groups"]] == ["2026-05", "2026-04", "2026-03"]
    # 全权：按实付合计降序 2136 > 1560 > 1130（poB 实付=含税额原值 None，不计入）
    c = client_for(db, username="analytics-nocost-grp", role="boss",
                   overrides={"data_purchase_cost": False})
    data = _summary(c, group_by="month")
    assert data["groups_ranking_restricted"] is True
    # 各月订单数并列（2/2/2）→ 键升序，不是实付降序的 [03, 05, 04]
    assert [g["key"] for g in data["groups"]] == ["2026-03", "2026-04", "2026-05"]
    for g in data["groups"]:
        assert g["orders"] == 2
        assert g["amount_paid"] is None and g["amount_inc"] is None


def test_order_detail_cost_masking_end_to_end(db, ds):
    """明细端点端到端脱敏（对抗复审确认的覆盖缺口）：明细是成本键最富的暴露面
    ——单头三口径、行级金额、category_matched 嵌套块全置 null；行数/qty/pn_std 保留。"""
    c = client_for(db, username="analytics-nocost-detail", role="boss",
                   overrides={"data_purchase_cost": False})
    r = c.get(f"/api/purchases/analytics/orders/{ds.poa.id}",
              params={"category": "电器件"})
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("amount_inc", "amount_ex", "amount_paid"):
        assert d[key] is None, key
    assert d["lines_count"] == 1                          # 只剩电器件行（计数不遮）
    assert len(d["lines"]) == 1
    ln = d["lines"][0]
    for key in ("unit_price", "line_amount", "line_amount_inc", "line_amount_ex"):
        assert ln[key] is None, key
    assert ln["qty"] is not None and ln["pn_std"] == "PN-DQ"   # 非成本键不误伤
    cm = d["category_matched"]                            # dict-in-dict 递归脱敏路径
    assert cm["lines"] == 1
    for key in ("amount_inc", "amount_ex", "amount_paid"):
        assert cm[key] is None, key


def test_order_detail_customer_masking_end_to_end(db, ds):
    """明细端点客户脱敏：customer/customer_id 置 null，销售员/关联单号/金额不误伤。"""
    c = client_for(db, username="analytics-nocus-detail", role="boss",
                   overrides={"data_customer": False})
    r = c.get(f"/api/purchases/analytics/orders/{ds.poa.id}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["customer"] is None and d["customer_id"] is None
    assert d["salesperson"] == "销售小王"
    assert d["linked_sales_order_no"] == "XS-001"
    assert d["amount_inc"] is not None                    # 金额不受客户脱敏影响


def test_new_amount_keys_registered_for_masking():
    """新增派生金额键必须登记进 FIELD_GROUPS（随 data_purchase_cost 遮蔽）——
    前车之鉴见 config.FIELD_GROUPS 注释。"""
    registered = set()
    for keys in config.FIELD_GROUPS.values():
        registered.update(keys)
    for key in ("amount_paid", "nominal_inc_share", "line_amount_inc", "line_amount_ex"):
        assert key in registered, key
    assert "amount_paid" in config.FIELD_GROUPS["purchase_cost"]
    assert "nominal_inc_share" in config.FIELD_GROUPS["purchase_cost"]
    assert "line_amount_inc" in config.FIELD_GROUPS["purchase_cost"]
    assert "line_amount_ex" in config.FIELD_GROUPS["purchase_cost"]


# ---------------------------------------------------------------- 其他边界

def test_order_detail_404_for_missing_or_inactive(ds, boss):
    assert boss.get("/api/purchases/analytics/orders/99999999").status_code == 404
    assert boss.get(f"/api/purchases/analytics/orders/{ds.poe.id}").status_code == 404


def test_filters_echo_reflects_request(boss, ds):
    data = _summary(boss, date_from="2026-03-01", date_to="2026-03-31",
                    source_type=["指定采购"], category=["电器件"], group_by="month")
    assert data["filters_echo"] == {
        "date_from": "2026-03-01", "date_to": "2026-03-31",
        "source_type": ["指定采购"], "purchaser": [], "salesperson": [],
        "customer_id": [], "category": ["电器件"], "group_by": "month", "group_key": None}
    assert data["as_of"] == date.today().isoformat()


def test_summary_service_level_validation(db):
    from app.services.purchase_analytics import FilterValidationError
    with pytest.raises(FilterValidationError):
        purchase_analytics.summary(db, None, date_from=date(2026, 5, 1),
                                   date_to=date(2026, 3, 1))
    with pytest.raises(FilterValidationError):
        purchase_analytics.orders(db, None, sort="nope")
