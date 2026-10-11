"""D-27 source membership survives rejected rows and bounds only observed days."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.maintenance_wbdd_source_snapshot import (
    block_snapshot_errors,
    capture_source_snapshot,
    daily_order_windows,
)


def frame(*rows, index=None):
    return pd.DataFrame([
        {"数据ID(不可修改)": parent, "需求单号": number,
         "需求明细.数据ID(不可修改)": child, **extra}
        for parent, number, child, extra in rows
    ], index=index)


def row(parent="head-a", number="WBDD-20260609-0001", child="line-a", **extra):
    return parent, number, child, extra


def capture(df, **kwargs):
    return capture_source_snapshot(df, sheet_name="维保需求单", **kwargs)


def test_capture_keeps_raw_members_even_when_business_fields_are_invalid():
    snapshot = capture(frame(
        row(child="line-b", **{"需求明细.需供货产品": "", "需求明细.需求数量": "bad"}),
        row(child="line-a", **{"收货地址": "PRIVATE-ADDRESS", "联系人": "PRIVATE-NAME"}),
        row(child="line-a"),
    ))
    assert snapshot == {
        "version": 1, "sheet_name": "维保需求单", "row_count": 3,
        "orders": [{"raw_order_id": "head-a", "order_no": "WBDD-20260609-0001",
                    "row_numbers": [1, 2, 3], "line_ids": ["line-a", "line-b"],
                    "blocked_reasons": []}],
        "line_ids": ["line-a", "line-b"], "blocked_reasons": [],
        "ignored_wbdd_sheets": [],
    }
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert "PRIVATE" not in encoded and "bad" not in encoded


def test_bad_rows_block_only_their_parent_and_never_disappear():
    snapshot = capture(frame(row(), row("head-b", "WBDD-20260609-0003", "line-b")))
    blocked = block_snapshot_errors(snapshot, [
        SimpleNamespace(row_no=1, error_type="bad_number", raw_row={"secret": "private"}),
        {"row_no": 1, "error_type": "empty_pn_inactive", "error_detail": "private"},
    ])
    assert blocked["orders"][0]["line_ids"] == ["line-a"]
    assert blocked["orders"][0]["blocked_reasons"] == [
        "parse_error:bad_number", "parse_error:empty_pn_inactive",
    ]
    assert blocked["orders"][1]["blocked_reasons"] == []
    assert snapshot["orders"][0]["blocked_reasons"] == []
    assert "private" not in json.dumps(blocked)


def test_unlocatable_errors_block_file_and_address_conflict_does_not():
    snapshot = capture(frame(row()))
    unchanged = block_snapshot_errors(snapshot, [
        {"row_no": 999, "error_type": "maintenance_address_conflict"},
    ])
    assert unchanged == snapshot
    blocked = block_snapshot_errors(snapshot, [{"row_no": 999, "error_type": "missing_raw_id"}])
    assert blocked["blocked_reasons"] == ["parse_error:missing_raw_id"]
    assert daily_order_windows(blocked) == []


def test_daily_windows_do_not_bridge_old_correction_and_new_increment():
    snapshot = capture(frame(
        row("new-b", "WBDD-20260809-0007", "n7"),
        row("old-b", "WBDD-20260609-0003", "o3"),
        row("old-a", "WBDD-20260609-0001", "o1"),
        row("new-a", "WBDD-20260809-0005", "n5"),
    ))
    assert daily_order_windows(snapshot) == [
        {"date": "2026-06-09", "from_sequence": 1, "to_sequence": 3},
        {"date": "2026-08-09", "from_sequence": 5, "to_sequence": 7},
    ]
    assert capture_source_snapshot(frame(row()), sheet_name="one")["orders"][0]["line_ids"] == ["line-a"]
    assert daily_order_windows(capture(frame(row()))) == [
        {"date": "2026-06-09", "from_sequence": 1, "to_sequence": 1},
    ]


@pytest.mark.parametrize("number", [
    "WBDD-20260230-0001", "WBDD-20261301-0001", "WBDD-20260229-0001",
    "WBDD-20260609-001", "WBDD-20260609-00001", "WBDD-20260609-０００１",
    "XWBDD-20260609-0001", "WBDD-20260609-0001-extra", "wbdd-20260609-0001",
])
def test_invalid_order_numbers_do_not_supply_ranges(number):
    snapshot = capture(frame(row(number=number)))
    assert snapshot["orders"][0]["blocked_reasons"] == ["invalid_order_no"]
    assert daily_order_windows(snapshot) == []


def test_real_leap_day_is_accepted():
    assert daily_order_windows(capture(frame(row(number="WBDD-20240229-0010")))) == [
        {"date": "2024-02-29", "from_sequence": 10, "to_sequence": 10},
    ]


def test_same_number_under_distinct_raw_parents_blocks_both():
    snapshot = capture(frame(row(), row("head-b", child="line-b")))
    assert all(order["blocked_reasons"] == ["duplicate_order_number"] for order in snapshot["orders"])
    assert snapshot["line_ids"] == ["line-a", "line-b"]
    assert daily_order_windows(snapshot) == []


def test_one_raw_parent_with_conflicting_numbers_has_no_chosen_identity():
    snapshot = capture(frame(row(), row(number="WBDD-20260609-0002", child="line-b")))
    assert snapshot["orders"][0]["order_no"] is None
    assert snapshot["orders"][0]["blocked_reasons"] == ["order_number_conflict"]
    assert snapshot["orders"][0]["line_ids"] == ["line-a", "line-b"]


def test_same_child_under_distinct_parents_blocks_both_and_keeps_global_id():
    snapshot = capture(frame(row(), row("head-b", "WBDD-20260609-0002")))
    assert all(order["blocked_reasons"] == ["line_parent_conflict"] for order in snapshot["orders"])
    assert snapshot["line_ids"] == ["line-a"]


def test_orphan_child_blocks_file_but_stays_in_global_membership():
    snapshot = capture(frame(row(parent=None), row("head-b", "WBDD-20260609-0002", "line-b")))
    assert snapshot["blocked_reasons"] == ["missing_order_id"]
    assert snapshot["line_ids"] == ["line-a", "line-b"]
    assert daily_order_windows(snapshot) == []


def test_missing_number_blocks_parent_without_losing_its_child():
    snapshot = capture(frame(row(number=None)))
    assert snapshot["orders"][0]["blocked_reasons"] == ["missing_order_no"]
    assert snapshot["orders"][0]["line_ids"] == ["line-a"]


def test_head_only_blank_rows_and_missing_cells_are_serializable():
    snapshot = capture(frame(row(child=pd.NA), row(None, None, None)))
    assert snapshot["row_count"] == 2
    assert snapshot["orders"][0]["line_ids"] == []
    assert snapshot["orders"][0]["row_numbers"] == [1]
    assert snapshot["blocked_reasons"] == []
    json.dumps(snapshot, allow_nan=False)


def test_unprocessed_source_sheet_blocks_coverage_and_is_sorted():
    snapshot = capture(frame(row()), ignored_wbdd_sheets=["第二页", "备用页", "第二页"])
    assert snapshot["ignored_wbdd_sheets"] == ["备用页", "第二页"]
    assert snapshot["blocked_reasons"] == ["ignored_wbdd_sheets"]
    assert daily_order_windows(snapshot) == []


def test_missing_or_duplicate_identity_header_blocks_absence_inference():
    original = frame(row())
    missing = capture(original.drop(columns=["需求明细.数据ID(不可修改)"]))
    assert missing["blocked_reasons"] == ["missing_identity_columns"]
    assert daily_order_windows(missing) == []
    duplicate = capture(pd.concat([original, original[["需求单号"]]], axis=1))
    assert duplicate["blocked_reasons"] == ["duplicate_identity_columns"]
    assert daily_order_windows(duplicate) == []


def test_original_data_row_indices_match_transform_errors():
    snapshot = capture(frame(row(), row(child="line-b"), index=[4, 9]))
    assert snapshot["orders"][0]["row_numbers"] == [5, 10]
    blocked = block_snapshot_errors(snapshot, [{"row_no": 10, "error_type": "bad_date"}])
    assert blocked["orders"][0]["blocked_reasons"] == ["parse_error:bad_date"]


def test_snapshot_ordering_is_deterministic_and_ignores_detail_sequence():
    snapshot = capture(frame(
        row("z", "WBDD-20260609-0003", "z2", **{"需求明细.序号": 1}),
        row("a", "WBDD-20260609-0001", "a2", **{"需求明细.序号": 1}),
        row("a", "WBDD-20260609-0001", "a1", **{"需求明细.序号": 1}),
    ))
    assert [order["raw_order_id"] for order in snapshot["orders"]] == ["a", "z"]
    assert snapshot["orders"][0]["line_ids"] == ["a1", "a2"]
    assert capture(frame()).get("orders") == []
