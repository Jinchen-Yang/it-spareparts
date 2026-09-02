"""M1-6：WBDD 专用上传端点契约（plan v1.3 §4.1）——矩阵/错类型零写入/幂等/flag。"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import permissions
from app.auth import hash_password
from app.config import get_settings
from app.etl import loader, pipeline
from app.main import app
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_source_assignments as source_assignments
from app.services import maintenance_project_operations as operations
from app.services import maintenance_wbdd_import as wbdd
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook

_PASSWORD = "synthetic-wbdd-password-1"


@pytest.fixture(autouse=True)
def _boss_flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def _client(db, *, username: str, role: str = "readonly",
            overrides: dict[str, bool] | None = None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password(_PASSWORD), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {}, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _upload(client: TestClient, path: str, *, key: str | None = None):
    headers = {"Idempotency-Key": key or f"idem-{uuid.uuid4()}"}
    with open(path, "rb") as f:
        return client.post(
            "/api/maintenance/wbdd-imports",
            files={"file": ("wbdd.xlsx", f,
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet")},
            headers=headers,
        )


def _wbdd_file(tmp_path, name="wbdd.xlsx", **kw):
    return write_workbook(str(tmp_path / name), COLUMNS_91,
                          make_rows(orders=1, lines_per_order=1, **kw))


def _zero_rows(db) -> bool:
    return (db.execute(select(func.count(FMaintenanceOrder.id))).scalar_one() == 0
            and db.execute(select(func.count(FMaintenanceLine.id))).scalar_one() == 0)


# ---------- 权限矩阵 ----------

def test_upload_requires_auth(db, tmp_path):
    client = TestClient(app)
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 401


def test_upload_denied_without_action_key(db, tmp_path):
    client = _client(db, username="wbdd-page-only",
                     overrides={"page_maintenance": True})
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 403
    assert _zero_rows(db)


def test_upload_denied_without_page(db, tmp_path):
    client = _client(db, username="wbdd-no-page",
                     overrides={"action_maintenance_wbdd_import": True})
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 403


def test_upload_allowed_with_page_and_action(db, tmp_path):
    client = _client(db, username="wbdd-uploader",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = _upload(client, _wbdd_file(tmp_path))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layout"] == "91"
    assert body["orders_inserted"] == 1
    assert "recompute" in body and "snapshot_diff" in body
    assert body["replayed"] is False


def test_upload_auto_assigns_only_current_batch_by_xsdd(db, tmp_path):
    """只投影本批：已有合同 owner 挂靠；无 owner 留待销售事实建项。"""
    existing = MaintenanceProject(
        project_id="wbdd-existing-project",
        project_code="WBDD-EXISTING",
        display_name="已有维保项目",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    # 名称恰好命中另一个项目也不能推翻 XSDD 唯一 owner；名称只会成为灰字 alias。
    name_collision = MaintenanceProject(
        project_id="wbdd-name-collision-project",
        project_code="WBDD-NAME-COLLISION",
        display_name="已有维保项目-别名一",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add_all([existing, name_collision])
    db.commit()
    operations.create_contract(
        db,
        project_id=existing.project_id,
        contract_id="wbdd-existing-contract",
        contract_no="XSDD-20990101-0001",
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="test",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="建立销售合同 owner",
        operated_by="test",
    )
    db.commit()

    # 先留一张不属于本次上传的历史未归属单；新导入不得顺手扫描全库。
    historical_rows = make_rows(orders=1, lines_per_order=1,
                                project="历史未归属项目")
    historical_rows[0].update({
        "数据ID(不可修改)": "HIST-O-001",
        "需求单号": "WBDD-HIST-001",
        "销售订单": "XSDD-20990101-0099",
        "需求明细.数据ID(不可修改)": "HIST-L-001",
    })
    historical_path = write_workbook(
        str(tmp_path / "historical-unassigned.xlsx"),
        COLUMNS_91,
        historical_rows,
    )
    pipeline.run_import(
        db,
        historical_path,
        "historical-unassigned.xlsx",
        uploaded_by="fixture",
        mode="upsert",
    )
    db.commit()

    rows = make_rows(orders=4, lines_per_order=1)
    cases = [
        ("CUR-O-001", "CUR-L-001", "WBDD-CUR-001",
         "XSDD-20990101-0001", "已有维保项目-别名一"),
        ("CUR-O-002", "CUR-L-002", "WBDD-CUR-002",
         "XSDD-20990101-0001", "已有维保项目-别名二"),
        ("CUR-O-003", "CUR-L-003", "WBDD-CUR-003",
         "XSDD-20990101-0002", "同号新项目名称一"),
        ("CUR-O-004", "CUR-L-004", "WBDD-CUR-004",
         "XSDD-20990101-0002", "同号新项目名称二"),
    ]
    for row, (order_id, line_id, order_no, xsdd, project_name) in zip(rows, cases):
        row.update({
            "数据ID(不可修改)": order_id,
            "需求单号": order_no,
            "销售订单": xsdd,
            "销售人员": "导入不应回填负责人",
            "项目名": project_name,
            "需求明细.数据ID(不可修改)": line_id,
        })
    current_path = write_workbook(
        str(tmp_path / "current-batch.xlsx"), COLUMNS_91, rows
    )
    client = _client(
        db,
        username="wbdd-scoped-auto-link",
        overrides={"page_maintenance": True,
                   "action_maintenance_wbdd_import": True},
    )
    response = _upload(client, current_path)
    assert response.status_code == 200, response.text
    assert response.json()["auto_assignment"] == {
        "assigned_orders": 2,
        "matched_projects": 1,
        "created_projects": 0,
        "skipped_groups": 1,
        "skipped_ambiguous": 0,
        "sales_filled_projects": 0,
        "manager_filled_projects": 0,
        "assignments_created": 0,
        "pending_owner_order_ids": ["CUR-O-003", "CUR-O-004"],
        "pending_owner_order_ids_truncated": False,
    }

    db.expire_all()
    assignments = {
        source_id: project_id
        for source_id, project_id in db.execute(
            select(
                MaintenanceSourceOrderAssignment.source_order_id,
                MaintenanceSourceOrderAssignment.project_id,
            ).where(MaintenanceSourceOrderAssignment.is_active.is_(True))
        )
    }
    assert "HIST-O-001" not in assignments
    assert assignments["CUR-O-001"] == existing.project_id
    assert assignments["CUR-O-002"] == existing.project_id
    assert "CUR-O-003" not in assignments
    assert "CUR-O-004" not in assignments
    assert (
        db.get(MaintenanceProjectXsdd, "20990101-0001").project_id
        == existing.project_id
    )
    assert db.get(MaintenanceProjectXsdd, "20990101-0002") is None
    assert db.get(MaintenanceProjectXsdd, "20990101-0099") is None
    assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 2
    db.refresh(existing)
    assert existing.salesperson is None
    assert existing.project_manager_id is None


def test_skip_mode_assigns_from_current_head_identity(db, tmp_path):
    """skip 保留旧头时，自动归属必须使用 DB 当前 XSDD，不得按上传新值建错项目。"""
    project = MaintenanceProject(
        project_id="wbdd-skip-current-project",
        project_code="WBDD-SKIP-CURRENT",
        display_name="旧头项目",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add(project)
    db.add(MaintenanceProjectXsdd(
        xsdd_norm="20990104-0001",
        project_id=project.project_id,
        source="test",
    ))
    db.commit()

    original = make_rows(orders=1, lines_per_order=1, project="旧头项目")
    original[0]["销售订单"] = "XSDD-20990104-0001"
    original_path = write_workbook(
        str(tmp_path / "skip-original.xlsx"), COLUMNS_91, original
    )
    pipeline.run_import(
        db,
        original_path,
        "skip-original.xlsx",
        uploaded_by="fixture",
        mode="upsert",
    )
    db.commit()

    incoming = make_rows(orders=1, lines_per_order=1, project="上传新头项目")
    incoming[0]["销售订单"] = "XSDD-20990104-0002"
    incoming[0]["修改时间(必填)"] = "2026-07-17 12:00"
    incoming_path = write_workbook(
        str(tmp_path / "skip-incoming.xlsx"), COLUMNS_91, incoming
    )
    batch = pipeline.run_import(
        db,
        incoming_path,
        "skip-incoming.xlsx",
        uploaded_by="fixture",
        mode="skip",
        auto_assign_maintenance_projects=True,
    )
    db.commit()

    order = db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.raw_order_id == "SYN-O001"
    ))
    assert order.linked_sales_order_no == "XSDD-20990104-0001"
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment))
    assert assignment.project_id == project.project_id
    assert db.get(MaintenanceProjectXsdd, "20990104-0002") is None
    assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 1
    assert batch.report_json["auto_assignment"]["assigned_orders"] == 1


def test_wbdd_only_account_cannot_use_generic_import(db, tmp_path):
    """WBDD-only 账号连 /api/import/upload 都是 403（无 page_import）——铁律 6。"""
    client = _client(db, username="wbdd-only",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    with open(path, "rb") as f:
        resp = client.post("/api/import/upload",
                           files={"file": ("wbdd.xlsx", f, "application/octet-stream")})
    assert resp.status_code == 403


# ---------- 文件类型门（零写入） ----------

def test_non_wbdd_file_rejected_zero_write(db, tmp_path):
    """销售导出（订单编号+业务类型特征）传到 WBDD 端点 → 422 not_wbdd_file 零写入。"""
    from openpyxl import Workbook
    path = str(tmp_path / "sales.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.append(["订单编号", "业务类型", "订单日期"])
    ws.append(["XSDD-1", "备件销售", "2026-01-01"])
    wb.save(path)
    client = _client(db, username="wbdd-up2",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = _upload(client, path)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "not_wbdd_file"
    assert _zero_rows(db)
    assert db.execute(select(func.count(MaintenanceWbddImportReceipt.id))
                      ).scalar_one() == 0


def test_narrow_wbdd_file_rejected_layout_unknown(db, tmp_path):
    """能被识别为 maintenance 但非完整 90/91 列布局（截断导出）→ 422 layout_unknown 零写入。"""
    from openpyxl import Workbook
    path = str(tmp_path / "narrow.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.append(["需求单号", "需求类型", "需求明细.数据ID(不可修改)", "需求明细.需求数量"])
    ws.append(["WBDD-1", "报修供货", "L1", "1"])
    wb.save(path)
    client = _client(db, username="wbdd-up3",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    resp = _upload(client, path)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "layout_unknown"
    assert _zero_rows(db)


# ---------- 幂等 ----------

def test_idempotency_key_required(db, tmp_path):
    client = _client(db, username="wbdd-up4",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    with open(path, "rb") as f:
        resp = client.post("/api/maintenance/wbdd-imports",
                           files={"file": ("wbdd.xlsx", f, "application/octet-stream")})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_idempotency_key"


def test_import_lock_identity_is_text_safe_and_preserves_field_boundaries():
    identity = wbdd._import_lock_identity("operator\x00name", "key\x00value")
    assert "\x00" not in identity
    assert wbdd._import_lock_identity("a:b", "c") != wbdd._import_lock_identity(
        "a", "b:c"
    )


def test_same_idempotency_key_replays_original_report(db, tmp_path):
    client = _client(db, username="wbdd-up5",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    key = "replay-key-0001"
    first = _upload(client, path, key=key)
    assert first.status_code == 200
    lines_after_first = db.execute(
        select(func.count(FMaintenanceLine.id))).scalar_one()
    second = _upload(client, path, key=key)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    # 重放零新写
    assert db.execute(select(func.count(FMaintenanceLine.id))
                      ).scalar_one() == lines_after_first
    assert db.execute(select(func.count(MaintenanceWbddImportReceipt.id))
                      ).scalar_one() == 1


# ---------- flag 门 ----------

def test_flag_off_hides_endpoints_and_keeps_stable_routes(db, tmp_path):
    client = _client(db, username="wbdd-up6", role="admin",
                     overrides={})
    settings = get_settings()
    settings.maintenance_boss_dashboard_enabled = False
    try:
        resp = _upload(client, _wbdd_file(tmp_path))
        assert resp.status_code == 404
        assert client.get("/api/maintenance/wbdd-imports/latest").status_code == 404
        # 稳定维保端点不受影响
        assert client.get("/api/maintenance/projects").status_code == 200
    finally:
        settings.maintenance_boss_dashboard_enabled = True


# ---------- /latest ----------

def test_latest_health_before_and_after_upload(db, tmp_path):
    client = _client(db, username="wbdd-up7",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    empty = client.get("/api/maintenance/wbdd-imports/latest")
    assert empty.status_code == 200
    body = empty.json()
    assert body["readiness"] == "not_imported"
    assert body["orders_total"] is None          # 未导入绝不显示 0（铁律 5）
    _upload(client, _wbdd_file(tmp_path))
    after = client.get("/api/maintenance/wbdd-imports/latest").json()
    assert after["readiness"] == "ready"
    assert after["as_of"] == "2026-07-15"
    assert after["layout"] == "91"


def test_recompute_busy_rolls_back_whole_import_fail_closed(db, tmp_path):
    """单事务化（2026-08-26）：重算忙/失败 → 事实、回执、批次整体回滚（fail closed）。

    旧两半提交语义下，首调用 409 时回执已提交而成本回填缺失，需要回放补跑；
    现在导入事实、回执、成本重算与 revision bump 同一事务——重算失败即整体
    回滚，绝不留下「新事实 + 旧成本」的可见窗口。upsert 幂等，客户端同
    Idempotency-Key 整体重试安全（重试是全新导入，不是回放）。
    """
    from app.services.maintenance_cost import MaintenanceCostRecomputeBusy
    from app.services import maintenance_wbdd_import as wbdd

    client = _client(db, username="wbdd-replay",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["销售订单"] = "XSDD-20990102-0001"
    path = write_workbook(
        str(tmp_path / "recompute-rollback.xlsx"), COLUMNS_91, rows
    )
    key = f"idem-{uuid.uuid4()}"

    calls = {"n": 0}
    real = wbdd.maintenance_cost.recompute

    def flaky(session, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise MaintenanceCostRecomputeBusy("另一重算进行中")
        return real(session, **kw)

    wbdd.maintenance_cost.recompute = flaky
    try:
        first = _upload(client, path, key=key)
        assert first.status_code == 409
        assert first.json()["detail"]["code"] == "recompute_busy"
        # fail closed：事实/回执都没有落库（会话已被端点依赖关闭，重查确认）
        assert _zero_rows(db)
        assert db.scalar(select(func.count(SysImportBatch.id))) == 0
        assert db.execute(select(func.count(MaintenanceWbddImportReceipt.id))
                          ).scalar_one() == 0
        assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 0
        assert db.scalar(select(func.count(MaintenanceProjectXsdd.xsdd_norm))) == 0
        assert db.scalar(select(func.count(MaintenanceProjectAlias.alias_id))) == 0
        assert db.scalar(select(func.count(
            MaintenanceSourceOrderAssignment.assignment_id
        ))) == 0

        second = _upload(client, path, key=key)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["replayed"] is False, "首调用整体回滚后，重试是全新导入而非回放"
        assert body["recompute"] is not None
        assert calls["n"] == 2
        db.expire_all()
        receipt = db.execute(select(MaintenanceWbddImportReceipt)).scalars().one()
        assert receipt.report_json["recompute"] is not None
        assert db.scalar(select(func.count(SysImportBatch.id))) == 1
        assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 1
        assert db.scalar(select(func.count(MaintenanceProjectXsdd.xsdd_norm))) == 1
        assert db.scalar(select(func.count(MaintenanceProjectAlias.alias_id))) == 1
        assert db.scalar(select(func.count(
            MaintenanceSourceOrderAssignment.assignment_id
        ))) == 1
    finally:
        wbdd.maintenance_cost.recompute = real


def test_assignment_race_maps_to_retryable_conflict(db, tmp_path, monkeypatch):
    client = _client(
        db,
        username="wbdd-assignment-race",
        overrides={"page_maintenance": True,
                   "action_maintenance_wbdd_import": True},
    )
    path = _wbdd_file(tmp_path, "assignment-race.xlsx")

    def conflict(*_args, **_kwargs):
        raise loader.WorkbookInvalidationConflictError("synthetic assignment race")

    monkeypatch.setattr(pipeline, "run_import", conflict)
    response = _upload(client, path)

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"]["code"] == "import_concurrency_conflict"


def test_auto_assign_service_conflict_rolls_back_and_maps_to_409(
    db, tmp_path, monkeypatch
):
    """归属服务在 facts 写入后报 OCC：API 409，整批项目与事实零写入。"""
    client = _client(
        db,
        username="wbdd-auto-assign-conflict",
        overrides={"page_maintenance": True,
                   "action_maintenance_wbdd_import": True},
    )
    path = _wbdd_file(tmp_path, "auto-assign-conflict.xlsx")

    def conflict(*_args, **_kwargs):
        raise source_assignments.SourceAssignmentConflict(
            "synthetic auto assignment conflict"
        )

    monkeypatch.setattr(
        source_assignments,
        "auto_assign_imported_orders",
        conflict,
    )
    response = _upload(client, path)

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"]["code"] == "import_concurrency_conflict"
    db.expire_all()
    assert _zero_rows(db)
    assert db.scalar(select(func.count(SysImportBatch.id))) == 0
    assert db.scalar(select(func.count(MaintenanceWbddImportReceipt.id))) == 0
    assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 0
    assert db.scalar(select(func.count(MaintenanceProjectXsdd.xsdd_norm))) == 0
    assert db.scalar(select(func.count(MaintenanceProjectAlias.alias_id))) == 0
    assert db.scalar(select(func.count(
        MaintenanceSourceOrderAssignment.assignment_id
    ))) == 0


def test_auto_assign_target_escape_uses_real_envelope_guard(
    db, tmp_path, monkeypatch
):
    """实际目标不在导入前信封时，subset guard 必须 409 并回滚 facts。"""
    project = MaintenanceProject(
        project_id="wbdd-envelope-target",
        project_code="WBDD-ENVELOPE",
        display_name="信封外目标项目",
        lifecycle_status="ongoing",
        is_active=True,
        version=1,
    )
    db.add(project)
    db.add(MaintenanceProjectXsdd(
        xsdd_norm="20990103-0001",
        project_id=project.project_id,
        source="test",
    ))
    db.commit()

    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["销售订单"] = "XSDD-20990103-0001"
    path = write_workbook(
        str(tmp_path / "envelope-escape.xlsx"), COLUMNS_91, rows
    )
    client = _client(
        db,
        username="wbdd-envelope-escape",
        overrides={"page_maintenance": True,
                   "action_maintenance_wbdd_import": True},
    )

    # 模拟 prelock 之后才出现目标：apply 使用真实 subset guard 拒绝补晚锁。
    monkeypatch.setattr(
        source_assignments,
        "prelock_import_assignment_targets",
        lambda *_args, **_kwargs: set(),
    )
    response = _upload(client, path)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "import_concurrency_conflict"
    db.expire_all()
    assert _zero_rows(db)
    assert db.scalar(select(func.count(SysImportBatch.id))) == 0
    assert db.scalar(select(func.count(MaintenanceWbddImportReceipt.id))) == 0
    assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 1
    assert db.get(MaintenanceProjectXsdd, "20990103-0001").project_id == project.project_id
    assert db.scalar(select(func.count(MaintenanceProjectAlias.alias_id))) == 0
    assert db.scalar(select(func.count(
        MaintenanceSourceOrderAssignment.assignment_id
    ))) == 0


def test_unresolvable_xsdd_rolls_back_instead_of_succeeding_unassigned(
    db, tmp_path
):
    """唯一 owner 已停用时不能静默跳过；专用上传必须 409 且事实零写入。"""
    inactive = MaintenanceProject(
        project_id="wbdd-inactive-xsdd-owner",
        project_code="WBDD-INACTIVE",
        display_name="已停用历史项目",
        lifecycle_status="ended",
        is_active=False,
        version=1,
    )
    db.add(inactive)
    db.add(MaintenanceProjectXsdd(
        xsdd_norm="20990105-0001",
        project_id=inactive.project_id,
        source="test",
    ))
    db.commit()

    rows = make_rows(orders=1, lines_per_order=1, project="上传项目名")
    rows[0]["销售订单"] = "XSDD-20990105-0001"
    path = write_workbook(
        str(tmp_path / "inactive-xsdd-owner.xlsx"), COLUMNS_91, rows
    )
    client = _client(
        db,
        username="wbdd-inactive-xsdd-owner",
        overrides={"page_maintenance": True,
                   "action_maintenance_wbdd_import": True},
    )
    response = _upload(client, path)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "import_concurrency_conflict"
    db.expire_all()
    assert _zero_rows(db)
    assert db.scalar(select(func.count(SysImportBatch.id))) == 0
    assert db.scalar(select(func.count(MaintenanceWbddImportReceipt.id))) == 0
    assert db.scalar(select(func.count(MaintenanceProject.project_id))) == 1
    assert db.scalar(select(func.count(MaintenanceProjectAlias.alias_id))) == 0
    assert db.scalar(select(func.count(
        MaintenanceSourceOrderAssignment.assignment_id
    ))) == 0


def test_replay_backfills_recompute_for_legacy_half_committed_receipt(db, tmp_path):
    """历史半提交回执（2026-08-26 单事务化之前：事实已提交、recompute 缺失）：
    重放仍必须补跑成本回填，否则那批单的成本永远停在导入前口径。"""
    from app.services import maintenance_wbdd_import as wbdd

    client = _client(db, username="wbdd-legacy",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    key = f"idem-{uuid.uuid4()}"
    assert _upload(client, path, key=key).status_code == 200

    # 模拟单事务化之前留下的历史回执：report_json 里没有 recompute
    receipt = db.execute(select(MaintenanceWbddImportReceipt)).scalars().one()
    legacy_report = {k: v for k, v in (receipt.report_json or {}).items()
                     if k != "recompute"}
    receipt.report_json = legacy_report
    db.commit()

    calls = {"n": 0}
    real = wbdd.maintenance_cost.recompute

    def counted(session, **kw):
        calls["n"] += 1
        assert kw.get("commit") is False
        return real(session, **kw)

    wbdd.maintenance_cost.recompute = counted
    try:
        again = _upload(client, path, key=key)
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert again.json()["recompute"] is not None, "历史回执重放必须补跑成本回填"
        assert calls["n"] == 1
    finally:
        wbdd.maintenance_cost.recompute = real


def test_replay_does_not_rerun_recompute_when_already_done(db, tmp_path):
    """正常回放仍是纯读：不得重复触发重算（幂等）。"""
    from app.services import maintenance_wbdd_import as wbdd

    client = _client(db, username="wbdd-replay2",
                     overrides={"page_maintenance": True,
                                "action_maintenance_wbdd_import": True})
    path = _wbdd_file(tmp_path)
    key = f"idem-{uuid.uuid4()}"
    assert _upload(client, path, key=key).status_code == 200

    calls = {"n": 0}
    real = wbdd.maintenance_cost.recompute

    def counted(session, **kw):
        calls["n"] += 1
        return real(session, **kw)

    wbdd.maintenance_cost.recompute = counted
    try:
        again = _upload(client, path, key=key)
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert calls["n"] == 0
    finally:
        wbdd.maintenance_cost.recompute = real
