"""受控计划写 helper 合同（实施计划 Task 1 Step 1.6，设计 §4.1/§11.1）。

被测模块 `backend/app/services/maintenance_collection_milestones.py` 尚不存在，
本文件 import 即收集失败——这是预期的红测；实现后必须提供同名模块与函数。
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.auth import hash_password
from app.models.maintenance_manager import (
    MaintenanceCollectionMilestone,
    MaintenanceManagerUploadBatch,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.system import SysUser
from app.services.maintenance_collection_milestones import write_collection_milestone


def _seed(db, *, username: str = "milestone_helper_user") -> tuple[SysUser, str, str]:
    user = SysUser(
        username=username,
        role="purchaser",
        display_name="合成维保负责人",
        password_hash=hash_password("synthetic-password-123"),
    )
    project = MaintenanceProject(
        project_id=f"project-{username}",
        project_code=f"PM-{username}",
        display_name="合成回款提醒项目",
        lifecycle_status="ongoing",
    )
    db.add_all([user, project])
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id=f"pc-{username}",
        project_id=project.project_id,
        contract_id=f"contract-{username}",
        contract_no=f"XS-{username}",
        contract_amount=Decimal("100000.00"),
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic-test",
    )
    db.add(contract)
    db.commit()
    return user, project.project_id, contract.project_contract_id


def _seed_manager_batch(db, user: SysUser, *, batch_id: str) -> str:
    db.add(
        MaintenanceManagerUploadBatch(
            batch_id=batch_id,
            owner_user_id=user.id,
            report_month=date(2026, 8, 1),
            protocol_version="v3",
            template_version="synthetic-tpl",
            export_id="export-1",
            file_sha256="a" * 64,
            file_size=100,
            operation_key=f"operation-{batch_id}",
            semantic_hash="b" * 64,
            scope_version="c" * 64,
            data_version="d" * 64,
            status="valid",
            plan_json={},
            issues_json=[],
            created_by="synthetic-test",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    db.commit()
    return batch_id


def _seed_import_batch(db, user: SysUser, *, batch_id: str) -> str:
    db.execute(
        text(
            "INSERT INTO maintenance_collection_plan_import_batch "
            "(batch_id, owner_user_id, contract_version, file_sha256, file_size, "
            " original_filename, storage_key, operation_key, semantic_hash, data_version, "
            " apply_payload_hash, version, status, plan_json, issues_json, result_json, "
            " created_by, created_at, expires_at, applied_by, applied_at) "
            "VALUES (:batch_id, :owner_user_id, 'project-manager-xls-v1', :sha256, 1024, "
            " 'synthetic.xls', :storage_key, :operation_key, :semantic_hash, "
            " :data_version, NULL, 1, 'valid', '{}'::jsonb, '[]'::jsonb, NULL, "
            " 'synthetic-test', now(), now() + interval '24 hours', NULL, NULL)"
        ),
        {
            "batch_id": batch_id,
            "owner_user_id": user.id,
            "sha256": "1" * 64,
            "storage_key": f"storage-{batch_id}",
            "operation_key": f"operation-{batch_id}",
            "semantic_hash": "2" * 64,
            "data_version": "3" * 64,
        },
    )
    db.commit()
    return batch_id


def _call(
    db,
    *,
    user: SysUser,
    relation_id: str,
    sequence: int = 1,
    planned_date: date | None = date(2026, 9, 15),
    planned_amount: Decimal | str | None = Decimal("25000.00"),
    completeness_state: str = "complete",
    source: str = "direct_api",
    source_batch_id: str | None = None,
    collection_plan_import_batch_id: str | None = None,
    date_precision: str | None = None,
    operator: str = "synthetic-test",
) -> MaintenanceCollectionMilestone:
    return write_collection_milestone(
        db,
        project_id=f"project-{user.username}",
        project_contract_id=relation_id,
        sequence=sequence,
        planned_date=planned_date,
        planned_amount=planned_amount,
        completeness_state=completeness_state,
        source=source,
        source_batch_id=source_batch_id,
        collection_plan_import_batch_id=collection_plan_import_batch_id,
        date_precision=date_precision,
        operator=operator,
    )


def _get(db, relation_id: str, sequence: int) -> MaintenanceCollectionMilestone:
    return db.scalar(
        select(MaintenanceCollectionMilestone).where(
            MaintenanceCollectionMilestone.project_contract_id == relation_id,
            MaintenanceCollectionMilestone.sequence == sequence,
        )
    )


def test_manager_workbook_source_writes_day_precision(db):
    """manager workbook 来源统一写 date_precision=day（设计 §11.1）。"""
    user, _project_id, relation_id = _seed(db, username="day_precision_helper")
    _seed_manager_batch(db, user, batch_id="day-precision-manager-batch")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        source="manager_workbook_v3",
        source_batch_id="day-precision-manager-batch",
        operator="synthetic-manager",
    )
    db.commit()
    assert node.date_precision == "day"


def test_xls_source_writes_month_precision(db):
    """XLS 来源写 date_precision=month，并绑定专用导入批次。"""
    user, _project_id, relation_id = _seed(db, username="month_precision_helper")
    _seed_import_batch(db, user, batch_id="month-precision-import-batch")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        source="project_manager_xls_v1",
        collection_plan_import_batch_id="month-precision-import-batch",
        operator="synthetic-xls-admin",
    )
    db.commit()
    assert node.date_precision == "month"
    assert node.collection_plan_import_batch_id == "month-precision-import-batch"


def test_new_node_starts_pending_without_review_flag(db):
    user, _project_id, relation_id = _seed(db, username="fresh_pending_helper")
    node = _call(db, user=user, relation_id=relation_id)
    db.commit()
    assert node.follow_up_status == "pending"
    assert node.follow_up_review_required is False
    assert node.followed_up_by is None
    assert node.followed_up_at is None
    assert node.follow_up_note is None
    assert node.version == 1


def test_modifying_handled_node_keeps_follow_up_facts_and_marks_review(db):
    """已处理节点被改日期/金额：保留 handled 与处理人/时间，置 review_required=true。"""
    user, _project_id, relation_id = _seed(db, username="handled_modify_helper")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    followed_up_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    node.follow_up_status = "handled"
    node.followed_up_by = user.id
    node.followed_up_at = followed_up_at
    node.follow_up_note = "合成已跟进备注"
    db.commit()

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 10, 15),
        planned_amount=Decimal("30000.00"),
    )
    db.commit()
    assert updated.follow_up_status == "handled"
    assert updated.followed_up_by == user.id
    assert updated.followed_up_at == followed_up_at
    assert updated.follow_up_note == "合成已跟进备注"
    assert updated.follow_up_review_required is True
    assert updated.version == 2
    assert updated.planned_date == date(2026, 10, 15)
    assert updated.planned_amount == Decimal("30000.00")


def test_unchanged_planned_facts_do_not_bump_version(db):
    user, _project_id, relation_id = _seed(db, username="unchanged_facts_helper")
    node = _call(db, user=user, relation_id=relation_id)
    db.commit()
    assert node.version == 1

    again = _call(db, user=user, relation_id=relation_id)
    db.commit()
    assert again.version == 1, "计划事实未变化不得增加 version"
    assert again.follow_up_status == "pending"
    assert again.follow_up_review_required is False


def test_helper_never_deletes_source_missing_nodes(db):
    """来源缺失只报告不删除：helper 不处理未出现在新计划中的节点。"""
    user, _project_id, relation_id = _seed(db, username="source_missing_helper")
    first = _call(db, user=user, relation_id=relation_id, sequence=1)
    second = _call(db, user=user, relation_id=relation_id, sequence=2)
    db.commit()
    assert second.version == 1

    _call(db, user=user, relation_id=relation_id, sequence=1)
    db.commit()
    assert _get(db, relation_id, 2) is not None, "source-missing 节点不得被 helper 删除"
    assert _get(db, relation_id, 2).version == 1
    assert first.milestone_id != second.milestone_id


# ---------- 修复靶（P1-4：调用方精度冲突未拒绝 / 精度变化不 bump 版本） ----------
def test_conflicting_caller_precision_is_rejected(db):
    """调用方显式传入与来源默认精度冲突的 date_precision 必须拒绝（P1-4）。

    XLS 来源默认 month、manager/direct_api 默认 day；当前 helper 直接采纳
    调用方精度不校验冲突，本测试当前红。
    """
    user, _project_id, relation_id = _seed(db, username="precision_conflict_helper")
    _seed_import_batch(db, user, batch_id="precision-conflict-import-batch")
    _seed_manager_batch(db, user, batch_id="precision-conflict-manager-batch")
    with pytest.raises(ValueError):
        _call(
            db,
            user=user,
            relation_id=relation_id,
            source="project_manager_xls_v1",
            collection_plan_import_batch_id="precision-conflict-import-batch",
            date_precision="day",
        )
    db.rollback()
    with pytest.raises(ValueError):
        _call(db, user=user, relation_id=relation_id, date_precision="month")
    db.rollback()
    # 与来源默认一致的显式精度 → 正常创建
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        source="manager_workbook_v3",
        source_batch_id="precision-conflict-manager-batch",
        date_precision="day",
    )
    db.commit()
    assert node.date_precision == "day"
    assert node.version == 1


def test_date_only_change_bumps_version_pending(db):
    """仅计划日期变化 → version+1，pending 节点不置复核标记（守卫）。"""
    user, _project_id, relation_id = _seed(db, username="date_bump_pending_helper")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    assert node.version == 1

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 10, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    assert updated.version == 2
    assert updated.follow_up_status == "pending"
    assert updated.follow_up_review_required is False
    assert updated.planned_date == date(2026, 10, 15)


def test_amount_only_change_bumps_version_pending(db):
    """仅计划金额变化 → version+1，pending 节点不置复核标记（守卫）。"""
    user, _project_id, relation_id = _seed(db, username="amount_bump_pending_helper")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    assert node.version == 1

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("30000.00"),
    )
    db.commit()
    assert updated.version == 2
    assert updated.follow_up_review_required is False
    assert updated.planned_amount == Decimal("30000.00")


def test_precision_only_change_bumps_version_pending(db):
    """仅精度/来源变化（日期金额不变）也必须 version+1（P1-4）。

    当前实现把 date_precision 放进 ``source_changed``：精度变化只改写字段不
    bump version，本测试当前红。
    """
    user, _project_id, relation_id = _seed(db, username="prec_bump_pending")
    _seed_manager_batch(db, user, batch_id="precision-bump-manager-batch")
    _seed_import_batch(db, user, batch_id="precision-bump-import-batch")
    first = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
        source="manager_workbook_v3",
        source_batch_id="precision-bump-manager-batch",
        date_precision="day",
    )
    db.commit()
    assert first.version == 1
    assert first.date_precision == "day"

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
        source="project_manager_xls_v1",
        collection_plan_import_batch_id="precision-bump-import-batch",
        date_precision="month",
    )
    db.commit()
    assert updated.version == 2
    assert updated.date_precision == "month"
    assert updated.follow_up_status == "pending"
    assert updated.follow_up_review_required is False


def test_date_only_change_on_handled_keeps_facts_and_marks_review(db):
    """已处理节点仅日期变化：保留 handled 与处理人/时间/备注，置复核标记（守卫）。"""
    user, _project_id, relation_id = _seed(db, username="handled_date_bump_helper")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    followed_up_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    node.follow_up_status = "handled"
    node.followed_up_by = user.id
    node.followed_up_at = followed_up_at
    node.follow_up_note = "合成已跟进备注"
    db.commit()

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 10, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    assert updated.version == 2
    assert updated.follow_up_status == "handled"
    assert updated.followed_up_by == user.id
    assert updated.followed_up_at == followed_up_at
    assert updated.follow_up_note == "合成已跟进备注"
    assert updated.follow_up_review_required is True
    assert updated.planned_date == date(2026, 10, 15)


def test_amount_only_change_on_handled_keeps_facts_and_marks_review(db):
    """已处理节点仅金额变化：保留 handled 事实，置复核标记（守卫）。"""
    user, _project_id, relation_id = _seed(db, username="handled_amount_bump_helper")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
    )
    db.commit()
    followed_up_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    node.follow_up_status = "handled"
    node.followed_up_by = user.id
    node.followed_up_at = followed_up_at
    node.follow_up_note = "合成已跟进备注"
    db.commit()

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("30000.00"),
    )
    db.commit()
    assert updated.version == 2
    assert updated.follow_up_status == "handled"
    assert updated.followed_up_by == user.id
    assert updated.followed_up_at == followed_up_at
    assert updated.follow_up_note == "合成已跟进备注"
    assert updated.follow_up_review_required is True
    assert updated.planned_amount == Decimal("30000.00")


def test_precision_only_change_on_handled_keeps_facts_and_marks_review(db):
    """已处理节点被 XLS 再导入（同日期金额、精度 day→month）：version+1、
    保留 handled 事实并置复核标记（P1-4 红——当前精度变化不 bump 也不标记）。"""
    user, _project_id, relation_id = _seed(db, username="handled_prec_bump")
    _seed_manager_batch(db, user, batch_id="handled-precision-manager-batch")
    _seed_import_batch(db, user, batch_id="handled-precision-import-batch")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
        source="manager_workbook_v3",
        source_batch_id="handled-precision-manager-batch",
        date_precision="day",
    )
    db.commit()
    followed_up_at = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    node.follow_up_status = "handled"
    node.followed_up_by = user.id
    node.followed_up_at = followed_up_at
    node.follow_up_note = "合成已跟进备注"
    db.commit()

    updated = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
        source="project_manager_xls_v1",
        collection_plan_import_batch_id="handled-precision-import-batch",
        date_precision="month",
    )
    db.commit()
    assert updated.version == 2
    assert updated.date_precision == "month"
    assert updated.follow_up_status == "handled"
    assert updated.followed_up_by == user.id
    assert updated.followed_up_at == followed_up_at
    assert updated.follow_up_note == "合成已跟进备注"
    assert updated.follow_up_review_required is True


def test_source_batch_only_change_does_not_bump_version(db):
    """仅来源批次变化（同来源同事实同精度）不得增加 version（守卫；
    修复后精度不再参与 source_changed，本用例必须保持通过）。"""
    user, _project_id, relation_id = _seed(db, username="source_batch_only_helper")
    _seed_manager_batch(db, user, batch_id="source-batch-only-a")
    _seed_manager_batch(db, user, batch_id="source-batch-only-b")
    node = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
        source="manager_workbook_v3",
        source_batch_id="source-batch-only-a",
        date_precision="day",
    )
    db.commit()
    assert node.version == 1

    again = _call(
        db,
        user=user,
        relation_id=relation_id,
        planned_date=date(2026, 9, 15),
        planned_amount=Decimal("25000.00"),
        source="manager_workbook_v3",
        source_batch_id="source-batch-only-b",
        date_precision="day",
    )
    db.commit()
    assert again.version == 1, "来源批次字段变化不得增加 version"
    assert again.source_batch_id == "source-batch-only-b"
    assert again.follow_up_review_required is False
