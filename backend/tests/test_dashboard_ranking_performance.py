"""型号榜只给返回的 PN 计算价格统计，完整榜的计数/排名保持不变。"""
from datetime import date
from decimal import Decimal

import pytest

from app import permissions, security
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch
from app.services import dashboard


AS_OF = date(2026, 6, 1)


@pytest.fixture()
def ranked_parts(db):
    batch = SysImportBatch(filename="ranking.xlsx", file_type="sales", file_hash="ranking-bounded")
    db.add(batch)
    db.flush()
    parts = [DimPart(pn_std=f"PN-{i}") for i in range(4)]
    db.add_all(parts)
    db.flush()
    sales_order = FSalesOrder(
        raw_order_id="S1", order_no="S1", order_date=date(2026, 3, 1),
        data_status="已生效", import_batch_id=batch.id,
    )
    purchase_order = FPurchaseOrder(
        raw_order_id="P1", order_no="P1", order_date=date(2026, 2, 1),
        data_status="已生效", source_type="销售订单", is_tax_inclusive=True,
        import_batch_id=batch.id,
    )
    db.add_all([sales_order, purchase_order])
    db.flush()
    for i, (part, revenue, cost) in enumerate(zip(parts, [400, 100, 300, 200], [200, 200, 250, None])):
        db.add(FSalesLine(
            raw_line_id=f"SL{i}", order_id=sales_order.id, part_id=part.id,
            qty=Decimal(1), unit_price=Decimal(revenue) * Decimal("1.13"),
            revenue_amount=Decimal(revenue), cost_moving_avg=cost,
            cost_fifo=cost + 5 if cost is not None else None,
            counts_revenue=True, anomaly_flags=["no_cost"] if cost is None else [],
            import_batch_id=batch.id,
        ))
        for j, (qty, price) in enumerate([(1, 113), (3, 226)]):
            db.add(FPurchaseLine(
                raw_line_id=f"PL{i}-{j}", order_id=purchase_order.id, part_id=part.id,
                qty=Decimal(qty), unit_price=Decimal(price),
                line_amount=Decimal(qty * price), import_batch_id=batch.id,
            ))
    db.commit()
    return parts


def _capture_enrichment(monkeypatch):
    observed = {}
    for name in ("_purchase_price_stats", "_sale_price_stats"):
        original = getattr(dashboard, name)

        def capture(*args, _name=name, _original=original, **kwargs):
            observed[_name] = kwargs.get("part_ids")
            return _original(*args, **kwargs)

        monkeypatch.setattr(dashboard, name, capture)
    original_pool_map = dashboard.pool_metrics.active_pool_map

    def capture_pool(db, part_ids):
        observed["pool"] = list(part_ids)
        return original_pool_map(db, part_ids)

    monkeypatch.setattr(dashboard.pool_metrics, "active_pool_map", capture_pool)
    return observed


@pytest.mark.parametrize("cost_method", ["moving_avg", "fifo"])
@pytest.mark.parametrize("sort,order,page", [
    ("revenue", "desc", 2), ("gross_profit", "asc", 4), ("order_count", "desc", 3),
])
def test_price_work_is_bounded_by_page_and_both_boards(
    db, ranked_parts, monkeypatch, cost_method, sort, order, page,
):
    observed = _capture_enrichment(monkeypatch)
    result = dashboard.part_ranking(
        db, None, None, as_of=AS_OF, top=1, page_size=1, page=page,
        cost_method=cost_method, sort=sort, order=order,
    )
    returned = result["profitable"] + result["loss"] + result["ranking"]["items"]
    ids = {row["part_id"] for row in returned}
    assert result["counts"] == {
        "total_parts": 4, "with_cost": 3, "profitable": 2, "loss": 1, "no_cost_parts": 1,
    }
    assert result["profitable"][0]["pn_std"] == "PN-0"
    assert result["loss"][0]["pn_std"] == "PN-1"
    for name in ("_purchase_price_stats", "_sale_price_stats", "pool"):
        assert observed[name] is not None, f"{name} still processes every PN"
        assert set(observed[name]) == ids
    for row in returned:
        assert row["purchase_price"] == {
            "wavg": 175.0, "median": 150.0, "min": 100.0, "max": 200.0,
            "samples": 2, "last_date": "2026-02-01",
        }
        assert row["sale_price"]["samples"] == 1


def test_profit_hidden_enriches_only_the_visible_page(db, ranked_parts, monkeypatch):
    observed = _capture_enrichment(monkeypatch)
    perms = permissions._full()
    perms["data_profit"] = False
    ctx = security.UserContext(user_id="u", role="custom", permissions=perms, is_authenticated=True)
    result = dashboard.part_ranking(
        db, None, None, as_of=AS_OF, top=1, page_size=1, page=2, user_ctx=ctx,
    )
    assert result["profitable"] == result["loss"] == []
    assert result["ranking"]["effective_sort"] == "revenue"
    page_ids = {row["part_id"] for row in result["ranking"]["items"]}
    for name in ("_purchase_price_stats", "_sale_price_stats", "pool"):
        assert observed[name] is not None
        assert set(observed[name]) == page_ids


def test_tax_display_work_is_bounded_by_returned_rows(db, ranked_parts, monkeypatch):
    conversions = []
    original = dashboard._inc_from_ex

    def convert(amount):
        conversions.append(amount)
        return original(amount)

    monkeypatch.setattr(dashboard, "_inc_from_ex", convert)
    result = dashboard.part_ranking(
        db, None, None, as_of=AS_OF, top=1, page_size=1, page=2, sort="revenue",
    )
    returned = result["profitable"] + result["loss"] + result["ranking"]["items"]
    ids = {row["part_id"] for row in returned}
    # 营收一次，moving/fifo 各两次：只有实际展示行需要含税换算。
    assert len(conversions) == len(ids) * 5
    for row in returned:
        assert not any(key.startswith("_") for key in row)


@pytest.mark.parametrize("helper", [dashboard._purchase_price_stats, dashboard._sale_price_stats])
def test_bounded_price_sql_matches_unbounded_statistics(db, ranked_parts, helper):
    full = helper(db, None, AS_OF)
    requested = [ranked_parts[0].id, ranked_parts[2].id]
    limited = helper(db, None, AS_OF, part_ids=requested)
    assert limited == {part_id: full[part_id] for part_id in requested}
    assert helper(db, None, AS_OF, part_ids=[]) == {}
    assert helper(db, date(2026, 4, 1), AS_OF, part_ids=requested) == {}


def test_empty_ranking_does_not_scan_price_tables(db, ranked_parts, monkeypatch):
    observed = _capture_enrichment(monkeypatch)
    result = dashboard.part_ranking(db, None, None, as_of=AS_OF, pn="MISSING")
    assert result["ranking"]["total"] == 0
    for values in observed.values():
        assert values == []
