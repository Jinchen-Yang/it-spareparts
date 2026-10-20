"""D-33：旧项目明细入口保留自己的查询集合，分页与 CSV 共用筛选。"""

import csv
import io
import json
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch
from tests import factories as f
from tests.test_maintenance_download_object_semantics import _scoped_sales_client
from tests.test_maintenance_export_headers import (
    _admin_client,
    _cost_blind_maintenance_client,
    _profit_blind_maintenance_client,
)


@pytest.fixture()
def legacy_filter_rows(db):
    batch = SysImportBatch(
        filename="legacy-row-filters.xlsx",
        file_type="maintenance",
        file_hash="legacy-row-filters",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "LEGACY-JUL": f.maintenance_head(
                    "LEGACY-JUL",
                    order_no="WBDD-LEGACY-JUL",
                    on=date(2026, 7, 20),
                    project="旧明细筛选项目",
                ),
                "LEGACY-AUG": f.maintenance_head(
                    "LEGACY-AUG",
                    order_no="WBDD-LEGACY-AUG",
                    on=date(2026, 8, 20),
                    project="旧明细筛选项目",
                ),
            },
            [
                f.maintenance_line(
                    "LEGACY-JUL", f"LEGACY-{number}", f"KEEP-{number}",
                    qty=str(number),
                )
                for number in range(1, 5)
            ] + [
                f.maintenance_line("LEGACY-JUL", "LEGACY-DROP", "DROP", qty="3"),
                f.maintenance_line("LEGACY-AUG", "LEGACY-LATE", "KEEP-LATE", qty="3"),
            ],
        ),
        batch.id,
        date(2026, 8, 20),
    )
    db.commit()


def test_legacy_list_count_and_complete_csv_share_multifield_filters(
    db, legacy_filter_rows,
):
    client = _admin_client(db)
    params = {
        "project": "旧明细筛选项目",
        "filters": json.dumps([
            {"field": "pn_std", "op": "contains", "value": "KEEP"},
            {"field": "qty", "op": "range", "min": 2, "max": 4},
            {
                "field": "order_date", "op": "range",
                "min": "2026-07-01", "max": "2026-07-31",
            },
        ]),
    }

    first = client.get(
        "/api/maintenance/lines", params={**params, "page": 1, "page_size": 1},
    )
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 3
    assert [row["pn_std"] for row in first.json()["rows"]] == ["KEEP-4"]
    second = client.get(
        "/api/maintenance/lines", params={**params, "page": 2, "page_size": 1},
    )
    assert second.status_code == 200, second.text
    assert second.json()["total"] == 3
    assert [row["pn_std"] for row in second.json()["rows"]] == ["KEEP-3"]

    exported = client.get("/api/maintenance/lines/export", params=params)
    assert exported.status_code == 200, exported.text
    exported_rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert [row["PN"] for row in exported_rows] == ["KEEP-2", "KEEP-3", "KEEP-4"]
    assert len(exported_rows) == first.json()["total"]


@pytest.mark.parametrize("endpoint", ["/lines", "/lines/export"])
@pytest.mark.parametrize("sort_key", ["sort", "sort_by"])
def test_legacy_rows_reject_unsupported_sort_instead_of_ignoring_it(
    db, legacy_filter_rows, endpoint, sort_key,
):
    client = _admin_client(db)

    response = client.get(
        f"/api/maintenance{endpoint}",
        params={"project": "旧明细筛选项目", sort_key: "system_unit_cost_ex_tax"},
    )

    assert response.status_code == 422
    assert "total" not in response.json()


@pytest.mark.parametrize("endpoint", ["/lines", "/lines/export"])
@pytest.mark.parametrize("condition", [
    {"field": "raw_line_id", "op": "eq", "value": "LEGACY-1"},
    {"field": "signature", "op": "eq", "value": "secret"},
    {"field": "not_registered", "op": "is_empty"},
    {"field": "qty", "op": "contains", "value": "2"},
])
def test_legacy_rows_reject_technical_unknown_fields_and_invalid_operations(
    db, legacy_filter_rows, endpoint, condition,
):
    client = _admin_client(db)

    response = client.get(
        f"/api/maintenance{endpoint}",
        params={"project": "旧明细筛选项目", "filters": json.dumps([condition])},
    )

    assert response.status_code == 422
    assert "total" not in response.json()


