"""D-33：真实 API 的类型运算、空值、SQL 通配字符与三套成本语义。"""

import csv
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.inventory import PartPool
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceManualCostOverride,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch
from tests.boss_board_helpers import client_for, make_project
from tests.test_maintenance_boss_facts import _pool


BASE = "/api/maintenance/spare-part-lines"
WINDOW = {"range": "custom", "from": "2026-07-01", "to": "2026-07-31"}


def _filter_params(conditions):
    return {**WINDOW, "filters": json.dumps(conditions)}


def _csv_rows(response):
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.fixture()
def operator_rows(db):
    project = make_project(db, "筛选类型运算合成项目")
    batch = SysImportBatch(
        filename="row-filter-operators.xlsx",
        file_type="maintenance",
        file_hash="row-filter-operators",
    )
    db.add(batch)
    db.flush()
    # A/B/C/D 位于日期窗内；E/F 用来证明额外条件不会扩大既有日期窗。
    for tag, on, business_type, quantity, note, source in [
        ("A", date(2026, 7, 1), "备件维保", "0", "start%_finish", "wbdd"),
        ("B", date(2026, 7, 10), "整体维保", "2", "startABfinish", "wbdd"),
        ("C", date(2026, 7, 31), None, "4", "", "workbook_manual"),
        ("D", date(2026, 7, 10), "", None, None, "page_manual"),
        ("E", date(2026, 8, 1), "算力运维", "10", "start outside", "wbdd"),
        ("F", None, "备件维保", "9", "undated", "wbdd"),
    ]:
        order = FMaintenanceOrder(
            raw_order_id=f"filter-operators-{tag}",
            order_no=f"WBDD-OPERATORS-{tag}",
            order_date=on,
            project_std=project.display_name,
            business_type=business_type,
            data_status="已生效",
            import_batch_id=batch.id,
        )
        part = DimPart(pn_std=f"OPS-{tag}")
        db.add_all([order, part])
        db.flush()
        db.add(FMaintenanceLine(
            raw_line_id=f"filter-operators-line-{tag}",
            order_id=order.id,
            part_id=part.id,
            line_no=1,
            pn_std=f"OPS-{tag}",
            qty=Decimal(quantity) if quantity is not None else None,
            return_qty=Decimal("0"),
            line_note=note,
            edited_source=source,
            cost_source="none",
            import_batch_id=batch.id,
        ))
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid4()),
            source_order_id=order.raw_order_id,
            project_id=project.project_id,
            created_by="operator-fixture",
        ))
    db.commit()
    return project


