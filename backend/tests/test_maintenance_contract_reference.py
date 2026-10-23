"""合同参考模块的兼容性边界；这些断言描述当前实现，不重新裁定业务口径。"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.sales import FSalesOrder
from app.models.system import SysImportBatch
from app.services import maintenance_contract_reference as reference


TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def _business_date(monkeypatch):
    monkeypatch.setattr(reference, "business_today", lambda: TODAY)


def _project(db) -> str:
    project_id = str(uuid.uuid4())
    db.add(MaintenanceProject(
        project_id=project_id,
        project_code=project_id,
        display_name=f"合同参考-{project_id}",
        lifecycle_status="ongoing",
    ))
    db.flush()
    return project_id


def _contract(
    db,
    project_id: str,
    *,
    contract_no: str = "XSDD-LEDGER",
    contract_id: str | None = None,
    amount: str | None = "100.00",
    included: bool = True,
    mapping_state: str = "mapped",
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> None:
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()),
        project_id=project_id,
        contract_id=contract_id or str(uuid.uuid4()),
        contract_no=contract_no,
        amount_inc_tax=Decimal(amount) if amount is not None else None,
        status_mapping_state=mapping_state,
        status_mapping_version="contract-reference-test",
        included_in_total=included,
        effective_from=effective_from,
        effective_to=effective_to,
        source="contract-reference-test",
    ))
    db.flush()


def _fallback(db, projects_and_orders: list[tuple[str, str]], *, amount="100.00"):
    maintenance_batch = SysImportBatch(
        filename="contract-reference-maintenance.xlsx",
        file_type="maintenance",
        file_hash=uuid.uuid4().hex * 2,
        status="success",
    )
    sales_batch = SysImportBatch(
        filename="contract-reference-sales.xlsx",
        file_type="sales",
        file_hash=uuid.uuid4().hex * 2,
        status="success",
    )
    db.add_all([maintenance_batch, sales_batch])
    db.flush()
    for project_id, order_no in projects_and_orders:
        source_order_id = str(uuid.uuid4())
        db.add(FMaintenanceOrder(
            raw_order_id=source_order_id,
            order_no=f"WBDD-{source_order_id}",
            order_date=TODAY,
            linked_sales_order_no=order_no,
            data_status="已生效",
            import_batch_id=maintenance_batch.id,
        ))
        db.flush()
        db.add(MaintenanceSourceOrderAssignment(
            assignment_id=str(uuid.uuid4()),
            source_order_id=source_order_id,
            project_id=project_id,
            is_active=True,
            created_by="contract-reference-test",
        ))
    for order_no in sorted({order_no for _, order_no in projects_and_orders}):
        db.add(FSalesOrder(
            raw_order_id=str(uuid.uuid4()),
            order_no=order_no,
            amount_ex_tax=Decimal(amount),
            tax_rate=Decimal("0.13"),
            data_status="已生效",
            import_batch_id=sales_batch.id,
        ))
    db.flush()


@pytest.mark.parametrize("shared_key", ["stable_id", "contract_no"])
def test_ledger_detects_shared_owner_outside_requested_page(db, shared_key):
    requested, off_page = _project(db), _project(db)
    _contract(db, requested, contract_id="stable-a", contract_no="XSDD-A")
    _contract(
        db,
        off_page,
        contract_id="stable-a" if shared_key == "stable_id" else "stable-b",
        contract_no="XSDD-A" if shared_key == "contract_no" else "XSDD-B",
    )

    result = reference.current_contract_references(db, [requested])

    assert result == {requested: {
        "contract_nos": ["XSDD-A"],
        "amount_inc_tax": None,
        "contract_shared": True,
        "contract_incomplete": True,
    }}


def test_fallback_detects_shared_owner_outside_requested_page(db):
    requested, off_page = _project(db), _project(db)
    _fallback(db, [(requested, "XSDD-SHARED"), (off_page, "XSDD-SHARED")])

    result = reference.current_contract_references(db, [requested])

    # Raw fallback retains a diagnostic subtotal; each consumer applies its mask.
    assert result == {requested: {
        "contract_nos": ["XSDD-SHARED"],
        "amount_inc_tax": Decimal("113.00"),
        "contract_shared": True,
        "contract_incomplete": True,
    }}


@pytest.mark.parametrize("amount,included,mapping_state", [
    (None, True, "mapped"),
    ("100.00", False, "mapped"),
    (None, False, "unmapped"),
])
def test_any_current_ledger_row_suppresses_otherwise_valid_sales_fallback(
    db, amount, included, mapping_state,
):
    project_id = _project(db)
    _fallback(db, [(project_id, "XSDD-FALLBACK")])
    _contract(
        db, project_id, amount=amount, included=included, mapping_state=mapping_state,
    )

    result = reference.current_contract_references(db, [project_id])

    assert result == {project_id: {
        "contract_nos": ["XSDD-LEDGER"],
        "amount_inc_tax": None,
        "contract_shared": False,
        "contract_incomplete": True,
    }}


@pytest.mark.parametrize("fallback_amount", ["0.00", "-10.00"])
def test_ledger_zero_and_nonpositive_fallback_keep_distinct_existing_shapes(
    db, fallback_amount,
):
    ledger_project, fallback_project = _project(db), _project(db)
    _contract(db, ledger_project, amount="0.00")
    _fallback(db, [(fallback_project, "XSDD-NONPOSITIVE")], amount=fallback_amount)

    result = reference.current_contract_references(
        db, [ledger_project, fallback_project],
    )

    assert result[ledger_project]["amount_inc_tax"] == Decimal("0.00")
    assert result[ledger_project]["contract_incomplete"] is False
    # Characterization only: extraction must not silently normalize this old case.
    assert result[fallback_project]["amount_inc_tax"] is None
    assert result[fallback_project]["contract_incomplete"] is False


def test_current_intervals_keep_start_order_and_exclude_end_boundary(db):
    project_id = _project(db)
    _contract(
        db, project_id, contract_no="XSDD-EXPIRED", effective_to=TODAY,
    )
    _contract(
        db, project_id, contract_no="XSDD-FUTURE", effective_from=date(2026, 10, 24),
    )
    # Insert in the opposite order; the contract list follows effective_from.
    _contract(
        db, project_id, contract_no="XSDD-A", amount="20.00", effective_from=TODAY,
    )
    _contract(
        db, project_id, contract_no="XSDD-Z", amount="10.00",
        effective_from=date(2026, 9, 1),
    )

    result = reference.current_contract_references(db, [project_id])

    assert result == {project_id: {
        "contract_nos": ["XSDD-Z", "XSDD-A"],
        "amount_inc_tax": Decimal("30.00"),
        "contract_shared": False,
        "contract_incomplete": False,
    }}


def test_expired_ledger_allows_fallback_and_keeps_unlinked_project_absent(db):
    project_id, unlinked_project = _project(db), _project(db)
    _contract(db, project_id, effective_to=TODAY)
    _fallback(db, [(project_id, "XSDD-CURRENT-SALES")])

    result = reference.current_contract_references(
        db, [project_id, unlinked_project, str(uuid.uuid4())],
    )

    assert result == {project_id: {
        "contract_nos": ["XSDD-CURRENT-SALES"],
        "amount_inc_tax": Decimal("113.00"),
        "contract_shared": False,
        "contract_incomplete": False,
    }}


def test_batch_reads_do_not_grow_per_project_or_commit(db):
    project_ids = [_project(db) for _ in range(6)]
    _fallback(db, [(pid, f"XSDD-BATCH-{index}") for index, pid in enumerate(project_ids)])
    statements: list[str] = []
    commits: list[bool] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.lower().split()))

    def capture_commit(_session):
        commits.append(True)

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    event.listen(db, "after_commit", capture_commit)
    try:
        assert reference.current_contract_references(db, []) == {}
        assert statements == []
        single = reference.current_contract_references(db, project_ids[:1])
        single_statements = statements[:]
        statements.clear()
        batch = reference.current_contract_references(db, project_ids)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
        event.remove(db, "after_commit", capture_commit)

    assert set(batch) == set(project_ids)
    assert single == {project_ids[0]: batch[project_ids[0]]}
    assert len(single_statements) == len(statements) == 3
    assert all(statement.startswith("select ") for statement in single_statements + statements)
    assert commits == []


def test_legacy_board_name_keeps_same_function_identity():
    from app.services import maintenance_boss_board

    assert maintenance_boss_board._card_contracts is reference.current_contract_references
