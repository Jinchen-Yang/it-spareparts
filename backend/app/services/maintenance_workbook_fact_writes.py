"""Workbook fact mutations that participate in the caller's transaction.

Contract claiming, source-line cascades, and display-number allocation retain
their existing locking prerequisites. No helper owns commit or rollback.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import config
from app.business_time import business_today
from app.models.maintenance import FMaintenanceLine
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue, MaintenanceSiteIssueDisplayCounter, MaintenanceSiteIssueLine,
)
from app.models.sales import FSalesOrder
from app.models.system import SysImportBatch
from app.services import maintenance_project_identity
from app.services.maintenance_workbook_audit import _write_audit
from app.services.maintenance_workbook_plans import WorkbookError
from app.services.maintenance_workbook_queries import _current_contract_by_no


def _ensure_contract_for_xsdd_apply(
    db: Session,
    *,
    project: MaintenanceProject,
    contract_no: str,
    operated_by: str,
    assigned_xsdd_nos: set[str],
) -> tuple[MaintenanceProjectContract, bool]:
    """Create one current fallback relation inside the locked apply transaction."""

    contract_id = f"xsdd-{contract_no}"
    try:
        maintenance_project_identity.claim_xsdd_project(
            db,
            value=contract_no,
            project_id=project.project_id,
            source="project_master_workbook",
        )
    except maintenance_project_identity.XsddProjectConflict as exc:
        raise WorkbookError("contract_shared", str(exc)) from exc
    # Serialize this stable identity across projects; project row locks alone do
    # not protect two different projects from concurrently claiming one XSDD.
    db.execute(select(func.pg_advisory_xact_lock(
        func.hashtextextended(f"maintenance-contract:{contract_id}", 0)
    )))
    current = _current_contract_by_no(
        db, project.project_id, contract_no, lock=True)
    today = business_today()
    shared = db.scalar(
        select(MaintenanceProjectContract.project_contract_id)
        .where(
            (
                (MaintenanceProjectContract.contract_no == contract_no)
                | (
                    MaintenanceProjectContract.contract_id
                    == (current.contract_id if current is not None else contract_id)
                )
            ),
            MaintenanceProjectContract.project_id != project.project_id,
            MaintenanceProjectContract.effective_from <= today,
            (
                MaintenanceProjectContract.effective_to.is_(None)
                | (MaintenanceProjectContract.effective_to > today)
            ),
        )
        .limit(1)
        .with_for_update()
    )
    if shared is not None:
        raise WorkbookError(
            "contract_shared",
            f"合同 {contract_no} 已被其他项目当前使用，请先完成合同归属消歧",
        )
    if current is not None:
        return current, False
    # The caller took order -> active-assignment locks for every referenced
    # XSDD before any detail row or contract lock.  Use that locked snapshot;
    # do not reopen a TOCTOU window with an unlocked ownership query here.
    if contract_no not in assigned_xsdd_nos:
        raise WorkbookError(
            "contract_not_found",
            f"合同 {contract_no} 已不再由本项目当前 WBDD 挂靠，请重新下载",
        )
    if project.period_from is None:
        raise WorkbookError(
            "contract_effective_date_missing",
            "项目缺少业务开始日期，无法建立 XSDD 合同关系；请先补齐项目日期",
        )
    sales_candidates = list(db.execute(
        select(
            FSalesOrder.id,
            FSalesOrder.amount_ex_tax,
            FSalesOrder.tax_rate,
        )
        .join(SysImportBatch, SysImportBatch.id == FSalesOrder.import_batch_id)
        .where(
            FSalesOrder.order_no == contract_no,
            FSalesOrder.data_status == config.ACTIVE_STATUS,
            FSalesOrder.amount_ex_tax.is_not(None),
            SysImportBatch.file_type == "sales",
            SysImportBatch.status == "success",
        )
        .order_by(
            SysImportBatch.uploaded_at.desc().nullslast(),
            FSalesOrder.created_at.desc().nullslast(),
            FSalesOrder.import_batch_id.desc(),
            FSalesOrder.id.desc(),
        )
        .with_for_update(of=FSalesOrder)
    ).all())
    economic_candidates = {
        (
            Decimal(str(row.amount_ex_tax)),
            Decimal(str(row.tax_rate)) if row.tax_rate is not None else None,
        )
        for row in sales_candidates
    }
    if len(economic_candidates) > 1:
        raise WorkbookError(
            "sales_order_ambiguous",
            f"销售订单 {contract_no} 存在多条有效且金额/税率冲突的成功导入记录，"
            "无法自动建立合同关系，请先完成销售订单消歧。",
        )
    sale = sales_candidates[0] if sales_candidates else None
    amount_ex_tax = (
        Decimal(str(sale.amount_ex_tax)) if sale is not None else None
    )
    tax_rate = (
        Decimal(str(sale.tax_rate))
        if sale is not None and sale.tax_rate is not None else None
    )
    inc_tax = (
        amount_ex_tax * (Decimal("1") + tax_rate)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) \
        if amount_ex_tax is not None and tax_rate is not None else None
    contract = MaintenanceProjectContract(
        project_contract_id=str(uuid4()),
        project_id=project.project_id,
        contract_id=contract_id,
        contract_no=contract_no,
        contract_amount=amount_ex_tax,
        amount_inc_tax=inc_tax,
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="workbook-v2-xsdd",
        included_in_total=True,
        effective_from=max(project.period_from, today),
        source="sales_fallback",
        version=1,
    )
    db.add(contract)
    db.flush()
    _write_audit(
        db,
        project_id=project.project_id,
        entity_type="project_contract",
        entity_id=contract.project_contract_id,
        action="CREATE",
        operated_by=operated_by,
        reason="项目总表应用时按当前 XSDD 建立合同关系",
        after={"contract_no": contract_no, "source": "sales_fallback"},
    )
    return contract, True


def _cascade_void_site_lines(
    db: Session,
    line: FMaintenanceLine,
    *,
    project_id: str,
) -> int:
    """03 行作废时，按 source_line_id 文本匹配级联作废 06 领用返还行
    （06↔03 无 FK，best-effort，匹配不到不报错）。返回级联条数。"""
    if not line.raw_line_id:
        return 0
    site_lines = db.scalars(
        select(MaintenanceSiteIssueLine)
        .join(MaintenanceSiteIssue,
              MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id)
        .where(MaintenanceSiteIssue.project_id == project_id,
               MaintenanceSiteIssueLine.source_line_id == line.raw_line_id,
               MaintenanceSiteIssueLine.is_active.is_(True))
    ).all()
    for sl in site_lines:
        sl.is_active = False
    return len(site_lines)


def _v2_allocate_site_display_no(db: Session, issue_date: date) -> str:
    """分配人类可读展示号 LY-YYYYMMDD-NNNN（决策 0006，仅在 apply 事务内调用）。

    按日计数器行上的 INSERT .. ON CONFLICT DO UPDATE .. RETURNING 原子递增，
    行锁 + display_no 部分唯一索引共同保证不重号；分配与领用单 INSERT 同事务，
    整本回滚时一并撤销。apply 全程持有 DATA_CHANGE_ADVISORY_LOCK_KEY，天然串行；
    将来若有其他分配路径，计数器行锁同样兜底。display_no 分配后永不变更。
    """
    stmt = pg_insert(MaintenanceSiteIssueDisplayCounter).values(
        issue_date=issue_date, last_sequence=1,
    ).on_conflict_do_update(
        index_elements=[MaintenanceSiteIssueDisplayCounter.issue_date],
        set_={"last_sequence": MaintenanceSiteIssueDisplayCounter.last_sequence + 1},
    ).returning(MaintenanceSiteIssueDisplayCounter.last_sequence)
    sequence = db.execute(stmt).scalar_one()
    return f"LY-{issue_date:%Y%m%d}-{sequence:04d}"