@pytest.mark.parametrize(("condition", "expected"), [
    ({"field": "line_note", "op": "contains", "value": "start"}, ["OPS-A", "OPS-B"]),
    ({"field": "line_note", "op": "eq", "value": "start%_finish"}, ["OPS-A"]),
    ({"field": "line_note", "op": "prefix", "value": "start"}, ["OPS-A", "OPS-B"]),
    ({"field": "line_note", "op": "is_empty"}, ["OPS-C", "OPS-D"]),
    ({"field": "line_note", "op": "contains", "value": "%"}, ["OPS-A"]),
    ({"field": "line_note", "op": "contains", "value": "_"}, ["OPS-A"]),
    ({"field": "line_note", "op": "prefix", "value": "start%"}, ["OPS-A"]),
    ({"field": "business_type", "op": "in", "value": ["备件维保", "整体维保"]},
     ["OPS-A", "OPS-B"]),
    ({"field": "business_type", "op": "is_empty"}, ["OPS-C", "OPS-D"]),
    ({"field": "source", "op": "in", "value": ["workbook_manual", "page_manual"]},
     ["OPS-C", "OPS-D"]),
    ({"field": "source", "op": "is_empty"}, []),
    # 金臣 10-15：需求数量为空按 0 算（OPS-D 为 NULL）；「为空」仍只命中 NULL。
    ({"field": "qty", "op": "eq", "value": 0}, ["OPS-A", "OPS-D"]),
    ({"field": "qty", "op": "range", "min": 0, "max": 2}, ["OPS-A", "OPS-B", "OPS-D"]),
    ({"field": "qty", "op": "range", "min": 2}, ["OPS-B", "OPS-C"]),
    ({"field": "qty", "op": "range", "max": 2}, ["OPS-A", "OPS-B", "OPS-D"]),
    ({"field": "qty", "op": "is_empty"}, ["OPS-D"]),
    ({"field": "order_date", "op": "eq", "value": "2026-07-10"}, ["OPS-B", "OPS-D"]),
    ({"field": "order_date", "op": "range", "min": "2026-07-01", "max": "2026-07-31"},
     ["OPS-A", "OPS-B", "OPS-C", "OPS-D"]),
    ({"field": "order_date", "op": "range", "min": "2026-07-10"},
     ["OPS-B", "OPS-C", "OPS-D"]),
    ({"field": "order_date", "op": "range", "max": "2026-07-10"},
     ["OPS-A", "OPS-B", "OPS-D"]),
    ({"field": "order_date", "op": "is_empty"}, []),
], ids=[
    "text-contains", "text-equals", "text-prefix", "text-empty",
    "literal-percent", "literal-underscore", "literal-percent-prefix",
    "enum-in", "enum-empty", "source-in", "source-empty",
    "number-zero", "number-closed-range", "number-min", "number-max", "number-empty",
    "date-equals", "date-closed-range", "date-min", "date-max", "date-empty-with-window",
])
def test_typed_operators_share_list_count_and_csv(
    db, operator_rows, condition, expected,
):
    client = client_for(db, username="operator-admin", role="admin")
    params = _filter_params([condition])

    response = client.get(BASE + "/rows", params={**params, "page_size": 20})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(expected)
    assert sorted(row["pn_std"] for row in response.json()["rows"]) == expected
    exported = _csv_rows(client.get(BASE + "/export", params={**params, "format": "csv"}))
    assert sorted(row["PN"] for row in exported) == expected


def test_xlsx_preserves_multitype_closed_boundaries_and_complete_filtered_set(
    db, operator_rows,
):
    client = client_for(db, username="operator-xlsx-admin", role="admin")
    params = _filter_params([
        {"field": "business_type", "op": "in", "value": ["备件维保", "整体维保"]},
        {"field": "line_note", "op": "prefix", "value": "start"},
        {"field": "qty", "op": "range", "min": 0, "max": 2},
        {"field": "order_date", "op": "range", "min": "2026-07-01", "max": "2026-07-10"},
    ])

    listed = client.get(BASE + "/rows", params={**params, "page_size": 1})
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 2
    assert len(listed.json()["rows"]) == 1
    exported = client.get(BASE + "/export", params={**params, "format": "xlsx"})
    assert exported.status_code == 200, exported.text
    workbook = load_workbook(io.BytesIO(exported.content), data_only=True)
    try:
        values = list(workbook.active.values)
        pn_index = values[0].index("PN")
        assert [row[pn_index] for row in values[1:]] == ["OPS-A", "OPS-B"]
    finally:
        workbook.close()


def test_empty_date_on_project_rows_keeps_undated_rows_outside_global_window(
    db, operator_rows,
):
    client = client_for(db, username="operator-empty-date-admin", role="admin")
    path = (
        f"/api/maintenance/projects/stable/{operator_rows.project_id}"
        "/master-workbook/rows"
    )

    response = client.get(path, params={
        "sheet": "03_备件订单", "page": 1,
        "filters": json.dumps([{"field": "order_date", "op": "is_empty"}]),
    })

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert [row["pn_std"] for row in response.json()["rows"]] == ["OPS-F"]


