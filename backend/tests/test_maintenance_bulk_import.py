from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select, text

from app.business_time import business_today
from app.etl import loader, pipeline
from app.models.maintenance import (
    FMaintenanceOrder,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.models.sales import FSalesOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.services import maintenance_bulk_import as bulk
from app.services import maintenance_project_identity
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


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


def _ordinary_sales_workbook(
    tmp_path,
    *,
    order_no: str,
    raw_order_id: str,
    project_name: str,
    period_from: date | None = date(2026, 1, 1),
    period_to: date | None = date(2026, 12, 31),
    maintenance_business: str = "是",
    business_type: str = "备件维保",
    data_status: str = "已生效",
    include_status: bool = True,
    order_amount: str = "113",
    tax_rate: str = "13%",
    tax_amount: str = "13",
    amount_ex_tax: str = "100",
) -> str:
    workbook = Workbook()
    sheet = workbook.active
    system_headers = [
        "SeqNo", "ObjectId", "Status", "F0000118", "F0000059",
        "F0000119", "F0000131", "F0000132", "F0000021", "F0000053",
        "F0000054", "F0000055", "F0000056", "D0001F0000001",
        "D0001F0000002", "D0001F0000003", "D0001F0000004",
    ]
    captions = [
        "订单编号(必填)", "数据ID(不可修改)", "数据状态", "维保业务", "业务类型#",
        "项目名称(必填)", "维保起始日期(必填)", "维保终止日期(必填)", "订单金额", "是否含税(必填)",
        "税率(必填)", "税金", "不含税金额", "订单明细.数据ID(不可修改)",
        "订单明细.产品名称", "订单明细.订单数量", "订单明细.单价",
    ]
    values = [
        order_no, raw_order_id, data_status, maintenance_business, business_type, project_name,
        period_from, period_to, order_amount, "含税", tax_rate, tax_amount,
        amount_ex_tax, f"{raw_order_id}-line", "PN-AUTO-1", "1", order_amount,
    ]
    if not include_status:
        del system_headers[2]
        del captions[2]
        del values[2]
    sheet.append(system_headers)
    sheet.append(captions)
    sheet.append(values)
    path = tmp_path / f"{raw_order_id}-{abs(hash(project_name))}.xlsx"
    workbook.save(path)
    return str(path)


def _ordinary_wbdd_workbook(
    tmp_path,
    *,
    xsdd: str,
    raw_order_id: str,
    project_name: str,
) -> str:
    rows = make_rows(orders=1, lines_per_order=1, project=project_name)
    rows[0].update({
        "数据ID(不可修改)": raw_order_id,
        "需求单号": f"WBDD-{raw_order_id}",
        "销售订单": xsdd,
        "需求明细.数据ID(不可修改)": f"{raw_order_id}-line",
    })
    return write_workbook(
        str(tmp_path / f"{raw_order_id}.xlsx"), COLUMNS_91, rows
    )


def _seed_sales_authoritative_split(
    db,
    tmp_path,
    *,
    xsdd: str,
    canonical_primary_conflict: bool = False,
) -> dict:
    wbdd_by_project = {
        "canonical": [f"{xsdd}-canonical-1", f"{xsdd}-canonical-2"],
        "permissions": [f"{xsdd}-permissions-1"],
    }
    for owner, raw_order_ids in wbdd_by_project.items():
        for raw_order_id in raw_order_ids:
            path = _ordinary_wbdd_workbook(
                tmp_path,
                xsdd=f"XSDD-{xsdd}",
                raw_order_id=raw_order_id,
                project_name=f"历史 {owner} 容器",
            )
            pipeline.run_import(db, path, f"{raw_order_id}.xlsx", mode="upsert")

    projects = {
        owner: MaintenanceProject(
            project_id=f"sales-authority-{owner}",
            project_code=f"SALES-AUTHORITY-{owner.upper()}",
            display_name=f"销售权威归并 {owner}",
            lifecycle_status="ongoing",
            is_active=True,
            version=1,
        )
        for owner in ("canonical", "contract", "permissions")
    }
    db.add_all(projects.values())
    users = [
        SysUser(
            id=user_id,
            username=f"sales-authority-user-{user_id}",
            password_hash="not-a-login-secret",
            role="maintenance_manager" if user_id == 44 else "readonly",
            is_active=True,
        )
        for user_id in (44, 55, 98, 99)
    ]
    db.add_all(users)
    db.flush()

    selected_contract = MaintenanceProjectContract(
        project_contract_id="sales-authority-selected",
        project_id=projects["canonical"].project_id,
        contract_id=f"xsdd-XSDD-{xsdd}",
        contract_no=f"XSDD-{xsdd}",
        amount_inc_tax=Decimal("1670000.00"),
        contract_status="已生效",
        status_mapping_state="mapped",
        status_mapping_version="test-v1",
        included_in_total=True,
        effective_from=date(2025, 10, 16),
        source="sales_fallback",
        version=1,
    )
    superseded_contract = MaintenanceProjectContract(
        project_contract_id="sales-authority-superseded",
        project_id=projects["contract"].project_id,
        contract_id=f"xsdd-XSDD-{xsdd}",
        contract_no=f"XSDD-{xsdd}",
        amount_inc_tax=Decimal("1780283.02"),
        contract_status="已生效",
        status_mapping_state="mapped",
        status_mapping_version="test-v1",
        included_in_total=True,
        effective_from=date(2025, 10, 16),
        source="sales_fallback",
        version=1,
    )
    db.execute(text(
        "ALTER TABLE maintenance_project_contract DISABLE TRIGGER "
        "trg_maintenance_contract_claim_xsdd"
    ))
    db.execute(text(
        "ALTER TABLE maintenance_source_order_assignment DISABLE TRIGGER "
        "trg_maintenance_assignment_claim_xsdd"
    ))
    try:
        db.add_all([selected_contract, superseded_contract])
        assignments = []
        for owner, raw_order_ids in wbdd_by_project.items():
            for index, raw_order_id in enumerate(raw_order_ids):
                assignments.append(MaintenanceSourceOrderAssignment(
                    assignment_id=f"sales-authority-wbdd-{owner}-{index}",
                    source_order_id=raw_order_id,
                    project_id=projects[owner].project_id,
                    is_active=True,
                    version=1,
                    created_by="historical-fixture",
                ))
        db.add_all(assignments)
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_source_order_assignment ENABLE TRIGGER "
            "trg_maintenance_assignment_claim_xsdd"
        ))
        db.execute(text(
            "ALTER TABLE maintenance_project_contract ENABLE TRIGGER "
            "trg_maintenance_contract_claim_xsdd"
        ))

    collections = [
        MaintenanceCollectionSnapshot(
            collection_id=f"sales-authority-collection-{owner}",
            project_id=contract.project_id,
            project_contract_id=contract.project_contract_id,
            report_month=date(2026, 8, 1),
            cumulative_amount=Decimal("82325.40"),
            status="confirmed",
            receipt_reference="生产重复回款",
            remark="相同业务事实",
            source="legacy",
            version=1,
        )
        for owner, contract in (
            ("canonical", selected_contract),
            ("contract", superseded_contract),
        )
    ]
    db.add_all(collections)
    source_user_assignments = [
        MaintenanceProjectUserAssignment(
            assignment_id=f"sales-auth-user-{role}-{user_id}",
            project_id=projects["permissions"].project_id,
            responsibility_type=role,
            user_id=user_id,
            source_manager_text=f"历史账号 {user_id}",
            version=1,
            assigned_by="historical-fixture",
            assignment_reason="生产权限拓扑 fixture",
        )
        for role, user_id in (
            ("primary_manager", 44),
            ("viewer", 44),
            ("viewer", 55),
            ("viewer", 98),
        )
    ]
    db.add_all(source_user_assignments)
    canonical_user_assignment = None
    if canonical_primary_conflict:
        canonical_user_assignment = MaintenanceProjectUserAssignment(
            assignment_id="sales-authority-canonical-primary",
            project_id=projects["canonical"].project_id,
            responsibility_type="primary_manager",
            user_id=99,
            source_manager_text="canonical 原负责人",
            version=1,
            assigned_by="historical-fixture",
            assignment_reason="权限冲突 fixture",
        )
        db.add(canonical_user_assignment)
    db.commit()
    return {
        "projects": projects,
        "selected_contract": selected_contract,
        "superseded_contract": superseded_contract,
        "collections": collections,
        "source_user_assignments": source_user_assignments,
        "canonical_user_assignment": canonical_user_assignment,
        "wbdd_by_project": wbdd_by_project,
    }


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


