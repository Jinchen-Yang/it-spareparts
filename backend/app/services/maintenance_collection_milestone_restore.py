"""受控恢复已作废的回款计划节点。

该服务刻意独立于项目总表解析器：恢复是管理员级运维修复，不扩大普通
工作簿上传账号的权限。调用方必须提供当前版本和 Excel 中的计划事实；
服务只切换 ``is_active``，任何事实漂移都会整批失败关闭。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.models.maintenance_manager import MaintenanceCollectionMilestone
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.services import maintenance_project_operations as operations


class MilestoneRestoreError(Exception):
    """恢复错误基类。"""


class MilestoneRestoreInvalid(MilestoneRestoreError):
    """请求本身无效。"""


class MilestoneRestoreNotFound(MilestoneRestoreError):
    """目标项目或节点不存在。"""


class MilestoneRestoreConflict(MilestoneRestoreError):
    """生产事实或 OCC 版本已经变化。"""


@dataclass(frozen=True)
class MilestoneRestoreSpec:
    entity_id: str
    expected_version: int
    contract_no: str
    sequence: int
    planned_date: date
    planned_amount: Decimal
    date_precision: str


def _facts(milestone: MaintenanceCollectionMilestone) -> dict:
    return {
        "is_active": milestone.is_active,
        "version": milestone.version,
        "project_contract_id": milestone.project_contract_id,
        "sequence": milestone.sequence,
        "planned_date": milestone.planned_date.isoformat()
        if milestone.planned_date else None,
        "planned_amount": str(milestone.planned_amount)
        if milestone.planned_amount is not None else None,
        "date_precision": milestone.date_precision,
    }


def restore_collection_milestones(
    db: Session,
    *,
    project_id: str,
    specs: list[MilestoneRestoreSpec],
    reason: str,
    operated_by: str,
) -> dict:
    """整批恢复已作废节点；任一校验失败则由调用方回滚整个事务。"""

    normalized_reason = reason.strip()
    if len(normalized_reason) < 4 or len(normalized_reason) > 500:
        raise MilestoneRestoreInvalid("恢复理由长度必须为 4-500 个字符")
    if not specs or len(specs) > 24:
        raise MilestoneRestoreInvalid("每次必须恢复 1-24 条回款计划")
    specs = [
        MilestoneRestoreSpec(
            entity_id=spec.entity_id.strip(),
            expected_version=spec.expected_version,
            contract_no=spec.contract_no.strip(),
            sequence=spec.sequence,
            planned_date=spec.planned_date,
            planned_amount=spec.planned_amount,
            date_precision=spec.date_precision,
        )
        for spec in specs
    ]
    entity_ids = [spec.entity_id for spec in specs]
    if any(not value or len(value) > 36 for value in entity_ids):
        raise MilestoneRestoreInvalid("回款计划实体ID无效")
    if len(set(entity_ids)) != len(entity_ids):
        raise MilestoneRestoreInvalid("同一回款计划不能重复提交")
    coordinates = [(spec.contract_no, spec.sequence) for spec in specs]
    if len(set(coordinates)) != len(coordinates):
        raise MilestoneRestoreInvalid("同一合同期次不能重复提交")
    if any(spec.expected_version < 1 for spec in specs):
        raise MilestoneRestoreInvalid("基础版本必须为正整数")
    if any(not 1 <= spec.sequence <= 24 for spec in specs):
        raise MilestoneRestoreInvalid("回款期次必须为 1-24")
    if any(spec.date_precision not in {"day", "month"} for spec in specs):
        raise MilestoneRestoreInvalid("日期精度只能为 day 或 month")
    if any(spec.planned_amount <= 0 for spec in specs):
        raise MilestoneRestoreInvalid("计划回款金额必须为正数")

    # 与项目总表 apply 使用同一锁顺序：全局数据锁 -> workbook state ->
    # project -> contract -> milestone。恢复完成后 bump revision，使所有在途旧
    # Excel 立即 stale，避免缺行=作废再次把刚恢复的节点删掉。
    db.execute(
        select(func.pg_advisory_xact_lock(config.DATA_CHANGE_ADVISORY_LOCK_KEY))
    )
    workbook_state = operations.lock_workbook_states(
        db, project_ids=[project_id]
    )[project_id]
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None or not project.is_active:
        raise MilestoneRestoreNotFound("项目不存在、已归档或不可恢复")

    contract_nos = sorted({spec.contract_no.strip() for spec in specs})
    if any(not value for value in contract_nos):
        raise MilestoneRestoreInvalid("合同编号不能为空")
    contracts = list(db.scalars(
        select(MaintenanceProjectContract)
        .where(
            MaintenanceProjectContract.project_id == project_id,
            MaintenanceProjectContract.contract_no.in_(contract_nos),
            MaintenanceProjectContract.effective_to.is_(None),
        )
        .order_by(MaintenanceProjectContract.project_contract_id)
        .with_for_update()
    ))
    contract_by_no = {contract.contract_no: contract for contract in contracts}
    if set(contract_by_no) != set(contract_nos):
        raise MilestoneRestoreConflict("合同关系已经变化，请重新核对生产数据")

    milestones = list(db.scalars(
        select(MaintenanceCollectionMilestone)
        .where(MaintenanceCollectionMilestone.milestone_id.in_(sorted(entity_ids)))
        .order_by(MaintenanceCollectionMilestone.milestone_id)
        .with_for_update()
    ))
    milestone_by_id = {row.milestone_id: row for row in milestones}
    if set(milestone_by_id) != set(entity_ids):
        raise MilestoneRestoreNotFound("部分回款计划不存在，整批未恢复")

    restore_audits = list(db.scalars(
        select(MaintenanceProjectOperationAudit).where(
            MaintenanceProjectOperationAudit.project_id == project_id,
            MaintenanceProjectOperationAudit.entity_type == "collection_milestone",
            MaintenanceProjectOperationAudit.entity_id.in_(entity_ids),
            MaintenanceProjectOperationAudit.action == "RESTORE",
        )
    ))
    audits_by_entity: dict[str, list[MaintenanceProjectOperationAudit]] = {}
    for audit in restore_audits:
        audits_by_entity.setdefault(audit.entity_id, []).append(audit)

    restored: list[dict] = []
    replayed: list[dict] = []
    for spec in specs:
        milestone = milestone_by_id[spec.entity_id]
        contract = contract_by_no[spec.contract_no]
        expected_facts = (
            project_id,
            contract.project_contract_id,
            spec.sequence,
            spec.planned_date,
            Decimal(spec.planned_amount),
            spec.date_precision,
        )
        current_facts = (
            milestone.project_id,
            milestone.project_contract_id,
            milestone.sequence,
            milestone.planned_date,
            Decimal(milestone.planned_amount)
            if milestone.planned_amount is not None else None,
            milestone.date_precision,
        )
        if current_facts != expected_facts:
            raise MilestoneRestoreConflict(
                f"合同 {spec.contract_no} 第 {spec.sequence} 期与 Excel 不一致，整批未恢复"
            )

        # 安全重放：除了 active、事实一致和 version=N+1，还必须有本接口
        # 留下的 N->N+1 RESTORE 审计。这样不会把一个原本就有效的节点误报为
        # “恢复成功”。相同请求再次到达时返回 no-op，不重复 bump 或写审计。
        if milestone.is_active:
            if milestone.version != spec.expected_version + 1:
                raise MilestoneRestoreConflict(
                    f"合同 {spec.contract_no} 第 {spec.sequence} 期已被其他操作更新"
                )
            current_snapshot = _facts(milestone)
            has_restore_receipt = any(
                audit.before_json is not None
                and audit.after_json is not None
                and audit.before_json.get("is_active") is False
                and audit.before_json.get("version") == spec.expected_version
                and audit.after_json == current_snapshot
                for audit in audits_by_entity.get(milestone.milestone_id, [])
            )
            if not has_restore_receipt:
                raise MilestoneRestoreConflict(
                    f"合同 {spec.contract_no} 第 {spec.sequence} 期已有效，"
                    "但没有可验证的恢复审计"
                )
            replayed.append({
                "entity_id": milestone.milestone_id,
                "sequence": milestone.sequence,
                "version": milestone.version,
            })
            continue
        if milestone.version != spec.expected_version:
            raise MilestoneRestoreConflict(
                f"合同 {spec.contract_no} 第 {spec.sequence} 期版本已变化"
            )

        before = _facts(milestone)
        milestone.is_active = True
        milestone.version += 1
        after = _facts(milestone)
        db.add(MaintenanceProjectOperationAudit(
            project_id=project_id,
            entity_type="collection_milestone",
            entity_id=milestone.milestone_id,
            action="RESTORE",
            before_json=before,
            after_json=after,
            reason=normalized_reason,
            operated_by=operated_by,
        ))
        restored.append({
            "entity_id": milestone.milestone_id,
            "sequence": milestone.sequence,
            "version": milestone.version,
        })

    if restored:
        operations.bump_locked_workbook_revision(db, state=workbook_state)
    db.flush()
    return {
        "project_id": project_id,
        "restored_count": len(restored),
        "idempotent_replay_count": len(replayed),
        "restored": restored,
        "replayed": replayed,
        "workbook_revision": workbook_state.revision,
    }
