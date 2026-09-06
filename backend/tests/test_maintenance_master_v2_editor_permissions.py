"""2.7.0 项目总表编辑权下放（D-03，2026-09-02 拍板）。

D-03 原句：「项目负责人对本人项目工作簿全部字段可见可改（含成本与合同额）；
销售限本人项目，成本与合同额仍受 data_profit 利润键控制」。

- primary_manager 挂靠 → 下载/校验/应用全量可用（无 action 键、无 data_profit 也放行）；
- canonical 销售 → 必须同时持有 data_profit（V2 总表整本带成本列、无列级脱敏），
  否则 flag False 且下载/校验/应用 403；
- 仅 viewer 挂靠或无关账号 → 403 fail-closed；
- 合同额编辑对负责人放开（此前需管理员双键）。
"""
import io
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from app import permissions as permissions_mod
from app.auth import hash_password
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.system import SysUser
from app.security import UserContext
from app.services import maintenance_project_assignments as assignments
from app.services import maintenance_project_master_workbook as master

from tests.test_maintenance_project_master_v2_editable import (
    _make_project_with_line,
    _save,
)

_PASSWORD = "pw123456"


def _user(db, username, *, salesperson_name=None, display_name=None,
          data_profit=False):
    base = permissions_mod.effective("readonly", None)
    overrides = {"page_maintenance": True, "data_profit": data_profit}
    user = SysUser(
        username=username, role="readonly", display_name=display_name or username,
        salesperson_name=salesperson_name,
        password_hash=hash_password(_PASSWORD), is_active=True,
        template_code="readonly", template_version=1, template_perms=base,
        perm_overrides=overrides,
        permissions=permissions_mod.effective_from_snapshot(base, overrides),
    )
    db.add(user)
    db.flush()
    return user


def _client_for(db, user) -> TestClient:
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": user.username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _manager_client(db, project):
    user = _user(db, f"mgr-{uuid.uuid4().hex[:6]}")
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type="primary_manager", user_id=user.id, version=1,
        assigned_by="test", assignment_reason="负责人编辑权测试",
    ))
    return _client_for(db, user)


def _sales_client(db, project, *, match=True, data_profit=False):
    name = project.salesperson if match and project.salesperson else "销售甲"
    if match and not project.salesperson:
        project.salesperson = name
    user = _user(db, f"sales-{uuid.uuid4().hex[:6]}",
                 salesperson_name=name if match else "无关销售",
                 display_name=name if match else "无关销售",
                 data_profit=data_profit)
    return _client_for(db, user)


def _viewer_client(db, project):
    user = _user(db, f"viewer-{uuid.uuid4().hex[:6]}")
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type="viewer", user_id=user.id, version=1,
        assigned_by="test", assignment_reason="仅可见不应可编",
    ))
    return _client_for(db, user)


def _download(client, project_id):
    return client.get(f"/api/maintenance/projects/stable/{project_id}"
                      f"/master-workbook.xlsx")


def _upload(client, project_id, wb, *, force=False, action="apply"):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    files = {"file": ("m.xlsx", buf, "application/vnd.openxmlformats-"
                                     "officedocument.spreadsheetml.sheet")}
    data = {"force_takeover": "true"} if force else None
    return client.post(
        f"/api/maintenance/projects/stable/{project_id}/master-workbook/{action}",
        files=files, data=data)


def _validate(client, project_id, wb):
    return _upload(client, project_id, wb, action="validate")


def _master_flag(client, project_id):
    """展示板单卡与稳定项目详情下发的 can_edit_master_workbook（同一判定函数）。"""
    card = client.get(f"/api/maintenance/boss-board/projects/{project_id}")
    assert card.status_code == 200, card.text
    return card.json()["can_edit_master_workbook"]