def test_sales_amount_preserves_numeric_zero_values():
    adapter = bulk.SalesContractAmountAdapter()
    detected = _sheet(
        {
            "order_amount": 0,
            "tax_flag": 1,
            "tax_rate": 2,
            "tax_amount": 3,
            "amount_ex_tax": 4,
        },
        [(
            3,
            (
                Decimal("0.00"),
                "含税",
                Decimal("0"),
                Decimal("0.00"),
                Decimal("0.00"),
            ),
        )],
    )

    inc, amount_ex, rate = adapter._amounts(detected, detected.rows[0][1])

    assert inc == Decimal("0.00")
    assert amount_ex == Decimal("0.00")
    assert rate == Decimal("0")


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
            "data_status": 6,
        },
        [
            (3, ("XSDD-20240101-0001", "113", "含税", "13%", "13", "100", "已生效")),
            (4, ("XSDD-20240101-0001", "226", "含税", "13%", "26", "200", "已生效")),
        ],
    )

    plan = bulk.SalesContractAmountAdapter().build_plan(_ScalarsOnlyDb(), detected)

    assert [operation["action"] for operation in plan["operations"]] == ["blocked"]
    assert all(row["action"] == "error" for row in plan["rows"])
    assert any(
        issue["code"] == "order_level_fail_closed" for issue in plan["issues"]
    )