@pytest.mark.parametrize("condition", [
    {"field": "unknown", "op": "eq", "value": "x"},
    {"field": "line_id", "op": "eq", "value": 1},
    {"field": "part_id", "op": "eq", "value": 1},
    {"field": "raw_line_id", "op": "eq", "value": "x"},
    {"field": "readonly_hash", "op": "eq", "value": "x"},
    {"field": "baseline_token", "op": "eq", "value": "x"},
    {"field": "order_date", "op": "eq", "value": "2026-02-30"},
    {"field": "order_date", "op": "eq", "value": "20260701"},
    {"field": "qty", "op": "eq", "value": "NaN"},
    {"field": "qty", "op": "eq", "value": "Infinity"},
    {"field": "qty", "op": "range", "min": 4, "max": 2},
    {"field": "order_date", "op": "range", "min": "2026-07-31", "max": "2026-07-01"},
    {"field": "qty", "op": "eq", "value": True},
    {"field": "qty", "op": "range", "min": False},
    {"field": "qty", "op": "eq", "value": None},
    {"field": "qty", "op": "range"},
    {"field": "qty", "op": "range", "min": 1, "value": 2},
    {"field": "qty", "op": "eq", "value": 1, "unexpected": "value"},
    {"field": "qty", "op": "is_empty", "value": 0},
    {"field": "qty", "op": "contains", "value": "1"},
    {"field": "business_type", "op": "in", "value": []},
    {"field": "business_type", "op": "in", "value": "备件维保"},
    {"field": "business_type", "op": "in", "value": [True]},
    {"field": "line_note", "op": "eq", "value": {"text": "x"}},
], ids=[
    "unknown-field", "line-identity", "part-identity", "raw-identity", "readonly-hash",
    "price-token", "invalid-date", "noncanonical-date", "nan", "infinity",
    "reversed-number-range", "reversed-date-range", "boolean-equality",
    "boolean-endpoint", "null-equality", "missing-endpoints", "range-extra-value",
    "unknown-property", "empty-extra-value", "wrong-operator", "empty-in",
    "scalar-in", "boolean-in", "object-value",
])
def test_invalid_filters_fail_before_list_or_export(db, condition):
    client = client_for(db, username="invalid-filter-admin", role="admin")
    params = _filter_params([condition])

    for suffix in ("/rows", "/export"):
        response = client.get(BASE + suffix, params=params)
        assert response.status_code == 422, response.text
        assert "total" not in response.json()
        assert "rows" not in response.json()


def test_field_metadata_lists_type_specific_operations_without_technical_columns(db):
    client = client_for(db, username="operator-fields-admin", role="admin")

    response = client.get(BASE + "/fields")

    assert response.status_code == 200, response.text
    fields = {item["field"]: item for item in response.json()["fields"]}
    assert {
        "pool_name", "pool_status", "in_pool", "known_apply_cost_inc_tax",
        "legacy_unit_cost", "legacy_cost_amount", "legacy_cost_tier",
        "legacy_cost_tax_basis", "anomaly_flags",
    } <= fields.keys()
    for name, field_type, operators in [
        ("line_note", "text", ["contains", "eq", "prefix", "is_empty"]),
        ("business_type", "enum", ["in", "is_empty"]),
        ("source", "enum", ["in", "is_empty"]),
        ("qty", "number", ["eq", "range", "is_empty"]),
        ("order_date", "date", ["eq", "range", "is_empty"]),
        ("pool_name", "text", ["contains", "eq", "prefix", "is_empty"]),
        ("pool_status", "enum", ["in", "is_empty"]),
        ("known_apply_cost_inc_tax", "number", ["eq", "range", "is_empty"]),
        ("legacy_unit_cost", "number", ["eq", "range", "is_empty"]),
        ("legacy_cost_amount", "number", ["eq", "range", "is_empty"]),
    ]:
        assert fields[name]["type"] == field_type
        assert fields[name]["operators"] == operators
    assert {item["value"] for item in fields["source"]["options"]} == {
        "wbdd", "workbook_manual", "page_manual",
    }
    assert not {
        "line_id", "raw_line_id", "part_id", "readonly_hash", "baseline_token",
        "pool_id", "pool_version", "reference_pool_group_id", "reference_pool_version",
    } & fields.keys()


