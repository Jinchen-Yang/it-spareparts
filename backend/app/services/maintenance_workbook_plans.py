"""Workbook validation values and the shared validation exception.

These are the current protocol plans, not transaction or authorization objects.
Frozen dataclasses retain their existing mutable nested containers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.maintenance_workbook_protocol import ALL_SHEETS


class WorkbookError(ValueError):
    """回传给 API 层映射 422 的用户可读校验错误。"""

    def __init__(
        self, code: str, message: str, issues: list[dict] | None = None,
        *, details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []
        self.details = details or {}


@dataclass(frozen=True)
class ExpenseUpdate:
    raw_line_id: str
    is_create: bool = False
    bxd_no: str | None = None
    line_no: int | None = None
    # 2026-08-17 全面放开：所有事实列可改后回传覆盖
    expense_date: date | None = None
    person: str | None = None
    expense_type: str | None = None
    fee_category: str | None = None
    reason: str | None = None
    contract_no: str | None = None
    amount_ex_tax: Decimal | None = None
    amount_inc_tax: Decimal | None = None
    # Source amount basis is read-only in workbooks but must be preserved when
    # another field or the displayed ex-tax amount is updated.
    tax_basis: str | None = None
    data_status: str | None = None
    remark: str | None = None


@dataclass(frozen=True)
class CollectionOp:
    operation: str                       # CREATE | VOID | UPDATE
    project_contract_id: str
    contract_no: str
    report_month: date
    cumulative_amount: Decimal | None    # VOID 时为 None
    receipt_reference: str | None
    remark: str | None
    # 2026-08-17 全面放开：非 VOID 时可覆盖状态
    collection_status: str | None = None


@dataclass(frozen=True)
class WorkbookPlan:
    project_id: str
    expense_updates: tuple[ExpenseUpdate, ...]
    collection_ops: tuple[CollectionOp, ...]

    @property
    def summary(self) -> dict:
        creates = sum(1 for op in self.collection_ops if op.operation == "CREATE")
        updates = sum(1 for op in self.collection_ops if op.operation == "UPDATE")
        voids = sum(1 for op in self.collection_ops if op.operation == "VOID")
        return {
            "expense_creates": sum(item.is_create for item in self.expense_updates),
            "expense_updates": sum(not item.is_create for item in self.expense_updates),
            "collection_creates": creates,
            "collection_updates": updates,
            "collection_voids": voids,
        }


@dataclass(frozen=True)
class CostRefill:
    line_id: int | None
    unit_cost_ex_tax: Decimal | None
    unit_cost_inc_tax: Decimal | None
    reason: str | None
    note: str | None = None
    # 2026-08-19 全字段可编辑（03，#264/#267）：UPDATE/VOID 作用于既有行；
    # CREATE 新增行时 line_id 为 None，order_no+pn+qty 必填。
    operation: str = "UPDATE"
    pn: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    return_qty: Decimal | None = None
    serial_numbers: str | None = None
    order_no: str | None = None
    is_create: bool = False
    order_id: int | None = None
    part_id: int | None = None
    # Global workbook optimistic-concurrency snapshot.  ``None`` version means
    # no override existed at export; the signed token distinguishes that from
    # older workbooks which carried no snapshot at all.
    base_override_version: int | None = None
    base_override_active: bool | None = None
    base_unit_cost_ex_tax: Decimal | None = None
    base_unit_cost_inc_tax: Decimal | None = None
    base_reason: str | None = None


@dataclass(frozen=True)
class SourceOrderAssignmentChange:
    """项目内人工回传确认的一张 WBDD 归属更正。

    ``expected_*`` 把 validate 时看到的活跃挂靠版本带到 apply，避免校验后有人
    已经改挂而本次上传又把新结果覆盖掉。旧挂靠不会删除；正式 apply 复用来源单
    归属服务，生成不可变历史和双边审计。
    """

    source_order_id: str
    order_no: str
    expected_assignment_id: str | None
    expected_version: int | None
    previous_project_id: str | None
    previous_project_name: str | None


@dataclass(frozen=True)
class SiteReturnFlag:
    issue_line_id: str
    no_return: bool | None
    is_create: bool = False
    # 2026-08-23：06 缺行=作废（与 03/04 同语义）——删除导出中存在的领用行
    is_void: bool = False
    issue_id: str | None = None
    line_no: int | None = None
    part_id: int | None = None
    # 2026-08-17 全面放开：可回传覆盖的领用事实
    issue_no: str | None = None
    issue_date: date | None = None
    demand_order_no: str | None = None
    pn: str | None = None
    serial_number: str | None = None
    quantity: Decimal | None = None
    remark: str | None = None
    site_location_action: str = "keep"
    site_location: str | None = None
    # Excel 来源行；新增明细若附加到既有单头，issue_id 也须明确绑定。
    row_no: int | None = None


@dataclass(frozen=True)
class MasterPlan:
    project_id: str | None
    cost_refills: tuple[CostRefill, ...] = ()
    site_flags: tuple[SiteReturnFlag, ...] = ()
    inner: WorkbookPlan | None = None      # 04/05 复用 AB-3 的计划
    sheets: tuple[str, ...] = field(default=ALL_SHEETS)

    @property
    def summary(self) -> dict:
        out = {"cost_refills": len(self.cost_refills),
               "site_return_flags": len(self.site_flags),
               "expense_updates": 0, "collection_creates": 0,
               "collection_voids": 0}
        if self.inner is not None:
            out.update(self.inner.summary)
        return out


@dataclass(frozen=True)
class V2MilestoneChange:
    operation: str
    contract_no: str
    sequence: int
    planned_date: date | None
    date_precision: str
    planned_amount: Decimal | None
    entity_id: str | None
    base_version: int | None


@dataclass(frozen=True)
class ContractAmountChange:
    """01 项目概览中唯一生效合同的含税金额变更。"""

    project_contract_id: str
    base_version: int
    before_amount_inc_tax: Decimal | None
    amount_inc_tax: Decimal


@dataclass(frozen=True)
class ProjectNoteChange:
    """Signed 01 intent; live master CAS happens under the apply locks."""

    base_version: int
    before: str | None
    value: str | None


@dataclass(frozen=True)
class MasterV2Plan:
    project_id: str
    sheets: tuple[str, ...]
    export_id: str = ""
    file_sha256: str = ""
    expected_workbook_revision: int | None = None
    expected_workbook_data_version: str = ""
    cost_refills: tuple[CostRefill, ...] = ()
    site_flags: tuple[SiteReturnFlag, ...] = ()
    site_issue_merges: tuple[dict, ...] = ()
    site_header_intents: tuple[dict, ...] = ()
    site_issue_rows: dict[str, list[int]] = field(default_factory=dict)
    expense_updates: tuple[ExpenseUpdate, ...] = ()
    receipt_ops: tuple[CollectionOp, ...] = ()
    milestone_changes: tuple[V2MilestoneChange, ...] = ()
    assignment_changes: tuple[SourceOrderAssignmentChange, ...] = ()
    contract_amount_change: ContractAmountChange | None = None
    project_note_change: ProjectNoteChange | None = None
    # 04 报销作废（#264/#267 契约）：显式 VOID 操作列 + 缺行=作废。
    expense_voids: tuple[str, ...] = ()
    will_void_rows: tuple[dict, ...] = ()
    # 2.7.0 行级三路合并产物：字段级变更/冲突/接管明细。
    row_guards: tuple[V2RowGuard, ...] = ()
    force_takeover: bool = False
    field_changes: tuple[dict, ...] = ()
    conflicts: tuple[dict, ...] = ()
    overridden: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    # D-02 作废优先：落在已作废行/单上的改动，行级列明「已被谁何时作废，修改未生效」。
    voided_rows: tuple[dict, ...] = ()

    @property
    def summary(self) -> dict:
        updates = [x for x in self.cost_refills if not x.is_create and x.operation != "VOID"]
        return {
            # E2E #5：改数量不再被误报为 cost_overrides——行更新与成本覆盖分开计
            "line_updates": len(updates),
            "qty_updates": sum(x.qty is not None or x.return_qty is not None for x in updates),
            "cost_overrides": sum(
                x.unit_cost_ex_tax is not None or x.reason is not None for x in updates),
            "line_creates": sum(x.is_create for x in self.cost_refills),
            "line_voids": sum(x.operation == "VOID" for x in self.cost_refills),
            "order_reassignments": len(self.assignment_changes),
            "contract_updates": 1 if self.contract_amount_change is not None else 0,
            "project_note_updates": 1 if self.project_note_change is not None else 0,
            "expense_creates": sum(getattr(x, "is_create", False) for x in self.expense_updates),
            "expense_updates": sum(not getattr(x, "is_create", False) for x in self.expense_updates),
            "expense_voids": len(self.expense_voids),
            "plan_creates": sum(x.operation == "CREATE" for x in self.milestone_changes),
            "plan_updates": sum(x.operation == "UPDATE" for x in self.milestone_changes),
            "plan_voids": sum(x.operation == "VOID" for x in self.milestone_changes),
            "collection_updates": sum(
                operation.operation != "VOID" for operation in self.receipt_ops
            ),
            "collection_voids": sum(
                operation.operation == "VOID" for operation in self.receipt_ops
            ),
            "site_creates": sum(x.is_create for x in self.site_flags),
            "site_voids": sum(x.is_void for x in self.site_flags),
            "site_updates": sum(not x.is_create and not x.is_void
                                for x in self.site_flags),
            "site_issue_merges": len(self.site_issue_merges),
        }


@dataclass(frozen=True)
class V2RowGuard:
    """validate 时刻的服务端行指纹（只覆盖该 sheet 的基线字段）。

    2.7.0 的冲突判定是 `server_value != baseline`，而它在 validate 里、
    在 apply 的任何一把锁之外算完。指纹把「我算这个判定时读到的服务端行」
    钉下来，apply 拿到行锁之后重算一次；不等就说明判定所依据的服务端行在
    validate→apply 窗口内被人改过 —— 整本 fail-closed，一个字节都不写。

    只为**用户真正触碰过的行**采集：未触碰行本来就不写、也不在
    _v2_assert_entity_scope 的加锁集合里，给它们发指纹会让「项目里任何一行
    被人改过就整本 409」，那正是 2.7.0 明确废掉的整本硬拒。
    """

    sheet: str
    entity_id: str
    row_label: str
    digest: str
    fields: tuple[str, ...] = ()
    entity_type: str = "line"


@dataclass(frozen=True)
class _SiteAddressPatch:
    action: str = "keep"
    value: str | None = None