def test_sales_apply_prelocks_data_change_before_xsdd(monkeypatch):
    events: list[str] = []

    class _LockOnlyDb:
        def execute(self, _statement):
            events.append("data_change")

    monkeypatch.setattr(
        maintenance_project_identity,
        "lock_xsdd_identities",
        lambda _db, _values: events.append("xsdd") or [],
    )
    result = bulk.SalesContractAmountAdapter().apply_plan(
        _LockOnlyDb(),
        {"operations": []},
        operated_by="test",
        audit_reason="lock-order-test",
    )
    assert events == ["data_change", "xsdd"]
    assert result["written"] == 0


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


def test_ordinary_sales_upload_creates_one_xsdd_project_and_retains_peer_names(
    db, tmp_path
):
    order_no = "XSDD-20260902-0001"
    raw_order_id = "sales-auto-project-1"
    pre_delivery_name = "预交付-联通云服务器维保"
    formal_name = "联通云平台服务器维保集中项目"
    first_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=raw_order_id,
        project_name=pre_delivery_name,
    )

    first = pipeline.run_import(
        db,
        first_path,
        "sales-first.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )

    owner = db.get(MaintenanceProjectXsdd, "20260902-0001")
    assert owner is not None
    project = db.get(MaintenanceProject, owner.project_id)
    assert project is not None
    assert project.display_name == pre_delivery_name
    assert project.period_from == date(2026, 1, 1)
    assert project.period_to == date(2026, 12, 31)
    assert first.report_json["maintenance_sales_project_sync"]["status"] == "applied"
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project.project_id
    ))
    assert contract is not None
    assert contract.contract_no == order_no
    assert contract.amount_inc_tax == Decimal("113.00")

    second_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=raw_order_id,
        project_name=formal_name,
        period_from=None,
        period_to=date(2027, 12, 31),
    )
    second = pipeline.run_import(
        db,
        second_path,
        "sales-formal.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )

    assert second.report_json["maintenance_sales_project_sync"]["noop"] == 1
    assert project.period_from is None
    assert project.period_to == date(2027, 12, 31)
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 1
    aliases = set(db.scalars(select(MaintenanceProjectAlias.alias_name).where(
        MaintenanceProjectAlias.project_id == project.project_id
    )))
    assert {pre_delivery_name, formal_name} <= aliases
    peers = maintenance_project_identity.peer_names_by_project(
        db, [project.project_id]
    )
    assert {pre_delivery_name, formal_name} <= set(peers[project.project_id])

    project_version = project.version
    contract_version = contract.version
    alias_count = db.scalar(select(func.count()).select_from(MaintenanceProjectAlias))
    with open(second_path, "rb") as workbook_file:
        replay = bulk.sync_uploaded_sales_workbook(
            db,
            workbook_file.read(),
            "sales-formal-replay.xlsx",
            operated_by="sales-importer",
            import_batch_id=second.id,
        )
    assert replay["noop"] == 1
    assert project.version == project_version
    assert contract.version == contract_version
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectAlias)) == alias_count

    # Name equality alone never merges or blocks different XSDDs.  Only the
    # XSDD owner relation decides identity.
    other_xsdd_path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0003",
        raw_order_id="sales-auto-project-3",
        project_name=formal_name,
    )
    pipeline.run_import(
        db,
        other_xsdd_path,
        "sales-same-name-other-xsdd.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    other_owner = db.get(MaintenanceProjectXsdd, "20260902-0003")
    assert other_owner is not None and other_owner.project_id != project.project_id
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 2


def test_sales_upload_auto_merges_contract_wbdd_split_without_map(db, tmp_path):
    xsdd = "20260902-0030"
    wbdd_id = "sales-auto-merge-wbdd"
    wbdd_path = _ordinary_wbdd_workbook(
        tmp_path,
        xsdd=f"XSDD-{xsdd}",
        raw_order_id=wbdd_id,
        project_name="历史 WBDD 容器",
    )
    pipeline.run_import(db, wbdd_path, "split-wbdd.xlsx", mode="upsert")

    contract_owner = MaintenanceProject(
        project_id="sales-auto-merge-contract-owner",
        project_code="SALES-AUTO-MERGE-CONTRACT",
        display_name="销售合同容器",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    wbdd_owner = MaintenanceProject(
        project_id="sales-auto-merge-wbdd-owner",
        project_code="SALES-AUTO-MERGE-WBDD",
        display_name="历史 WBDD 容器",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add_all([contract_owner, wbdd_owner])
    db.flush()
    db.execute(text(
        "ALTER TABLE maintenance_project_contract DISABLE TRIGGER "
        "trg_maintenance_contract_claim_xsdd"
    ))
    db.execute(text(
        "ALTER TABLE maintenance_source_order_assignment DISABLE TRIGGER "
        "trg_maintenance_assignment_claim_xsdd"
    ))
    try:
        db.add(MaintenanceProjectContract(
            project_contract_id="sales-auto-merge-contract",
            project_id=contract_owner.project_id,
            contract_id="sales-auto-merge-contract-id",
            contract_no=f"XSDD-{xsdd}",
            amount_inc_tax=Decimal("113.00"),
            contract_status="已生效",
            status_mapping_state="mapped",
            status_mapping_version="test-v1",
            included_in_total=True,
            effective_from=date(2026, 1, 1),
            source="historical-fixture",
            version=1,
        ))
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id="sales-auto-merge-assignment",
            source_order_id=wbdd_id,
            project_id=wbdd_owner.project_id,
            is_active=True,
            version=1,
            created_by="historical-fixture",
        ))
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_source_order_assignment ENABLE TRIGGER "
            "trg_maintenance_assignment_claim_xsdd"
        ))
        db.execute(text(
            "ALTER TABLE maintenance_project_contract ENABLE TRIGGER "
            "trg_maintenance_contract_claim_xsdd"
        ))
    db.commit()
    assert db.get(MaintenanceProjectXsdd, xsdd) is None

    sales_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=f"XSDD-{xsdd}",
        raw_order_id="sales-auto-merge-upload",
        project_name="销售合同正式名称",
    )
    batch = pipeline.run_import(
        db,
        sales_path,
        "sales-auto-merge.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    db.commit()

    mapping = db.get(MaintenanceProjectXsdd, xsdd)
    assert mapping is not None
    assert mapping.project_id == contract_owner.project_id
    assert db.get(MaintenanceProject, wbdd_owner.project_id).is_active is False
    current_assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == wbdd_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assert current_assignment is not None
    assert current_assignment.project_id == contract_owner.project_id
    assert batch.report_json["maintenance_sales_project_sync"]["noop"] == 1


def test_sales_upload_uses_unique_gross_to_merge_production_split(db, tmp_path):
    xsdd = "20251017-0036"
    fixture = _seed_sales_authoritative_split(db, tmp_path, xsdd=xsdd)
    projects = fixture["projects"]
    selected = fixture["selected_contract"]
    superseded = fixture["superseded_contract"]

    preview = maintenance_project_identity.preview_historical_conflicts(db)
    conflict = next(
        row for row in preview["conflicts"] if row["xsdd_norm"] == xsdd
    )
    assert conflict["canonical_project_id"] == projects["canonical"].project_id
    assert conflict["contract_owner_project_ids"] == sorted([
        projects["canonical"].project_id,
        projects["contract"].project_id,
    ])
    assert len(conflict["exact_duplicate_candidates"]["collections"]) == 1

    sales_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=f"XSDD-{xsdd}",
        raw_order_id="sales-authority-incoming",
        project_name="腾讯 TCE 2025 维保项目",
        period_from=date(2025, 10, 16),
        period_to=date(2026, 10, 15),
        order_amount="1670000.00",
        tax_rate="6%",
        tax_amount="94528.30",
        amount_ex_tax="1575471.70",
    )
    batch = pipeline.run_import(
        db,
        sales_path,
        "sales-authority-production-shape.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    db.commit()

    canonical_id = projects["canonical"].project_id
    assert db.get(MaintenanceProjectXsdd, xsdd).project_id == canonical_id
    assert db.get(MaintenanceProject, projects["contract"].project_id).is_active is False
    assert db.get(
        MaintenanceProject, projects["permissions"].project_id
    ).is_active is False
    assert db.get(
        MaintenanceProjectContract, selected.project_contract_id
    ).effective_to is None
    archived_contract = db.get(
        MaintenanceProjectContract, superseded.project_contract_id
    )
    assert archived_contract.project_id == canonical_id
    assert archived_contract.included_in_total is False
    assert archived_contract.effective_to == business_today()

    collections = list(db.scalars(
        select(MaintenanceCollectionSnapshot).order_by(
            MaintenanceCollectionSnapshot.collection_id
        )
    ))
    assert len(collections) == 1
    assert collections[0].project_id == canonical_id
    assert collections[0].cumulative_amount == Decimal("82325.40")

    current_users = list(db.scalars(
        select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.project_id == canonical_id,
            MaintenanceProjectUserAssignment.archived_at.is_(None),
        )
    ))
    assert {
        (assignment.responsibility_type, assignment.user_id)
        for assignment in current_users
    } == {
        ("primary_manager", 44),
        ("viewer", 44),
        ("viewer", 55),
        ("viewer", 98),
    }
    assert all(
        assignment.archived_at is not None
        for assignment in fixture["source_user_assignments"]
    )
    for raw_order_ids in fixture["wbdd_by_project"].values():
        for raw_order_id in raw_order_ids:
            current_assignment = db.scalar(
                select(MaintenanceSourceOrderAssignment).where(
                    MaintenanceSourceOrderAssignment.source_order_id
                    == raw_order_id,
                    MaintenanceSourceOrderAssignment.is_active.is_(True),
                )
            )
            assert current_assignment.project_id == canonical_id
    assert batch.report_json["maintenance_sales_project_sync"]["noop"] == 1