@pytest.mark.parametrize("endpoint", ["/lines", "/lines/export"])
@pytest.mark.parametrize("condition", [
    {"field": "system_unit_cost_ex_tax", "op": "is_empty"},
    {"field": "system_unit_cost_ex_tax", "op": "eq", "value": 1},
    {"field": "cost_source", "op": "is_empty"},
])
def test_legacy_cost_blind_filters_are_rejected_without_count(
    db, legacy_filter_rows, endpoint, condition,
):
    client = _cost_blind_maintenance_client(db)

    response = client.get(
        f"/api/maintenance{endpoint}",
        params={"project": "旧明细筛选项目", "filters": json.dumps([condition])},
    )

    assert response.status_code == 403
    assert "total" not in response.json()
    assert "rows" not in response.json()


def test_legacy_amount_filters_keep_purchase_cost_eligibility_without_profit(
    db, legacy_filter_rows,
):
    client = _profit_blind_maintenance_client(db)
    params = {
        "project": "旧明细筛选项目",
        "filters": json.dumps([
            {"field": "system_unit_cost_ex_tax", "op": "is_empty"},
            {"field": "qty", "op": "eq", "value": 2},
        ]),
    }

    listed = client.get("/api/maintenance/lines", params=params)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert [row["pn_std"] for row in listed.json()["rows"]] == ["KEEP-2"]
    exported = client.get("/api/maintenance/lines/export", params=params)
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert [row["PN"] for row in rows] == ["KEEP-2"]


def test_legacy_filters_do_not_expand_salesperson_scope(db, legacy_filter_rows):
    orders = db.scalars(select(FMaintenanceOrder)).all()
    for order in orders:
        order.salesperson = (
            "本销售" if order.raw_order_id == "LEGACY-JUL" else "其他销售"
        )
    db.commit()
    client = _scoped_sales_client(db, salesperson_name="本销售")
    params = {
        "project": "旧明细筛选项目",
        "filters": json.dumps([{"field": "pn_std", "op": "prefix", "value": "KEEP"}]),
    }

    listed = client.get("/api/maintenance/lines", params=params)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 4
    assert [row["pn_std"] for row in listed.json()["rows"]] == [
        "KEEP-4", "KEEP-3", "KEEP-2", "KEEP-1",
    ]
    exported = client.get("/api/maintenance/lines/export", params=params)
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert [row["PN"] for row in rows] == ["KEEP-1", "KEEP-2", "KEEP-3", "KEEP-4"]

    hidden_only = {
        **params,
        "filters": json.dumps([{"field": "pn_std", "op": "eq", "value": "KEEP-LATE"}]),
    }
    empty_list = client.get("/api/maintenance/lines", params=hidden_only)
    assert empty_list.status_code == 200, empty_list.text
    assert empty_list.json()["total"] == 0
    assert empty_list.json()["rows"] == []
    empty_export = client.get("/api/maintenance/lines/export", params=hidden_only)
    assert empty_export.status_code == 422


def test_legacy_filters_keep_cost_start_active_line_and_source_status_scope(
    db, legacy_filter_rows,
):
    lines = {
        line.raw_line_id: line for line in db.scalars(select(FMaintenanceLine))
    }
    lines["LEGACY-4"].is_active = False
    late_order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == "LEGACY-AUG",
    ))
    late_order.data_status = "已取消"
    db.commit()
    client = _admin_client(db)
    params = {
        "project": "旧明细筛选项目",
        "filters": json.dumps([{"field": "pn_std", "op": "prefix", "value": "KEEP"}]),
    }

    listed = client.get("/api/maintenance/lines", params=params)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 3
    assert [row["pn_std"] for row in listed.json()["rows"]] == [
        "KEEP-3", "KEEP-2", "KEEP-1",
    ]
    exported = client.get("/api/maintenance/lines/export", params=params)
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert [row["PN"] for row in rows] == ["KEEP-1", "KEEP-2", "KEEP-3"]

    july_order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == "LEGACY-JUL",
    ))
    july_order.order_date = date(2023, 12, 31)
    db.commit()
    empty_list = client.get("/api/maintenance/lines", params=params)
    assert empty_list.status_code == 200, empty_list.text
    assert empty_list.json()["total"] == 0
    empty_export = client.get("/api/maintenance/lines/export", params=params)
    assert empty_export.status_code == 422