def test_pool_filters_follow_master_parts_and_prefer_active_then_recent_archived(
    db, operator_rows,
):
    target = DimPart(pn_std="OPS-MERGE-TARGET")
    db.add(target)
    db.flush()
    source = db.scalar(select(DimPart).where(DimPart.pn_std == "OPS-C"))
    source.status, source.merged_into_id = "merged", target.id
    db.commit()
    old_archive = _pool(db, "旧归档池", status="archived", pns=["OPS-A", "OPS-B", "OPS-C"])
    new_archive = _pool(db, "新归档池", status="archived", pns=["OPS-A", "OPS-B"])
    active = _pool(db, "主档有效池", pns=["OPS-A", "OPS-MERGE-TARGET"])
    # 即使归档池更新时间更晚，A/C 仍取有效池；只有归档池的 B 取较新者。
    for pool_id, updated in [
        (active, datetime(2020, 1, 1, tzinfo=timezone.utc)),
        (old_archive, datetime(2025, 1, 1, tzinfo=timezone.utc)),
        (new_archive, datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ]:
        db.get(PartPool, pool_id).updated_at = updated
    db.commit()
    client = client_for(db, username="pool-filter-admin", role="admin")

    response = client.get(BASE + "/rows", params={**WINDOW, "page_size": 20})
    assert response.status_code == 200, response.text
    assert {
        row["pn_std"]: (row["pool_name"], row["pool_status"])
        for row in response.json()["rows"]
    } == {
        "OPS-A": ("主档有效池", "active"),
        "OPS-B": ("新归档池", "archived"),
        "OPS-C": ("主档有效池", "active"),
        "OPS-D": (None, None),
    }
    for condition, expected in [
        ({"field": "pool_name", "op": "eq", "value": "主档有效池"}, ["OPS-A", "OPS-C"]),
        ({"field": "pool_status", "op": "in", "value": ["archived"]}, ["OPS-B"]),
        ({"field": "pool_name", "op": "eq", "value": "旧归档池"}, []),
        ({"field": "pool_status", "op": "is_empty"}, ["OPS-D"]),
    ]:
        params = _filter_params([condition])
        listed = client.get(BASE + "/rows", params=params)
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == len(expected)
        assert sorted(row["pn_std"] for row in listed.json()["rows"]) == expected
        exported = _csv_rows(client.get(BASE + "/export", params={**params, "format": "csv"}))
        assert sorted(row["PN"] for row in exported) == expected


@pytest.fixture()
def cost_operator_rows(db):
    project = make_project(db, "三套成本筛选合成项目")
    batch = SysImportBatch(
        filename="row-filter-costs.xlsx",
        file_type="maintenance",
        file_hash="row-filter-costs",
    )
    db.add(batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id="filter-costs-order", order_no="WBDD-FILTER-COSTS",
        order_date=date(2026, 7, 10), data_status="已生效",
        project_std=project.display_name, import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()), source_order_id=order.raw_order_id,
        project_id=project.project_id, created_by="cost-fixture",
    ))
    specs = [
        # 标签、来源、需求量、退货量、系统单位未税、系统未税金额、人工单位未税、active。
        ("AUTO", "direct", "4", "1", "10", "30", "99", True),
        ("FALLBACK", "none", "4", "1", "777", None, "20", True),
        ("ZERO-QTY", "none", "0", "0", None, None, "30", True),
        ("NULL-QTY", "none", None, "0", None, None, "40", True),
        ("INACTIVE", "none", "3", "0", None, None, "50", False),
        ("DIRTY", "dirty", "3", "0", "70", "210", "60", True),
        ("ZERO-PRICE", "direct", "3", "0", "0", "0", "80", True),
    ]
    for index, (tag, source, qty, returned, unit, amount, manual, active) in enumerate(specs):
        part = DimPart(pn_std=f"COST-{tag}")
        db.add(part)
        db.flush()
        ex_unit = Decimal(unit) if unit is not None else None
        ex_amount = Decimal(amount) if amount is not None else None
        line = FMaintenanceLine(
            raw_line_id=f"filter-costs-{tag}", order_id=order.id,
            part_id=part.id, pn_std=f"COST-{tag}", line_no=index + 1,
            qty=Decimal(qty) if qty is not None else None,
            return_qty=Decimal(returned), cost_source=source,
            cost_tax_basis="ex", confidence="high",
            unit_cost=ex_unit, cost_amount=ex_amount,
            unit_cost_ex_tax=ex_unit,
            unit_cost_inc_tax=ex_unit * Decimal("1.13") if ex_unit is not None else None,
            cost_amount_ex_tax=ex_amount,
            cost_amount_inc_tax=ex_amount * Decimal("1.13") if ex_amount is not None else None,
            import_batch_id=batch.id,
        )
        db.add(line)
        db.flush()
        db.add(MaintenanceManualCostOverride(
            line_id=line.id, unit_cost_ex_tax=Decimal(manual),
            unit_cost_inc_tax=Decimal(manual) * Decimal("1.13"),
            reason=f"fixture-{tag}", active=active,
        ))
    db.commit()
    return project


