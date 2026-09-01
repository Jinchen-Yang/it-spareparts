"""Dual-tax financial identities must be derived from rounded basis components."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import permissions, security
from app.api import dashboard as dashboard_api
from app.api import profit as profit_api
from app.db import get_db
from app.etl import loader
from app.main import app
from app.models.system import SysImportBatch
from app.services import dashboard, maintenance_margin, profit
from tests import factories as f


AS_OF = date(2026, 6, 1)


def _seed_rounding_case(
    db,
    *,
    suffix: str,
    purchase_ex: str,
    sale_inc: str,
) -> None:
    batch = SysImportBatch(
        filename=f"financial-identity-{suffix}.xlsx",
        file_type="purchase",
        file_hash=f"financial-identity-{suffix}",
    )
    db.add(batch)
    db.flush()
    purchase_id = f"P-{suffix}"
    sale_id = f"S-{suffix}"
    pn = f"PN-{suffix}"
    loader.load(
        db,
        f.purchase_result(
            {
                purchase_id: f.purchase_head(
                    purchase_id,
                    on=date(2026, 1, 5),
                    is_tax_inclusive=False,
                ),
            },
            [
                f.purchase_line(
                    purchase_id,
                    f"PL-{suffix}",
                    pn,
                    qty="10",
                    price=purchase_ex,
                ),
            ],
        ),
        batch.id,
        AS_OF,
    )
    loader.load(
        db,
        f.sales_result(
            {
                sale_id: f.sales_head(
                    sale_id,
                    order_no=f"SO-{suffix}",
                    on=date(2026, 2, 1),
                ),
            },
            [
                f.sales_line(
                    sale_id,
                    f"SL-{suffix}",
                    pn,
                    qty="1",
                    price=sale_inc,
                ),
            ],
        ),
        batch.id,
        AS_OF,
    )
    db.commit()
    profit.recompute(db)


@pytest.mark.parametrize(
    ("suffix", "purchase_ex", "sale_inc", "expected"),
    [
        (
            "SMALL",
            "0.02",
            "0.05",
            {
                "revenue_ex": 0.04,
                "cost_ex": 0.02,
                "profit_ex": 0.02,
                "revenue_inc": 0.05,
                "cost_inc": 0.02,
                "profit_inc": 0.03,
            },
        ),
        (
            "MIDPOINT",
            "0.49",
            "0.57",
            {
                "revenue_ex": 0.50,
                "cost_ex": 0.49,
                "profit_ex": 0.01,
                "revenue_inc": 0.57,
                "cost_inc": 0.55,
                "profit_inc": 0.02,
            },
        ),
    ],
)
def test_profit_service_subtracts_rounded_components_in_each_tax_basis(
    db,
    suffix,
    purchase_ex,
    sale_inc,
    expected,
):
    _seed_rounding_case(
        db,
        suffix=suffix,
        purchase_ex=purchase_ex,
        sale_inc=sale_inc,
    )

    row = profit.aggregate(db, "part", None, None, False)["rows"][0]

    assert row["revenue_costed_ex"] == expected["revenue_ex"]
    assert row["cost_moving_avg_ex"] == expected["cost_ex"]
    assert row["gross_profit_moving_ex"] == expected["profit_ex"]
    assert row["revenue_costed_inc"] == expected["revenue_inc"]
    assert row["cost_moving_avg_inc"] == expected["cost_inc"]
    assert row["gross_profit_moving_inc"] == expected["profit_inc"]
    assert row["gross_profit_moving_inc"] == pytest.approx(
        row["revenue_costed_inc"] - row["cost_moving_avg_inc"],
    )
    assert row["gross_profit_fifo_inc"] == expected["profit_inc"]


def test_dashboard_paths_keep_small_value_inc_profit_identity(db):
    _seed_rounding_case(
        db,
        suffix="DASHBOARD",
        purchase_ex="0.02",
        sale_inc="0.05",
    )

    kpi = dashboard.kpi(db, None, None, as_of=AS_OF)
    ranking = dashboard.part_ranking(db, None, None, as_of=AS_OF)
    orders = dashboard.sales_orders(db, as_of=AS_OF)

    assert kpi["gross_profit_inc"] == 0.03
    assert ranking["ranking"]["items"][0]["gross_profit_moving_inc"] == 0.03
    assert ranking["ranking"]["items"][0]["gross_profit_fifo_inc"] == 0.03
    assert orders["items"][0]["total_gross_profit_inc"] == 0.03


def test_profit_and_dashboard_api_keep_small_value_inc_profit_identity(db):
    _seed_rounding_case(
        db,
        suffix="API",
        purchase_ex="0.02",
        sale_inc="0.05",
    )
    ctx = security.UserContext(
        user_id="rounding-review",
        role="boss",
        permissions=permissions._full(),
        is_authenticated=True,
    )
    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[profit_api.current_role] = lambda: ctx.role
    app.dependency_overrides[profit_api.get_current_user_context] = lambda: ctx
    app.dependency_overrides[dashboard_api.current_role] = lambda: ctx.role
    app.dependency_overrides[dashboard_api.get_current_user_context] = lambda: ctx
    try:
        client = TestClient(app)
        profit_response = client.get("/api/profit", params={"dimension": "part"})
        kpi_response = client.get("/api/dashboard/kpi")
        ranking_response = client.get(
            "/api/dashboard/part-ranking",
            params={"pn": "PN-API"},
        )
        orders_response = client.get(
            "/api/dashboard/sales",
            params={"order_no": "SO-API"},
        )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)

    assert profit_response.status_code == 200, profit_response.text
    assert kpi_response.status_code == 200, kpi_response.text
    assert ranking_response.status_code == 200, ranking_response.text
    assert orders_response.status_code == 200, orders_response.text
    assert profit_response.json()["rows"][0]["gross_profit_moving_inc"] == 0.03
    assert kpi_response.json()["gross_profit_inc"] == 0.03
    assert (
        ranking_response.json()["ranking"]["items"][0][
            "gross_profit_moving_inc"
        ]
        == 0.03
    )
    assert orders_response.json()["items"][0]["total_gross_profit_inc"] == 0.03


def test_maintenance_contribution_subtracts_rounded_basis_components():
    result = maintenance_margin.calculate_contract_margin(
        revenue_inc=Decimal("0.57"),
        revenue_ex=Decimal("0.50"),
        tax_rate=Decimal("0.13"),
        parts_cost_inc_tax=Decimal("0.55"),
        parts_cost_ex_tax=Decimal("0.49"),
        cost_quality_inc="actual_only",
        cost_quality_ex="actual_only",
        expense_data_available=True,
        date_filtered=False,
        expense_inc=Decimal("0.01"),
        expense_ex=Decimal("0.01"),
    )

    assert result["revenue_inc"] == Decimal("0.57")
    assert result["parts_gross_profit_inc"] == Decimal("0.02")
    assert result["parts_gross_profit_ex"] == Decimal("0.01")
    assert result["contribution_profit_inc"] == Decimal("0.01")
    assert result["contribution_profit_ex"] == Decimal("0.00")