def test_sales_prelock_claims_missing_map_for_unique_contract_owner(db):
    xsdd = "20251230-0027"
    project = MaintenanceProject(
        project_id="sales-missing-map-owner",
        project_code="SALES-MISSING-MAP-OWNER",
        display_name="已有唯一销售合同项目",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add(project)
    db.flush()
    db.execute(text(
        "ALTER TABLE maintenance_project_contract DISABLE TRIGGER "
        "trg_maintenance_contract_claim_xsdd"
    ))
    try:
        db.add(MaintenanceProjectContract(
            project_contract_id="sales-missing-map-contract",
            project_id=project.project_id,
            contract_id="sales-missing-map-contract-id",
            contract_no=f"XSDD-{xsdd}",
            amount_inc_tax=Decimal("113.00"),
            contract_status="已生效",
            status_mapping_state="mapped",
            status_mapping_version="test-v1",
            included_in_total=True,
            effective_from=date(2025, 12, 30),
            source="historical-fixture",
            version=1,
        ))
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_project_contract ENABLE TRIGGER "
            "trg_maintenance_contract_claim_xsdd"
        ))
    db.commit()
    assert db.get(MaintenanceProjectXsdd, xsdd) is None

    result = maintenance_project_identity.auto_merge_sales_xsdd_conflicts(
        db,
        incoming_amount_inc_tax_by_xsdd={xsdd: Decimal("113.00")},
        operated_by="sales-importer",
    )
    db.commit()

    assert result["merged_group_count"] == 0
    assert db.get(MaintenanceProjectXsdd, xsdd).project_id == project.project_id


