"""Workbook contract eligibility and canonical expense queries.

Current relationships, editable contract amounts, and unique expense ownership
are different questions. Keep their existing eligibility rules separate here.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceProjectExpenseAttribution
from app.services.maintenance_workbook_plans import WorkbookError


def _current_contracts(
    db: Session,
    project_id: str,
    *,
    lock: bool = False,
) -> list[MaintenanceProjectContract]:
    """Only relationships effective at the current business date."""

    today = business_today()
    statement = (
        select(MaintenanceProjectContract)
        .where(
            MaintenanceProjectContract.project_id == project_id,
            MaintenanceProjectContract.effective_from <= today,
            (
                MaintenanceProjectContract.effective_to.is_(None)
                | (MaintenanceProjectContract.effective_to > today)
            ),
        )
        .order_by(
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_contract_id,
        )
    )
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


def _current_contract_by_no(
    db: Session,
    project_id: str,
    contract_no: str,
    *,
    lock: bool = False,
) -> MaintenanceProjectContract | None:
    matches = [
        contract for contract in _current_contracts(db, project_id, lock=lock)
        if contract.contract_no == contract_no
    ]
    if len(matches) > 1:
        raise WorkbookError(
            "contract_ambiguous",
            f"合同 {contract_no} 当前存在多条生效关系，请先消歧后重新下载",
        )
    return matches[0] if matches else None


def _v2_editable_contracts(
    db: Session,
    project_id: str,
    contracts,
) -> list[MaintenanceProjectContract]:
    today = business_today()
    included_current = [
        contract for contract in contracts
        if contract.included_in_total
        and contract.effective_from <= today
        and (contract.effective_to is None or contract.effective_to > today)
    ]
    # 2026-09-02 拍板：合同额对项目负责人/销售与管理员全量放开。
    # 仍要求「当前计入的合同恰好一份」，保证项目总额能一一对应回写到唯一
    # 合同事实；mapped/共享不再硬拒——共享合同可编辑，apply 侧以
    # contract base_version CAS + 审计兜底，共享状态在导出侧提示。
    if len(included_current) != 1:
        return []
    only = included_current[0]
    if only.project_id != project_id:
        return []
    return [only]


def _current_unique_contracts(
    db: Session,
    project_id: str,
    *,
    contract_nos: set[str] | None = None,
    lock: bool = False,
) -> dict[str, MaintenanceProjectContract]:
    """Return current contract identities owned by exactly one live relation.

    ``linked_sales_order_no`` is not a foreign key.  Treating it as project
    ownership leaked the same BXD row through every project that happened to
    share a contract number.  Expense mutation is therefore allowed only when
    the URL project owns one current relation and no other current relation in
    any project uses that contract number.

    During apply, all matching live relationship rows are locked in one global
    order before uniqueness is recomputed.  This prevents two workbook applies
    from observing different subsets or taking cross-project rows in reverse
    order.
    """

    today = business_today()
    current_condition = (
        (MaintenanceProjectContract.effective_from <= today)
        & (
            MaintenanceProjectContract.effective_to.is_(None)
            | (MaintenanceProjectContract.effective_to > today)
        )
    )
    candidates = set(db.scalars(
        select(MaintenanceProjectContract.contract_no)
        .where(
            MaintenanceProjectContract.project_id == project_id,
            current_condition,
        )
        .order_by(
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_contract_id,
        )
    ).all())
    if contract_nos is not None:
        candidates &= {str(value) for value in contract_nos if value}
    if not candidates:
        return {}

    statement = (
        select(MaintenanceProjectContract)
        .where(
            MaintenanceProjectContract.contract_no.in_(sorted(candidates)),
            current_condition,
        )
        .order_by(
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.project_contract_id,
        )
    )
    if lock:
        statement = statement.with_for_update()
    rows = list(db.scalars(statement).all())
    by_no: dict[str, list[MaintenanceProjectContract]] = {}
    for row in rows:
        by_no.setdefault(row.contract_no, []).append(row)
    return {
        contract_no: matches[0]
        for contract_no, matches in by_no.items()
        if len(matches) == 1 and matches[0].project_id == project_id
    }


def project_expenses(db: Session, project_id: str) -> list[FProjectExpense]:
    """Project-scoped editable expenses from canonical attribution.

    作废行（data_status='已作废'）彻底不导出（#264/#267 读侧修复 3：
    用户拍板 2026-08-19）——导出口径即回传口径，缺行=作废的对账语义
    要求导出侧从源头剔除已作废行，否则作废行永远循环出现在往返文件里。
    """
    writable_contracts = _current_unique_contracts(db, project_id)
    contract_nos = set(writable_contracts)
    if not contract_nos:
        return []
    rows = list(db.execute(
        select(FProjectExpense, MaintenanceProjectExpenseAttribution)
        .join(
            MaintenanceProjectExpenseAttribution,
            MaintenanceProjectExpenseAttribution.raw_expense_line_id
            == FProjectExpense.raw_line_id,
        )
        .where(
            MaintenanceProjectExpenseAttribution.project_id == project_id,
            MaintenanceProjectExpenseAttribution.project_contract_id.in_([
                contract.project_contract_id
                for contract in writable_contracts.values()
            ]),
            FProjectExpense.linked_sales_order_no.in_(contract_nos),
            or_(FProjectExpense.data_status.is_(None),
                FProjectExpense.data_status != "已作废"),
        )
        .order_by(FProjectExpense.expense_date, FProjectExpense.raw_line_id)
    ).all())
    return [
        expense
        for expense, attribution in rows
        if (
            expense.linked_sales_order_no in writable_contracts
            and attribution.project_contract_id
            == writable_contracts[expense.linked_sales_order_no].project_contract_id
        )
    ]
