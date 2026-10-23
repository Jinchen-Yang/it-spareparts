"""Live workbook ownership checks and locked validation evidence.

The caller owns the surrounding transaction and must acquire the documented
workbook/project locks before the locked scope and row-guard checks.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance import (
    FMaintenanceLine, FMaintenanceOrder, FProjectExpense, MaintenanceManualCostOverride,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot, MaintenanceProjectExpenseAttribution,
    MaintenanceSiteIssue, MaintenanceSiteIssueLine,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services.maintenance_workbook_plans import MasterV2Plan, V2RowGuard, WorkbookError
from app.services.maintenance_workbook_protocol import (
    V2_EXPENSE_BASE_FIELDS, V2_PART_BASE_FIELDS, V2_RECEIPT_BASE_FIELDS,
    V2_SITE_BASE_FIELDS, V2_SHEET_EXPENSE, V2_SHEET_PARTS, V2_SHEET_RECEIPTS,
    V2_SHEET_SITE,
)
from app.services.maintenance_workbook_queries import project_expenses as _project_expenses
from app.services.maintenance_workbook_rows import (
    _v2_expense_row_values, _v2_part_row_values, _v2_receipt_row_values,
    _v2_site_identity_values,
)
from app.services.maintenance_workbook_values import _v2_row_base_hash


def _expected_expense_ids(db: Session, project_id: str) -> list[str]:
    # 与 _v2_build_expense 同口径：稳定 attribution + current/unshared XSDD。
    return [expense.raw_line_id for expense in _project_expenses(db, project_id)]


def _expected_part_ids(db: Session, project_id: str) -> set[int]:
    """Active 03 entities whose current assignment belongs to this project."""

    from app.services import maintenance_demands
    from app.services.query_filters import active_orders

    return set(db.scalars(active_orders(
        select(FMaintenanceLine.id)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .join(
            MaintenanceSourceOrderAssignment,
            (MaintenanceSourceOrderAssignment.source_order_id
             == FMaintenanceOrder.raw_order_id)
            & MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id == project_id,
            FMaintenanceLine.is_active.is_(True),
            maintenance_demands.active_demand_condition(),
        ),
        FMaintenanceOrder,
    )).all())


def _expected_site_ids(db: Session, project_id: str) -> set[str]:
    """Active 06 entities whose owning issue belongs to this project.

    与 ``_v2_build_site`` 同一口径：行活跃且单头未作废。单头 void 的行不在
    实体范围内——它们既不导出，也不允许被上传改动（D-01）。
    """

    return set(db.scalars(
        select(MaintenanceSiteIssueLine.issue_line_id)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssue.normalized_status != "void",
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
    ).all())


def _v2_assert_entity_scope(
    db: Session,
    *,
    project_id: str,
    part_ids: set[int] | None = None,
    site_ids: set[str] | None = None,
    expense_ids: set[str] | None = None,
    linked_sales_order_nos: set[str] | None = None,
    lock_rows: bool = False,
    lock_site_merge_scope: bool = False,
) -> set[str]:
    """Fail the complete workbook before a foreign fact can be mutated.

    Signed export metadata proves which rows the server exported, but it is not
    an authorization cache: ownership can change between export, validation,
    and apply.  Each boundary therefore rebuilds the three live project sets.
    """

    requested_parts = {int(value) for value in (part_ids or set())}
    requested_sites = {str(value) for value in (site_ids or set())}
    requested_expenses = {str(value) for value in (expense_ids or set())}
    requested_contract_nos = {
        str(value) for value in (linked_sales_order_nos or set()) if value
    }
    locked_project_xsdd_nos: set[str] = set()

    if lock_rows and (requested_parts or requested_contract_nos):
        # Canonical reassignment order: source order -> active assignment ->
        # detail line.  Include every WBDD referenced by a deferred XSDD
        # contract before locking *any* detail line, otherwise a plan-only
        # apply can invert the assignment writer's project->order lock order.
        line_edges = list(db.execute(
            select(FMaintenanceLine.id, FMaintenanceLine.order_id)
            .where(FMaintenanceLine.id.in_(requested_parts))
            .order_by(FMaintenanceLine.id)
        ).all()) if requested_parts else []
        order_ids = {int(order_id) for _line_id, order_id in line_edges}
        if requested_contract_nos:
            order_ids.update(db.scalars(
                select(FMaintenanceOrder.id).where(
                    FMaintenanceOrder.linked_sales_order_no.in_(
                        requested_contract_nos
                    )
                )
            ).all())
        locked_orders = list(db.scalars(
            select(FMaintenanceOrder)
            .where(FMaintenanceOrder.id.in_(sorted(order_ids)))
            .order_by(FMaintenanceOrder.raw_order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()) if order_ids else []
        source_ids = [order.raw_order_id for order in locked_orders]
        locked_assignments: list[MaintenanceSourceOrderAssignment] = []
        if source_ids:
            locked_assignments = list(db.scalars(
                select(MaintenanceSourceOrderAssignment)
                .where(
                    MaintenanceSourceOrderAssignment.source_order_id.in_(source_ids),
                    MaintenanceSourceOrderAssignment.is_active.is_(True),
                )
                .order_by(MaintenanceSourceOrderAssignment.source_order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all())
        project_sources = {
            assignment.source_order_id
            for assignment in locked_assignments
            if assignment.project_id == project_id
        }
        locked_project_xsdd_nos = {
            str(order.linked_sales_order_no)
            for order in locked_orders
            if (
                order.raw_order_id in project_sources
                and order.linked_sales_order_no
            )
        }
        if requested_parts:
            locked_lines = list(db.scalars(
                select(FMaintenanceLine)
                .where(FMaintenanceLine.id.in_(requested_parts))
                .order_by(FMaintenanceLine.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all())
            if {line.id for line in locked_lines} != requested_parts:
                raise WorkbookError(
                    "line_not_in_project",
                    "工作簿包含已不存在、已作废或不属于本项目的备件事实，请重新下载",
                )

    foreign_parts = requested_parts - _expected_part_ids(db, project_id)
    if foreign_parts:
        raise WorkbookError(
            "line_not_in_project",
            "工作簿包含已不存在、已作废或不属于本项目的备件事实，请重新下载",
        )
    if lock_rows and (requested_sites or lock_site_merge_scope):
        # Site writers use parent issue -> child line.  Resolve the edge first,
        # lock parents in stable order, then lock children and re-read the live
        # join; reversing that order deadlocks with an issue-level writer.
        site_edges = list(db.execute(
            select(
                MaintenanceSiteIssueLine.issue_line_id,
                MaintenanceSiteIssueLine.issue_id,
            )
            .where(MaintenanceSiteIssueLine.issue_line_id.in_(requested_sites))
            .order_by(MaintenanceSiteIssueLine.issue_line_id)
        ).all())
        if {line_id for line_id, _issue_id in site_edges} != requested_sites:
            raise WorkbookError(
                "site_line_not_in_project",
                "工作簿包含已不存在、已作废或不属于本项目的领用事实，请重新下载",
            )
        parent_issue_ids = sorted({issue_id for _line_id, issue_id in site_edges})
        if lock_site_merge_scope:
            parent_issue_ids = sorted(db.scalars(select(MaintenanceSiteIssue.issue_id).where(
                MaintenanceSiteIssue.project_id == project_id,
            )))
        list(db.scalars(
            select(MaintenanceSiteIssue)
            .where(MaintenanceSiteIssue.issue_id.in_(parent_issue_ids))
            .order_by(MaintenanceSiteIssue.issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all())
        locked_site_lines = list(db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(MaintenanceSiteIssueLine.issue_id.in_(parent_issue_ids)
                   if lock_site_merge_scope else MaintenanceSiteIssueLine.issue_line_id.in_(requested_sites))
            .order_by(MaintenanceSiteIssueLine.issue_line_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all())
        if (
            not requested_sites.issubset({line.issue_line_id for line in locked_site_lines})
            or any(line.issue_id not in parent_issue_ids for line in locked_site_lines)
        ):
            raise WorkbookError(
                "site_line_not_in_project",
                "领用事实归属已变化，请重新下载当前项目总表",
            )

    foreign_sites = requested_sites - _expected_site_ids(db, project_id)
    if foreign_sites:
        raise WorkbookError(
            "site_line_not_in_project",
            "工作簿包含已不存在、已作废或不属于本项目的领用事实，请重新下载",
        )
    if lock_rows and requested_expenses:
        expense_keys = {f"bxd:{raw_line_id}" for raw_line_id in requested_expenses}
        locked_attributions = list(db.scalars(
            select(MaintenanceProjectExpenseAttribution)
            .where(
                MaintenanceProjectExpenseAttribution.expense_id.in_(expense_keys)
            )
            .order_by(MaintenanceProjectExpenseAttribution.expense_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all())
        if (
            {attribution.expense_id for attribution in locked_attributions}
            != expense_keys
            or any(
                attribution.project_id != project_id
                for attribution in locked_attributions
            )
        ):
            raise WorkbookError(
                "expense_not_in_project",
                "工作簿包含已不存在、已作废或不属于本项目的报销归因，请重新下载",
            )
        locked_expenses = list(db.scalars(
            select(FProjectExpense)
            .where(FProjectExpense.raw_line_id.in_(requested_expenses))
            .order_by(FProjectExpense.raw_line_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all())
        if {expense.raw_line_id for expense in locked_expenses} != requested_expenses:
            raise WorkbookError(
                "expense_not_in_project",
                "工作簿包含已不存在、已作废或不属于本项目的报销事实，请重新下载",
            )
    expected_expenses = set(_expected_expense_ids(db, project_id))
    foreign_expenses = requested_expenses - expected_expenses
    if foreign_expenses:
        raise WorkbookError(
            "expense_not_in_project",
            "工作簿包含已不存在、已作废或不属于本项目的报销事实，请重新下载",
        )
    return locked_project_xsdd_nos


def _v2_recheck_row_guards(db: Session, plan: MasterV2Plan) -> None:
    """锁后复核：每个受保护实体的服务端指纹必须仍等于 validate 时刻的值。

    调用位置必须在 _v2_assert_entity_scope(lock_rows=True) 之后、任何写之前。
    那次调用已经把 03/04/06 三族的既有行 FOR UPDATE 锁住并读成新鲜的。
    地址来自新增明细时，单头可能不在既有行锁集合中；本函数在项目 state 锁已持有
    的前提下按 ID 顺序补锁地址单头。每族只发常数条批量查询，语句数与行数无关。

    不等即整本 fail-closed：validate 的三路合并是拿「当时读到的服务端值」跟基线
    比的，那个前提一旦失效，plan 里的每一个 delta 都不再可信。

    05_实收回款的快照**不由** _v2_assert_entity_scope 加锁（真正的行锁在
    maintenance_expense_collection_workbook 里，要到 ec.apply 才发生），但在这个
    位置复核它依然权威，靠的是另外三把锁的合力：
      - 新鲜性：apply 顶部的 db.expire_all()；05 分支还用列级标量读，直接绕开
        identity map，不依赖「expire 之后没人加载过快照」这个无机制保证的前提。
      - 稳定性：本事务此刻已持 DATA_CHANGE_ADVISORY_LOCK_KEY 全局锁、本项目的
        workbook state 行锁、以及 maintenance_project 行锁。app/ 下能写快照的
        五个点各自至少被其中一把挡住：
          operations.create_collection / operations.update_collection（取 state 锁）、
          maintenance_expense_collection_workbook.apply（取 state 锁）、
          maintenance_project_identity 的归并（取同一把全局 advisory + states）、
          maintenance_project_identity.apply_exact_collection_dedupe
            （**不取 state 锁**，靠同一把全局 advisory + 项目行锁才被挡住）。
    最后那条是这个不变量最脆的一环：日后若新增一个既不取 state 锁、也不取项目行
    锁、也不取全局 advisory 的快照写者，本复核会静默失去权威性而不报任何错。
    改这五个点的锁之前请先回来看这段。
    """

    if not plan.row_guards:
        return

    by_sheet: dict[str, list[V2RowGuard]] = defaultdict(list)
    for guard in plan.row_guards:
        by_sheet[guard.sheet].append(guard)

    fresh_by_key: dict[tuple[str, str], tuple[dict, tuple[str, ...]] | None] = {}

    part_guards = by_sheet.get(V2_SHEET_PARTS, [])
    if part_guards:
        line_ids = [int(g.entity_id) for g in part_guards]
        lines = {
            line.id: line for line in db.scalars(
                select(FMaintenanceLine).where(FMaintenanceLine.id.in_(line_ids)))
        }
        orders = {
            order.id: order for order in db.scalars(
                select(FMaintenanceOrder).where(FMaintenanceOrder.id.in_(
                    {line.order_id for line in lines.values()})))
        } if lines else {}
        overrides = {
            override.line_id: override for override in db.scalars(
                select(MaintenanceManualCostOverride).where(
                    MaintenanceManualCostOverride.line_id.in_(line_ids),
                    MaintenanceManualCostOverride.active.is_(True),
                ))
        }
        for guard in part_guards:
            line = lines.get(int(guard.entity_id))
            fresh_by_key[(guard.sheet, guard.entity_id)] = None if line is None else (
                _v2_part_row_values(
                    line, orders.get(line.order_id), overrides.get(line.id)),
                V2_PART_BASE_FIELDS,
            )

    expense_guards = by_sheet.get(V2_SHEET_EXPENSE, [])
    if expense_guards:
        raw_ids = [g.entity_id for g in expense_guards]
        expenses = {
            expense.raw_line_id: expense for expense in db.scalars(
                select(FProjectExpense).where(
                    FProjectExpense.raw_line_id.in_(raw_ids)))
        }
        for guard in expense_guards:
            expense = expenses.get(guard.entity_id)
            fresh_by_key[(guard.sheet, guard.entity_id)] = None if expense is None else (
                _v2_expense_row_values(expense, ""), V2_EXPENSE_BASE_FIELDS,
            )

    all_site_guards = by_sheet.get(V2_SHEET_SITE, [])
    header_guards = [guard for guard in all_site_guards if guard.entity_type == "site_issue"]
    if header_guards:
        issues = {issue.issue_id: issue for issue in db.scalars(
            select(MaintenanceSiteIssue).where(
                MaintenanceSiteIssue.issue_id.in_([g.entity_id for g in header_guards]),
                MaintenanceSiteIssue.project_id == plan.project_id,
                MaintenanceSiteIssue.normalized_status != "void",
            ).order_by(MaintenanceSiteIssue.issue_id).with_for_update()
            .execution_options(populate_existing=True))}
        for guard in header_guards:
            issue = issues.get(guard.entity_id)
            fresh_by_key[(guard.sheet, guard.entity_id)] = (
                ({"领用地址": issue.site_location or ""}, ("领用地址",)) if issue else None)
    site_guards = [guard for guard in all_site_guards if guard.entity_type != "site_issue"]
    if site_guards:
        site_ids = [g.entity_id for g in site_guards]
        site_lines = {
            line.issue_line_id: line for line in db.scalars(
                select(MaintenanceSiteIssueLine).where(
                    MaintenanceSiteIssueLine.issue_line_id.in_(site_ids)))
        }
        issues = {
            issue.issue_id: issue for issue in db.scalars(
                select(MaintenanceSiteIssue).where(MaintenanceSiteIssue.issue_id.in_(
                    {line.issue_id for line in site_lines.values()})))
        } if site_lines else {}
        for guard in site_guards:
            line = site_lines.get(guard.entity_id)
            issue = issues.get(line.issue_id) if line is not None else None
            fresh_by_key[(guard.sheet, guard.entity_id)] = (
                None if line is None or issue is None
                else (_v2_site_identity_values(line, issue), V2_SITE_BASE_FIELDS)
            )

    receipt_guards = by_sheet.get(V2_SHEET_RECEIPTS, [])
    if receipt_guards:
        collection_ids = [g.entity_id for g in receipt_guards]
        # 列级标量读：不进 identity map，因此新鲜性不依赖调用顺序。
        snapshot_rows = {
            row.collection_id: row for row in db.execute(
                select(
                    MaintenanceCollectionSnapshot.collection_id,
                    # report_month 不在基线字段里，但 _v2_receipt_row_values
                    # 会读它；不选就 AttributeError。
                    MaintenanceCollectionSnapshot.report_month,
                    MaintenanceCollectionSnapshot.cumulative_amount,
                    MaintenanceCollectionSnapshot.status,
                    MaintenanceCollectionSnapshot.receipt_reference,
                    MaintenanceCollectionSnapshot.remark,
                ).where(
                    MaintenanceCollectionSnapshot.collection_id.in_(collection_ids)
                )
            ).all()
        }
        for guard in receipt_guards:
            row = snapshot_rows.get(guard.entity_id)
            # 合同编号不在 V2_RECEIPT_BASE_FIELDS 里，传 "" 与采集侧一致。
            fresh_by_key[(guard.sheet, guard.entity_id)] = None if row is None else (
                _v2_receipt_row_values(row, ""), V2_RECEIPT_BASE_FIELDS,
            )

    drifted: list[dict] = []
    for guard in plan.row_guards:
        fresh = fresh_by_key.get((guard.sheet, guard.entity_id))
        # fresh is None：行已消失/父单不见了。_v2_assert_entity_scope 通常已经
        # 拦过，这里只兜底，同样按漂移处理，绝不放行。
        if fresh is None or _v2_row_base_hash(fresh[0], guard.fields or fresh[1]) != guard.digest:
            drifted.append({
                "sheet": guard.sheet, "row": guard.row_label,
                "entity_id": guard.entity_id, "field": "（整行）",
                "old": "", "new": "", "base": "",
                "reason": "server_changed_since_validate",
            })
    if drifted:
        raise WorkbookError(
            "row_conflicts",
            "部分行在校验与写入之间被他人更新，本次上传未写入任何数据；"
            "请重新下载项目总表后再试。",
            issues=drifted,
        )
