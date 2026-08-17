"""受控计划写 helper：manager workbook 与 XLS apply 共用的唯一写路径（Task 1 Step 1.6）。

规则（设计 §4.1 / §11.1）：
- manager workbook 写 ``date_precision=day``；XLS 写 ``month``（未显式给定时按来源派生）。
- 新建节点初始 ``follow_up_status=pending``、``follow_up_review_required=false``。
- 已处理节点的计划日期/金额被修改时：保留 ``handled`` 与处理人/时间/备注，
  仅置 ``follow_up_review_required=true``。
- 计划事实（日期/金额/完整度）未变化时不增加 version；来源批次字段变化只更新不 bump。
- 本 helper 只写计划事实，绝不删除 source-missing 节点；金额一律走 Decimal，不做浮点运算。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.maintenance_manager import MaintenanceCollectionMilestone

# 来源 → 默认精度：XLS 排期只能表达月份（YYYY年M月），其余来源按具体日期。
_SOURCE_DEFAULT_PRECISION = {
    "project_manager_xls_v1": "month",
}


def write_collection_milestone(
    db: Session,
    *,
    project_id: str,
    project_contract_id: str,
    sequence: int,
    planned_date: date | None,
    planned_amount: Decimal | str | None,
    completeness_state: str,
    source: str,
    source_batch_id: str | None = None,
    collection_plan_import_batch_id: str | None = None,
    ledger_batch_id: str | None = None,
    date_precision: str | None = None,
    operator: str = "system",
) -> MaintenanceCollectionMilestone:
    """按 ``(project_contract_id, sequence)`` 创建或更新一个计划节点并返回。

    - 创建：version=1、pending/false，source 与两个批次 FK 按互斥规则写入。
    - 更新：先比较计划事实；事实未变化不 bump version。修改 handled 节点时
      保留 handled 与处理人/时间/备注并置 ``follow_up_review_required=true``。
    - ``date_precision`` 按来源派生；调用方显式传入与来源默认精度冲突的值
      一律拒绝（P1-4 失败关闭：XLS 只能表达月份，其他来源按具体日期）。
    - ``operator`` 只用于审计签名位；本 helper 不写操作账本（follow-up 账本
      由车道 A 写入），审计由各调用方自行追加。
    """
    expected_precision = _SOURCE_DEFAULT_PRECISION.get(source, "day")
    if date_precision is None:
        date_precision = expected_precision
    elif date_precision != expected_precision:
        raise ValueError(
            f"来源 {source} 的计划精度必须为 {expected_precision}，"
            f"调用方传入 {date_precision} 与之冲突"
        )
    amount = Decimal(str(planned_amount)) if planned_amount is not None else None

    milestone = db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id == project_contract_id,
            MaintenanceCollectionMilestone.sequence == sequence,
        )
    )
    if milestone is None:
        milestone = MaintenanceCollectionMilestone(
            milestone_id=str(uuid4()),
            project_id=project_id,
            project_contract_id=project_contract_id,
            sequence=sequence,
            planned_date=planned_date,
            planned_amount=amount,
            completeness_state=completeness_state,
            source=source,
            source_batch_id=source_batch_id,
            collection_plan_import_batch_id=collection_plan_import_batch_id,
            ledger_batch_id=ledger_batch_id,
            date_precision=date_precision,
            follow_up_status="pending",
            follow_up_review_required=False,
            follow_up_note=None,
            followed_up_by=None,
            followed_up_at=None,
            version=1,
        )
        db.add(milestone)
        db.flush()
        return milestone

    # 语义变化集（P1-4）：计划日期/金额/完整度/精度任一变化都 bump version；
    # 来源与批次字段是引用关系，纯来源/批次变化不改变计划语义，不 bump。
    planned_changed = (
        milestone.planned_date != planned_date
        or milestone.planned_amount != amount
        or milestone.completeness_state != completeness_state
        or milestone.date_precision != date_precision
    )
    source_changed = (
        milestone.source != source
        or milestone.source_batch_id != source_batch_id
        or milestone.collection_plan_import_batch_id != collection_plan_import_batch_id
        or milestone.ledger_batch_id != ledger_batch_id
    )
    if not planned_changed and not source_changed:
        return milestone
    milestone.source = source
    milestone.source_batch_id = source_batch_id
    milestone.collection_plan_import_batch_id = collection_plan_import_batch_id
    milestone.ledger_batch_id = ledger_batch_id
    milestone.date_precision = date_precision
    if planned_changed:
        milestone.planned_date = planned_date
        milestone.planned_amount = amount
        milestone.completeness_state = completeness_state
        if milestone.follow_up_status == "handled":
            # 保留 handled 与处理人/时间/备注，仅标记“计划有变更”待复核。
            milestone.follow_up_review_required = True
        milestone.version += 1
    db.flush()
    return milestone
