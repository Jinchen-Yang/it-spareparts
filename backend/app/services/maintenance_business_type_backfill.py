"""存量项目业务类型回填：从销售订单推，唯一才补，冲突交人工（2026-09-08）。

背景：生产 648 个项目里 647 个 ``maintenance_project.business_type`` 为空——这个字段
此前只有台账导入会写，而台账尚未进生产；XSDD 建项虽然解析出了业务类型却没往下传
（已在同批修复）。于是卡墙的业务类型筛选一个项目也筛不出来。写入侧修好只管新项目，
存量 647 个手工补录不现实，需要一次可审计的回填。

推导链沿用卡片「销售」与维保负责人回填的同一形状（``salesperson_modes_by_project``）：

    项目 → 活跃挂靠的 WBDD 单 → linked_sales_order_no → f_sales_order.business_type

三条铁律：

1. **只填空**：``business_type`` 已有值的项目一律不进候选——人工补录过的、台账写过的
   都不许被自动推导盖掉（与 ``backfill_owner_fields`` 同口径）。
2. **唯一才填**：名下推出两种及以上业务类型 ⇒ 不猜，列进 ``conflicts`` 交人工。多合同
   项目本来就可能横跨两种业务，「业务类型只作分类、不作判定依据」的口径下，替它选一个
   就是替它编事实。
3. **先预览后应用**：``preview()`` 只读、不写库；``apply()`` 逐项走
   ``catalog.update_project`` ——版本 +1、审计留痕改前改后，与人工补录同一条写路径，
   不另开一个绕过审计的后门。

推不出来的项目（没有活跃挂靠单、或挂靠单的销售订单没填业务类型）如实列进
``underivable``：回填不是万能的，剩下多少要让人看见，别让人以为跑一次就包圆了。
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.maintenance import FMaintenanceOrder
from app.models.sales import FSalesOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment


def _blank_business_type():
    return or_(
        MaintenanceProject.business_type.is_(None),
        func.btrim(MaintenanceProject.business_type) == "",
    )


def candidate_project_ids(db: Session) -> list[str]:
    """待回填候选：主档有效、业务类型为空。只填空，绝不覆盖。"""

    return list(
        db.scalars(
            select(MaintenanceProject.project_id)
            .where(
                MaintenanceProject.is_active.is_(True),
                _blank_business_type(),
            )
            .order_by(MaintenanceProject.project_id)
        )
    )


def derive_by_project(
    db: Session, project_ids: list[str]
) -> dict[str, dict[str, list[str]]]:
    """{project_id: {业务类型: [证据 XSDD 单号...]}}，一次查询覆盖整批。

    只认**活跃**挂靠（``is_active``）与**非空**业务类型：失效挂靠不是当前事实，
    空业务类型推不出东西。
    """

    if not project_ids:
        return {}
    rows = db.execute(
        select(
            MaintenanceSourceOrderAssignment.project_id,
            func.btrim(FSalesOrder.business_type),
            FSalesOrder.order_no,
        )
        .select_from(FMaintenanceOrder)
        .join(
            MaintenanceSourceOrderAssignment,
            and_(
                MaintenanceSourceOrderAssignment.source_order_id
                == FMaintenanceOrder.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            ),
        )
        .join(
            FSalesOrder,
            FSalesOrder.order_no == FMaintenanceOrder.linked_sales_order_no,
        )
        .where(
            MaintenanceSourceOrderAssignment.project_id.in_(project_ids),
            FSalesOrder.business_type.is_not(None),
            func.btrim(FSalesOrder.business_type) != "",
        )
        .group_by(
            MaintenanceSourceOrderAssignment.project_id,
            func.btrim(FSalesOrder.business_type),
            FSalesOrder.order_no,
        )
    ).all()

    derived: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for project_id, business_type, order_no in rows:
        derived[project_id][business_type].append(order_no)
    return {
        project_id: {
            business_type: sorted(order_nos)
            for business_type, order_nos in sorted(by_type.items())
        }
        for project_id, by_type in derived.items()
    }


def preview(db: Session) -> dict:
    """只读：这一轮回填会填哪些、哪些冲突、哪些推不出来。不写库。"""

    candidates = candidate_project_ids(db)
    derived = derive_by_project(db, candidates)
    names = dict(
        db.execute(
            select(MaintenanceProject.project_id, MaintenanceProject.display_name)
            .where(MaintenanceProject.project_id.in_(candidates or [""]))
        ).all()
    )

    fillable: list[dict] = []
    conflicts: list[dict] = []
    underivable: list[dict] = []
    for project_id in candidates:
        by_type = derived.get(project_id) or {}
        row = {"project_id": project_id, "display_name": names.get(project_id)}
        if not by_type:
            underivable.append(row)
        elif len(by_type) == 1:
            business_type, order_nos = next(iter(by_type.items()))
            fillable.append({
                **row,
                "business_type": business_type,
                "evidence_order_nos": order_nos,
            })
        else:
            conflicts.append({
                **row,
                "business_types": sorted(by_type),
                "evidence": {
                    business_type: order_nos
                    for business_type, order_nos in by_type.items()
                },
            })
    return {
        "candidates": len(candidates),
        "fillable": fillable,
        "conflicts": conflicts,
        "underivable": underivable,
    }


def apply(db: Session, *, operated_by: str, reason: str) -> dict:
    """把 preview 里 fillable 的那批写进去；冲突与推不出来的一个字都不动。

    逐项走 ``catalog.update_project``：版本 +1、审计留痕改前改后，与人工补录完全
    同一条写路径。写入前重新预览一次——不接受调用方传进来的计划，避免「预览时能填、
    应用时已被人工补过」这种把人工改动盖掉的窗口。
    """

    from app.services import maintenance_project_catalog as catalog

    plan = preview(db)
    filled = 0
    for row in plan["fillable"]:
        project = db.get(MaintenanceProject, row["project_id"])
        if project is None or not project.is_active:
            continue
        # 双保险：候选条件已经过滤过，这里再确认一次「确实是空的」再写。
        if (project.business_type or "").strip():
            continue
        catalog.update_project(
            db,
            project_id=project.project_id,
            version=project.version,
            updates={"business_type": row["business_type"]},
            reason=(
                f"{reason}｜按销售订单业务类型回填，证据："
                f"{'、'.join(row['evidence_order_nos'][:5])}"
            ),
            operated_by=operated_by,
        )
        filled += 1
    db.flush()
    return {
        "candidates": plan["candidates"],
        "filled": filled,
        "conflicts": len(plan["conflicts"]),
        "underivable": len(plan["underivable"]),
    }
