from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.services import maintenance_bulk_import as bulk


class _ScalarsOnlyDb:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self, _statement):
        return list(self.rows)


class _ScalarSequenceDb:
    def __init__(self, *rows):
        self.rows = list(rows)

    def scalar(self, _statement):
        return self.rows.pop(0) if self.rows else None


def _sheet(field_indexes: dict[str, int], rows: list[tuple]) -> bulk.DetectedSheet:
    width = max(field_indexes.values()) + 1
    return bulk.DetectedSheet(
        name="Sheet1",
        header_row=2,
        header_rows=(1, 2),
        headers=tuple("" for _ in range(width)),
        system_headers=tuple("" for _ in range(width)),
        field_indexes=field_indexes,
        field_matches={},
        rows=tuple((row_no, tuple(values)) for row_no, values in rows),
    )


def test_sales_exact_machine_header_wins_duplicate_caption_at_row_20():
    workbook = Workbook()
    sheet = workbook.active
    systems = (
        "SeqNo",
        "F0000021",
        "F0000053",
        "Purchase.F0000099",
        "Sales.F0000054",
        "F0000055",
        "F0000056",
    )
    captions = (
        "订单编号(必填)",
        "订单金额",
        "是否含税(必填)",
        "税率(必填)",
        "税率(必填)",
        "税金",
        "不含税金额",
    )
    for column, value in enumerate(systems, start=1):
        sheet.cell(19, column, value)
    for column, value in enumerate(captions, start=1):
        sheet.cell(20, column, value)
    values = (
        "XSDD-20240708-0093",
        Decimal("31440000"),
        "含税",
        "6%",  # another document's identically captioned field
        "13%",
        Decimal("3616991.15"),
        Decimal("27823008.85"),
    )
    for column, value in enumerate(values, start=1):
        sheet.cell(21, column, value)
    stream = BytesIO()
    workbook.save(stream)

    adapter, detected = bulk._detect(stream.getvalue())

    assert adapter.key == "sales_contract_amount"
    assert detected.header_row == 20
    assert detected.field_indexes["tax_rate"] == 4
    inc, amount_ex, rate = adapter._amounts(detected, detected.rows[0][1])
    assert inc == Decimal("31440000.00")
    assert amount_ex == Decimal("27823008.85")
    assert rate == Decimal("0.130000")


def test_sales_amount_can_derive_missing_rate_from_ex_tax_and_tax():
    adapter = bulk.SalesContractAmountAdapter()
    detected = _sheet(
        {
            "order_amount": 0,
            "tax_flag": 1,
            "tax_rate": 2,
            "tax_amount": 3,
            "amount_ex_tax": 4,
        },
        [(3, ("31440000", "含税", "", "3616991.15", "27823008.85"))],
    )

    inc, amount_ex, rate = adapter._amounts(detected, detected.rows[0][1])

    assert inc == Decimal("31440000.00")
    assert amount_ex == Decimal("27823008.85")
    assert rate == Decimal("0.130000")


def test_sales_form_without_tax_rate_column_is_recognized_from_ex_and_tax():
    workbook = Workbook()
    sheet = workbook.active
    for column, value in enumerate(
        ("SeqNo", "F0000055", "F0000056"), start=1
    ):
        sheet.cell(1, column, value)
    for column, value in enumerate(
        ("订单编号(必填)", "税金", "不含税金额"), start=1
    ):
        sheet.cell(2, column, value)
    for column, value in enumerate(
        ("XSDD-1", "13", "100"), start=1
    ):
        sheet.cell(3, column, value)
    stream = BytesIO()
    workbook.save(stream)

    adapter, detected = bulk._detect(stream.getvalue())
    inc, amount_ex, rate = adapter._amounts(detected, detected.rows[0][1])

    assert adapter.key == "sales_contract_amount"
    assert inc == Decimal("113.00")
    assert amount_ex == Decimal("100.00")
    assert rate == Decimal("0.130000")


def test_sales_amount_never_treats_missing_rate_as_zero_for_lone_ex_tax():
    adapter = bulk.SalesContractAmountAdapter()
    detected = _sheet(
        {"amount_ex_tax": 0, "tax_rate": 1},
        [(3, ("100", ""))],
    )

    with pytest.raises(bulk.BulkImportInvalid, match="不能从单一未税金额"):
        adapter._amounts(detected, detected.rows[0][1])


