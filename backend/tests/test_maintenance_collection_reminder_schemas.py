"""回款提醒 API v1 DTO 合同（K0 Task 1 Step 1.5）。

逐项镜像 `.ai/contracts/maintenance-collections/collection-reminders-api-v1.yaml`：
- 每个请求 schema 使用 ``ConfigDict(extra="forbid")``；
- 每个端点合成 200 response 的必需字段与精确键名；
- 403/409/404/422/413/415 domain error 与 422 request-validation 形状；
- ``planned_amount`` 只接受 ``str | None``（禁止 number 隐式转换）；
- follow-up 判别规则：handle 只允许 note；reschedule 必填 planned_month+reason 且禁 note；
  reopen 必填 reason 且禁 planned_month+note；
- idempotency_key 8–128 字符、expected_version >= 1。

被测模块 `backend/app/schemas/maintenance_collection_reminders.py` 尚不存在，
本文件 import 即收集失败——这是预期的红测；实现后必须提供同名模块与下列类名。
source-file 端点是二进制附件响应（Content-Type: application/vnd.ms-excel），无 JSON DTO。
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.schemas.maintenance_collection_reminders import (
    ActionableMilestone,
    ApplyBinding,
    ApplyCounts,
    ApplyRequest,
    ApplyResponse,
    BindingOptionContract,
    BindingOptionProject,
    BindingOptionsResponse,
    ContractRef,
    DirectoryResponse,
    DirectoryRow,
    DomainError,
    DomainErrorDetail,
    FollowUpRequest,
    FollowUpResponse,
    ImportIssue,
    ManagerAssignment,
    MilestoneDiff,
    MilestoneOperation,
    MilestoneRow,
    PreviewBinding,
    PreviewCounts,
    PreviewOrder,
    PreviewResponse,
    ProjectDetailResponse,
    ProjectRef,
    ReminderCounts,
    RequestValidationError,
    SearchRequest,
    ServicePeriod,
    ValidationErrorItem,
)


# ---------- 合成响应构建辅助 ----------
def _manager_assignment() -> ManagerAssignment:
    return ManagerAssignment(username="synthetic-manager", display_name="合成维保负责人")


def _service_period() -> ServicePeriod:
    return ServicePeriod(
        service_start=date(2026, 1, 1),
        service_end=date(2026, 12, 31),
        completeness_state="complete",
    )


def _contract_ref() -> ContractRef:
    return ContractRef(
        project_contract_id="pc-1",
        contract_no="XS-1",
        relation_status="active",
        lifecycle_status="active",
        version=1,
    )


def _project_ref() -> ProjectRef:
    return ProjectRef(
        project_id="p-1",
        project_code="PM-1",
        display_name="合成项目",
        lifecycle_status="ongoing",
        version=1,
        manager_assignment=_manager_assignment(),
        service_period=_service_period(),
        contracts=[_contract_ref()],
    )


def _reminder_counts() -> ReminderCounts:
    return ReminderCounts(
        total=1,
        needs_review=0,
        handled=0,
        incomplete=0,
        overdue=0,
        due_this_month=1,
        upcoming=0,
    )


def _milestone_row(**overrides) -> MilestoneRow:
    values = dict(
        milestone_id="m-1",
        project_contract_id="pc-1",
        contract_no="XS-1",
        sequence=1,
        planned_date=date(2026, 8, 1),
        date_precision="month",
        planned_month="2026-08",
        planned_amount="25000.00",
        completeness_state="complete",
        follow_up_status="pending",
        reminder_state="due_this_month",
        follow_up_review_required=False,
        followed_up_by=None,
        followed_up_at=None,
        follow_up_note=None,
        last_operation=None,
        version=1,
    )
    values.update(overrides)
    return MilestoneRow(**values)


# ---------- 1. 请求 schema：extra=forbid ----------
def test_request_schemas_forbid_extra_fields():
    with pytest.raises(ValidationError):
        SearchRequest(unknown_field=True)
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1,
            idempotency_key="key-12345678",
            action="handle",
            unknown_field=True,
        )
    with pytest.raises(ValidationError):
        ApplyRequest(
            expected_batch_version=1,
            expected_data_version="synthetic-version-1",
            bindings=[],
            unknown_field=True,
        )
    with pytest.raises(ValidationError):
        ApplyBinding(
            row_key="row-1",
            external_order_no="ORDER-001",
            project_id="p-1",
            project_version=1,
            project_contract_id="pc-1",
            project_contract_version=1,
            existing_binding_version=None,
            reason=None,
            unknown_field=True,
        )


def test_search_request_defaults():
    payload = SearchRequest().model_dump(mode="json")
    assert payload == {
        "q": "",
        "owner_scope": "me",
        "reminder_state": None,
        "page": 1,
        "page_size": 24,
    }


# ---------- 2. search → directory_response ----------
def test_search_directory_response_serializes_with_exact_keys():
    milestone = ActionableMilestone(
        milestone_id="m-1",
        project_contract_id="pc-1",
        contract_no="XS-1",
        sequence=1,
        planned_month="2026-08",
        planned_amount="25000.00",
        reminder_state="due_this_month",
        version=1,
    )
    response = DirectoryResponse(
        rows=[
            DirectoryRow(
                project=_project_ref(),
                reminder_counts=_reminder_counts(),
                next_actionable_milestone=milestone,
            )
        ],
        total=1,
        page=1,
        page_size=24,
        owner_scope="me",
        allowed_owner_scopes=["me"],
        as_of=date(2026, 8, 14),
        data_version="synthetic-version-1",
        amount_visibility="visible",
    )
    payload = response.model_dump()
    assert set(payload) == {
        "rows", "total", "page", "page_size", "owner_scope",
        "allowed_owner_scopes", "as_of", "data_version", "amount_visibility",
    }
    row = payload["rows"][0]
    assert set(row) == {"project", "reminder_counts", "next_actionable_milestone"}
    assert set(row["project"]) == {
        "project_id", "project_code", "display_name", "lifecycle_status", "version",
        "manager_assignment", "service_period", "contracts",
    }
    assert set(row["project"]["manager_assignment"]) == {"username", "display_name"}
    assert set(row["project"]["service_period"]) == {
        "service_start", "service_end", "completeness_state",
    }
    assert set(row["project"]["contracts"][0]) == {
        "project_contract_id", "contract_no", "relation_status", "lifecycle_status", "version",
    }
    assert set(row["reminder_counts"]) == {
        "total", "needs_review", "handled", "incomplete",
        "overdue", "due_this_month", "upcoming",
    }
    assert set(row["next_actionable_milestone"]) == {
        "milestone_id", "project_contract_id", "contract_no", "sequence",
        "planned_month", "planned_amount", "reminder_state", "version",
    }


# ---------- 3. detail → project_detail_response ----------
def test_project_detail_response_serializes_with_exact_keys():
    operation = MilestoneOperation(
        operation_id="op-1",
        action="handle",
        reason=None,
        actor_display_name="合成操作者",
        created_at=datetime(2026, 8, 10, 9, 30, tzinfo=UTC),
        result_version=2,
    )
    response = ProjectDetailResponse(
        project=_project_ref(),
        summary=_reminder_counts(),
        rows=[_milestone_row(last_operation=operation)],
        as_of=date(2026, 8, 14),
        data_version="synthetic-version-1",
        amount_visibility="visible",
    )
    payload = response.model_dump()
    assert set(payload) == {"project", "summary", "rows", "as_of", "data_version",
                            "amount_visibility"}
    row = payload["rows"][0]
    assert set(row) == {
        "milestone_id", "project_contract_id", "contract_no", "sequence",
        "planned_date", "date_precision", "planned_month", "planned_amount",
        "completeness_state", "follow_up_status", "reminder_state",
        "follow_up_review_required", "followed_up_by", "followed_up_at",
        "follow_up_note", "last_operation", "version",
    }
    assert set(row["last_operation"]) == {
        "operation_id", "action", "reason", "actor_display_name",
        "created_at", "result_version",
    }
    assert payload["rows"][0]["planned_amount"] == "25000.00"


# ---------- 4. follow_up → request + response ----------
def test_follow_up_request_and_response_serialize_with_exact_keys():
    request = FollowUpRequest(
        expected_version=1,
        idempotency_key="key-12345678",
        action="handle",
        note="已电话确认",
    )
    payload = request.model_dump()
    assert set(payload) == {"expected_version", "idempotency_key", "action",
                            "planned_month", "note", "reason"}
    assert payload["planned_month"] is None
    assert payload["reason"] is None

    response = FollowUpResponse(
        row=_milestone_row(),
        data_version="synthetic-version-1",
        idempotent_replay=False,
    )
    assert set(response.model_dump()) == {"row", "data_version", "idempotent_replay"}


def test_follow_up_request_discriminated_rules():
    # handle：note 可选，reason 禁止
    FollowUpRequest(expected_version=1, idempotency_key="key-12345678", action="handle")
    FollowUpRequest(
        expected_version=1, idempotency_key="key-12345678", action="handle",
        note="已电话确认",
    )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="handle",
            reason="不该出现",
        )
    # reschedule：planned_month + reason 必填，note 禁止
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reschedule",
            reason="客户要求延后",
        )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reschedule",
            planned_month="2026-09",
        )
    FollowUpRequest(
        expected_version=1, idempotency_key="key-12345678", action="reschedule",
        planned_month="2026-09", reason="客户要求延后",
    )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reschedule",
            planned_month="2026-09", reason="客户要求延后", note="多余",
        )
    # reopen：reason 必填，planned_month 与 note 禁止
    with pytest.raises(ValidationError):
        FollowUpRequest(expected_version=1, idempotency_key="key-12345678", action="reopen")
    FollowUpRequest(
        expected_version=1, idempotency_key="key-12345678", action="reopen",
        reason="误操作，重新打开",
    )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reopen",
            reason="误操作，重新打开", planned_month="2026-09",
        )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reopen",
            reason="误操作，重新打开", note="多余",
        )


def test_follow_up_request_bounds():
    with pytest.raises(ValidationError):
        FollowUpRequest(expected_version=0, idempotency_key="key-12345678", action="handle")
    with pytest.raises(ValidationError):
        FollowUpRequest(expected_version=1, idempotency_key="short", action="handle")
    with pytest.raises(ValidationError):
        FollowUpRequest(expected_version=1, idempotency_key="x" * 129, action="handle")
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="handle",
            note="x" * 1001,
        )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reschedule",
            planned_month="2026-13", reason="月份超出范围",
        )


# ---------- 5. preview → preview_response ----------
def test_preview_response_serializes_with_exact_keys():
    issue = ImportIssue(
        code="synthetic_contract_error",
        severity="blocker",
        row_key=None,
        sequence=None,
        message="合成示例，不含业务值",
    )
    diff = MilestoneDiff(
        sequence=1,
        planned_month="2026-08",
        planned_amount="25000.00",
        change="create",
        expected_milestone_version=None,
    )
    binding = PreviewBinding(
        status="pending_review",
        project_id=None,
        project_version=None,
        project_contract_id=None,
        project_contract_version=None,
        existing_binding_version=None,
    )
    order = PreviewOrder(
        row_key="row-1",
        external_order_no="ORDER-001",
        source_project_name=None,
        binding=binding,
        milestone_diffs=[diff],
        warning_codes=[],
        blocker_codes=[],
    )
    counts = PreviewCounts(
        projects=1, milestones=1, bound=0, pending_binding=1, blockers=0,
        warnings=0, create=1, update=0, unchanged=0, source_missing=0,
    )
    response = PreviewResponse(
        batch_id="batch-1",
        batch_version=1,
        data_version="synthetic-version-1",
        status="valid",
        contract_version="project-manager-xls-v1",
        file_sha256="a" * 64,
        counts=counts,
        rows=[order],
        issues=[issue],
        can_apply=False,
        expires_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
    )
    payload = response.model_dump()
    assert set(payload) == {
        "batch_id", "batch_version", "data_version", "status", "contract_version",
        "file_sha256", "counts", "rows", "issues", "can_apply", "expires_at",
    }
    assert set(payload["counts"]) == {
        "projects", "milestones", "bound", "pending_binding", "blockers", "warnings",
        "create", "update", "unchanged", "source_missing",
    }
    assert set(payload["rows"][0]) == {
        "row_key", "external_order_no", "source_project_name", "binding",
        "milestone_diffs", "warning_codes", "blocker_codes",
    }
    assert set(payload["rows"][0]["binding"]) == {
        "status", "project_id", "project_version", "project_contract_id",
        "project_contract_version", "existing_binding_version",
    }
    assert set(payload["rows"][0]["milestone_diffs"][0]) == {
        "sequence", "planned_month", "planned_amount", "change",
        "expected_milestone_version",
    }
    assert set(payload["issues"][0]) == {
        "code", "severity", "row_key", "sequence", "message",
    }


# ---------- 6. binding-options → binding_options_response ----------
def test_binding_options_response_serializes_with_exact_keys():
    contract = BindingOptionContract(
        project_contract_id="pc-1",
        contract_no="XS-1",
        relation_status="active",
        lifecycle_status="active",
        version=1,
    )
    project = BindingOptionProject(
        project_id="p-1",
        project_code="PM-1",
        display_name="合成项目",
        version=1,
        contracts=[contract],
    )
    response = BindingOptionsResponse(
        batch_id="batch-1",
        rows=[project],
        total=1,
        page=1,
        page_size=20,
        q="合成",
    )
    payload = response.model_dump()
    assert set(payload) == {"batch_id", "rows", "total", "page", "page_size", "q"}
    assert set(payload["rows"][0]) == {
        "project_id", "project_code", "display_name", "version", "contracts",
    }
    assert set(payload["rows"][0]["contracts"][0]) == {
        "project_contract_id", "contract_no", "relation_status", "lifecycle_status", "version",
    }


# ---------- 7. apply → apply_request + apply_response ----------
def test_apply_request_and_response_serialize_with_exact_keys():
    binding = ApplyBinding(
        row_key="row-1",
        external_order_no="ORDER-001",
        project_id="p-1",
        project_version=1,
        project_contract_id="pc-1",
        project_contract_version=1,
        existing_binding_version=None,
        reason=None,
    )
    request = ApplyRequest(
        expected_batch_version=1,
        expected_data_version="synthetic-version-1",
        bindings=[binding],
    )
    payload = request.model_dump()
    assert set(payload) == {"expected_batch_version", "expected_data_version", "bindings"}
    assert set(payload["bindings"][0]) == {
        "row_key", "external_order_no", "project_id", "project_version",
        "project_contract_id", "project_contract_version",
        "existing_binding_version", "reason",
    }
    counts = ApplyCounts(
        created=1, updated=0, unchanged=0, source_missing=0, needs_review=0,
    )
    response = ApplyResponse(
        batch_id="batch-1",
        batch_version=1,
        data_version="synthetic-version-1",
        status="applied",
        counts=counts,
        idempotent_replay=False,
        applied_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
    )
    payload = response.model_dump()
    assert set(payload) == {
        "batch_id", "batch_version", "data_version", "status", "counts",
        "idempotent_replay", "applied_at",
    }
    assert set(payload["counts"]) == {
        "created", "updated", "unchanged", "source_missing", "needs_review",
    }


# ---------- 8. 错误体 ----------
def test_domain_error_body_structure_for_4xx_and_5xx_statuses():
    for code, message in (
        ("permission_denied", "无权执行此操作"),
        ("version_conflict", "数据已变化，请刷新后重试"),
        ("not_found", "资源不存在或不可见"),
        ("upload_too_large", "工作簿超过上传安全上限"),
        ("unsupported_media_type", "只接受 multipart/form-data 的 .xls 文件"),
    ):
        error = DomainError(
            detail=DomainErrorDetail(
                code=code,
                message=message,
                current_version=None,
                current_data_version=None,
                issues=[],
            )
        )
        payload = error.model_dump()
        assert set(payload) == {"detail"}
        assert set(payload["detail"]) == {
            "code", "message", "current_version", "current_data_version", "issues",
        }
        assert payload["detail"]["issues"] == []
    # 409 携带 current_version / current_data_version
    conflict = DomainError(
        detail=DomainErrorDetail(
            code="version_conflict",
            message="数据已变化，请刷新后重试",
            current_version=2,
            current_data_version="synthetic-version-2",
            issues=[],
        )
    )
    assert conflict.model_dump()["detail"]["current_version"] == 2
    # 422 domain error 携带 issues[]
    invalid = DomainError(
        detail=DomainErrorDetail(
            code="invalid_request",
            message="请求字段或工作簿不符合合同",
            current_version=None,
            current_data_version=None,
            issues=[
                ImportIssue(
                    code="synthetic_contract_error",
                    severity="blocker",
                    row_key=None,
                    sequence=None,
                    message="合成示例，不含业务值",
                )
            ],
        )
    )
    issues = invalid.model_dump()["detail"]["issues"]
    assert set(issues[0]) == {"code", "severity", "row_key", "sequence", "message"}


def test_request_validation_error_shape():
    item = ValidationErrorItem(
        loc=["body", "expected_version"],
        msg="Input should be greater than or equal to 1",
        type="greater_than_equal",
    )
    error = RequestValidationError(detail=[item])
    payload = error.model_dump()
    assert set(payload) == {"detail"}
    assert set(payload["detail"][0]) == {"loc", "msg", "type"}
    assert payload["detail"][0]["loc"] == ["body", "expected_version"]


# ---------- 9. 金额只接受十进制定点字符串或 null ----------
def test_planned_amount_accepts_only_decimal_string_or_null():
    row = _milestone_row(planned_amount="25000.00")
    assert row.planned_amount == "25000.00"
    row_none = _milestone_row(planned_amount=None)
    assert row_none.planned_amount is None
    with pytest.raises(ValidationError):
        _milestone_row(planned_amount=25000.00)
    with pytest.raises(ValidationError):
        _milestone_row(planned_amount=25000)
    with pytest.raises(ValidationError):
        ActionableMilestone(
            milestone_id="m-1",
            project_contract_id="pc-1",
            contract_no="XS-1",
            sequence=1,
            planned_month="2026-08",
            planned_amount=25000.00,
            reminder_state="due_this_month",
            version=1,
        )
    with pytest.raises(ValidationError):
        MilestoneDiff(
            sequence=1,
            planned_month="2026-08",
            planned_amount=25000.00,
            change="create",
            expected_milestone_version=None,
        )


# ---------- 10. 枚举受限 ----------
def test_response_enums_are_restricted():
    with pytest.raises(ValidationError):
        DirectoryResponse(
            rows=[], total=0, page=1, page_size=24, owner_scope="team",
            allowed_owner_scopes=["me"], as_of=date(2026, 8, 14),
            data_version="synthetic-version-1", amount_visibility="visible",
        )
    with pytest.raises(ValidationError):
        DirectoryResponse(
            rows=[], total=0, page=1, page_size=24, owner_scope="me",
            allowed_owner_scopes=["me"], as_of=date(2026, 8, 14),
            data_version="synthetic-version-1", amount_visibility="guessed",
        )
    with pytest.raises(ValidationError):
        _milestone_row(date_precision="week")
    with pytest.raises(ValidationError):
        _milestone_row(follow_up_status="done")
    with pytest.raises(ValidationError):
        _milestone_row(reminder_state="closed")
    with pytest.raises(ValidationError):
        FollowUpRequest(expected_version=1, idempotency_key="key-12345678", action="delete")
    with pytest.raises(ValidationError):
        _milestone_row(sequence=25)
    with pytest.raises(ValidationError):
        DirectoryResponse(
            rows=[], total=0, page=1, page_size=0, owner_scope="me",
            allowed_owner_scopes=["me"], as_of=date(2026, 8, 14),
            data_version="synthetic-version-1", amount_visibility="visible",
        )


# ---------- 11. 金额可见性与 scope 唯一性 / reason 非空白（P1-2/P1-3 修复靶） ----------
def test_restricted_visibility_rejects_non_null_amounts_in_directory_response():
    """amount_visibility=restricted 时目录里所有计划金额必须为 null（设计 §7.1
    "受限时所有计划金额为 null，不能只靠前端隐藏"）；当前 DTO 无跨字段校验，
    本测试当前红。"""
    milestone = ActionableMilestone(
        milestone_id="m-1",
        project_contract_id="pc-1",
        contract_no="XS-1",
        sequence=1,
        planned_month="2026-08",
        planned_amount="25000.00",
        reminder_state="due_this_month",
        version=1,
    )
    with pytest.raises(ValidationError):
        DirectoryResponse(
            rows=[
                DirectoryRow(
                    project=_project_ref(),
                    reminder_counts=_reminder_counts(),
                    next_actionable_milestone=milestone,
                )
            ],
            total=1,
            page=1,
            page_size=24,
            owner_scope="me",
            allowed_owner_scopes=["me"],
            as_of=date(2026, 8, 14),
            data_version="synthetic-version-1",
            amount_visibility="restricted",
        )
    # 受限 + 金额为 null → 构造成功
    masked = ActionableMilestone(
        milestone_id="m-1",
        project_contract_id="pc-1",
        contract_no="XS-1",
        sequence=1,
        planned_month="2026-08",
        planned_amount=None,
        reminder_state="due_this_month",
        version=1,
    )
    DirectoryResponse(
        rows=[
            DirectoryRow(
                project=_project_ref(),
                reminder_counts=_reminder_counts(),
                next_actionable_milestone=masked,
            )
        ],
        total=1,
        page=1,
        page_size=24,
        owner_scope="me",
        allowed_owner_scopes=["me"],
        as_of=date(2026, 8, 14),
        data_version="synthetic-version-1",
        amount_visibility="restricted",
    )
    # visible + 金额 → 构造成功
    DirectoryResponse(
        rows=[
            DirectoryRow(
                project=_project_ref(),
                reminder_counts=_reminder_counts(),
                next_actionable_milestone=milestone,
            )
        ],
        total=1,
        page=1,
        page_size=24,
        owner_scope="me",
        allowed_owner_scopes=["me"],
        as_of=date(2026, 8, 14),
        data_version="synthetic-version-1",
        amount_visibility="visible",
    )


def test_restricted_visibility_rejects_non_null_amounts_in_project_detail_response():
    """详情响应同样：restricted 时任何行的 planned_amount 必须为 null（P1-2 红）。"""
    with pytest.raises(ValidationError):
        ProjectDetailResponse(
            project=_project_ref(),
            summary=_reminder_counts(),
            rows=[_milestone_row(planned_amount="25000.00")],
            as_of=date(2026, 8, 14),
            data_version="synthetic-version-1",
            amount_visibility="restricted",
        )
    # restricted + 全部金额 null → 构造成功
    ProjectDetailResponse(
        project=_project_ref(),
        summary=_reminder_counts(),
        rows=[
            _milestone_row(planned_amount=None),
            _milestone_row(planned_amount=None, sequence=2),
        ],
        as_of=date(2026, 8, 14),
        data_version="synthetic-version-1",
        amount_visibility="restricted",
    )
    # visible + 金额 → 构造成功
    ProjectDetailResponse(
        project=_project_ref(),
        summary=_reminder_counts(),
        rows=[_milestone_row(planned_amount="25000.00")],
        as_of=date(2026, 8, 14),
        data_version="synthetic-version-1",
        amount_visibility="visible",
    )


def test_allowed_owner_scopes_must_be_unique():
    """allowed_owner_scopes 必须去重（YAML ``unique: true``）；当前 DTO 接受重复
    列表，本测试当前红。"""
    with pytest.raises(ValidationError):
        DirectoryResponse(
            rows=[], total=0, page=1, page_size=24, owner_scope="me",
            allowed_owner_scopes=["me", "me"], as_of=date(2026, 8, 14),
            data_version="synthetic-version-1", amount_visibility="visible",
        )
    DirectoryResponse(
        rows=[], total=0, page=1, page_size=24, owner_scope="me",
        allowed_owner_scopes=["me", "all"], as_of=date(2026, 8, 14),
        data_version="synthetic-version-1", amount_visibility="visible",
    )


def test_follow_up_reschedule_and_reopen_reject_whitespace_reason():
    """reason 去除首尾空白后必须非空（设计 §7.3）；当前只检查 ``is None``，
    纯空白通过校验，本测试当前红。"""
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reschedule",
            planned_month="2026-09", reason="   ",
        )
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="reopen",
            reason="\t\n ",
        )
    # 首尾空白但内容非空 → 构造成功
    FollowUpRequest(
        expected_version=1, idempotency_key="key-12345678", action="reschedule",
        planned_month="2026-09", reason=" 已电话确认 ",
    )
    # handle 严格性保持：任何 reason（含纯空白）都拒绝
    with pytest.raises(ValidationError):
        FollowUpRequest(
            expected_version=1, idempotency_key="key-12345678", action="handle",
            reason="   ",
        )
