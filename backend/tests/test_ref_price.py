"""近期加权成交价参考（销售出价用）：近 N 条且 D 天内成交价，时间半衰期加权。

需求（用户 2026-06-12 确认）：不给"建议售价"（销售自行把握加价），只给一个稳的
成交价参考——近5条且30天内，越近权重越高、越久越低，削单笔异常价的方差。

日期全部用 today-相对（age == days_ago，确定可复现，不随运行日漂移）。
"""
from datetime import date, timedelta

from app.etl import loader
from app.models.system import SysImportBatch
from app.services import part_overview, profit
from tests import factories as f


def _load(db, pn, specs, h):
    """specs: [(days_ago, price, business_type)]；一条一单。载入后 recompute 落 counts_revenue。"""
    b = SysImportBatch(filename="t.xlsx", file_type="sales", file_hash=h)
    db.add(b)
    db.flush()
    today = date.today()
    orders, lines = {}, []
    for i, (days_ago, price, btype) in enumerate(specs):
        oid = f"O{i}"
        orders[oid] = f.sales_head(oid, on=today - timedelta(days=days_ago), business_type=btype)
        lines.append(f.sales_line(oid, f"L{i}", pn, qty="1", price=str(price)))
    loader.load(db, f.sales_result(orders, lines), b.id, today)
    db.commit()
    profit.recompute(db)   # counts_revenue 在 recompute 落库（按 business_type）
    db.commit()


def _ref(db, pn):
    part, _ = part_overview.resolve_part(db, pn)
    return part_overview._weighted_recent_sale_price(db, part.id)


def test_ref_price_window_counts_revenue_and_exact_weighting(db):
    """窗口外/换货排除 + 精确加权值（钉死公式）。"""
    _load(db, "REF-A", [
        (1, 100, "备件销售"),
        (5, 110, "备件销售"),
        (25, 130, "备件销售"),
        (40, 999, "备件销售"),    # >30 天，窗口外 → 排除
        (2, 8888, "销售换货"),    # 30 天内但非营收类(counts_revenue=False) → 排除
    ], "href_a")
    r = _ref(db, "REF-A")
    assert r["ref_sale_samples"] == 3, "只算窗口内的营收成交（排除40天外 + 换货）"
    assert r["ref_window_days"] == 30
    # ages=1/5/25, halflife=15: w=0.9549/0.7937/0.3150
    # ref = (95.49+87.31+40.95)/(2.0636) ≈ 108.43；朴素均值=113.33，近端权重高 → 偏向 100
    assert abs(r["ref_sale_price"] - 108.43) <= 0.02, f"加权值不符: {r['ref_sale_price']}"
    assert r["ref_sale_price"] < 113.33, "近端权重更高 → 参考价低于朴素均值"


def test_ref_price_caps_at_max_n(db):
    """窗口内超过 REF_PRICE_MAX_N 条时，只取最近 5 条。"""
    _load(db, "REF-B", [(i, 100, "备件销售") for i in range(1, 8)], "href_b")  # 7 条
    r = _ref(db, "REF-B")
    assert r["ref_sale_samples"] == 5


def test_ref_price_recent_dominates(db):
    """越近权重越高：近端低价 + 远端高价 → 参考价明显偏向近端，低于朴素均值。"""
    _load(db, "REF-C", [(0, 100, "备件销售"), (28, 200, "备件销售")], "href_c")
    r = _ref(db, "REF-C")
    assert r["ref_sale_samples"] == 2
    assert 100 <= r["ref_sale_price"] < 150, "朴素均值150；近端权重高 → 明显 <150"


def test_ref_price_none_when_window_empty(db):
    """窗口内无成交：返回 None + samples=0（形状仍完整）。"""
    _load(db, "REF-D", [(40, 500, "备件销售"), (60, 600, "备件销售")], "href_d")
    r = _ref(db, "REF-D")
    assert r["ref_sale_price"] is None
    assert r["ref_sale_samples"] == 0
    assert r["ref_window_days"] == 30


def test_ref_price_exposed_in_quick_pricing_and_overview(db):
    """quick_pricing(AI/批量出价) 与 get_overview(前端全景) 都带该参考；not-found 形状一致。"""
    _load(db, "REF-E", [(1, 100, "备件销售"), (3, 120, "备件销售")], "href_e")
    qp = part_overview.quick_pricing(db, "REF-E")
    assert {"ref_sale_price", "ref_sale_samples", "ref_window_days"} <= set(qp)
    assert qp["ref_sale_samples"] == 2 and qp["ref_sale_price"] is not None

    ov = part_overview.get_overview(db, "REF-E")
    assert "sale_price_ref" in ov
    assert ov["sale_price_ref"]["ref_sale_samples"] == 2

    # not-found：键齐、值空（供 lookup_prices_bulk spread 形状稳定）
    nf = part_overview.quick_pricing(db, "NOPE-XX")
    assert nf["ref_sale_price"] is None and nf["ref_sale_samples"] == 0
    assert nf["ref_window_days"] == 30
