"""Task 4 车道 B2：preview/binding-options/apply/source-file 红测（Step 4.1–4.2）。

覆盖：
- preview：幂等收敛（同 key 重放、不同文件 409、新 key 新批次、并发唯一键收敛）、
  零领域事实写入、error 批次保留证据、blocaker/warning 计数、row_key/data_version。
- binding options：q≥2、page_size≤50、最小字段、绝不返回全量、404。
- apply：原子写入、幂等重放/409、批次/数据/节点版本漂移整批 409 零写入、
  apply flag fail-closed、canary 403、未绑定 422、handled 保留 + review_required、
  source_missing 只报告不删除、unchanged 不 bump、改派审计、并发最多一个写入、
  过期 409、非 owner 404。
- API：判别绑定结构（改派必须有 reason）、preview/apply 冻结形状。
"""

from __future__ import annotations

import hashlib
import os
import threading
import traceback
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import auth
from app import permissions as _perms
from app.api import maintenance_collection_plan_imports as imports_api
from app.auth import hash_password
from app.db import SessionLocal
from app.models.maintenance_manager import (
    MaintenanceCollectionMilestone,
    MaintenanceCollectionMilestoneOperation,
    MaintenanceCollectionPlanImportBatch,
    MaintenanceCollectionPlanSourceBinding,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.models.system import SysUser
from app.services import maintenance_collection_plan_imports as imports
from tests.test_maintenance_collection_plan_xls import (
    ORDERED_HEADERS,
    build_synthetic_biff8,
)

AS_OF = date(2026, 8, 14)
OPERATOR = "plan_admin"


# ---------- 种子 ----------

def _sys_user(db, *, username: str, role: str = "admin", import_action: bool = False) -> SysUser:
    graph = _perms.effective(role, None)
    template = dict(graph)
    overrides = {}
    if import_action:
        template["action_maintenance_collection_plan_import"] = False
        overrides["action_maintenance_collection_plan_import"] = True
    user = SysUser(
        username=username,
        role=role,
        display_name=f"合成{username}",
        password_hash=hash_password("synthetic-password-123"),
        template_perms=template,
        perm_overrides=overrides or None,
    )
    db.add(user)
    db.commit()
    return user


def _client(db, *, username: str, role: str = "admin", import_action: bool = False):
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    if user is None:
        user = _sys_user(db, username=username, role=role, import_action=import_action)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(imports_api.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client, user


def _project(
    db,
    *,
    suffix: str,
    active: bool = True,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> tuple[str, str]:
    project = MaintenanceProject(
        project_id=f"plan-project-{suffix}",
        project_code=f"PLAN-{suffix}",
        display_name=f"合成回款项目 {suffix}",
        lifecycle_status="ongoing",
        is_active=active,
    )
    db.add(project)
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id=f"plan-pc-{suffix}",
        project_id=project.project_id,
        contract_id=f"plan-contract-{suffix}",
        contract_no=f"XS-PLAN-{suffix}",
        contract_amount=None,
        contract_status="active",
        status_mapping_state="mapped",
        status_mapping_version="synthetic-v1",
        included_in_total=True,
        effective_from=effective_from,
        effective_to=effective_to,
        source="synthetic-test",
    )
    db.add(contract)
    db.commit()
    return project.project_id, contract.project_contract_id


def _binding(
    db,
    *,
    order_no: str,
    project_id: str,
    pc_id: str,
    user: SysUser,
    binding_id: str,
    version: int = 1,
) -> MaintenanceCollectionPlanSourceBinding:
    binding = MaintenanceCollectionPlanSourceBinding(
        binding_id=binding_id,
        source_system="project_manager_xls_v1",
        external_order_no=order_no,
        project_id=project_id,
        project_contract_id=pc_id,
        binding_status="reviewed",
        reviewed_by=user.id,
        reviewed_at=datetime.now(UTC),
        version=version,
    )
    db.add(binding)
    db.commit()
    return binding


def _milestone(
    db,
    *,
    project_id: str,
    pc_id: str,
    sequence: int,
    milestone_id: str,
    planned_date: date | None = date(2026, 9, 1),
    planned_amount: Decimal | None = Decimal("100.00"),
    source: str = "direct_api",
    batch_id: str | None = None,
    date_precision: str = "month",
    follow_up_status: str = "pending",
    follow_up_review_required: bool = False,
    followed_up_by: int | None = None,
    followed_up_at: datetime | None = None,
    version: int = 1,
) -> MaintenanceCollectionMilestone:
    milestone = MaintenanceCollectionMilestone(
        milestone_id=milestone_id,
        project_id=project_id,
        project_contract_id=pc_id,
        sequence=sequence,
        planned_date=planned_date,
        planned_amount=planned_amount,
        completeness_state="complete",
        source=source,
        source_batch_id=None,
        collection_plan_import_batch_id=batch_id,
        date_precision=date_precision,
        follow_up_status=follow_up_status,
        follow_up_review_required=follow_up_review_required,
        follow_up_note=None,
        followed_up_by=followed_up_by,
        followed_up_at=followed_up_at,
        version=version,
    )
    db.add(milestone)
    db.commit()
    return milestone


def _settings(monkeypatch, **overrides) -> None:
    base = dict(
        raw_file_dir=os.environ["RAW_FILE_DIR"],
        maintenance_collection_plan_apply_enabled=True,
        maintenance_collection_canary_project_id=None,
    )
    base.update(overrides)
    monkeypatch.setattr(imports, "get_settings", lambda: SimpleNamespace(**base))


def _cells(rows):
    cells = []
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            if value is not None:
                cells.append((r, c, value))
    return cells


def _row(
    order_no: str,
    project_name: str,
    *,
    months: list[str] | None = None,
    amounts: list | None = None,
    order_amount: float | str | None = 100.0,
) -> list:
    values = [None] * 64
    values[0] = order_no
    values[4] = project_name
    if order_amount is not None:
        values[9] = order_amount
    if months:
        for idx, month in enumerate(months):
            values[16 + idx * 2] = month
    if amounts:
        for idx, amount in enumerate(amounts):
            values[17 + idx * 2] = amount
    return values


def _workbook(*rows) -> bytes:
    return build_synthetic_biff8(
        [{"name": "维保项目清单", "cells": _cells([list(ORDERED_HEADERS), *rows])}]
    )


def _plan_admin(db, *, import_action: bool = True) -> SysUser:
    return _sys_user(
        db,
        username=OPERATOR,
        role="admin",
        import_action=import_action,
    )


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def _preview_service(
    db,
    user: SysUser,
    content: bytes,
    *,
    key: str = "preview-key-0001",
    filename: str = "synthetic-plan.xls",
):
    return imports.preview_collection_plan_import(
        db,
        content=content,
        filename=filename,
        idempotency_key=key,
        owner_user_id=user.id,
        operator=user.username,
        user_ctx=SimpleNamespace(user_id=user.username, role="admin", is_authenticated=True),
        as_of=AS_OF,
    )


def _bindings_payload(preview: dict, selections: dict[str, tuple[str, str]]) -> list[dict]:
    """把 row_key → (project_id, pc_id) 选择转换为 apply bindings。

    冻结 DTO 判别规则：existing_binding_version 非空时必须携带非空 reason
    （改派/确认绑定统一填写）；调用方可按需覆盖 reason。
    """
    result = []
    for row in preview["rows"]:
        project_id, pc_id = selections[row["row_key"]]
        existing_version = (
            row["binding"]["existing_binding_version"]
            if row["binding"]["status"] == "reviewed"
            else None
        )
        result.append(
            {
                "row_key": row["row_key"],
                "external_order_no": row["external_order_no"],
                "project_id": project_id,
                "project_version": 1,
                "project_contract_id": pc_id,
                "project_contract_version": 1,
                "existing_binding_version": existing_version,
                "reason": "确认既有绑定" if existing_version is not None else None,
            }
        )
    return result


def _apply_service(
    db,
    user: SysUser,
    *,
    batch_id: str,
    preview: dict,
    bindings: list[dict],
    expected_batch_version: int | None = None,
    expected_data_version: str | None = None,
):
    return imports.apply_collection_plan_import(
        db,
        batch_id=batch_id,
        expected_batch_version=(
            expected_batch_version if expected_batch_version is not None else preview["batch_version"]
        ),
        expected_data_version=(
            expected_data_version if expected_data_version is not None else preview["data_version"]
        ),
        bindings=_apply_binding_models(bindings),
        owner_user_id=user.id,
        operator=user.username,
        user_ctx=SimpleNamespace(user_id=user.username, role="admin", is_authenticated=True),
        as_of=AS_OF,
    )


def _apply_binding_models(bindings: list[dict]):
    from app.schemas.maintenance_collection_reminders import ApplyBinding

    return [ApplyBinding.model_validate(binding) for binding in bindings]


# ---------- preview ----------

def test_preview_creates_batch_and_evidence_with_zero_domain_writes(db):
    user = _plan_admin(db)
    content = _workbook(
        _row("ORD-0001", "合成项目 A", months=["2026年9月", "2026年10月"], amounts=[100.0, 200.0], order_amount=300.0),
        _row("ORD-0002", "合成项目 B", months=["2026年11月"], amounts=[50.0], order_amount=50.0),
    )
    digest = hashlib.sha256(content).hexdigest()
    preview = _preview_service(db, user, content)
    db.commit()

    assert preview["contract_version"] == "project-manager-xls-v1"
    assert preview["file_sha256"] == digest
    assert preview["status"] == "valid"
    assert preview["batch_version"] == 1
    assert preview["data_version"]
    assert preview["can_apply"] is True
    assert preview["expires_at"] > datetime.now(UTC)
    counts = preview["counts"]
    assert counts["projects"] == 2
    assert counts["milestones"] == 3
    assert counts["bound"] == 0
    assert counts["pending_binding"] == 2
    assert counts["blockers"] == 0
    assert counts["warnings"] == 0
    assert counts["create"] == 3
    assert len(preview["rows"]) == 2
    row = preview["rows"][0]
    assert row["binding"]["status"] == "pending_review"
    assert row["binding"]["project_id"] is None
    assert [d["change"] for d in row["milestone_diffs"]] == ["create", "create"]
    assert row["milestone_diffs"][0]["planned_month"] == "2026-09"
    assert row["milestone_diffs"][0]["planned_amount"] == "100.0"

    # 零领域事实写入：绑定/milestone/operation 都没有；只有批次与受控原件。
    db.expire_all()
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 0
    assert _count(db, MaintenanceCollectionMilestone) == 0
    assert _count(db, MaintenanceCollectionMilestoneOperation) == 0
    batch = db.scalar(select(MaintenanceCollectionPlanImportBatch))
    assert batch is not None
    assert batch.status == "valid"
    assert batch.plan_json["contract_version"] == "project-manager-xls-v1"
    assert batch.storage_key != "synthetic-plan.xls"
    evidence = Path(os.environ["RAW_FILE_DIR"]) / "maintenance-collection-plans" / batch.storage_key
    assert evidence.is_file()
    assert evidence.read_bytes() == content


def test_preview_bound_order_reports_reviewed_binding_and_diffs(db):
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="bound")
    _binding(db, order_no="ORD-BOUND-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-preview-1")
    content = _workbook(_row("ORD-BOUND-1", "合成项目 B1", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["counts"]["bound"] == 1
    assert preview["counts"]["pending_binding"] == 0
    row = preview["rows"][0]
    assert row["binding"]["status"] == "reviewed"
    assert row["binding"]["project_id"] == project_id
    assert row["binding"]["project_contract_id"] == pc_id
    assert row["binding"]["existing_binding_version"] == 1
    assert row["milestone_diffs"][0]["change"] == "create"
    assert row["milestone_diffs"][0]["expected_milestone_version"] is None


def test_preview_existing_milestone_change_classification(db):
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="diff")
    _binding(db, order_no="ORD-DIFF-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-diff-1")
    # 节点 1 事实相同（unchanged）；节点 2 计划不同（update）。
    _milestone(
        db, project_id=project_id, pc_id=pc_id, sequence=1, milestone_id="m-diff-1",
        planned_date=date(2026, 9, 1), planned_amount=Decimal("100.00"),
    )
    _milestone(
        db, project_id=project_id, pc_id=pc_id, sequence=2, milestone_id="m-diff-2",
        planned_date=date(2026, 1, 1), planned_amount=Decimal("1.00"),
    )
    content = _workbook(
        _row("ORD-DIFF-1", "合成项目 D", months=["2026年9月", "2026年10月"], amounts=[100.0, 200.0], order_amount=300.0)
    )
    preview = _preview_service(db, user, content)
    db.commit()
    diffs = preview["rows"][0]["milestone_diffs"]
    assert [d["change"] for d in diffs] == ["unchanged", "update"]
    assert [d["expected_milestone_version"] for d in diffs] == [1, 1]
    assert preview["counts"]["unchanged"] == 1
    assert preview["counts"]["update"] == 1


def test_preview_same_key_same_file_replays_same_batch(db):
    user = _plan_admin(db)
    content = _workbook(_row("ORD-REPLAY-1", "合成项目 R", months=["2026年9月"], amounts=[100.0]))
    first = _preview_service(db, user, content)
    db.commit()
    second = _preview_service(db, user, content)
    db.commit()
    assert second["batch_id"] == first["batch_id"]
    assert second["data_version"] == first["data_version"]
    assert _count(db, MaintenanceCollectionPlanImportBatch) == 1


def test_preview_same_key_different_file_409(db):
    user = _plan_admin(db)
    first = _preview_service(
        db, user, _workbook(_row("ORD-A-1", "项目 A", months=["2026年9月"], amounts=[100.0]))
    )
    db.commit()
    with pytest.raises(imports.CollectionPlanImportConflict) as excinfo:
        _preview_service(
            db, user, _workbook(_row("ORD-B-1", "项目 B", months=["2026年9月"], amounts=[200.0]))
        )
    assert excinfo.value.current_version == 1
    assert excinfo.value.current_data_version == first["data_version"]


def test_preview_new_key_same_file_creates_new_batch(db):
    user = _plan_admin(db)
    content = _workbook(_row("ORD-NEWKEY-1", "项目 N", months=["2026年9月"], amounts=[100.0]))
    first = _preview_service(db, user, content, key="preview-key-0001")
    db.commit()
    second = _preview_service(db, user, content, key="preview-key-0002")
    db.commit()
    assert second["batch_id"] != first["batch_id"]
    assert _count(db, MaintenanceCollectionPlanImportBatch) == 2


def test_preview_contract_error_keeps_evidence_and_replays_invalid(db):
    user = _plan_admin(db)
    headers = list(ORDERED_HEADERS)
    headers[16] = "回款时间X"
    content = build_synthetic_biff8([{"name": "Plan", "cells": _cells([headers])}])
    with pytest.raises(imports.CollectionPlanImportInvalid) as excinfo:
        _preview_service(db, user, content)
    assert excinfo.value.issues
    assert excinfo.value.issues[0]["code"] == "header_signature_mismatch"
    # 失败也保留哈希与受控原件证据。
    db.expire_all()
    batch = db.scalar(select(MaintenanceCollectionPlanImportBatch))
    assert batch is not None
    assert batch.status == "error"
    assert batch.plan_json is None
    assert batch.file_sha256 == hashlib.sha256(content).hexdigest()
    assert (Path(os.environ["RAW_FILE_DIR"]) / "maintenance-collection-plans" / batch.storage_key).is_file()
    # 同 key 重试 → 重放同一合同错误。
    with pytest.raises(imports.CollectionPlanImportInvalid):
        _preview_service(db, user, content)


def test_preview_blockers_produce_error_batch_and_counts(db):
    user = _plan_admin(db)
    orphan_row = _row("ORD-ORPHAN-1", "项目 O", months=["2026年9月"])
    content = _workbook(orphan_row)
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["status"] == "error"
    assert preview["can_apply"] is False
    assert preview["counts"]["blockers"] == 1
    assert any(issue["code"] == "orphan_date" for issue in preview["issues"])
    db.expire_all()
    batch = db.scalar(select(MaintenanceCollectionPlanImportBatch))
    assert batch.status == "error"
    assert batch.plan_json is not None


def test_preview_total_mismatch_is_warning_not_blocker(db):
    user = _plan_admin(db)
    content = _workbook(
        _row("ORD-WARN-1", "项目 W", months=["2026年9月"], amounts=[100.0], order_amount=999.0)
    )
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["status"] == "valid"
    assert preview["can_apply"] is True
    assert preview["counts"]["warnings"] == 1
    assert preview["rows"][0]["warning_codes"] == ["plan_total_mismatch"]
    assert any(issue["code"] == "plan_total_mismatch" for issue in preview["issues"])


def test_preview_short_idempotency_key_rejected(db):
    user = _plan_admin(db)
    with pytest.raises(imports.CollectionPlanImportInvalid):
        _preview_service(db, user, _workbook(), key="short")


def test_preview_concurrent_same_key_converges_to_one_batch(db, monkeypatch):
    user = _plan_admin(db)
    # 只把纯值传给 worker 线程，绝不在线程内触碰主会话的 ORM 对象。
    owner_user_id = user.id
    username = user.username
    content = _workbook(_row("ORD-CONC-1", "项目 C", months=["2026年9月"], amounts=[100.0]))
    raw_dir = Path(os.environ["RAW_FILE_DIR"]) / "maintenance-collection-plans"
    raw_dir.mkdir(parents=True, exist_ok=True)
    before = set(os.listdir(raw_dir))
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        session = SessionLocal()
        try:
            payload = imports.preview_collection_plan_import(
                session,
                content=content,
                filename="concurrent-plan.xls",
                idempotency_key="concurrent-key-01",
                owner_user_id=owner_user_id,
                operator=username,
                user_ctx=SimpleNamespace(user_id=username, role="admin", is_authenticated=True),
                as_of=AS_OF,
            )
            session.commit()
            results.append(payload)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    db.expire_all()
    assert _count(db, MaintenanceCollectionPlanImportBatch) == 1
    assert len({payload["batch_id"] for payload in results}) == 1
    after = set(os.listdir(raw_dir))
    assert len(after - before) == 1  # 并发 loser 只清理自己的文件，仅剩一份受控原件


# ---------- binding options ----------

def test_binding_options_search_minimal_fields_and_paging(db):
    user = _plan_admin(db)
    _project(db, suffix="alpha")
    _project(db, suffix="beta")
    _project(db, suffix="gamma")
    content = _workbook(_row("ORD-OPT-1", "项目 O", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    payload = imports.search_collection_binding_options(
        db,
        batch_id=preview["batch_id"],
        q_text="PLAN-al",
        page=1,
        page_size=20,
        user_ctx=SimpleNamespace(user_id=user.username, role="admin", is_authenticated=True),
    )
    assert payload["total"] == 1
    assert len(payload["rows"]) == 1
    project = payload["rows"][0]
    assert set(project.keys()) == {"project_id", "project_code", "display_name", "version", "contracts"}
    assert project["project_code"] == "PLAN-alpha"
    assert project["version"] == 1
    assert len(project["contracts"]) == 1
    contract = project["contracts"][0]
    assert set(contract.keys()) == {
        "project_contract_id", "contract_no", "relation_status", "lifecycle_status", "version",
    }
    assert contract["project_contract_id"] == "plan-pc-alpha"
    assert contract["relation_status"] == "active"
    assert contract["lifecycle_status"] == "active"


def test_binding_options_short_query_and_page_size_rejected(db):
    user = _plan_admin(db)
    content = _workbook(_row("ORD-OPT-2", "项目 O", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    ctx = SimpleNamespace(user_id=user.username, role="admin", is_authenticated=True)
    with pytest.raises(imports.CollectionPlanImportInvalid):
        imports.search_collection_binding_options(db, batch_id=preview["batch_id"], q_text="a", page=1, page_size=20, user_ctx=ctx)
    with pytest.raises(imports.CollectionPlanImportInvalid):
        imports.search_collection_binding_options(db, batch_id=preview["batch_id"], q_text="   ", page=1, page_size=20, user_ctx=ctx)
    with pytest.raises(imports.CollectionPlanImportInvalid):
        imports.search_collection_binding_options(db, batch_id=preview["batch_id"], q_text="alpha", page=1, page_size=51, user_ctx=ctx)


def test_binding_options_batch_not_found_404(db):
    user = _plan_admin(db)
    with pytest.raises(imports.CollectionPlanImportNotFound):
        imports.search_collection_binding_options(
            db,
            batch_id="no-such-batch",
            q_text="alpha",
            page=1,
            page_size=20,
            user_ctx=SimpleNamespace(user_id=user.username, role="admin", is_authenticated=True),
        )


# ---------- apply ----------

def test_apply_creates_bindings_and_milestones_atomically(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="apply")
    content = _workbook(
        _row("ORD-APP-1", "项目 A", months=["2026年9月", "2026年10月"], amounts=[100.0, 200.0], order_amount=300.0)
    )
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()

    assert result["status"] == "applied"
    assert result["idempotent_replay"] is False
    assert result["counts"]["created"] == 2
    assert result["counts"]["updated"] == 0
    assert result["counts"]["unchanged"] == 0
    assert result["counts"]["source_missing"] == 0
    assert result["counts"]["needs_review"] == 0

    db.expire_all()
    binding = db.scalar(select(MaintenanceCollectionPlanSourceBinding))
    assert binding is not None
    assert binding.project_id == project_id
    assert binding.project_contract_id == pc_id
    assert binding.binding_status == "reviewed"
    assert binding.reviewed_by == user.id
    milestones = db.scalars(
        select(MaintenanceCollectionMilestone).order_by(MaintenanceCollectionMilestone.sequence)
    ).all()
    assert [(m.sequence, m.date_precision) for m in milestones] == [(1, "month"), (2, "month")]
    assert milestones[0].planned_date == date(2026, 9, 1)
    assert milestones[0].planned_amount == Decimal("100.0")
    assert milestones[0].source == "project_manager_xls_v1"
    assert milestones[0].collection_plan_import_batch_id == preview["batch_id"]
    batch = db.get(MaintenanceCollectionPlanImportBatch, preview["batch_id"])
    assert batch.status == "applied"
    assert batch.version == 2
    assert batch.apply_payload_hash
    assert batch.applied_by == OPERATOR
    assert batch.applied_at is not None
    assert batch.result_json["counts"]["created"] == 2


def test_apply_replay_same_payload_returns_first_result(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="replay")
    content = _workbook(_row("ORD-REP-1", "项目 R", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    first = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    second = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    assert second["idempotent_replay"] is True
    assert second["counts"] == first["counts"]
    assert _count(db, MaintenanceCollectionMilestone) == 1
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 1


def test_apply_same_batch_different_payload_409(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_a, pc_a = _project(db, suffix="a")
    project_b, pc_b = _project(db, suffix="b")
    content = _workbook(_row("ORD-DIFF-1", "项目 D", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    first_bindings = _bindings_payload(preview, {row["row_key"]: (project_a, pc_a) for row in preview["rows"]})
    _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=first_bindings)
    db.commit()
    other_bindings = _bindings_payload(preview, {row["row_key"]: (project_b, pc_b) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportConflict):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=other_bindings)
    db.rollback()


def test_apply_batch_version_drift_409(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="batchver")
    content = _workbook(_row("ORD-BV-1", "项目 B", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportConflict) as excinfo:
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings, expected_batch_version=99)
    assert excinfo.value.current_version == 1
    db.rollback()
    db.expire_all()
    assert _count(db, MaintenanceCollectionMilestone) == 0
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 0


def test_apply_milestone_version_drift_409_zero_writes(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="milestonever")
    _binding(db, order_no="ORD-MV-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-mv-1")
    _milestone(
        db, project_id=project_id, pc_id=pc_id, sequence=1, milestone_id="m-mv-1",
        planned_date=date(2026, 9, 1), planned_amount=Decimal("100.00"),
    )
    content = _workbook(_row("ORD-MV-1", "项目 M", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    # 预览后被他人修改了节点版本 → 整批 409，零领域写入。
    db.expire_all()
    milestone = db.get(MaintenanceCollectionMilestone, "m-mv-1")
    milestone.version = 2
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportConflict):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.rollback()
    db.expire_all()
    batch = db.get(MaintenanceCollectionPlanImportBatch, preview["batch_id"])
    assert batch.status == "valid"
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 1  # 预览前既有绑定，无新增
    assert _count(db, MaintenanceCollectionMilestone) == 1


def test_apply_fails_closed_when_apply_flag_disabled(db, monkeypatch):
    _settings(monkeypatch, maintenance_collection_plan_apply_enabled=False)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="flagoff")
    content = _workbook(_row("ORD-FLAG-1", "项目 F", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportPermissionError):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.rollback()
    db.expire_all()
    assert _count(db, MaintenanceCollectionMilestone) == 0


def test_apply_canary_scope_denied_whole_batch(db, monkeypatch):
    _settings(monkeypatch, maintenance_collection_canary_project_id="plan-project-canary")
    user = _plan_admin(db)
    canary_project, canary_pc = _project(db, suffix="canary")
    other_project, other_pc = _project(db, suffix="other")
    content = _workbook(
        _row("ORD-CAN-1", "项目 C", months=["2026年9月"], amounts=[100.0]),
        _row("ORD-CAN-2", "项目 C2", months=["2026年10月"], amounts=[200.0]),
    )
    preview = _preview_service(db, user, content)
    db.commit()
    rows = {row["row_key"]: row for row in preview["rows"]}
    # 一个绑定到 canary、一个绑定到其他项目 → 整批 403 / canary_scope_denied。
    bindings = _bindings_payload(preview, {
        rows[list(rows)[0]]["row_key"]: (canary_project, canary_pc),
        rows[list(rows)[1]]["row_key"]: (other_project, other_pc),
    })
    with pytest.raises(imports.CollectionPlanImportCanaryDenied):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.rollback()
    db.expire_all()
    assert _count(db, MaintenanceCollectionMilestone) == 0
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 0


def test_apply_unbound_order_without_binding_422(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="unbound")
    content = _workbook(
        _row("ORD-UB-1", "项目 U", months=["2026年9月"], amounts=[100.0]),
        _row("ORD-UB-2", "项目 U2", months=["2026年10月"], amounts=[200.0]),
    )
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportInvalid):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings[:-1])
    db.rollback()
    db.expire_all()
    assert _count(db, MaintenanceCollectionMilestone) == 0


def test_apply_reviewed_binding_with_empty_client_bindings_creates_milestone(db, monkeypatch):
    """已有 reviewed binding 且客户端 bindings=[] 也可 apply 创建 milestone（设计 §6.4/§8.4）。

    reviewed 行沿用冻结 plan_json 里的项目/合同/版本前提，不要求浏览器重复提交。
    """
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="reviewed-empty")
    _binding(db, order_no="ORD-RE-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-re-1")
    content = _workbook(_row("ORD-RE-1", "项目 R", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["counts"]["bound"] == 1
    assert preview["counts"]["pending_binding"] == 0
    assert preview["rows"][0]["binding"]["status"] == "reviewed"
    assert preview["rows"][0]["binding"]["existing_binding_version"] == 1

    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=[])
    db.commit()
    assert result["status"] == "applied"
    assert result["idempotent_replay"] is False
    assert result["counts"]["created"] == 1

    db.expire_all()
    binding = db.scalar(select(MaintenanceCollectionPlanSourceBinding))
    assert binding.external_order_no == "ORD-RE-1"
    assert binding.project_id == project_id
    assert binding.project_contract_id == pc_id
    assert binding.version == 1  # 沿用既有 reviewed 绑定：不重建、不 bump、不写改派审计
    assert _count(db, MaintenanceProjectOperationAudit) == 0
    milestone = db.scalar(select(MaintenanceCollectionMilestone))
    assert milestone is not None
    assert milestone.project_contract_id == pc_id
    assert milestone.planned_date == date(2026, 9, 1)
    assert milestone.planned_amount == Decimal("100.0")
    assert milestone.collection_plan_import_batch_id == preview["batch_id"]
    batch = db.get(MaintenanceCollectionPlanImportBatch, preview["batch_id"])
    assert batch.status == "applied"
    assert batch.version == 2


def test_apply_mixed_reviewed_and_pending_submits_only_pending_row(db, monkeypatch):
    """混合 reviewed + pending 时只提交 pending 行选择也可 apply。

    reviewed 行缺省沿用冻结 plan_json 绑定，pending 行必须人工选择后才可应用。
    """
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_a, pc_a = _project(db, suffix="mixed-a")
    project_b, pc_b = _project(db, suffix="mixed-b")
    _binding(db, order_no="ORD-MX-1", project_id=project_a, pc_id=pc_a, user=user, binding_id="bind-mx-1")
    content = _workbook(
        _row("ORD-MX-1", "项目 A", months=["2026年9月"], amounts=[100.0]),
        _row("ORD-MX-2", "项目 B", months=["2026年10月"], amounts=[200.0]),
    )
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["counts"]["bound"] == 1
    assert preview["counts"]["pending_binding"] == 1
    reviewed = next(row for row in preview["rows"] if row["binding"]["status"] == "reviewed")
    pending = next(row for row in preview["rows"] if row["binding"]["status"] == "pending_review")
    assert reviewed["external_order_no"] == "ORD-MX-1"
    assert pending["external_order_no"] == "ORD-MX-2"

    bindings = [
        {
            "row_key": pending["row_key"],
            "external_order_no": pending["external_order_no"],
            "project_id": project_b,
            "project_version": 1,
            "project_contract_id": pc_b,
            "project_contract_version": 1,
            "existing_binding_version": None,
            "reason": None,
        }
    ]
    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    assert result["status"] == "applied"
    assert result["counts"]["created"] == 2
    assert result["counts"]["updated"] == 0
    assert result["counts"]["needs_review"] == 0

    db.expire_all()
    rows = db.scalars(
        select(MaintenanceCollectionPlanSourceBinding).order_by(
            MaintenanceCollectionPlanSourceBinding.external_order_no
        )
    ).all()
    assert len(rows) == 2
    by_order = {row.external_order_no: row for row in rows}
    assert by_order["ORD-MX-1"].project_contract_id == pc_a
    assert by_order["ORD-MX-1"].version == 1
    assert by_order["ORD-MX-2"].project_contract_id == pc_b
    assert by_order["ORD-MX-2"].version == 1
    milestones = db.scalars(
        select(MaintenanceCollectionMilestone).order_by(MaintenanceCollectionMilestone.sequence)
    ).all()
    assert len(milestones) == 2
    assert {m.project_contract_id for m in milestones} == {pc_a, pc_b}
    assert {m.planned_amount for m in milestones} == {Decimal("100.0"), Decimal("200.0")}
    # reviewed 行沿用冻结绑定 → 不产生改派审计。
    assert _count(db, MaintenanceProjectOperationAudit) == 0


def test_apply_handled_milestone_update_keeps_handled_and_sets_review_required(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="handled")
    _binding(db, order_no="ORD-HD-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-hd-1")
    _milestone(
        db, project_id=project_id, pc_id=pc_id, sequence=1, milestone_id="m-hd-1",
        planned_date=date(2026, 1, 1), planned_amount=Decimal("1.00"),
        follow_up_status="handled", follow_up_review_required=False,
        followed_up_by=user.id, followed_up_at=datetime.now(UTC), version=2,
    )
    content = _workbook(_row("ORD-HD-1", "项目 H", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["rows"][0]["milestone_diffs"][0]["change"] == "update"
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    assert result["counts"]["updated"] == 1
    assert result["counts"]["needs_review"] == 1
    db.expire_all()
    milestone = db.get(MaintenanceCollectionMilestone, "m-hd-1")
    assert milestone.follow_up_status == "handled"
    assert milestone.follow_up_review_required is True
    assert milestone.planned_date == date(2026, 9, 1)
    assert milestone.version == 3


def test_apply_source_missing_reported_never_deleted(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="missing")
    _binding(db, order_no="ORD-MS-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-ms-1")
    content = _workbook(_row("ORD-MS-1", "项目 M", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    # 旧 XLS 节点（序列 2 不在新计划中）→ source_missing 只报告。
    _milestone(
        db, project_id=project_id, pc_id=pc_id, sequence=2, milestone_id="m-ms-old",
        planned_date=date(2026, 5, 1), planned_amount=Decimal("50.00"),
        source="project_manager_xls_v1", batch_id=preview["batch_id"], version=1,
    )
    # 重新预览：预览期差异同样报告 source_missing（含旧节点版本前提）。
    preview = _preview_service(db, user, content, key="preview-key-0002")
    db.commit()
    assert preview["counts"]["source_missing"] == 1
    diffs = preview["rows"][0]["milestone_diffs"]
    missing_diff = next(d for d in diffs if d["change"] == "source_missing")
    assert missing_diff["sequence"] == 2
    assert missing_diff["planned_month"] is None
    assert missing_diff["expected_milestone_version"] == 1
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    assert result["counts"]["source_missing"] == 1
    db.expire_all()
    old = db.get(MaintenanceCollectionMilestone, "m-ms-old")
    assert old is not None
    assert old.planned_date == date(2026, 5, 1)


def test_apply_unchanged_facts_do_not_bump_version(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="unchanged")
    _binding(db, order_no="ORD-UC-1", project_id=project_id, pc_id=pc_id, user=user, binding_id="bind-uc-1")
    _milestone(
        db, project_id=project_id, pc_id=pc_id, sequence=1, milestone_id="m-uc-1",
        planned_date=date(2026, 9, 1), planned_amount=Decimal("100.0"),
    )
    content = _workbook(_row("ORD-UC-1", "项目 U", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    assert preview["rows"][0]["milestone_diffs"][0]["change"] == "unchanged"
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["updated"] == 0
    db.expire_all()
    milestone = db.get(MaintenanceCollectionMilestone, "m-uc-1")
    assert milestone.version == 1


def test_apply_reassignment_requires_reason_and_writes_audit(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_a, pc_a = _project(db, suffix="reassign-a")
    project_b, pc_b = _project(db, suffix="reassign-b")
    _binding(db, order_no="ORD-RA-1", project_id=project_a, pc_id=pc_a, user=user, binding_id="bind-ra-1")
    content = _workbook(_row("ORD-RA-1", "项目 R", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_b, pc_b) for row in preview["rows"]})
    bindings[0]["reason"] = "项目归属调整"
    result = _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.commit()
    assert result["counts"]["created"] == 1
    db.expire_all()
    binding = db.scalar(select(MaintenanceCollectionPlanSourceBinding))
    assert binding.project_id == project_b
    assert binding.project_contract_id == pc_b
    assert binding.version == 2
    audit = db.scalar(
        select(MaintenanceProjectOperationAudit).where(
            MaintenanceProjectOperationAudit.action == "reassign"
        )
    )
    assert audit is not None
    assert audit.before_json["project_contract_id"] == pc_a
    assert audit.after_json["project_contract_id"] == pc_b
    assert audit.reason == "项目归属调整"
    assert audit.operated_by == OPERATOR
    # 新合同上没有旧版本前提 → 节点直接创建。
    milestone = db.scalar(select(MaintenanceCollectionMilestone))
    assert milestone.project_contract_id == pc_b


def test_apply_reassignment_to_contract_with_existing_sequence_requires_repreview(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_a, pc_a = _project(db, suffix="reassign-existing-a")
    project_b, pc_b = _project(db, suffix="reassign-existing-b")
    _binding(db, order_no="ORD-RX-1", project_id=project_a, pc_id=pc_a, user=user, binding_id="bind-rx-1")
    _milestone(
        db, project_id=project_b, pc_id=pc_b, sequence=1, milestone_id="m-rx-target",
        planned_date=date(2026, 8, 1), planned_amount=Decimal("80.00"), version=3,
    )
    content = _workbook(_row("ORD-RX-1", "项目 RX", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_b, pc_b) for row in preview["rows"]})
    bindings[0]["reason"] = "项目归属调整"

    with pytest.raises(imports.CollectionPlanImportConflict):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)

    db.rollback()
    db.expire_all()
    binding = db.scalar(select(MaintenanceCollectionPlanSourceBinding))
    assert binding.project_id == project_a
    assert binding.project_contract_id == pc_a
    assert binding.version == 1
    target = db.get(MaintenanceCollectionMilestone, "m-rx-target")
    assert target.project_contract_id == pc_b
    assert target.planned_date == date(2026, 8, 1)
    assert target.planned_amount == Decimal("80.00")
    assert target.version == 3


def test_apply_reassignment_without_reason_rejected_by_dto(db, monkeypatch):
    """改派必须有非空理由（判别结构在 DTO 层强制）。"""
    from app.schemas.maintenance_collection_reminders import ApplyBinding

    with pytest.raises(ValueError):
        ApplyBinding.model_validate(
            {
                "row_key": "row",
                "external_order_no": "ORD-RA-2",
                "project_id": "p-b",
                "project_version": 1,
                "project_contract_id": "pc-b",
                "project_contract_version": 1,
                "existing_binding_version": 1,
                "reason": "   ",
            }
        )


def test_apply_expired_batch_409(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    project_id, pc_id = _project(db, suffix="expired")
    content = _workbook(_row("ORD-EXP-1", "项目 E", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    db.expire_all()
    batch = db.get(MaintenanceCollectionPlanImportBatch, preview["batch_id"])
    batch.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportConflict):
        _apply_service(db, user, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.rollback()
    db.expire_all()
    assert _count(db, MaintenanceCollectionMilestone) == 0


def test_apply_not_owner_404(db, monkeypatch):
    _settings(monkeypatch)
    owner = _plan_admin(db)
    stranger = _sys_user(db, username="plan_stranger", role="admin", import_action=True)
    project_id, pc_id = _project(db, suffix="owner")
    content = _workbook(_row("ORD-OWN-1", "项目 O", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, owner, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    with pytest.raises(imports.CollectionPlanImportNotFound):
        _apply_service(db, stranger, batch_id=preview["batch_id"], preview=preview, bindings=bindings)
    db.rollback()


def test_apply_concurrent_at_most_one_writes(db, monkeypatch):
    _settings(monkeypatch)
    user = _plan_admin(db)
    owner_user_id = user.id
    username = user.username
    project_id, pc_id = _project(db, suffix="concurrent")
    content = _workbook(_row("ORD-ACC-1", "项目 C", months=["2026年9月"], amounts=[100.0]))
    preview = _preview_service(db, user, content)
    db.commit()
    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    binding_models = _apply_binding_models(bindings)
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        session = SessionLocal()
        try:
            payload = imports.apply_collection_plan_import(
                session,
                batch_id=preview["batch_id"],
                expected_batch_version=preview["batch_version"],
                expected_data_version=preview["data_version"],
                bindings=binding_models,
                owner_user_id=owner_user_id,
                operator=username,
                user_ctx=SimpleNamespace(user_id=username, role="admin", is_authenticated=True),
                as_of=AS_OF,
            )
            session.commit()
            results.append(payload)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    db.expire_all()
    # 两个并发 apply 最多一个产生领域写入：只有一组节点与一条绑定。
    assert _count(db, MaintenanceCollectionMilestone) == 1
    assert _count(db, MaintenanceCollectionPlanSourceBinding) == 1
    assert sorted(payload["idempotent_replay"] for payload in results) == [False, True]
    batch = db.get(MaintenanceCollectionPlanImportBatch, preview["batch_id"])
    assert batch.status == "applied"


# ---------- API 形状 ----------

def test_api_preview_and_apply_roundtrip_shape(db, monkeypatch):
    _settings(monkeypatch)
    client, user = _client(db, username="plan_api_admin", role="admin", import_action=True)
    project_id, pc_id = _project(db, suffix="api")
    content = _workbook(_row("ORD-API-1", "项目 A", months=["2026年9月"], amounts=[100.0]))
    preview_response = client.post(
        "/api/maintenance/collection-plan-imports/preview",
        files={"file": ("api-plan.xls", content, "application/vnd.ms-excel")},
        headers={"Idempotency-Key": "api-preview-key-01"},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert set(preview.keys()) == {
        "batch_id", "batch_version", "data_version", "status", "contract_version",
        "file_sha256", "counts", "rows", "issues", "can_apply", "expires_at",
    }
    row = preview["rows"][0]
    assert set(row.keys()) == {
        "row_key", "external_order_no", "source_project_name", "binding",
        "milestone_diffs", "warning_codes", "blocker_codes",
    }
    assert set(row["binding"].keys()) == {
        "status", "project_id", "project_version", "project_contract_id",
        "project_contract_version", "existing_binding_version",
    }
    assert set(row["milestone_diffs"][0].keys()) == {
        "sequence", "planned_month", "planned_amount", "change", "expected_milestone_version",
    }

    options = client.get(
        f"/api/maintenance/collection-plan-imports/{preview['batch_id']}/binding-options",
        params={"q": "PLAN-api", "page": 1, "page_size": 20},
    )
    assert options.status_code == 200, options.text
    assert options.json()["rows"][0]["contracts"][0]["project_contract_id"] == pc_id

    bindings = _bindings_payload(preview, {row["row_key"]: (project_id, pc_id) for row in preview["rows"]})
    apply_response = client.post(
        f"/api/maintenance/collection-plan-imports/{preview['batch_id']}/apply",
        json={
            "expected_batch_version": preview["batch_version"],
            "expected_data_version": preview["data_version"],
            "bindings": bindings,
        },
    )
    assert apply_response.status_code == 200, apply_response.text
    applied = apply_response.json()
    assert set(applied.keys()) == {
        "batch_id", "batch_version", "data_version", "status", "counts",
        "idempotent_replay", "applied_at",
    }
    assert applied["status"] == "applied"
    assert applied["counts"]["created"] == 1

    replay_response = client.post(
        f"/api/maintenance/collection-plan-imports/{preview['batch_id']}/apply",
        json={
            "expected_batch_version": preview["batch_version"],
            "expected_data_version": preview["data_version"],
            "bindings": bindings,
        },
    )
    assert replay_response.status_code == 200
    assert replay_response.json()["idempotent_replay"] is True

    # 已应用批次 + 不同 payload → 409 version_conflict（领域错误体）。
    other_project, other_pc = _project(db, suffix="api-other")
    other_bindings = _bindings_payload(preview, {row["row_key"]: (other_project, other_pc) for row in preview["rows"]})
    conflict = client.post(
        f"/api/maintenance/collection-plan-imports/{preview['batch_id']}/apply",
        json={
            "expected_batch_version": preview["batch_version"],
            "expected_data_version": preview["data_version"],
            "bindings": other_bindings,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "version_conflict"
