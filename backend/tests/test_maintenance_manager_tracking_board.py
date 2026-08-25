"""Manager workbook facts must remain visible and actionable after apply (#206)."""

from datetime import UTC, date, datetime

from app.auth import hash_password
from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileLink,
    MaintenanceAcceptanceDeliverable,
    MaintenanceCollectionMilestone,
    MaintenanceServicePeriod,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.security import UserContext
from app.services.maintenance_project_operations import project_operations


def _ctx(username: str) -> UserContext:
    return UserContext(
        role="admin",
        user_id=username,
        is_authenticated=True,
        permissions=None,
    )


def test_directory_card_uses_manager_tracking_facts_and_real_attachment_state(db):
    user = SysUser(
        username="tracking_board_manager",
        role="purchaser",
        display_name="合成项目经理",
        password_hash=hash_password("synthetic-password-123"),
    )
    project = MaintenanceProject(
        project_id="tracking-board-project",
        project_code="PM-TRACKING-BOARD",
        display_name="合成跟踪看板项目",
        lifecycle_status="ongoing",
    )
    db.add_all([user, project])
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id="tracking-board-relation",
        project_id=project.project_id,
        contract_id="tracking-board-contract",
        contract_no="XS-TRACKING-BOARD",
        contract_amount=100000,
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
    )
    deliverable = MaintenanceAcceptanceDeliverable(
        deliverable_id="tracking-board-deliverable",
        project_id=project.project_id,
        deliverable_type="acceptance_report",
        due_date=date(2026, 8, 5),
        submission_status="not_submitted",
        submitted_at=None,
        submitted_by=None,
        approval_status="not_reviewed",
        approved_at=None,
        approved_by=None,
        rejection_reason=None,
        configuration_state="configured",
    )
    file = BusinessFile(
        file_id="tracking-board-file",
        storage_provider="local",
        object_key="maintenance_acceptance/tracking-board-file.pdf",
        original_filename="合成验收报告.pdf",
        mime_type="application/pdf",
        size_bytes=128,
        sha256="a" * 64,
        security_state="active",
        uploaded_by=user.username,
    )
    db.add_all([
        MaintenanceProjectUserAssignment(
            assignment_id="tracking-board-assignment",
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            assigned_at=datetime.now(UTC),
            assigned_by="synthetic-admin",
            assignment_reason="合成负责人映射",
        ),
        contract,
        deliverable,
        file,
    ])
    db.flush()
    db.add_all([
        MaintenanceServicePeriod(
            project_id=project.project_id,
            service_start=date(2026, 1, 1),
            service_end=date(2026, 12, 31),
            completeness_state="complete",
            source="direct_api",
            source_batch_id=None,
        ),
        MaintenanceCollectionMilestone(
            milestone_id="tracking-board-milestone",
            project_id=project.project_id,
            project_contract_id=contract.project_contract_id,
            sequence=1,
            planned_date=date(2026, 8, 1),
            planned_amount=20000,
            completeness_state="complete",
            source="direct_api",
            source_batch_id=None,
        ),
        BusinessFileLink(
            link_id="tracking-board-file-link",
            file_id=file.file_id,
            entity_type="maintenance_acceptance_deliverable",
            entity_id=deliverable.deliverable_id,
            relation_type="evidence",
            acl_scope="project_members",
            created_by=user.username,
        ),
    ])
    db.commit()

    payload = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user.username),
        owner_scope="all",
    )

    card = payload["rows"][0]
    tracking = card["manager_tracking"]
    assert tracking["service_period"] == {
        "service_start": "2026-01-01",
        "service_end": "2026-12-31",
        "completeness_state": "complete",
    }
    assert tracking["next_collection_milestone"]["sequence"] == 1
    assert tracking["next_collection_milestone"]["planned_date"] == "2026-08-01"
    assert tracking["next_collection_milestone"]["overdue_days"] == 8
    assert tracking["acceptance"]["due_date"] == "2026-08-05"
    assert tracking["acceptance"]["overdue_days"] == 4
    assert tracking["acceptance"]["attachment_count"] == 1
    assert card["attachment_status"] == "available"
    assert "附件状态待接入" not in card["missing_data_labels"]
    assert "期限待补" not in card["missing_data_labels"]
    tasks = {row["rule_key"]: row for row in card["task_summary"]["rows"]}
    assert tasks["collection_plan:tracking-board-relation:1"]["is_overdue"] is True
    assert tasks["acceptance:report_due"]["is_overdue"] is True

    for selector in (
        "collection_plan:tracking-board-relation:1",
        "计划回款",
        "acceptance:report_due",
        "验收报告",
        "critical",
        "all",
    ):
        filtered = project_operations(
            db,
            as_of=date(2026, 8, 9),
            user_ctx=_ctx(user.username),
            owner_scope="all",
            reminder=selector,
        )
        assert filtered["total"] == 1, selector
        assert filtered["rows"][0]["project_id"] == project.project_id

    due_filtered = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user.username),
        owner_scope="all",
        task_type="计划回款",
        due_from=date(2026, 8, 1),
        due_to=date(2026, 8, 1),
    )
    assert due_filtered["total"] == 1


def test_missing_manager_tracking_facts_remain_visible_as_small_labels(db):
    user = SysUser(
        username="tracking_board_missing_manager",
        role="purchaser",
        display_name="合成项目经理",
        password_hash=hash_password("synthetic-password-123"),
    )
    project = MaintenanceProject(
        project_id="tracking-board-missing-project",
        project_code="PM-TRACKING-MISSING",
        display_name="合成缺失跟踪项目",
        lifecycle_status="ongoing",
    )
    db.add_all([user, project])
    db.flush()
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="tracking-board-missing-assignment",
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            assigned_at=datetime.now(UTC),
            assigned_by="synthetic-admin",
            assignment_reason="合成负责人映射",
        )
    )
    db.commit()

    payload = project_operations(
        db,
        as_of=date(2026, 8, 9),
        user_ctx=_ctx(user.username),
        owner_scope="all",
    )

    card = payload["rows"][0]
    assert card["manager_tracking"]["service_period"]["completeness_state"] == "empty"
    assert card["manager_tracking"]["acceptance"]["configuration_state"] == "pending_business_configuration"
    assert "维保期限待补" in card["missing_data_labels"]
    # 2026-08-25：验收无截止日概念、月度全量表入口不存在——两个过时
    # 标签已取消（死胡同提示），附件提示保留
    assert "验收截止日待补" not in card["missing_data_labels"]
    assert "验收业务配置待确认" not in card["missing_data_labels"]
    assert "验收附件待上传" in card["missing_data_labels"]
    assert card["attachment_status"] == "missing"
    # 截止日类任务随标签退役；未提交验收的提醒保留（唯一保留的验收提醒）
    task_rules = {row["rule_key"] for row in card["task_summary"]["rows"]}
    assert "acceptance:missing_due" not in task_rules
    assert "acceptance:report_due" in task_rules