def test_sales_upload_keeps_multiple_contract_owners_fail_closed(db, tmp_path):
    xsdd = "20260902-0031"
    projects = [
        MaintenanceProject(
            project_id=f"sales-multi-owner-{suffix}",
            project_code=f"SALES-MULTI-OWNER-{suffix.upper()}",
            display_name=f"多合同容器 {suffix}",
            lifecycle_status="ongoing",
            is_active=True,
            version=1,
        )
        for suffix in ("a", "b")
    ]
    db.add_all(projects)
    db.flush()
    db.execute(text(
        "ALTER TABLE maintenance_project_contract DISABLE TRIGGER "
        "trg_maintenance_contract_claim_xsdd"
    ))
    try:
        db.add_all([
            MaintenanceProjectContract(
                project_contract_id=f"sales-multi-contract-{index}",
                project_id=project.project_id,
                contract_id=f"sales-multi-contract-id-{index}",
                contract_no=f"XSDD-{xsdd}",
                amount_inc_tax=Decimal("113.00"),
                contract_status="已生效",
                status_mapping_state="mapped",
                status_mapping_version="test-v1",
                included_in_total=True,
                effective_from=date(2026, 1, 1),
                source="historical-fixture",
                version=1,
            )
            for index, project in enumerate(projects)
        ])
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_project_contract ENABLE TRIGGER "
            "trg_maintenance_contract_claim_xsdd"
        ))
    db.commit()

    sales_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=f"XSDD-{xsdd}",
        raw_order_id="sales-multi-owner-upload",
        project_name="多合同销售上传",
    )
    with pytest.raises(loader.ImportIntegrityError, match="唯一销售合同 owner"):
        pipeline.run_import(
            db,
            sales_path,
            "sales-multi-owner.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()

    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.get(MaintenanceProjectXsdd, xsdd) is None
    assert all(db.get(MaintenanceProject, row.project_id).is_active for row in projects)


def test_sales_upload_keeps_permission_collision_fail_closed(db, tmp_path):
    xsdd = "20251017-0037"
    fixture = _seed_sales_authoritative_split(
        db,
        tmp_path,
        xsdd=xsdd,
        canonical_primary_conflict=True,
    )
    sales_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=f"XSDD-{xsdd}",
        raw_order_id="sales-authority-permission-conflict",
        project_name="权限冲突不得覆盖",
        order_amount="1670000.00",
        tax_rate="6%",
        tax_amount="94528.30",
        amount_ex_tax="1575471.70",
    )

    with pytest.raises(loader.ImportIntegrityError, match="用户关系方案.*唯一键冲突"):
        pipeline.run_import(
            db,
            sales_path,
            "sales-authority-permission-conflict.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()

    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.get(MaintenanceProjectXsdd, xsdd) is None
    assert all(
        db.get(MaintenanceProject, project.project_id).is_active
        for project in fixture["projects"].values()
    )
    assert all(
        assignment.archived_at is None
        for assignment in fixture["source_user_assignments"]
    )
    assert fixture["canonical_user_assignment"].archived_at is None
    assert fixture["superseded_contract"].effective_to is None
    assert fixture["superseded_contract"].included_in_total is True


def test_wbdd_first_defers_then_sales_links_and_sales_first_links_directly(
    db, tmp_path
):
    first_xsdd = "XSDD-20260902-0010"
    first_wbdd_id = "wbdd-first-order"
    wbdd_first = _ordinary_wbdd_workbook(
        tmp_path,
        xsdd=first_xsdd,
        raw_order_id=first_wbdd_id,
        project_name="WBDD 名称不能建项目",
    )
    wbdd_batch = pipeline.run_import(
        db,
        wbdd_first,
        "wbdd-first.xlsx",
        uploaded_by="maintenance-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    db.commit()

    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 0
    assert db.scalar(select(func.count()).select_from(
        MaintenanceSourceOrderAssignment
    )) == 0
    assert first_wbdd_id in wbdd_batch.report_json["auto_assignment"][
        "pending_owner_order_ids"
    ]

    first_sales = _ordinary_sales_workbook(
        tmp_path,
        order_no=first_xsdd,
        raw_order_id="sales-after-wbdd",
        project_name="销售事实建立的项目",
    )
    pipeline.run_import(
        db,
        first_sales,
        "sales-after-wbdd.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    db.commit()

    first_owner = db.get(MaintenanceProjectXsdd, "20260902-0010")
    first_assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == first_wbdd_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assert first_owner is not None
    assert first_assignment is not None
    assert first_assignment.project_id == first_owner.project_id

    second_xsdd = "XSDD-20260902-0011"
    second_sales = _ordinary_sales_workbook(
        tmp_path,
        order_no=second_xsdd,
        raw_order_id="sales-before-wbdd",
        project_name="先销售后需求项目",
    )
    pipeline.run_import(
        db,
        second_sales,
        "sales-before-wbdd.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    db.commit()
    second_owner = db.get(MaintenanceProjectXsdd, "20260902-0011")
    assert second_owner is not None

    second_wbdd_id = "sales-first-wbdd-order"
    sales_first_wbdd = _ordinary_wbdd_workbook(
        tmp_path,
        xsdd=second_xsdd,
        raw_order_id=second_wbdd_id,
        project_name="完全不同的 WBDD 名称",
    )
    pipeline.run_import(
        db,
        sales_first_wbdd,
        "sales-first-wbdd.xlsx",
        uploaded_by="maintenance-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    db.commit()
    second_assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == second_wbdd_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assert second_assignment is not None
    assert second_assignment.project_id == second_owner.project_id


@pytest.mark.parametrize("discard_mode", ["tombstone", "inactive"])
def test_discarded_wbdd_assignment_does_not_claim_sales_owner_or_relink(
    db, tmp_path, discard_mode
):
    xsdd = "XSDD-20260902-0020"
    wbdd_id = f"discarded-wbdd-{discard_mode}"
    wbdd_path = _ordinary_wbdd_workbook(
        tmp_path,
        xsdd=xsdd,
        raw_order_id=wbdd_id,
        project_name="已废弃历史 WBDD 项目",
    )
    pipeline.run_import(
        db,
        wbdd_path,
        f"{discard_mode}-wbdd.xlsx",
        uploaded_by="fixture",
        mode="upsert",
    )
    legacy = MaintenanceProject(
        project_id=f"discarded-legacy-{discard_mode}",
        project_code=f"DISCARDED-{discard_mode.upper()}",
        display_name="已废弃历史容器",
        lifecycle_status="ended",
        is_active=True,
        version=1,
    )
    db.add(legacy)
    db.flush()
    db.execute(text(
        "ALTER TABLE maintenance_source_order_assignment DISABLE TRIGGER "
        "trg_maintenance_assignment_claim_xsdd"
    ))
    try:
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=f"discarded-assignment-{discard_mode}",
            source_order_id=wbdd_id,
            project_id=legacy.project_id,
            is_active=True,
            version=1,
            created_by="historical-fixture",
        ))
        db.flush()
    finally:
        db.execute(text(
            "ALTER TABLE maintenance_source_order_assignment ENABLE TRIGGER "
            "trg_maintenance_assignment_claim_xsdd"
        ))
    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == wbdd_id
    ))
    if discard_mode == "inactive":
        order.data_status = "已作废"
    else:
        now = datetime.now(timezone.utc)
        intent = MaintenanceDemandDeleteIntent(
            intent_id="discarded-intent-tombstone",
            idempotency_key="discarded-intent-key",
            request_digest="a" * 64,
            selection_digest="b" * 64,
            status="executed",
            reason="测试销售 owner 忽略墓碑",
            operated_by="test",
            header_count=1,
            line_count=1,
            created_at=now,
            expires_at=now,
        )
        db.add(intent)
        db.flush()
        db.add(MaintenanceDemandTombstone(
            source_order_id=wbdd_id,
            delete_intent_id=intent.intent_id,
            version_digest="c" * 64,
            deleted_by="test",
            delete_reason="测试墓碑",
            deleted_at=now,
            version=1,
        ))
    db.commit()

    sales_path = _ordinary_sales_workbook(
        tmp_path,
        order_no=xsdd,
        raw_order_id=f"sales-after-discard-{discard_mode}",
        project_name="销售权威新项目",
    )
    pipeline.run_import(
        db,
        sales_path,
        f"sales-after-{discard_mode}.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    owner = db.get(MaintenanceProjectXsdd, "20260902-0020")
    assert owner is not None and owner.project_id != legacy.project_id
    active_assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == wbdd_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    ))
    assert active_assignment is not None
    assert active_assignment.project_id == legacy.project_id