def test_is_project_workbook_editor_matrix(db):
    project, _part, _order, _line = _make_project_with_line(db)
    manager = _user(db, f"mgr-{uuid.uuid4().hex[:6]}")
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type="primary_manager", user_id=manager.id, version=1,
        assigned_by="test", assignment_reason="负责人",
    ))
    db.commit()

    yes = UserContext(user_id=manager.username, role="readonly",
                      is_authenticated=True)
    assert assignments.is_project_workbook_editor(
        db, project_id=project.project_id, user_ctx=yes)

    project.salesperson = "销售甲"
    db.commit()
    # D-03：销售限本人项目，成本与合同额仍受 data_profit 控制——V2 总表整本带
    # 成本列，没有利润键的本人项目销售不是编辑者；持键才是。
    sales = UserContext(user_id="whatever", role="readonly",
                        salesperson_name="销售甲", is_authenticated=True,
                        permissions={"data_profit": True})
    assert assignments.is_project_workbook_editor(
        db, project_id=project.project_id, user_ctx=sales)
    sales_no_profit = UserContext(user_id="whatever", role="readonly",
                                  salesperson_name="销售甲", is_authenticated=True,
                                  permissions={"data_profit": False})
    assert not assignments.is_project_workbook_editor(
        db, project_id=project.project_id, user_ctx=sales_no_profit)
    # 负责人分支不看利润键（D-03 第一句：全部字段含成本与合同额）
    assert assignments.is_project_workbook_editor(
        db, project_id=project.project_id,
        user_ctx=UserContext(user_id=manager.username, role="readonly",
                             is_authenticated=True,
                             permissions={"data_profit": False}))

    stranger = UserContext(user_id="nobody", role="readonly",
                           salesperson_name="别的销售", is_authenticated=True,
                           permissions={"data_profit": True})
    assert not assignments.is_project_workbook_editor(
        db, project_id=project.project_id, user_ctx=stranger)
    anonymous = UserContext(user_id=None, role="readonly", is_authenticated=False)
    assert not assignments.is_project_workbook_editor(
        db, project_id=project.project_id, user_ctx=anonymous)


def test_project_manager_without_action_key_can_download_and_apply(
    db, monkeypatch,
):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, line = _make_project_with_line(db)
    client = _manager_client(db, project)

    resp = _download(client, project.project_id)
    assert resp.status_code == 200, resp.text
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb[master.V2_SHEET_PARTS]
    headers = {c.value: c.column for c in ws[1]}
    target_row = next(
        r for r in range(2, ws.max_row + 1)
        if str(ws.cell(r, headers["实体ID"]).value or "") == str(line.id))
    ws.cell(target_row, headers["需求数量"], 3)

    applied = _upload(client, project.project_id, wb)
    assert applied.status_code == 200, applied.text
    assert applied.json()["line_updates"] >= 1
    db.refresh(line)
    assert line.qty == Decimal("3.00")


def test_salesperson_of_project_needs_profit_key_for_flag_download_validate_apply(
    db, monkeypatch,
):
    """D-03：销售限本人项目，成本与合同额仍受 data_profit 控制。

    持利润键的本人项目销售：flag True，下载/校验/应用放行；
    无利润键的同一销售：flag False，下载/校验/应用一律 403——四处必须同口径，
    否则前端按 flag 给出「上传覆盖」入口而后端 403（或反过来）。
    """
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, _line = _make_project_with_line(db)
    workbook = load_workbook(io.BytesIO(
        master.build_project_master_v2(db, project_id=project.project_id)))

    with_profit = _sales_client(db, project, match=True, data_profit=True)
    assert _master_flag(with_profit, project.project_id) is True
    assert _download(with_profit, project.project_id).status_code == 200
    assert _validate(with_profit, project.project_id, workbook).status_code == 200
    assert _upload(with_profit, project.project_id, workbook).status_code == 200

    without_profit = _sales_client(db, project, match=True, data_profit=False)
    assert _master_flag(without_profit, project.project_id) is False
    assert _download(without_profit, project.project_id).status_code == 403
    assert _validate(without_profit, project.project_id, workbook).status_code == 403
    assert _upload(without_profit, project.project_id, workbook).status_code == 403