def test_cost_row_projection_keeps_system_effective_and_manual_separate(
    db, cost_operator_rows,
):
    client = client_for(db, username="cost-projection-admin", role="admin")

    response = client.get(BASE + "/rows", params={**WINDOW, "page_size": 20})
    assert response.status_code == 200, response.text
    actual = {}
    columns = (
        "system_unit_cost_ex_tax", "effective_unit_cost_ex_tax",
        "manual_unit_cost_ex_tax", "effective_cost_amount_inc_tax",
    )
    for row in response.json()["rows"]:
        assert row["known_apply_cost_inc_tax"] == row["effective_cost_amount_inc_tax"]
        actual[row["pn_std"]] = tuple(
            Decimal(str(row[name])) if row[name] is not None else None
            for name in columns
        )
    assert actual == {
        "COST-AUTO": (Decimal("10"), Decimal("10"), Decimal("99"), Decimal("33.90")),
        "COST-FALLBACK": (Decimal("777"), Decimal("20"), Decimal("20"), Decimal("67.80")),
        "COST-ZERO-QTY": (None, Decimal("30"), Decimal("30"), Decimal("0")),
        "COST-NULL-QTY": (None, None, Decimal("40"), None),
        "COST-INACTIVE": (None, None, None, None),
        "COST-DIRTY": (Decimal("70"), None, Decimal("60"), None),
        "COST-ZERO-PRICE": (Decimal("0"), Decimal("0"), Decimal("80"), Decimal("0")),
    }


@pytest.mark.parametrize(("condition", "expected"), [
    ({"field": "system_unit_cost_ex_tax", "op": "eq", "value": "777"}, ["COST-FALLBACK"]),
    ({"field": "effective_unit_cost_ex_tax", "op": "eq", "value": "20"}, ["COST-FALLBACK"]),
    ({"field": "manual_unit_cost_ex_tax", "op": "eq", "value": "99"}, ["COST-AUTO"]),
    ({"field": "effective_cost_amount_inc_tax", "op": "eq", "value": "67.80"},
     ["COST-FALLBACK"]),
    ({"field": "effective_cost_amount_inc_tax", "op": "range", "min": 0, "max": 0},
     ["COST-ZERO-PRICE", "COST-ZERO-QTY"]),
    ({"field": "effective_unit_cost_ex_tax", "op": "is_empty"},
     ["COST-DIRTY", "COST-INACTIVE", "COST-NULL-QTY"]),
    ({"field": "manual_unit_cost_ex_tax", "op": "is_empty"}, ["COST-INACTIVE"]),
    ({"field": "known_apply_cost_inc_tax", "op": "eq", "value": "67.80"},
     ["COST-FALLBACK"]),
], ids=[
    "raw-system-value", "manual-effective-fallback", "manual-with-system-priority",
    "effective-net-tax-amount", "real-zero-amounts", "invalid-evidence-remains-empty",
    "inactive-manual-is-empty", "known-apply-cost-keeps-effective-meaning",
])
def test_cost_filters_match_effective_projection_and_csv(
    db, cost_operator_rows, condition, expected,
):
    client = client_for(db, username="cost-filter-admin", role="admin")
    params = _filter_params([condition])

    response = client.get(BASE + "/rows", params={**params, "page_size": 20})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == len(expected)
    assert sorted(row["pn_std"] for row in response.json()["rows"]) == expected
    exported = _csv_rows(client.get(BASE + "/export", params={**params, "format": "csv"}))
    assert sorted(row["PN"] for row in exported) == expected