@pytest.mark.parametrize(
    ("maintenance_business", "business_type"),
    [("否", "单次维修"), ("是", "备件销售")],
)
def test_ordinary_sales_accepts_either_maintenance_classifier(
    db, tmp_path, maintenance_business, business_type
):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0012",
        raw_order_id=f"sales-marker-or-{maintenance_business}",
        project_name="生产双分类维保项目",
        maintenance_business=maintenance_business,
        business_type=business_type,
    )
    batch = pipeline.run_import(
        db,
        path,
        "sales-marker-or.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    assert batch.report_json["maintenance_sales_project_sync"]["status"] == "applied"
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 1


def test_ordinary_sales_rejects_unrecognized_maintenance_flag(db, tmp_path):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0016",
        raw_order_id="sales-marker-unknown",
        project_name="未知标记项目",
        maintenance_business="待确认",
        business_type="备件销售",
    )
    with pytest.raises(loader.ImportIntegrityError, match="预锁失败"):
        pipeline.run_import(
            db,
            path,
            "sales-marker-unknown.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0


def test_ordinary_sales_requires_active_status_and_strict_xsdd(db, tmp_path):
    missing_status = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0013",
        raw_order_id="sales-missing-status",
        project_name="缺状态项目",
        include_status=False,
    )
    with pytest.raises(loader.ImportIntegrityError, match="预锁失败"):
        pipeline.run_import(
            db,
            missing_status,
            "sales-missing-status.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()

    invalid_xsdd = _ordinary_sales_workbook(
        tmp_path,
        order_no="SALE-INVALID-001",
        raw_order_id="sales-invalid-xsdd",
        project_name="非法销售单号项目",
    )
    with pytest.raises(loader.ImportIntegrityError, match="预锁失败"):
        pipeline.run_import(
            db,
            invalid_xsdd,
            "sales-invalid-xsdd.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0


@pytest.mark.parametrize(
    ("order_no", "include_status"),
    [
        ("SALE-INVALID-001", True),
        ("XSDD-20260902-19", True),
        ("XSDD-20260902-00019", True),
        ("XSDD-20260902-0019", False),
    ],
)
def test_dedicated_sales_preview_requires_strict_xsdd_and_status(
    db, tmp_path, order_no, include_status
):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=f"dedicated-strict-{include_status}",
        project_name="专用导入严格门禁项目",
        include_status=include_status,
    )
    with open(path, "rb") as workbook_file:
        artifact = bulk.build_preview(
            db, workbook_file.read(), "dedicated-strict.xlsx"
        )
    assert artifact.adapter_key == "sales_contract_amount"
    assert artifact.plan["summary"]["blocking_errors"] >= 1
    assert not [
        row for row in artifact.plan["operations"]
        if row.get("action") not in {"blocked", "error"}
    ]
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 0


def test_ordinary_sales_keeps_three_and_four_digit_xsdds_distinct(db, tmp_path):
    for suffix in ("044", "0044"):
        path = _ordinary_sales_workbook(
            tmp_path,
            order_no=f"XSDD-20260902-{suffix}",
            raw_order_id=f"sales-suffix-{suffix}",
            project_name="相同名称但不同销售订单",
        )
        pipeline.run_import(
            db,
            path,
            f"sales-suffix-{suffix}.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )

    assert set(db.scalars(select(MaintenanceProjectXsdd.xsdd_norm))) == {
        "20260902-044",
        "20260902-0044",
    }
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 2
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 2


def test_inactive_maintenance_sales_does_not_create_project(db, tmp_path):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0015",
        raw_order_id="sales-inactive-maintenance",
        project_name="未生效维保项目",
        data_status="已作废",
    )
    batch = pipeline.run_import(
        db,
        path,
        "sales-inactive-maintenance.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    assert batch.report_json["maintenance_sales_project_sync"]["status"] \
        == "no_maintenance_rows"
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0


def test_ordinary_sales_reuses_generic_transform_without_bulk_redetect(
    db, tmp_path, monkeypatch
):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0014",
        raw_order_id="sales-no-redetect",
        project_name="不二次展开 XLSX",
    )
    monkeypatch.setattr(
        bulk,
        "_detect",
        lambda _data: (_ for _ in ()).throw(AssertionError("must not redetect")),
    )
    pipeline.run_import(
        db,
        path,
        "sales-no-redetect.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 1


def test_ordinary_sales_auto_project_allows_missing_period(db, tmp_path):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0002",
        raw_order_id="sales-auto-project-invalid",
        project_name="缺期限的维保项目",
        period_from=None,
        period_to=None,
    )

    pipeline.run_import(
        db,
        path,
        "sales-missing-period.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    project = db.scalar(select(MaintenanceProject))
    assert project is not None
    assert project.period_from is None and project.period_to is None
    assert project.version == 1
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 1
    assert db.get(MaintenanceProjectXsdd, "20260902-0002") is not None


def test_owner_backed_maintenance_sales_still_requires_project_name(db, tmp_path):
    project = MaintenanceProject(
        project_id="sales-owner-blank-name-project",
        project_code="XSDD-20260902-0021",
        display_name="既有 XSDD owner 项目",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceProjectXsdd(
        xsdd_norm="20260902-0021",
        project_id=project.project_id,
        source="test-map-only-owner",
    ))
    db.commit()

    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0021",
        raw_order_id="sales-owner-blank-name",
        project_name="",
    )
    with pytest.raises(
        loader.ImportIntegrityError,
        match="维保销售订单自动建项失败",
    ):
        pipeline.run_import(
            db,
            path,
            "sales-owner-blank-name.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()

    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 0


@pytest.mark.parametrize(
    ("period_from", "period_to"),
    [(date(2026, 1, 1), None), (None, date(2026, 12, 31))],
)
def test_ordinary_sales_auto_project_preserves_one_sided_period(
    db, tmp_path, period_from, period_to
):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0017",
        raw_order_id="sales-one-sided-period",
        project_name="单侧期限维保项目",
        period_from=period_from,
        period_to=period_to,
    )
    pipeline.run_import(
        db,
        path,
        "sales-one-sided-period.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    project = db.scalar(select(MaintenanceProject))
    assert project is not None
    assert project.period_from == period_from
    assert project.period_to == period_to


def test_ordinary_sales_inverted_period_rolls_back_all_facts(db, tmp_path):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0018",
        raw_order_id="sales-inverted-period",
        project_name="倒置期限维保项目",
        period_from=date(2027, 1, 1),
        period_to=date(2026, 1, 1),
    )
    with pytest.raises(loader.ImportIntegrityError, match="自动建项失败"):
        pipeline.run_import(
            db,
            path,
            "sales-inverted-period.xlsx",
            uploaded_by="sales-importer",
            mode="upsert",
            auto_assign_maintenance_projects=True,
        )
    db.rollback()
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 0


def test_existing_sales_owner_preserves_period_when_source_period_is_inverted(
    db, tmp_path
):
    order_no = "XSDD-20260902-0019"
    raw_order_id = "sales-inverted-existing-owner"
    original = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=raw_order_id,
        project_name="已有期限维保项目",
        period_from=date(2026, 1, 1),
        period_to=date(2026, 12, 31),
    )
    pipeline.run_import(
        db,
        original,
        "sales-valid-period.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )

    inverted = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=raw_order_id,
        project_name="已有期限维保项目",
        period_from=date(2027, 1, 1),
        period_to=date(2026, 1, 1),
        order_amount="226",
        tax_amount="26",
        amount_ex_tax="200",
    )
    pipeline.run_import(
        db,
        inverted,
        "sales-inverted-existing-period.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )

    owner = db.get(MaintenanceProjectXsdd, "20260902-0019")
    project = db.get(MaintenanceProject, owner.project_id)
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == project.project_id
    ))
    assert project.period_from == date(2026, 1, 1)
    assert project.period_to == date(2026, 12, 31)
    assert contract.amount_inc_tax == Decimal("226.00")