def test_master_edit_flag_mirrors_validate_gate(db, monkeypatch):
    """flag 与上传门（validate）逐账号同真同假：负责人 True/200；仅 viewer False/403；
    只有 data_profit、没有上传动作键也没有挂靠的账号 False/403。"""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, _line = _make_project_with_line(db)
    workbook = load_workbook(io.BytesIO(
        master.build_project_master_v2(db, project_id=project.project_id)))

    manager = _manager_client(db, project)
    assert _master_flag(manager, project.project_id) is True
    assert _validate(manager, project.project_id, workbook).status_code == 200

    viewer = _viewer_client(db, project)
    assert _master_flag(viewer, project.project_id) is False
    assert _validate(viewer, project.project_id, workbook).status_code == 403

    profit_only = _client_for(
        db, _user(db, f"profit-{uuid.uuid4().hex[:6]}", data_profit=True))
    assert _master_flag(profit_only, project.project_id) is False
    assert _validate(profit_only, project.project_id, workbook).status_code == 403


def test_stable_project_detail_exposes_master_edit_flag(db, monkeypatch):
    """面板总是先取稳定项目详情；展示板单卡可能 404，flag 也要挂在详情的 project 上
    （同一判定函数）：负责人 True，仅 viewer False。"""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, _line = _make_project_with_line(db)
    detail_path = f"/api/maintenance/projects/stable/{project.project_id}"

    manager = _manager_client(db, project)
    detail = manager.get(detail_path)
    assert detail.status_code == 200, detail.text
    assert detail.json()["project"]["can_edit_master_workbook"] is True

    viewer = _viewer_client(db, project)
    detail = viewer.get(detail_path)
    assert detail.status_code == 200, detail.text
    assert detail.json()["project"]["can_edit_master_workbook"] is False


def test_viewer_or_outside_salesperson_denied(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, _line = _make_project_with_line(db)

    viewer = _viewer_client(db, project)
    assert _download(viewer, project.project_id).status_code == 403

    outsider = _sales_client(db, project, match=False)
    assert _download(outsider, project.project_id).status_code == 403


def test_contract_amount_editable_by_manager(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, _line = _make_project_with_line(db)
    client = _manager_client(db, project)

    resp = _download(client, project.project_id)
    assert resp.status_code == 200, resp.text
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb[master.V2_SHEET_OVERVIEW]
    for row in range(2, ws.max_row + 1):
        if ws.cell(row, 1).value == "合同总额（含税）":
            ws.cell(row, 2, "20000.00")
            break
    else:
        pytest.fail("概览缺少合同总额行")
    applied = _upload(client, project.project_id, wb)
    assert applied.status_code == 200, applied.text


def test_board_project_card_exposes_master_edit_flag_from_the_same_gate(
    db, monkeypatch,
):
    """D-03：前端上传入口跟服务端 can_edit_master_workbook 走，它与上传门同一判定。

    负责人账号没有上传动作键、也没有 data_profit：卡片 flag 为 True 且下载放行；
    无关账号 flag 为 False 且下载 403——两者必须同时成立，否则前端就会与后端漂移。
    """
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "maintenance_project_master_v2_enabled",
                        True)
    project, _part, _order, _line = _make_project_with_line(db)
    card_path = f"/api/maintenance/boss-board/projects/{project.project_id}"

    manager = _manager_client(db, project)
    card = manager.get(card_path)
    assert card.status_code == 200, card.text
    assert card.json()["can_edit_master_workbook"] is True
    assert _download(manager, project.project_id).status_code == 200

    outsider = _sales_client(db, project, match=False)
    card = outsider.get(card_path)
    assert card.status_code == 200, card.text
    assert card.json()["can_edit_master_workbook"] is False
    assert _download(outsider, project.project_id).status_code == 403
