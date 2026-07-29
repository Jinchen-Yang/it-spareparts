"""取消单导入与统计（宋总诉求：已取消/作废单也入库、可按月/季/年统计）。

验证核心口径：取消单能查到、能统计，但不影响"已生效"业务计算。
"""
from datetime import date
from decimal import Decimal

import pytest

from app.etl import loader
from app.models.system import SysImportBatch
from app.services import purchase_analysis, purchase_query
from tests import factories as f


@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hcancel")
    db.add(b)
    db.flush()
    return b


def _seed(db, batch):
    """3 生效 + 2 取消 + 1 进行中，跨 2026-01 / 2026-02。"""
    orders = {
        "A1": f.purchase_head("A1", on=date(2026, 1, 5), data_status="已生效"),
        "A2": f.purchase_head("A2", on=date(2026, 1, 20), data_status="已取消"),
        "A3": f.purchase_head("A3", on=date(2026, 1, 25), data_status="进行中"),
        "B1": f.purchase_head("B1", on=date(2026, 2, 8), data_status="已生效"),
        "B2": f.purchase_head("B2", on=date(2026, 2, 9), data_status="已生效"),
        "B3": f.purchase_head("B3", on=date(2026, 2, 10), data_status="已取消"),
    }
    lines = [
        f.purchase_line(k, f"L{k}", f"PN-{k}", qty="1", price="100")
        for k in orders
    ]
    loader.load(db, f.purchase_result(orders, lines), batch.id, date(2026, 6, 1), mode="skip")
    db.commit()


def test_recent_default_only_active(db, batch):
    """默认（status=None）仍只返回已生效，且带出 data_status 字段。"""
    _seed(db, batch)
    out = purchase_query.recent_purchases(db, days=3660)
    assert out["total"] == 3                              # 仅 3 个已生效
    assert all(it["data_status"] == "已生效" for it in out["items"])


def test_recent_filter_cancelled(db, batch):
    """status='已取消' → 查到 2 个取消单（宋总要的"取消单可见"）。"""
    _seed(db, batch)
    out = purchase_query.recent_purchases(db, days=3660, status="已取消")
    assert out["total"] == 2
    assert {it["order_no"] for it in out["items"]} == {"A2", "B3"}


def test_recent_status_all(db, batch):
    """status='全部' → 不限状态，6 单全出。"""
    _seed(db, batch)
    out = purchase_query.recent_purchases(db, days=3660, status="全部")
    assert out["total"] == 6


def test_cancellation_stats_by_month(db, batch):
    _seed(db, batch)
    res = purchase_analysis.cancellation_stats(db, granularity="month")
    by_period = {r["period"]: r for r in res["rows"]}
    assert set(by_period) == {"2026-01", "2026-02"}
    # 2026-01：3 单(1生效/1取消/1进行中)，取消 1
    jan = by_period["2026-01"]
    assert jan["total"] == 3 and jan["cancelled"] == 1
    assert jan["cancel_rate"] == round(100 / 3, 2)
    # 2026-02：3 单(2生效/1取消)，取消 1
    feb = by_period["2026-02"]
    assert feb["total"] == 3 and feb["cancelled"] == 1
    # 汇总：6 单，取消 2
    assert res["summary"]["total"] == 6 and res["summary"]["cancelled"] == 2
    # 排序：最近期间在前
    assert res["rows"][0]["period"] == "2026-02"


def test_cancellation_stats_by_year(db, batch):
    _seed(db, batch)
    res = purchase_analysis.cancellation_stats(db, granularity="year")
    assert len(res["rows"]) == 1
    assert res["rows"][0]["period"] == "2026"
    assert res["rows"][0]["total"] == 6 and res["rows"][0]["cancelled"] == 2


def test_cancellation_stats_aggregates_each_order_by_its_authoritative_tax_basis(db, batch):
    orders = {
        "INC": f.purchase_head(
            "INC",
            on=date(2026, 3, 1),
            data_status="已取消",
            is_tax_inclusive=True,
            amount_ex_tax=Decimal("999.00"),
            amount_inc_tax=Decimal("113.00"),
        ),
        "EX": f.purchase_head(
            "EX",
            on=date(2026, 3, 2),
            data_status="已取消",
            is_tax_inclusive=False,
            amount_ex_tax=Decimal("100.00"),
            amount_inc_tax=Decimal("999.00"),
        ),
        "DEFAULT_EX": f.purchase_head(
            "DEFAULT_EX",
            on=date(2026, 3, 3),
            data_status="已取消",
            is_tax_inclusive=None,
            amount_ex_tax=Decimal("50.00"),
            amount_inc_tax=Decimal("999.00"),
        ),
    }
    lines = [
        f.purchase_line(raw_id, f"L-{raw_id}", f"PN-{raw_id}", qty="1", price="1")
        for raw_id in orders
    ]
    loader.load(
        db,
        f.purchase_result(orders, lines),
        batch.id,
        date(2026, 6, 1),
        mode="skip",
    )
    db.commit()

    result = purchase_analysis.cancellation_stats(db, granularity="month")
    march = next(row for row in result["rows"] if row["period"] == "2026-03")
    cancelled = march["by_status"]["已取消"]

    assert cancelled["amount_ex"] == 250.0
    assert cancelled["amount_inc"] == 282.5
    assert cancelled["amount"] == 250.0
    assert march["cancelled_amount_ex"] == 250.0
    assert march["cancelled_amount_inc"] == 282.5
    assert march["cancelled_amount"] == 250.0
    assert result["summary"]["cancelled_amount_ex"] == 250.0
    assert result["summary"]["cancelled_amount_inc"] == 282.5
    assert result["summary"]["cancelled_amount"] == 250.0


def test_cancelled_counted_in_stats_but_excluded_from_active(db, batch):
    """关键口径：取消单进了库、能被统计(cancelled=2)，但默认业务口径(已生效，
    成本/利润同此口径)只认 3 单——取消单不污染金额计算。"""
    _seed(db, batch)
    stats = purchase_analysis.cancellation_stats(db, granularity="year")
    assert stats["summary"]["cancelled"] == 2          # 取消单被统计到
    active = purchase_query.recent_purchases(db, days=3660)
    assert active["total"] == 3                          # 业务/成本口径只认已生效
    assert stats["summary"]["total"] == 6                # 统计口径含全部状态
