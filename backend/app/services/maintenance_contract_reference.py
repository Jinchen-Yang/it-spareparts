"""批量读取项目当前合同参考事实，供不同展示入口各自投影。

调用者负责项目授权和金额资格；结果可含诊断小计，不是可直接下发的 DTO。
共享检测必须读取全局关联，但结果只包含调用者传入的项目。
本模块保留既有台账及销售回退行为，不决定各入口的完整金额展示规则。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TypedDict

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app import tax_policy
from app.business_time import business_today
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services.query_filters import active_orders


class ContractReference(TypedDict):
    contract_nos: list[str]
    amount_inc_tax: Decimal | None
    contract_shared: bool
    contract_incomplete: bool


def current_contract_references(
    db: Session, project_ids: list[str]
) -> dict[str, ContractReference]:
    """批量返回既有实现的当前合同参考，保持台账优先及 XSDD 回退行为。

    当前台账存在时不启用销售回退；否则读取活跃挂靠的 XSDD 销售事实。
    共享检测包含本次请求之外的项目，返回值仅含请求项目中有参考来源者。
    amount_inc_tax 可能是诊断小计；调用者仍须结合共享/不完整标记决定展示。
    本次抽取兼容现有缺值、零值和日期行为，不构成对历史业务口径的重新确认。
    """
    from app import config
    from app.models.maintenance_project import MaintenanceProjectContract
    from app.models.sales import FSalesOrder
    from app.models.system import SysImportBatch

    if not project_ids:
        return {}
    rows = db.execute(
        select(
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.contract_id,
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.amount_inc_tax,
            MaintenanceProjectContract.included_in_total,
            MaintenanceProjectContract.status_mapping_state,
        )
        .where(
            MaintenanceProjectContract.project_id.in_(project_ids),
            MaintenanceProjectContract.effective_from <= business_today(),
            or_(
                MaintenanceProjectContract.effective_to.is_(None),
                MaintenanceProjectContract.effective_to > business_today(),
            ),
        )
        .order_by(MaintenanceProjectContract.effective_from)
    ).all()
    included_contract_ids = {
        contract_id
        for _pid, contract_id, _no, _amount, included, _mapping in rows
        if included
    }
    included_contract_nos = {
        no
        for _pid, _contract_id, no, _amount, included, _mapping in rows
        if included and no
    }
    projects_by_contract_id: dict[str, set[str]] = {}
    projects_by_contract_no: dict[str, set[str]] = {}
    if included_contract_ids or included_contract_nos:
        for contract_id, contract_no, related_project_id in db.execute(
            select(
                MaintenanceProjectContract.contract_id,
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.project_id,
            )
            .where(
                or_(
                    MaintenanceProjectContract.contract_id.in_(included_contract_ids),
                    MaintenanceProjectContract.contract_no.in_(included_contract_nos),
                ),
                MaintenanceProjectContract.included_in_total.is_(True),
                MaintenanceProjectContract.effective_from <= business_today(),
                or_(
                    MaintenanceProjectContract.effective_to.is_(None),
                    MaintenanceProjectContract.effective_to > business_today(),
                ),
            )
            .group_by(
                MaintenanceProjectContract.contract_id,
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.project_id,
            )
        ):
            projects_by_contract_id.setdefault(contract_id, set()).add(
                related_project_id
            )
            if contract_no:
                projects_by_contract_no.setdefault(contract_no, set()).add(
                    related_project_id
                )
    conflicting_contract_ids = {
        contract_id
        for contract_id, related_projects in projects_by_contract_id.items()
        if len(related_projects) > 1
    }
    conflicting_contract_nos = {
        contract_no
        for contract_no, related_projects in projects_by_contract_no.items()
        if len(related_projects) > 1
    }
    out: dict[str, Any] = {}
    for pid, contract_id, no, amount, included, mapping_state in rows:
        bucket = out.setdefault(
            pid,
            {
                "contract_nos": [],
                "amount_inc_tax": None,
                "contract_shared": False,
                "contract_incomplete": False,
                "_included_contract_ids": [],
                "_included_contract_nos": [],
                "_included_count": 0,
            },
        )
        if no and no not in bucket["contract_nos"]:
            bucket["contract_nos"].append(no)
        if mapping_state != "mapped":
            bucket["contract_incomplete"] = True
        elif included:
            bucket["_included_count"] += 1
            bucket["_included_contract_ids"].append(contract_id)
            bucket["_included_contract_nos"].append(no)
            if amount is not None:
                bucket["amount_inc_tax"] = (
                    bucket["amount_inc_tax"] or Decimal(0)
                ) + amount
            else:
                bucket["contract_incomplete"] = True

    for bucket in out.values():
        contract_ids = bucket.pop("_included_contract_ids")
        contract_nos = bucket.pop("_included_contract_nos")
        included_count = bucket.pop("_included_count")
        duplicate = len(contract_ids) != len(set(contract_ids)) or len(
            contract_nos
        ) != len(set(contract_nos))
        shared = any(
            contract_id in conflicting_contract_ids for contract_id in contract_ids
        ) or any(
            contract_no in conflicting_contract_nos for contract_no in contract_nos
        )
        if included_count == 0 or duplicate or shared:
            bucket["contract_incomplete"] = True
        if duplicate or shared:
            # 重复或跨项目冲突时连“已知小计”都可能双计，不返回伪精确金额。
            bucket["amount_inc_tax"] = None
        bucket["contract_shared"] = shared

    # —— XSDD 回退：只对「台账没给出金额」的项目生效，台账永远优先 ——
    # 只有“完全没有当前合同行”的项目才走销售事实回退。已有合同行但含税额
    # 缺失时必须诚实标 incomplete，不能用 13%/0% 猜测覆盖权威事实。
    fallback_ids = [pid for pid in project_ids if pid not in out]
    if not fallback_ids:
        return out
    active = and_(
        MaintenanceSourceOrderAssignment.source_order_id
        == FMaintenanceOrder.raw_order_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True),
    )
    # 全量「XSDD→挂了哪些项目」映射：shared 判定必须看全局（不止本页），
    # 否则翻页会把共用单误标成独占。
    xsdd_rows = db.execute(
        active_orders(
            select(
                MaintenanceSourceOrderAssignment.project_id,
                FMaintenanceOrder.linked_sales_order_no,
            )
            .select_from(FMaintenanceOrder)
            .join(MaintenanceSourceOrderAssignment, active)
            .where(FMaintenanceOrder.linked_sales_order_no.is_not(None))
            .group_by(
                MaintenanceSourceOrderAssignment.project_id,
                FMaintenanceOrder.linked_sales_order_no,
            ),
            FMaintenanceOrder,
        )
    ).all()
    order_projects: dict[str, set] = {}
    project_orders: dict[str, list] = {}
    for pid, ono in xsdd_rows:
        order_projects.setdefault(ono, set()).add(pid)
        if pid in fallback_ids:
            project_orders.setdefault(pid, []).append(ono)
    all_orders = sorted(
        {o for pid in fallback_ids for o in project_orders.get(pid, [])}
    )
    amounts: dict[str, Decimal] = {}
    ambiguous_orders: set[str] = set()
    if all_orders:
        # One batched evidence query for every fallback project.  Failed or
        # wrong-type import batches are not facts, and ACTIVE_STATUS_ONLY must
        # not weaken this financial boundary when disabled elsewhere.
        candidates = db.execute(
            select(
                FSalesOrder.order_no,
                FSalesOrder.amount_ex_tax,
                FSalesOrder.tax_rate,
            )
            .join(
                SysImportBatch,
                SysImportBatch.id == FSalesOrder.import_batch_id,
            )
            .where(
                FSalesOrder.order_no.in_(all_orders),
                FSalesOrder.data_status == config.ACTIVE_STATUS,
                FSalesOrder.amount_ex_tax.is_not(None),
                FSalesOrder.tax_rate.is_not(None),
                SysImportBatch.file_type == "sales",
                SysImportBatch.status == "success",
            )
            .order_by(
                FSalesOrder.order_no,
                SysImportBatch.uploaded_at,
                FSalesOrder.created_at,
                SysImportBatch.id,
                FSalesOrder.id,
            )
        ).all()
        economics_by_order: dict[str, set[tuple[Decimal, Decimal, Decimal]]] = {}
        for order_no, amount_ex_tax, tax_rate in candidates:
            ex_tax = Decimal(str(amount_ex_tax))
            rate = Decimal(str(tax_rate))
            inc_tax = tax_policy.round_money(ex_tax * (Decimal("1") + rate))
            economics_by_order.setdefault(order_no, set()).add((ex_tax, rate, inc_tax))
        for order_no, economics in economics_by_order.items():
            if len(economics) != 1:
                ambiguous_orders.add(order_no)
                continue
            amounts[order_no] = next(iter(economics))[2]
    for pid in fallback_ids:
        onos = sorted(project_orders.get(pid, []))
        if not onos:
            continue
        bucket = out.setdefault(
            pid,
            {
                "contract_nos": [],
                "amount_inc_tax": None,
                "contract_shared": False,
                "contract_incomplete": False,
            },
        )
        if not bucket["contract_nos"]:
            bucket["contract_nos"] = onos
        total = sum((amounts.get(o) or Decimal(0)) for o in onos)
        missing = [o for o in onos if o not in amounts]
        conflicts = [o for o in onos if o in ambiguous_orders]
        if conflicts:
            # One ambiguous XSDD invalidates the project total: returning the
            # remaining known subtotal would look like a precise contract cap.
            bucket["amount_inc_tax"] = None
        elif total > 0:
            bucket["amount_inc_tax"] = total
        bucket["contract_shared"] = any(
            len(order_projects.get(o, set())) > 1 for o in onos
        )
        # 共享 XSDD 可保留金额作“参考”，但不能据此给多个项目计算成本率/红黄绿；
        # 否则同一合同总额会被重复当成每个项目的独占预算。
        bucket["contract_incomplete"] = (
            bool(missing) or bool(conflicts) or bucket["contract_shared"]
        )
    return out
