"""DEV-05B1 采购价倍率校准预览的只读 HTTP 契约。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysAuditLog, SysImportBatch, SysUser


PASSWORD = "pw123456"


def _client(db, username: str, *, page: bool, cost: bool) -> TestClient:
    perms = permissions.effective("readonly", {
        "page_governance": page,
        "data_purchase_cost": cost,
        "data_profit": False,
    })
    db.add(SysUser(
        username=username,
        role="readonly",
        password_hash=hash_password(PASSWORD),
        permissions=perms,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={
        "username": username, "password": PASSWORD,
    })
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _seed_calibration_facts(db) -> dict[str, object]:
    batch = SysImportBatch(
        filename="采购价校准.xlsx", file_type="purchase",
        file_hash="calibration-preview", uploaded_by="测试导入员", status="success",
    )
    part_a = DimPart(pn_std="CAL-A", description="校准盘 A")
    part_b = DimPart(pn_std="CAL-B", description="校准盘 B")
    db.add_all([batch, part_a, part_b])
    db.flush()

    sequence = 0

    def add_line(
        *, part: DimPart, day: date | None, purchase_type: str,
        price: str, tax_inclusive: bool | None, qty: str = "1",
        status: str = "已生效",
    ) -> FPurchaseLine:
        nonlocal sequence
        sequence += 1
        order = FPurchaseOrder(
            raw_order_id=f"cal-order-{sequence}",
            order_no=f"CG-CAL-{sequence:03d}",
            order_date=day,
            source_type=purchase_type,
            is_tax_inclusive=tax_inclusive,
            data_status=status,
            import_batch_id=batch.id,
        )
        db.add(order)
        db.flush()
        line = FPurchaseLine(
            raw_line_id=f"cal-line-{sequence}",
            order_id=order.id,
            line_no=1,
            part_id=part.id,
            pn_std=part.pn_std,
            description=part.description,
            qty=Decimal(qty),
            unit="块",
            unit_price=Decimal(price),
            line_amount=Decimal(price) * Decimal(qty),
            import_batch_id=batch.id,
        )
        db.add(line)
        db.flush()
        return line

    # 销售订单 / CAL-A：未税 100 -> 300 -> 50 -> 500，倍率依次 3↑、6↓、10↑。
    prior_a = add_line(
        part=part_a, day=date(2026, 6, 30), purchase_type="销售订单",
        price="113", tax_inclusive=True,
    )
    a_300 = add_line(
        part=part_a, day=date(2026, 7, 1), purchase_type="销售订单",
        price="339", tax_inclusive=True,
    )
    a_50 = add_line(
        part=part_a, day=date(2026, 7, 1), purchase_type="销售订单",
        price="50", tax_inclusive=False,
    )
    a_500 = add_line(
        part=part_a, day=date(2026, 7, 2), purchase_type="销售订单",
        price="565", tax_inclusive=None,
    )

    # 同类型 / CAL-B：相等价仍是可比对，但不属于涨/跌候选。
    add_line(
        part=part_b, day=date(2026, 6, 30), purchase_type="销售订单",
        price="100", tax_inclusive=False,
    )
    add_line(
        part=part_b, day=date(2026, 7, 2), purchase_type="销售订单",
        price="100", tax_inclusive=False,
    )

    # 指定采购单独分区：10 -> 100，倍率 10↑。
    add_line(
        part=part_a, day=date(2026, 6, 30), purchase_type="指定采购",
        price="10", tax_inclusive=False,
    )
    designated_100 = add_line(
        part=part_a, day=date(2026, 7, 1), purchase_type="指定采购",
        price="100", tax_inclusive=False,
    )

    # 这些行必须整体排除，也不能成为后续行的“前值”。
    add_line(
        part=part_a, day=date(2026, 7, 1), purchase_type="销售订单",
        price="9999", tax_inclusive=False, status="已取消",
    )
    add_line(
        part=part_a, day=date(2026, 7, 3), purchase_type="销售订单",
        price="0", tax_inclusive=False,
    )
    add_line(
        part=part_a, day=date(2026, 7, 3), purchase_type="销售订单",
        price="999", tax_inclusive=False, qty="0",
    )
    add_line(
        part=part_a, day=None, purchase_type="销售订单",
        price="999", tax_inclusive=False,
    )
    add_line(
        part=part_a, day=date(2026, 7, 20), purchase_type="销售订单",
        price="1", tax_inclusive=False,
    )
    db.commit()
    return {
        "part_a": part_a,
        "prior_a": prior_a,
        "a_300": a_300,
        "a_50": a_50,
        "a_500": a_500,
        "designated_100": designated_100,
    }


def _threshold(payload: dict, multiplier: int) -> dict:
    return next(row for row in payload["thresholds"] if row["multiplier"] == multiplier)


def _purchase_type(payload: dict, name: str) -> dict:
    return next(row for row in payload["purchase_types"] if row["purchase_type"] == name)


def test_purchase_price_preview_stats_tax_direction_and_deterministic_samples(db):
    seeded = _seed_calibration_facts(db)
    client = _client(db, "calibration_reader", page=True, cost=True)
    params = {
        "date_from": "2026-07-01",
        "date_to": "2026-07-10",
        "sample_limit": 20,
    }

    first = client.get("/api/data-quality/calibration/purchase-price", params=params)
    second = client.get("/api/data-quality/calibration/purchase-price", params=params)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    payload = first.json()

    assert payload["rule_code"] == "purchase_adjacent_price_ratio"
    assert payload["rule_version"] == "preview-v1"
    assert payload["eligible_pairs"] == 5
    assert payload["distinct_parts"] == 2
    assert payload["data_through"] == "2026-07-02"
    assert payload["parameters"] == {
        "date_from": "2026-07-01",
        "date_to": "2026-07-10",
        "purchase_type": None,
        "sample_limit": 20,
    }

    expected = {
        2: (4, 3, 1),
        3: (4, 3, 1),
        5: (3, 2, 1),
        10: (2, 2, 0),
    }
    for multiplier, (candidates, increased, decreased) in expected.items():
        row = _threshold(payload, multiplier)
        assert row["eligible_pairs"] == 5
        assert row["candidate_pairs"] == candidates
        assert row["candidate_rate"] == candidates / 5
        assert row["increased_pairs"] == increased
        assert row["decreased_pairs"] == decreased

    sales = _purchase_type(payload, "销售订单")
    designated = _purchase_type(payload, "指定采购")
    assert sales["eligible_pairs"] == 4
    assert designated["eligible_pairs"] == 1
    assert _threshold(sales, 10)["candidate_pairs"] == 1
    assert _threshold(designated, 10)["candidate_pairs"] == 1

    increase = next(row for row in payload["direction_groups"]
                    if row["purchase_type"] == "销售订单"
                    and row["direction"] == "increase")
    decrease = next(row for row in payload["direction_groups"]
                    if row["purchase_type"] == "销售订单"
                    and row["direction"] == "decrease")
    assert increase["comparable_pairs"] == 2
    assert decrease["comparable_pairs"] == 1
    assert _threshold(increase, 5)["candidate_rate"] == 0.5
    assert _threshold(decrease, 5)["candidate_rate"] == 1.0

    # 时间窗只限制“本次”：6/30 的未税 100 仍是 7/1 的前值。
    pair = next(item for item in payload["samples"]
                if item["current_line_id"] == seeded["a_300"].id
                and item["multiplier"] == 3)
    assert pair["previous_line_id"] == seeded["prior_a"].id
    assert pair["previous_unit_price_ex_tax"] == 100
    assert pair["current_unit_price_ex_tax"] == 300
    assert pair["previous_tax_basis"] == "inc_tax"
    assert pair["current_tax_basis"] == "inc_tax"
    assert pair["ratio"] == 3
    assert pair["direction"] == "increase"
    assert "supplier" not in pair and "purchaser" not in pair

    # generated_at 可变，但同快照+同参数的统计、样本集和顺序必须完全一致。
    for key in ("eligible_pairs", "distinct_parts", "thresholds",
                "purchase_types", "direction_groups", "samples", "sample_boundary"):
        assert first.json()[key] == second.json()[key]
    bounded = client.get("/api/data-quality/calibration/purchase-price", params={
        **params, "sample_limit": 2,
    }).json()
    counts: dict[tuple[int, str], int] = {}
    for item in bounded["samples"]:
        key = (item["multiplier"], item["direction"])
        counts[key] = counts.get(key, 0) + 1
    assert all(count <= 2 for count in counts.values())


def test_purchase_type_and_date_filters_match_independent_sql(db):
    _seed_calibration_facts(db)
    client = _client(db, "calibration_sql", page=True, cost=True)
    response = client.get("/api/data-quality/calibration/purchase-price", params={
        "date_from": "2026-07-01",
        "date_to": "2026-07-10",
        "purchase_type": "销售订单",
        "sample_limit": 1,
    })
    assert response.status_code == 200, response.text
    payload = response.json()

    # 独立原生 SQL 只使用明确工作示例的口径，不复用服务层 CTE。
    sql = text("""
        WITH priced AS (
          SELECT l.id AS line_id, l.part_id, o.id AS order_id, o.order_date,
                 CASE WHEN o.is_tax_inclusive IS FALSE THEN l.unit_price
                      ELSE l.unit_price / 1.13 END AS unit_ex
          FROM f_purchase_line l
          JOIN f_purchase_order o ON o.id = l.order_id
          WHERE o.data_status = '已生效'
            AND o.source_type = '销售订单'
            AND o.order_date IS NOT NULL
            AND o.order_date <= DATE '2026-07-10'
            AND l.part_id IS NOT NULL AND l.qty > 0 AND l.unit_price > 0
        ), adjacent AS (
          SELECT *, lag(unit_ex) OVER (
            PARTITION BY part_id ORDER BY order_date, order_id, line_id
          ) AS previous_ex
          FROM priced
        ), pairs AS (
          SELECT *, greatest(unit_ex / previous_ex, previous_ex / unit_ex) AS ratio
          FROM adjacent
          WHERE previous_ex > 0 AND order_date >= DATE '2026-07-01'
        )
        SELECT count(*) AS eligible,
               count(*) FILTER (WHERE ratio >= 2) AS c2,
               count(*) FILTER (WHERE ratio >= 3) AS c3,
               count(*) FILTER (WHERE ratio >= 5) AS c5,
               count(*) FILTER (WHERE ratio >= 10) AS c10
        FROM pairs
    """)
    independent = db.execute(sql).mappings().one()
    assert payload["eligible_pairs"] == independent["eligible"]
    assert [_threshold(payload, n)["candidate_pairs"] for n in (2, 3, 5, 10)] == [
        independent["c2"], independent["c3"], independent["c5"], independent["c10"],
    ]
    assert [row["purchase_type"] for row in payload["purchase_types"]] == ["销售订单"]


def test_preview_is_fail_closed_and_performs_zero_writes(db):
    _seed_calibration_facts(db)
    allowed = _client(db, "calibration_allowed", page=True, cost=True)
    no_page = _client(db, "calibration_no_page", page=False, cost=True)
    no_cost = _client(db, "calibration_no_cost", page=True, cost=False)

    for denied in (no_page, no_cost):
        response = denied.get("/api/data-quality/calibration/purchase-price", params={
            "date_from": "2026-07-01", "date_to": "2026-07-10",
        })
        assert response.status_code == 403
        assert "ratio" not in response.text.lower()
        assert "unit_price" not in response.text.lower()
        assert "candidate" not in response.text.lower()

    before = {
        "issues": db.scalar(select(func.count()).select_from(FactDataQualityIssue)),
        "audits": db.scalar(select(func.count()).select_from(SysAuditLog)),
        "orders": db.execute(select(
            FPurchaseOrder.id, FPurchaseOrder.data_status, FPurchaseOrder.order_date,
            FPurchaseOrder.source_type, FPurchaseOrder.is_tax_inclusive,
        ).order_by(FPurchaseOrder.id)).all(),
        "lines": db.execute(select(
            FPurchaseLine.id, FPurchaseLine.order_id, FPurchaseLine.part_id,
            FPurchaseLine.qty, FPurchaseLine.unit_price, FPurchaseLine.line_amount,
        ).order_by(FPurchaseLine.id)).all(),
    }
    response = allowed.get("/api/data-quality/calibration/purchase-price", params={
        "date_from": "2026-07-01", "date_to": "2026-07-10",
    })
    assert response.status_code == 200, response.text
    db.expire_all()
    after = {
        "issues": db.scalar(select(func.count()).select_from(FactDataQualityIssue)),
        "audits": db.scalar(select(func.count()).select_from(SysAuditLog)),
        "orders": db.execute(select(
            FPurchaseOrder.id, FPurchaseOrder.data_status, FPurchaseOrder.order_date,
            FPurchaseOrder.source_type, FPurchaseOrder.is_tax_inclusive,
        ).order_by(FPurchaseOrder.id)).all(),
        "lines": db.execute(select(
            FPurchaseLine.id, FPurchaseLine.order_id, FPurchaseLine.part_id,
            FPurchaseLine.qty, FPurchaseLine.unit_price, FPurchaseLine.line_amount,
        ).order_by(FPurchaseLine.id)).all(),
    }
    assert after == before


def test_preview_validates_date_range_and_sample_limit(db, monkeypatch):
    _seed_calibration_facts(db)
    client = _client(db, "calibration_validation", page=True, cost=True)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 16)

    monkeypatch.setattr("app.api.data_quality_calibration.date", FixedDate)
    default_window = client.get(
        "/api/data-quality/calibration/purchase-price", params={"sample_limit": 1},
    )
    assert default_window.status_code == 200, default_window.text
    # 默认截止今日，7/20 的未来行不能变成可比对。
    assert default_window.json()["eligible_pairs"] == 5
    backwards = client.get("/api/data-quality/calibration/purchase-price", params={
        "date_from": "2026-07-10", "date_to": "2026-07-01",
    })
    assert backwards.status_code == 422
    assert client.get(
        "/api/data-quality/calibration/purchase-price", params={"sample_limit": 0},
    ).status_code == 422
    assert client.get(
        "/api/data-quality/calibration/purchase-price", params={"sample_limit": 21},
    ).status_code == 422