def test_ordinary_non_maintenance_sales_upload_never_creates_project(db, tmp_path):
    path = _ordinary_sales_workbook(
        tmp_path,
        order_no="XSDD-20260902-0004",
        raw_order_id="ordinary-hardware-sale",
        project_name="普通备件销售",
        maintenance_business="否",
        business_type="备件销售",
    )

    batch = pipeline.run_import(
        db,
        path,
        "ordinary-hardware-sale.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )

    assert batch.report_json["maintenance_sales_project_sync"]["status"] == "no_maintenance_rows"
    assert db.scalar(select(func.count()).select_from(FSalesOrder)) == 1
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectXsdd)) == 0


def test_skip_mode_existing_sales_fact_does_not_trigger_project_overwrite(db, tmp_path):
    order_no = "XSDD-20260902-0005"
    raw_order_id = "sales-skip-existing"
    original = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=raw_order_id,
        project_name="普通销售原始事实",
        maintenance_business="否",
        business_type="备件销售",
    )
    pipeline.run_import(
        db,
        original,
        "sales-skip-original.xlsx",
        uploaded_by="sales-importer",
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )

    maintenance_revision = _ordinary_sales_workbook(
        tmp_path,
        order_no=order_no,
        raw_order_id=raw_order_id,
        project_name="不应在 skip 模式生效的维保项目",
        maintenance_business="否",
        business_type="单次维修",
    )
    skipped = pipeline.run_import(
        db,
        maintenance_revision,
        "sales-skip-maintenance.xlsx",
        uploaded_by="sales-importer",
        mode="skip",
        auto_assign_maintenance_projects=True,
    )

    assert skipped.report_json["maintenance_sales_project_sync"]["status"] == "no_maintenance_rows"
    sales = db.scalar(select(FSalesOrder).where(FSalesOrder.raw_order_id == raw_order_id))
    assert sales is not None and sales.business_type == "备件销售"
    assert db.scalar(select(func.count()).select_from(MaintenanceProject)) == 0
    assert db.scalar(select(func.count()).select_from(MaintenanceProjectContract)) == 0
