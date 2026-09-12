"""存量项目业务类型回填（2026-09-08）：从销售订单推，唯一才补，冲突交人工。

生产 648 个项目里 647 个 `maintenance_project.business_type` 为空。写入侧已修好
（此后新建的 XSDD 项目自带业务类型），但存量得回填——647 个手工补录不现实。

推导链沿用卡片「销售」与负责人回填的同一形状（`salesperson_modes_by_project`）：
项目 → 活跃挂靠的 WBDD 单 → `linked_sales_order_no` → `f_sales_order.business_type`。

三条铁律：
1. **只填空**，绝不覆盖已有值（人工补录过的、台账写过的都不许动）。
2. **唯一才填**：名下推出两种及以上业务类型 ⇒ 不猜，列进冲突清单交人工。
   这是多合同项目的常态，也是「业务类型只作分类」口径下唯一诚实的处置。
3. **先预览后应用**：预览不写库，应用逐项走 catalog.update_project ⇒ 版本 +1、
   审计留痕改前改后，与人工补录同一条写路径。
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.maintenance import FMaintenanceOrder
from app.models.sales import FSalesOrder
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectAuditLog
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import maintenance_business_type_backfill as backfill


def _batch(db) -> int:
    from app.models.system import SysImportBatch

    row = SysImportBatch(
        filename="bt-backfill.xlsx", file_hash=uuid.uuid4().hex,
        file_type="maintenance", status="success", uploaded_by="bt-test",
    )
    db.add(row)
    db.flush()
    return row.id


def _project(db, *, business_type=None) -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()),
        project_code=f"BTB-{uuid.uuid4().hex[:8]}",
        display_name=f"回填测试项目-{uuid.uuid4().hex[:6]}",
        lifecycle_status="ongoing",
        business_type=business_type,
        is_active=True,
        version=1,
    )
    db.add(project)
    db.flush()
    return project


def _sales_order(db, order_no: str, business_type: str) -> FSalesOrder:
    row = FSalesOrder(
        raw_order_id=f"raw-{uuid.uuid4()}",
        order_no=order_no,
        order_date=date(2026, 1, 1),
        business_type=business_type,
        data_status="已生效",
        amount_ex_tax=Decimal("100000.00"),
        tax_rate=Decimal("0.13"),
        import_batch_id=_batch(db),
    )
    db.add(row)
    db.flush()
    return row


_ARCHIVED_AT = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _attach(db, project, order_no: str, *, active=True, data_status="已生效"):
    """给项目挂一张活跃 WBDD 单，单上挂着某个 XSDD。"""

    order = FMaintenanceOrder(
        raw_order_id=f"wbdd-{uuid.uuid4()}",
        order_no=f"WBDD-{uuid.uuid4().hex[:8]}",
        order_date=date(2026, 1, 1),
        linked_sales_order_no=order_no,
        data_status=data_status,
        import_batch_id=_batch(db),
    )
    db.add(order)
    db.flush()
    assignment = MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()),
        source_order_id=order.raw_order_id,
        project_id=project.project_id,
        is_active=active,
        version=1,
        created_by="bt-test",
        # 失效行必须一次插到位：CHECK 要求同时带归档人且 archived_at >= created_at，
        # 而挂靠历史有不可变触发器（source order assignment history is immutable），
        # 插进去之后翻不了。
        created_at=None if active else _ARCHIVED_AT,
        archived_by=None if active else "bt-test",
        archived_at=None if active else _ARCHIVED_AT,
    )
    db.add(assignment)
    db.flush()
    db.flush()
    return order


# ---------- 预览 ----------

def test_preview_derives_a_unique_business_type_without_writing(db):
    project = _project(db)
    _sales_order(db, "XSDD-20260101-0001", "整体维保")
    _attach(db, project, "XSDD-20260101-0001")
    db.commit()

    plan = backfill.preview(db)

    fillable = {row["project_id"]: row for row in plan["fillable"]}
    assert project.project_id in fillable
    assert fillable[project.project_id]["business_type"] == "整体维保"
    assert fillable[project.project_id]["evidence_order_nos"] == ["XSDD-20260101-0001"]

    db.refresh(project)
    assert project.business_type is None, "预览不得写库"


def test_preview_reports_conflicts_instead_of_guessing(db):
    """名下两种业务类型：不猜，进冲突清单。"""

    project = _project(db)
    _sales_order(db, "XSDD-20260101-0002", "整体维保")
    _sales_order(db, "XSDD-20260101-0003", "备件维保")
    _attach(db, project, "XSDD-20260101-0002")
    _attach(db, project, "XSDD-20260101-0003")
    db.commit()

    plan = backfill.preview(db)

    assert project.project_id not in {row["project_id"] for row in plan["fillable"]}
    conflict = next(
        row for row in plan["conflicts"] if row["project_id"] == project.project_id
    )
    assert sorted(conflict["business_types"]) == ["备件维保", "整体维保"]


def test_preview_skips_projects_that_already_have_a_business_type(db):
    """只填空：人工补录过 / 台账写过的一律不动。"""

    project = _project(db, business_type="算力运维")
    _sales_order(db, "XSDD-20260101-0004", "整体维保")
    _attach(db, project, "XSDD-20260101-0004")
    db.commit()

    plan = backfill.preview(db)
    every = (
        {row["project_id"] for row in plan["fillable"]}
        | {row["project_id"] for row in plan["conflicts"]}
        | {row["project_id"] for row in plan["underivable"]}
    )
    assert project.project_id not in every


def test_preview_lists_projects_with_no_evidence_as_underivable(db):
    """推不出来的要如实列出来，别让人以为回填能包圆。"""

    project = _project(db)
    db.commit()

    plan = backfill.preview(db)
    assert project.project_id in {row["project_id"] for row in plan["underivable"]}


def test_preview_ignores_inactive_assignments_and_blank_business_types(db):
    project = _project(db)
    _sales_order(db, "XSDD-20260101-0005", "整体维保")
    _attach(db, project, "XSDD-20260101-0005", active=False)
    _sales_order(db, "XSDD-20260101-0006", "")
    _attach(db, project, "XSDD-20260101-0006")
    db.commit()

    plan = backfill.preview(db)
    assert project.project_id in {row["project_id"] for row in plan["underivable"]}


# ---------- 应用 ----------

def test_apply_writes_only_the_previewed_projects_with_audit(db):
    target = _project(db)
    _sales_order(db, "XSDD-20260101-0007", "备件维保")
    _attach(db, target, "XSDD-20260101-0007")

    conflicted = _project(db)
    _sales_order(db, "XSDD-20260101-0008", "整体维保")
    _sales_order(db, "XSDD-20260101-0009", "算力运维")
    _attach(db, conflicted, "XSDD-20260101-0008")
    _attach(db, conflicted, "XSDD-20260101-0009")
    db.commit()

    before_version = target.version
    result = backfill.apply(db, operated_by="bt-backfill-test", reason="存量回填")
    db.commit()

    assert result["filled"] == 1
    assert result["conflicts"] == 1

    db.refresh(target)
    db.refresh(conflicted)
    assert target.business_type == "备件维保"
    assert target.version == before_version + 1
    assert conflicted.business_type is None, "冲突项目一个字都不许写"

    audit = db.scalars(
        select(MaintenanceProjectAuditLog)
        .where(
            MaintenanceProjectAuditLog.entity_id == target.project_id,
            MaintenanceProjectAuditLog.action == "update",
        )
        .order_by(MaintenanceProjectAuditLog.id.desc())
    ).first()
    assert audit is not None
    assert (audit.after_json or {}).get("business_type") == "备件维保"
    assert (audit.before_json or {}).get("business_type") is None
    assert "回填" in (audit.reason or "")


def test_apply_is_idempotent(db):
    project = _project(db)
    _sales_order(db, "XSDD-20260101-0010", "整体维保")
    _attach(db, project, "XSDD-20260101-0010")
    db.commit()

    first = backfill.apply(db, operated_by="bt-backfill-test", reason="存量回填")
    db.commit()
    second = backfill.apply(db, operated_by="bt-backfill-test", reason="存量回填")
    db.commit()

    assert first["filled"] == 1
    assert second["filled"] == 0, "重跑不得重复写"
    db.refresh(project)
    assert project.version == 2


def test_apply_never_overwrites_a_human_edit(db):
    """回填跑在人工补录之后也不能把人改的盖掉。"""

    project = _project(db, business_type="算力运维")
    _sales_order(db, "XSDD-20260101-0011", "整体维保")
    _attach(db, project, "XSDD-20260101-0011")
    db.commit()

    result = backfill.apply(db, operated_by="bt-backfill-test", reason="存量回填")
    db.commit()

    assert result["filled"] == 0
    db.refresh(project)
    assert project.business_type == "算力运维"


# ---------- HTTP 端点 ----------

def _client(db, *, username: str, role: str = "admin", overrides: dict | None = None):
    from fastapi.testclient import TestClient

    from app import permissions
    from app.auth import hash_password
    from app.main import app
    from app.models.system import SysUser

    base = permissions.effective(role, None)
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password("backfill-pw-1"), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {},
        permissions=permissions.effective_from_snapshot(base, overrides or {}),
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "backfill-pw-1"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


_BASE = "/api/maintenance/projects/stable/business-type-backfill"


def test_preview_endpoint_is_read_only(db):
    project = _project(db)
    _sales_order(db, "XSDD-20260101-0020", "整体维保")
    _attach(db, project, "XSDD-20260101-0020")
    db.commit()

    client = _client(db, username="bt-backfill-admin")
    response = client.get(f"{_BASE}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert project.project_id in {row["project_id"] for row in body["fillable"]}

    db.expire_all()
    assert db.get(MaintenanceProject, project.project_id).business_type is None


def test_apply_endpoint_writes_and_reports(db):
    project = _project(db)
    _sales_order(db, "XSDD-20260101-0021", "备件维保")
    _attach(db, project, "XSDD-20260101-0021")
    db.commit()

    client = _client(db, username="bt-backfill-admin2")
    response = client.post(f"{_BASE}/apply", json={"reason": "存量业务类型回填"})
    assert response.status_code == 200, response.text
    assert response.json()["filled"] >= 1

    db.expire_all()
    assert db.get(MaintenanceProject, project.project_id).business_type == "备件维保"


def test_backfill_requires_the_project_manage_action(db):
    """批量改经营口径不能对只读账号开放。"""

    client = _client(db, username="bt-backfill-readonly", role="readonly",
                     overrides={"page_maintenance": True})
    assert client.get(f"{_BASE}/preview").status_code == 403
    assert client.post(
        f"{_BASE}/apply", json={"reason": "试图越权回填"}
    ).status_code == 403