def test_sales_conflicting_duplicate_blocks_entire_order(monkeypatch):
    contract = SimpleNamespace(
        project_id="project-1",
        project_contract_id="relation-1",
        amount_inc_tax=Decimal("10.00"),
        version=1,
    )
    monkeypatch.setattr(
        bulk,
        "_contract_maps",
        lambda _db: ({"20240101-0001": [contract]}, {"20240101-0001": contract}),
    )
    monkeypatch.setattr(bulk, "_all_contracts_by_order", lambda _db, _values: {})
    monkeypatch.setattr(bulk, "_assignment_evidence", lambda _db, _values: ({}, {}))
    detected = _sheet(
        {
            "order_no": 0,
            "order_amount": 1,
            "tax_flag": 2,
            "tax_rate": 3,
            "tax_amount": 4,
            "amount_ex_tax": 5,
        },
        [
            (3, ("XSDD-20240101-0001", "113", "含税", "13%", "13", "100")),
            (4, ("XSDD-20240101-0001", "226", "含税", "13%", "26", "200")),
        ],
    )

    plan = bulk.SalesContractAmountAdapter().build_plan(_ScalarsOnlyDb(), detected)

    assert [operation["action"] for operation in plan["operations"]] == ["blocked"]
    assert all(row["action"] == "error" for row in plan["rows"])
    assert any(
        issue["code"] == "order_level_fail_closed" for issue in plan["issues"]
    )


def test_receipt_risk_row_blocks_all_months_without_partial_cumulative(monkeypatch):
    contract = SimpleNamespace(
        project_id="project-1",
        project_contract_id="relation-1",
        contract_no="XSDD-20240101-0001",
        version=1,
    )
    monkeypatch.setattr(
        bulk,
        "_contract_maps",
        lambda _db: ({"20240101-0001": [contract]}, {"20240101-0001": contract}),
    )
    detected = _sheet(
        {"order_no": 0, "receipt_no": 1, "receipt_date": 2, "actual_amount": 3, "remark": 4},
        [
            (3, ("XSDD-20240101-0001", "SK-1", date(2026, 1, 10), "100", "")),
            (4, ("XSDD-20240101-0001", "SK-2", date(2026, 1, 20), "20", "坏账")),
            (5, ("XSDD-20240101-0001", "SK-3", date(2026, 2, 1), "50", "")),
        ],
    )

    plan = bulk.ReceiptCollectionAdapter().build_plan(_ScalarsOnlyDb(), detected)

    assert {operation["action"] for operation in plan["operations"]} == {"conflict"}
    assert {operation["report_month"] for operation in plan["operations"]} == {
        "2026-01-01",
        "2026-02-01",
    }
    assert all(
        operation["new_cumulative_amount"] is None
        for operation in plan["operations"]
    )
    assert not any(
        operation["action"] in {"create", "noop"}
        for operation in plan["operations"]
    )


def test_incomplete_receipt_history_blocks_every_month_for_contract(monkeypatch):
    contract = SimpleNamespace(
        project_id="project-1",
        project_contract_id="relation-1",
        contract_no="XSDD-20240101-0001",
        version=1,
    )
    existing = SimpleNamespace(
        project_contract_id="relation-1",
        report_month=date(2025, 12, 1),
        status="confirmed",
        cumulative_amount=Decimal("10.00"),
        collection_id="collection-1",
        version=1,
        receipt_reference="SK-OLD",
    )
    monkeypatch.setattr(
        bulk,
        "_contract_maps",
        lambda _db: ({"20240101-0001": [contract]}, {"20240101-0001": contract}),
    )
    detected = _sheet(
        {"order_no": 0, "receipt_no": 1, "receipt_date": 2, "actual_amount": 3},
        [
            (3, ("XSDD-20240101-0001", "SK-1", date(2026, 1, 10), "100")),
            (4, ("XSDD-20240101-0001", "SK-2", date(2026, 2, 10), "50")),
        ],
    )

    plan = bulk.ReceiptCollectionAdapter().build_plan(
        _ScalarsOnlyDb([existing]), detected
    )

    assert len(plan["operations"]) == 2
    assert all(operation["action"] == "conflict" for operation in plan["operations"])
    assert all(
        any(
            issue["code"] == "incomplete_receipt_history"
            for issue in operation["issues"]
        )
        for operation in plan["operations"]
    )


