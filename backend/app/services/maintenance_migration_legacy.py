"""Read-only legacy WBDD/BXD truth for Issue #210 before/after reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app import config, tax_policy
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder, FProjectExpense
from app.models.maintenance_project import MaintenanceProjectContract
from app.services.maintenance_migration_controls import canonical_hash
from app.services.maintenance_migration_source import MaintenanceMigrationSourceError


LEGACY_TRUTH_VERSION = "maintenance-legacy-truth-v1"
_MAX_LEGACY_COST_LINES = 200_000
_MAX_LEGACY_ORDERS = 200_000
_MAX_LEGACY_ASSIGNMENTS = 400_000
_MAX_LEGACY_EXPENSE_LINES = 200_000
_MAX_LEGACY_CONTRACT_RELATIONS = 200_000
_CENT = Decimal("0.01")
_MONEY_LIMIT = Decimal("1000000000000")
_QTY_LIMIT = Decimal("1000000000000")


class MaintenanceMigrationLegacyError(MaintenanceMigrationSourceError):
    pass


def _table_has_columns(db: Session, table_name: str, columns: set[str]) -> bool:
    if db.scalar(text("SELECT to_regclass(:name)"), {"name": table_name}) is None:
        return False
    found = set(
        db.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :name"
            ),
            {"name": table_name},
        )
    )
    return columns <= found


def _money(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MaintenanceMigrationLegacyError(f"{label}无效") from exc
    if not number.is_finite() or number < 0 or number >= _MONEY_LIMIT:
        raise MaintenanceMigrationLegacyError(f"{label}超出允许范围")
    return number


def _money_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _qty(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MaintenanceMigrationLegacyError(f"{label}无效") from exc
    if not number.is_finite() or abs(number) >= _QTY_LIMIT:
        raise MaintenanceMigrationLegacyError(f"{label}超出允许范围")
    return number


def _qty_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _blocker(
    blockers: list[dict[str, Any]],
    code: str,
    detail: str,
    *,
    entity_id: str | None = None,
) -> None:
    blocker: dict[str, Any] = {"code": code, "detail": detail}
    if entity_id is not None:
        blocker["entity_id"] = entity_id
    blockers.append(blocker)


def _assignment_rows(
    db: Session,
    *,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> tuple[list[Any], bool]:
    required = {
        "assignment_id",
        "source_order_id",
        "project_id",
        "is_active",
        "version",
    }
    if not _table_has_columns(db, "maintenance_source_order_assignment", required):
        return [], False
    db.execute(text("LOCK TABLE maintenance_source_order_assignment IN SHARE MODE"))
    source_order_ids = list(
        db.scalars(
            select(FMaintenanceOrder.raw_order_id)
            .where(
                FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
                or_(
                    FMaintenanceOrder.order_date.is_(None),
                    FMaintenanceOrder.order_date <= as_of,
                ),
            )
            .order_by(FMaintenanceOrder.raw_order_id)
            .limit(_MAX_LEGACY_ORDERS + 1)
        )
    )
    if len(source_order_ids) > _MAX_LEGACY_ORDERS:
        raise MaintenanceMigrationLegacyError("旧 WBDD 单据超过迁移安全上限")
    if not source_order_ids:
        return [], True
    rows = list(
        db.execute(
            text(
                "SELECT assignment_id, source_order_id, project_id, version "
                "FROM maintenance_source_order_assignment "
                "WHERE is_active IS TRUE "
                "AND source_order_id = ANY(CAST(:source_order_ids AS VARCHAR[])) "
                "ORDER BY source_order_id, project_id, assignment_id "
                f"LIMIT {_MAX_LEGACY_ASSIGNMENTS + 1}"
            ),
            {"source_order_ids": source_order_ids},
        )
    )
    if len(rows) > _MAX_LEGACY_ASSIGNMENTS:
        raise MaintenanceMigrationLegacyError("旧 WBDD 多项目归属超过迁移安全上限")
    assigned_order_ids = {str(row.source_order_id) for row in rows}
    for source_order_id in source_order_ids:
        if source_order_id not in assigned_order_ids:
            _blocker(
                blockers,
                "legacy_assignment_missing",
                "冻结截止日内有效旧 WBDD 没有 #201 稳定项目归属",
                entity_id=source_order_id,
            )
    return rows, True


def _legacy_cost_lines(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    assignments, assignment_contract_ready = _assignment_rows(
        db,
        as_of=as_of,
        blockers=blockers,
    )
    if not assignment_contract_ready:
        _blocker(
            blockers,
            "legacy_assignment_contract_missing",
            "旧口径 WBDD 尚未接入 #201 稳定项目归属，不能证明旧项目成本全集",
        )
        return [], False
    assignment_by_source: dict[str, list[Any]] = {}
    for row in assignments:
        assignment_by_source.setdefault(str(row.source_order_id), []).append(row)
    duplicated = [
        key
        for key, rows in assignment_by_source.items()
        if len(rows) != 1 and any(str(row.project_id) == project_id for row in rows)
    ]
    if duplicated:
        for source_order_id in duplicated:
            _blocker(
                blockers,
                "legacy_assignment_ambiguous",
                "同一旧 WBDD 存在多个当前有效项目归属",
                entity_id=source_order_id,
            )
    source_order_ids = {
        key
        for key, rows in assignment_by_source.items()
        if len(rows) == 1 and str(rows[0].project_id) == project_id
    }
    if not source_order_ids:
        return [], True
    rows = db.execute(
        select(FMaintenanceOrder, FMaintenanceLine)
        .join(FMaintenanceLine, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(
            FMaintenanceOrder.raw_order_id.in_(source_order_ids),
            FMaintenanceOrder.data_status == config.ACTIVE_STATUS,
            or_(
                FMaintenanceOrder.order_date.is_(None),
                FMaintenanceOrder.order_date <= as_of,
            ),
        )
        .order_by(
            FMaintenanceOrder.order_date,
            FMaintenanceOrder.raw_order_id,
            FMaintenanceLine.line_no,
            FMaintenanceLine.raw_line_id,
        )
        .limit(_MAX_LEGACY_COST_LINES + 1)
    ).all()
    if len(rows) > _MAX_LEGACY_COST_LINES:
        raise MaintenanceMigrationLegacyError("单项目旧 WBDD 明细超过迁移安全上限")
    output: list[dict[str, Any]] = []
    for order, line in rows:
        line_id = str(line.raw_line_id)
        assignment = assignment_by_source[str(order.raw_order_id)][0]
        quantity_parse_failed = False
        try:
            demand_quantity = (
                _qty(line.qty, "旧 WBDD 需求数量") if line.qty is not None else None
            )
            return_quantity = (
                _qty(line.return_qty, "旧 WBDD 退货数量")
                if line.return_qty is not None
                else Decimal("0")
            )
        except MaintenanceMigrationLegacyError as exc:
            quantity_parse_failed = True
            demand_quantity = None
            return_quantity = Decimal("0")
            _blocker(
                blockers,
                "legacy_quantity_invalid",
                str(exc),
                entity_id=line_id,
            )
        effective_quantity: Decimal | None = None
        cost_ex: Decimal | None = None
        cost_inc: Decimal | None = None
        unit_ex: Decimal | None = None
        unit_inc: Decimal | None = None
        if line.unit_cost_ex_tax is not None and line.unit_cost_inc_tax is not None:
            try:
                unit_ex = _money(line.unit_cost_ex_tax, "旧 WBDD 未税单价")
                unit_inc = _money(line.unit_cost_inc_tax, "旧 WBDD 含税单价")
            except MaintenanceMigrationLegacyError as exc:
                _blocker(
                    blockers,
                    "legacy_cost_invalid",
                    str(exc),
                    entity_id=line_id,
                )
        if order.order_date is None:
            _blocker(
                blockers,
                "legacy_order_date_missing",
                "旧 WBDD 缺少制单日期，不能证明属于冻结截止日",
                entity_id=line_id,
            )
        if quantity_parse_failed:
            pass
        elif demand_quantity is None or demand_quantity < 0 or return_quantity < 0:
            _blocker(
                blockers,
                "legacy_quantity_invalid",
                "旧 WBDD 需求数量或退货数量无效",
                entity_id=line_id,
            )
        elif return_quantity > demand_quantity:
            _blocker(
                blockers,
                "legacy_return_exceeds_demand",
                "旧 WBDD 退货数量大于需求数量",
                entity_id=line_id,
            )
        else:
            effective_quantity = demand_quantity - return_quantity
        if line.unit_cost_ex_tax is None or line.unit_cost_inc_tax is None:
            _blocker(
                blockers,
                "legacy_cost_missing",
                "旧 WBDD 缺少可复算双税成本单价",
                entity_id=line_id,
            )
        elif unit_ex is None or unit_inc is None:
            pass
        else:
            try:
                if line.cost_tax_basis == "ex":
                    expected_unit = _money(
                        unit_ex * tax_policy.TAX_FACTOR,
                        "旧 WBDD 含税单价",
                    )
                    tax_matches = unit_inc == expected_unit
                elif line.cost_tax_basis == "inc":
                    expected_unit = _money(
                        unit_inc / tax_policy.TAX_FACTOR,
                        "旧 WBDD 未税单价",
                    )
                    tax_matches = unit_ex == expected_unit
                else:
                    raise MaintenanceMigrationLegacyError("旧 WBDD 成本税价基准无效")
            except MaintenanceMigrationLegacyError as exc:
                _blocker(
                    blockers,
                    "legacy_cost_invalid",
                    str(exc),
                    entity_id=line_id,
                )
                tax_basis_valid = False
            else:
                tax_basis_valid = True
            if tax_basis_valid and not tax_matches:
                _blocker(
                    blockers,
                    "legacy_cost_tax_mismatch",
                    "旧 WBDD 含税单价无法按固定 13% 由未税单价复算",
                    entity_id=line_id,
                )
            elif tax_basis_valid and effective_quantity is not None:
                try:
                    cost_ex = _money(effective_quantity * unit_ex, "旧 WBDD 未税成本")
                    cost_inc = _money(effective_quantity * unit_inc, "旧 WBDD 含税成本")
                    stored_ex = (
                        _money(line.cost_amount_ex_tax, "旧 WBDD 已存未税成本")
                        if line.cost_amount_ex_tax is not None
                        else None
                    )
                    stored_inc = (
                        _money(line.cost_amount_inc_tax, "旧 WBDD 已存含税成本")
                        if line.cost_amount_inc_tax is not None
                        else None
                    )
                except MaintenanceMigrationLegacyError as exc:
                    cost_ex = None
                    cost_inc = None
                    _blocker(
                        blockers,
                        "legacy_cost_invalid",
                        str(exc),
                        entity_id=line_id,
                    )
                else:
                    if (stored_ex is not None and stored_ex != cost_ex) or (
                        stored_inc is not None and stored_inc != cost_inc
                    ):
                        _blocker(
                            blockers,
                            "legacy_stored_cost_mismatch",
                            "旧 WBDD 已存成本与需求数量减退货数量的旧公式不一致",
                            entity_id=line_id,
                        )
        output.append(
            {
                "source_order_id": str(order.raw_order_id),
                "source_line_id": line_id,
                "order_no": order.order_no,
                "order_date": order.order_date.isoformat()
                if order.order_date
                else None,
                "pn": line.pn_std or line.pn_raw,
                "sn": line.serial_numbers,
                "part_id": line.part_id,
                "demand_quantity": _qty_text(demand_quantity),
                "return_quantity": _qty_text(return_quantity),
                "effective_quantity": _qty_text(effective_quantity),
                "unit_cost_ex_tax": _money_text(unit_ex),
                "unit_cost_inc_tax": _money_text(unit_inc),
                "cost_tax_basis": line.cost_tax_basis,
                "cost_amount_ex_tax": _money_text(cost_ex),
                "cost_amount_inc_tax": _money_text(cost_inc),
                "stored_cost_amount_ex_tax": _money_text(line.cost_amount_ex_tax),
                "stored_cost_amount_inc_tax": _money_text(line.cost_amount_inc_tax),
                "assignment_id": str(assignment.assignment_id),
                "assignment_version": int(assignment.version),
                "order_import_batch_id": order.import_batch_id,
                "line_import_batch_id": line.import_batch_id,
            }
        )
    return output, True


def _legacy_expenses(
    db: Session,
    *,
    project_id: str,
    as_of: date,
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(FProjectExpense)
        .where(
            FProjectExpense.data_status == config.MAINT_EXPENSE_ACTIVE_STATUS,
            or_(
                FProjectExpense.expense_date.is_(None),
                FProjectExpense.expense_date <= as_of,
            ),
        )
        .order_by(FProjectExpense.expense_date, FProjectExpense.raw_line_id)
        .limit(_MAX_LEGACY_EXPENSE_LINES + 1)
    ).all()
    if len(rows) > _MAX_LEGACY_EXPENSE_LINES:
        raise MaintenanceMigrationLegacyError("旧 BXD 明细超过迁移安全上限")

    contract_nos = sorted(
        {
            str(row.linked_sales_order_no).strip()
            for row in rows
            if str(row.linked_sales_order_no or "").strip()
        }
    )
    relations = (
        list(
            db.scalars(
                select(MaintenanceProjectContract)
                .where(
                    MaintenanceProjectContract.contract_no.in_(contract_nos),
                    MaintenanceProjectContract.effective_from <= as_of,
                )
                .order_by(
                    MaintenanceProjectContract.contract_no,
                    MaintenanceProjectContract.effective_from,
                    MaintenanceProjectContract.project_contract_id,
                )
                .limit(_MAX_LEGACY_CONTRACT_RELATIONS + 1)
            )
        )
        if contract_nos
        else []
    )
    if len(relations) > _MAX_LEGACY_CONTRACT_RELATIONS:
        raise MaintenanceMigrationLegacyError("旧 BXD 合同归属超过迁移安全上限")
    relations_by_contract: dict[str, list[MaintenanceProjectContract]] = {}
    for relation in relations:
        relations_by_contract.setdefault(relation.contract_no, []).append(relation)

    output: list[dict[str, Any]] = []
    for row in rows:
        row_id = str(row.raw_line_id)
        contract_no = str(row.linked_sales_order_no or "").strip()
        if row.expense_date is None:
            _blocker(
                blockers,
                "legacy_expense_date_missing",
                "旧 BXD 缺少报销日期，不能确定当日项目归属",
                entity_id=row_id,
            )
            continue
        applicable_relations = [
            relation
            for relation in relations_by_contract.get(contract_no, [])
            if relation.effective_from <= row.expense_date
            and (
                relation.effective_to is None
                or row.expense_date < relation.effective_to
            )
        ]
        owner_projects = {relation.project_id for relation in applicable_relations}
        if not owner_projects:
            _blocker(
                blockers,
                "legacy_expense_contract_scope_missing",
                "旧 BXD 在报销日没有可证明的唯一项目合同归属",
                entity_id=row_id,
            )
            continue
        if project_id not in owner_projects:
            # The expense is provably owned by another project on its business date.
            continue
        if len(applicable_relations) != 1:
            _blocker(
                blockers,
                "legacy_expense_contract_ambiguous",
                "旧 BXD 合同在报销日存在重叠项目归属",
                entity_id=contract_no,
            )
            continue
        relation = applicable_relations[0]
        amount_ex: Decimal | None = None
        amount_inc: Decimal | None = None
        if row.amount_ex_tax is not None and row.amount_inc_tax is not None:
            try:
                amount_ex = _money(row.amount_ex_tax, "旧 BXD 未税金额")
                amount_inc = _money(row.amount_inc_tax, "旧 BXD 含税金额")
            except MaintenanceMigrationLegacyError as exc:
                _blocker(
                    blockers,
                    "legacy_expense_amount_invalid",
                    str(exc),
                    entity_id=row_id,
                )
        if row.amount_ex_tax is None or row.amount_inc_tax is None:
            _blocker(
                blockers,
                "legacy_expense_amount_missing",
                "旧 BXD 缺少双税金额",
                entity_id=row_id,
            )
        elif amount_ex is not None and amount_inc is not None:
            try:
                if row.tax_basis in {"default_ex", "ex"}:
                    expected_amount = _money(
                        amount_ex * tax_policy.TAX_FACTOR,
                        "旧 BXD 含税金额",
                    )
                    tax_matches = amount_inc == expected_amount
                elif row.tax_basis == "inc":
                    expected_amount = _money(
                        amount_inc / tax_policy.TAX_FACTOR,
                        "旧 BXD 未税金额",
                    )
                    tax_matches = amount_ex == expected_amount
                else:
                    raise MaintenanceMigrationLegacyError("旧 BXD 税价基准无效")
            except MaintenanceMigrationLegacyError as exc:
                _blocker(
                    blockers,
                    "legacy_expense_amount_invalid",
                    str(exc),
                    entity_id=row_id,
                )
            else:
                if not tax_matches:
                    _blocker(
                        blockers,
                        "legacy_expense_tax_mismatch",
                        "旧 BXD 含税金额无法按固定 13% 由未税金额复算",
                        entity_id=row_id,
                    )
        output.append(
            {
                "expense_id": row_id,
                "expense_ref": row.bxd_no or row.raw_line_id,
                "expense_date": row.expense_date.isoformat()
                if row.expense_date
                else None,
                "normalized_status": "approved",
                "raw_status": row.data_status,
                "contract_no": contract_no,
                "project_contract_id": relation.project_contract_id,
                "contract_id": relation.contract_id,
                "contract_relation_version": relation.version,
                "contract_effective_from": relation.effective_from.isoformat(),
                "contract_effective_to": (
                    relation.effective_to.isoformat() if relation.effective_to else None
                ),
                "tax_basis": row.tax_basis,
                "amount_ex_tax": _money_text(amount_ex),
                "amount_inc_tax": _money_text(amount_inc),
                "import_batch_id": row.import_batch_id,
            }
        )
    return output


def load_project_legacy_truth(
    db: Session, project_id: str, as_of: date
) -> dict[str, Any]:
    """Load immutable old-formula facts; any incomplete scope fails closed."""

    db.execute(
        text(
            "LOCK TABLE f_maintenance_order, f_maintenance_line, "
            "f_project_expense, maintenance_project_contract IN SHARE MODE"
        )
    )
    blockers: list[dict[str, Any]] = []
    cost_lines, assignment_ready = _legacy_cost_lines(
        db, project_id=project_id, as_of=as_of, blockers=blockers
    )
    expenses = _legacy_expenses(
        db, project_id=project_id, as_of=as_of, blockers=blockers
    )
    coverage = {
        "legacy_truth_version": LEGACY_TRUTH_VERSION,
        "business_as_of": as_of.isoformat(),
        "assignment_contract_ready": assignment_ready,
        "cost_line_count": len(cost_lines),
        "expense_line_count": len(expenses),
    }
    evidence = {
        "cost_lines": cost_lines,
        "expenses": expenses,
        "source_coverage": coverage,
    }
    return {
        **evidence,
        "source_hash": canonical_hash(evidence),
        "source_ready": assignment_ready and not blockers,
        "blockers": blockers,
    }
