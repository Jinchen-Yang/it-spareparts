r"""回款提醒 API v1 DTO（K0 Task 1 Step 1.5）。

逐项镜像 ``.ai/contracts/maintenance-collections/collection-reminders-api-v1.yaml``：
- 所有请求 schema 使用 ``ConfigDict(extra="forbid")``；
- 金额字段只接受十进制定点字符串或 null（``StrictStr`` 拒绝 Pydantic v2 对
  int/float 的宽松 str 转换），形状 ``^[0-9]+(?:\.[0-9]{1,2})?$`` 与 XLS 合同一致；
- follow-up 判别规则：handle 只允许 note；reschedule 必填 planned_month+reason
  且禁 note；reopen 必填 reason 且禁 planned_month+note；
- 错误体：domain_error（code/message/current_version/current_data_version/issues[]）
  与 request_validation_error（detail: loc/msg/type）。
source-file 端点是二进制附件响应，无 JSON DTO，不在此模块。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

# 金额文本：只接受十进制定点字符串（XLS 合同 plan_amount.text_regex）。
DecimalAmount = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]{1,2})?$"),
]
# 计划月份：固定 YYYY-MM，月份 01–12。
YearMonth = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
]

DatePrecision = Literal["day", "month"]
FollowUpStatus = Literal["pending", "handled"]
ReminderState = Literal[
    "needs_review", "handled", "incomplete", "overdue", "due_this_month", "upcoming"
]
FollowUpAction = Literal["handle", "reschedule", "reopen"]
OwnerScope = Literal["me", "all"]
AmountVisibility = Literal["visible", "restricted"]
BatchStatus = Literal["valid", "error", "applied", "expired"]
BindingStatus = Literal["reviewed"]
PreviewBindingStatus = Literal["reviewed", "pending_review"]
MilestoneChangeKind = Literal["create", "update", "unchanged", "source_missing"]
IssueSeverity = Literal["warning", "blocker"]


# ---------- 通用引用结构 ----------
class ManagerAssignment(BaseModel):
    username: str | None
    display_name: str | None


class ServicePeriod(BaseModel):
    service_start: date | None
    service_end: date | None
    completeness_state: str


class ContractRef(BaseModel):
    project_contract_id: str
    contract_no: str | None
    relation_status: str
    lifecycle_status: str
    version: int = Field(ge=1)


class ProjectRef(BaseModel):
    project_id: str
    project_code: str
    display_name: str
    lifecycle_status: str
    version: int = Field(ge=1)
    manager_assignment: ManagerAssignment
    service_period: ServicePeriod
    contracts: list[ContractRef]


class ReminderCounts(BaseModel):
    total: int = Field(ge=0)
    needs_review: int = Field(ge=0)
    handled: int = Field(ge=0)
    incomplete: int = Field(ge=0)
    overdue: int = Field(ge=0)
    due_this_month: int = Field(ge=0)
    upcoming: int = Field(ge=0)


class ActionableMilestone(BaseModel):
    milestone_id: str
    project_contract_id: str
    contract_no: str | None
    sequence: int = Field(ge=1, le=24)
    planned_month: YearMonth | None
    planned_amount: DecimalAmount | None
    reminder_state: ReminderState
    version: int = Field(ge=1)


class MilestoneOperation(BaseModel):
    operation_id: str
    action: FollowUpAction
    reason: str | None
    actor_display_name: str
    created_at: datetime
    result_version: int = Field(ge=1)


class MilestoneRow(BaseModel):
    milestone_id: str
    project_contract_id: str
    contract_no: str | None
    sequence: int = Field(ge=1, le=24)
    planned_date: date | None
    date_precision: DatePrecision
    planned_month: YearMonth | None
    planned_amount: DecimalAmount | None
    completeness_state: str
    follow_up_status: FollowUpStatus
    reminder_state: ReminderState
    follow_up_review_required: bool
    followed_up_by: str | None
    followed_up_at: datetime | None
    follow_up_note: str | None
    last_operation: MilestoneOperation | None
    version: int = Field(ge=1)


# ---------- 目录 / 详情 ----------
class DirectoryRow(BaseModel):
    project: ProjectRef
    reminder_counts: ReminderCounts
    next_actionable_milestone: ActionableMilestone | None


class DirectoryResponse(BaseModel):
    rows: list[DirectoryRow]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    owner_scope: OwnerScope
    allowed_owner_scopes: list[OwnerScope]
    as_of: date
    data_version: str
    amount_visibility: AmountVisibility

    @field_validator("allowed_owner_scopes")
    @classmethod
    def _owner_scopes_unique(cls, value: list[OwnerScope]) -> list[OwnerScope]:
        """allowed_owner_scopes 必须去重（YAML ``unique: true``，P1-2）。"""
        if len(value) != len(set(value)):
            raise ValueError("allowed_owner_scopes 不允许重复")
        return value

    @model_validator(mode="after")
    def _amount_visibility_fail_closed(self) -> "DirectoryResponse":
        """金额受限时目录里所有计划金额必须为 null（设计 §7.1，P1-2）：
        不能只靠前端隐藏。"""
        if self.amount_visibility == "restricted":
            for row in self.rows:
                milestone = row.next_actionable_milestone
                if milestone is not None and milestone.planned_amount is not None:
                    raise ValueError("金额受限时所有计划金额必须为空")
        return self


class ProjectDetailResponse(BaseModel):
    project: ProjectRef
    summary: ReminderCounts
    rows: list[MilestoneRow]
    as_of: date
    data_version: str
    amount_visibility: AmountVisibility

    @model_validator(mode="after")
    def _amount_visibility_fail_closed(self) -> "ProjectDetailResponse":
        """金额受限时详情里任何行的计划金额必须为 null（设计 §7.1，P1-2）。"""
        if self.amount_visibility == "restricted":
            for row in self.rows:
                if row.planned_amount is not None:
                    raise ValueError("金额受限时所有计划金额必须为空")
        return self


# ---------- follow-up ----------
class FollowUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    action: FollowUpAction
    planned_month: YearMonth | None = None
    note: str | None = Field(default=None, max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _discriminated_fields(self) -> "FollowUpRequest":
        """判别规则：handle 只允许 note；reschedule 必填 planned_month+reason 且禁
        note；reopen 必填 reason 且禁 planned_month+note（YAML discriminated_rules）。"""
        if self.action == "handle":
            if self.planned_month is not None or self.reason is not None:
                raise ValueError("标记已处理只允许填写备注，不允许月份或理由")
        elif self.action == "reschedule":
            if self.planned_month is None or self.reason is None:
                raise ValueError("改期必须同时提供计划月份与理由")
            if not self.reason.strip():
                raise ValueError("改期理由去除首尾空白后必须非空")
            if self.note is not None:
                raise ValueError("改期不允许填写备注")
        elif self.action == "reopen":
            if self.reason is None or not self.reason.strip():
                raise ValueError("重新打开必须提供去除首尾空白后非空的理由")
            if self.planned_month is not None or self.note is not None:
                raise ValueError("重新打开不允许填写月份或备注")
        return self


class FollowUpResponse(BaseModel):
    row: MilestoneRow
    data_version: str
    idempotent_replay: bool


# ---------- 导入预览 ----------
class ImportIssue(BaseModel):
    code: str
    severity: IssueSeverity
    row_key: str | None
    sequence: int | None = Field(ge=1, le=24)
    message: str


class MilestoneDiff(BaseModel):
    sequence: int = Field(ge=1, le=24)
    planned_month: YearMonth | None
    planned_amount: DecimalAmount | None
    change: MilestoneChangeKind
    expected_milestone_version: int | None = Field(ge=1)


class PreviewBinding(BaseModel):
    status: PreviewBindingStatus
    project_id: str | None
    project_version: int | None = Field(ge=1)
    project_contract_id: str | None
    project_contract_version: int | None = Field(ge=1)
    existing_binding_version: int | None = Field(ge=1)


class PreviewOrder(BaseModel):
    row_key: str
    external_order_no: str
    source_project_name: str | None
    binding: PreviewBinding
    milestone_diffs: list[MilestoneDiff]
    warning_codes: list[str]
    blocker_codes: list[str]


class PreviewCounts(BaseModel):
    projects: int = Field(ge=0)
    milestones: int = Field(ge=0)
    bound: int = Field(ge=0)
    pending_binding: int = Field(ge=0)
    blockers: int = Field(ge=0)
    warnings: int = Field(ge=0)
    create: int = Field(ge=0)
    update: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    source_missing: int = Field(ge=0)


class PreviewResponse(BaseModel):
    batch_id: str
    batch_version: int = Field(ge=1)
    data_version: str
    status: BatchStatus
    contract_version: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    counts: PreviewCounts
    rows: list[PreviewOrder]
    issues: list[ImportIssue]
    can_apply: bool
    expires_at: datetime


# ---------- 绑定候选搜索 ----------
class BindingOptionContract(BaseModel):
    project_contract_id: str
    contract_no: str | None
    relation_status: str
    lifecycle_status: str
    version: int = Field(ge=1)


class BindingOptionProject(BaseModel):
    project_id: str
    project_code: str
    display_name: str
    version: int = Field(ge=1)
    contracts: list[BindingOptionContract]


class BindingOptionsResponse(BaseModel):
    batch_id: str
    rows: list[BindingOptionProject]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=50)
    q: str


# ---------- 应用 ----------
class ApplyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_key: str
    external_order_no: str
    project_id: str
    project_version: int = Field(ge=1)
    project_contract_id: str
    project_contract_version: int = Field(ge=1)
    existing_binding_version: int | None = Field(ge=1)
    reason: str | None = Field(max_length=1000)

    @model_validator(mode="after")
    def _reassignment_requires_reason(self) -> "ApplyBinding":
        """改派已有绑定必须填写非空理由；新绑定 existing_binding_version 必须为 null。"""
        if self.existing_binding_version is not None and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("改派已有绑定必须填写理由")
        return self


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_batch_version: int = Field(ge=1)
    expected_data_version: str
    bindings: list[ApplyBinding]


class ApplyCounts(BaseModel):
    created: int = Field(ge=0)
    updated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    source_missing: int = Field(ge=0)
    needs_review: int = Field(ge=0)


class ApplyResponse(BaseModel):
    batch_id: str
    batch_version: int = Field(ge=1)
    data_version: str
    status: BatchStatus
    counts: ApplyCounts
    idempotent_replay: bool
    applied_at: datetime


# ---------- 搜索请求 ----------
class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(default="", max_length=256)
    owner_scope: OwnerScope = "me"
    reminder_state: ReminderState | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=24, ge=1, le=200)


# ---------- 错误体（error_contract） ----------
class DomainErrorDetail(BaseModel):
    code: str
    message: str
    current_version: int | None = None
    current_data_version: str | None = None
    issues: list[ImportIssue] = Field(default_factory=list)


class DomainError(BaseModel):
    detail: DomainErrorDetail


class ValidationErrorItem(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class RequestValidationError(BaseModel):
    detail: list[ValidationErrorItem]