def _transfer_batch(*, status: str, selected: list[str]) -> tuple[str, str, str, object]:
    token = f"1.{('x' * 32)}"
    payload_hash = "a" * 64
    data_version = "version-1"
    selection_hash = bulk._canonical_hash(
        {"payload_hash": payload_hash, "row_keys": sorted(selected)}
    )
    report = {
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "payload_hash": payload_hash,
        "data_version": data_version,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "selected_row_keys": selected,
        "selection_hash": selection_hash,
        "result": {
            "status": "done",
            "rows": [
                {
                    "row_key": key,
                    "action": "update_contract",
                    "project_id": "project-1",
                }
                for key in selected
            ],
        },
    }
    batch = SimpleNamespace(
        id=1,
        file_type=bulk.TRANSFER_BATCH_TYPE,
        uploaded_by="operator",
        status=status,
        report_json=report,
    )
    return token, payload_hash, data_version, batch


def test_success_replay_rejects_different_row_keys():
    token, payload_hash, data_version, batch = _transfer_batch(
        status="success", selected=["row-1"]
    )

    with pytest.raises(bulk.BulkImportConflict, match="row_keys"):
        bulk.apply_transfer(
            _ScalarSequenceDb(batch),
            preview_token=token,
            payload_hash=payload_hash,
            data_version=data_version,
            row_keys=["row-2"],
            operated_by="operator",
        )


def test_success_replay_accepts_same_row_key_set_in_any_order():
    token, payload_hash, data_version, batch = _transfer_batch(
        status="success", selected=["row-1", "row-2"]
    )

    result = bulk.apply_transfer(
        _ScalarSequenceDb(batch),
        preview_token=token,
        payload_hash=payload_hash,
        data_version=data_version,
        row_keys=["row-2", "row-1"],
        operated_by="operator",
    )

    assert result["status"] == "done"


def test_success_replay_rechecks_current_project_scope():
    token, payload_hash, data_version, batch = _transfer_batch(
        status="success", selected=["row-1"]
    )

    with pytest.raises(bulk.BulkImportScopeDenied):
        bulk.apply_transfer(
            _ScalarSequenceDb(batch),
            preview_token=token,
            payload_hash=payload_hash,
            data_version=data_version,
            row_keys=["row-1"],
            operated_by="operator",
            allowed_project_ids={"project-2"},
        )


def test_processing_apply_rejects_out_of_scope_target_before_adapter_write():
    token, payload_hash, data_version, batch = _transfer_batch(
        status="processing", selected=[]
    )
    batch.report_json.update(
        {
            "row_map": {"row-1": {"plan_index": 0, "operation_index": 0}},
            "public": {
                "rows": [{"row_key": "row-1", "row_status": "ready"}]
            },
            "plans": [
                {
                    "form_type": "sales_contract_amount",
                    "plan": {
                        "operations": [
                            {
                                "action": "update_contract",
                                "project_id": "project-2",
                            }
                        ]
                    },
                }
            ],
        }
    )

    with pytest.raises(bulk.BulkImportScopeDenied):
        bulk.apply_transfer(
            _ScalarSequenceDb(batch),
            preview_token=token,
            payload_hash=payload_hash,
            data_version=data_version,
            row_keys=["row-1"],
            operated_by="operator",
            allowed_project_ids={"project-1"},
        )


def test_preview_rejects_out_of_scope_operation_before_persist(monkeypatch):
    artifact = bulk.PreviewArtifact(
        adapter_key="sales_contract_amount",
        file_type="maint_contract",
        file_hash="b" * 64,
        filename="sales.xlsx",
        plan={
            "operations": [
                {"action": "update_contract", "project_id": "project-2"}
            ],
            "summary": {"source_rows": 1},
        },
    )
    monkeypatch.setattr(bulk, "build_preview", lambda *_args: artifact)

    with pytest.raises(bulk.BulkImportScopeDenied):
        bulk.preview_transfer(
            object(),
            [("sales.xlsx", b"xlsx")],
            operated_by="operator",
            allowed_project_ids={"project-1"},
        )
