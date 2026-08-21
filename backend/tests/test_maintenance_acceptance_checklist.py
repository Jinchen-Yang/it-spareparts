"""验收需求清单 Excel 导入（2026-08-21 客户反馈）。

服务层：解析归一化/示例行防呆/幂等重放/整表替换/失败关闭；
API 层：权限门（无 action 403）、GET 清单、模板下载。
"""

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.auth import hash_password
from app.main import app
from app.models.maintenance_acceptance_checklist import (
    MaintenanceAcceptanceChecklistBatch,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.system import SysUser
from app.services import maintenance_acceptance_checklist as svc

_PASSWORD = "synthetic-checklist-1"


def _xlsx(rows: list[tuple], *, sheet: str = "验收清单", header_row: int = 1) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for _ in range(header_row - 1):
        ws.append([])
    ws.append(["验收需求", "是否完成"])
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _project(db, name="清单测试项目") -> MaintenanceProject:
    proj = MaintenanceProject(project_id=str(uuid.uuid4()),
                              project_code=name, display_name=name,
                              lifecycle_status="ongoing")
    db.add(proj)
    db.commit()
    return proj


# ---------------------------------------------------------------- 服务层

def test_parse_normalizes_done_values_and_skips_examples(db):
    data = _xlsx([
        ("（示例）会被自动忽略", "是"),
        ("设备巡检报告归档", "是"),
        ("备件损耗清单签字", "否"),
        ("培训完成", "已完成"),
        ("文档移交", "未完成"),
        ("旧设备处置", "YES"),
        ("标签清理", "n"),
        ("完成为空视为待验收", ""),
    ], header_row=2)  # 表头不在第一行也要能定位
    parsed = svc.parse_checklist_workbook(data, "checklist.xlsx")
    assert parsed["item_rows"] == 7
    assert parsed["issue_rows"] == 0
    done = {it["requirement"]: it["done"] for it in parsed["items"]}
    assert done["设备巡检报告归档"] is True
    assert done["培训完成"] is True
    assert done["旧设备处置"] is True
    assert done["备件损耗清单签字"] is False
    assert done["文档移交"] is False
    assert done["标签清理"] is False
    assert done["完成为空视为待验收"] is False


def test_parse_marks_unrecognized_done_as_issue(db):
    data = _xlsx([("正常条目", "是"), ("异常条目", "大概吧")])
    parsed = svc.parse_checklist_workbook(data, "checklist.xlsx")
    assert parsed["issue_rows"] == 1
    assert any("大概吧" in msg for it in parsed["items"] for msg in it["issues"])


def test_parse_rejects_missing_header(db):
    wb = Workbook()
    wb.active.append(["别的列", "又一列"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(svc.ChecklistParseError):
        svc.parse_checklist_workbook(buf.getvalue(), "bad.xlsx")


def _preview(db, project, data, *, key=None, by="tester"):
    parsed = svc.parse_checklist_workbook(data, "checklist.xlsx")
    batch_id = svc.store_preview(db, parsed, project_id=project.project_id,
                                 uploaded_by=by,
                                 idempotency_key=key or f"key-{uuid.uuid4()}")
    # replay 分支对幂等冲突会 db.rollback()（API 层已 commit 的生产路径语义）——
    # 测试直连服务层必须先提交，否则回滚撤掉刚 flush 的批次
    db.commit()
    return batch_id


def test_store_preview_idempotent_replay(db):
    project = _project(db)
    key = "replay-key-123456"
    data = _xlsx([("条目一", "是")])
    bid1 = _preview(db, project, data, key=key)
    bid2 = _preview(db, project, data, key=key)
    assert bid1 == bid2
    # 同 key 不同文件 → 冲突
    other = _xlsx([("条目二", "否")])
    with pytest.raises(svc.ChecklistBatchError):
        _preview(db, project, other, key=key)


def test_apply_replaces_current_and_keeps_history(db):
    project = _project(db)
    bid1 = _preview(db, project, _xlsx([("旧条目", "是")]))
    result1 = svc.apply_batch(db, bid1, operated_by="tester")
    assert result1["item_rows"] == 1
    assert result1["replaced_batch_id"] is None

    bid2 = _preview(db, project, _xlsx([("新条目A", "是"), ("新条目B", "否")]))
    result2 = svc.apply_batch(db, bid2, operated_by="tester")
    assert result2["replaced_batch_id"] == bid1

    payload = svc.project_checklist(db, project.project_id)
    assert payload["current"]["batch_id"] == bid2
    assert [it["requirement"] for it in payload["current"]["items"]] \
        == ["新条目A", "新条目B"]
    assert payload["current"]["done_rows"] == 1
    assert payload["current"]["todo_rows"] == 1
    assert [h["batch_id"] for h in payload["history"]] == [bid2, bid1]


def test_apply_rejects_issue_rows_fail_closed(db):
    project = _project(db)
    good = _preview(db, project, _xlsx([("好条目", "是")]))
    svc.apply_batch(db, good, operated_by="tester")
    bad = _preview(db, project, _xlsx([("坏条目", "也许")]))
    with pytest.raises(svc.ChecklistBatchError, match="问题行"):
        svc.apply_batch(db, bad, operated_by="tester")
    batch = db.get(MaintenanceAcceptanceChecklistBatch, bad)
    assert batch.status == "failed"
    # 当前清单未被污染
    payload = svc.project_checklist(db, project.project_id)
    assert payload["current"]["batch_id"] == good


def test_apply_rejects_foreign_batch(db):
    project = _project(db)
    bid = _preview(db, project, _xlsx([("条目", "是")]), by="someone-else")
    with pytest.raises(svc.ChecklistBatchError, match="本人"):
        svc.apply_batch(db, bid, operated_by="tester")


def test_template_examples_are_ignored_on_reimport(db):
    data = svc.build_template()
    # 模板原样上传：示例行全被防呆跳过 → 空清单报错，不产生垃圾行
    with pytest.raises(svc.ChecklistParseError, match="没有任何数据行"):
        svc.parse_checklist_workbook(data, "template.xlsx")


# ---------------------------------------------------------------- API 层

def _client(db, *, username: str, permissions: dict, role: str = "readonly"):
    db.add(SysUser(username=username, role=role, display_name=username,
                   password_hash=hash_password(_PASSWORD), is_active=True,
                   permissions=permissions))
    db.commit()
    client = TestClient(app)
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return client


def test_checklist_api_permission_gate_and_roundtrip(db):
    project = _project(db)
    base = f"/api/maintenance/projects/stable/{project.project_id}/acceptance/checklist"
    allowed = _client(db, username="cl-allowed", permissions={
        "page_maintenance": True,
        "action_maintenance_acceptance_checklist_import": True,
    })
    denied = _client(db, username="cl-denied", permissions={
        "page_maintenance": True,
    })

    # 无 action：preview 403；GET 也 403（readonly 未挂靠任何项目——fail-closed）
    assert denied.post(
        f"{base}/preview",
        files={"file": ("c.xlsx", _xlsx([("条目", "是")]),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")},
        headers={"Idempotency-Key": "denied-key-12345"},
    ).status_code == 403

    resp = allowed.get(base)
    assert resp.status_code == 403, resp.text  # readonly 无 FULL_SCOPE/挂靠

    # admin 全链路：模板 → preview → apply → GET
    admin = _client(db, username="cl-admin", role="admin", permissions={})
    template = admin.get(f"{base}/template")
    assert template.status_code == 200
    assert template.content[:2] == b"PK"  # xlsx 是 zip

    preview = admin.post(
        f"{base}/preview",
        files={"file": ("c.xlsx", _xlsx([("条目一", "是"), ("条目二", "否")]),
                        "application/vnd.openxmlformats-officedocument"
                        ".spreadsheetml.sheet")},
        headers={"Idempotency-Key": "admin-key-12345"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["item_rows"] == 2
    assert body["will_replace_rows"] == 0

    apply_resp = admin.post(
        f"/api/maintenance/acceptance-checklist/{body['batch_id']}/apply")
    assert apply_resp.status_code == 200, apply_resp.text
    assert apply_resp.json()["item_rows"] == 2

    got = admin.get(base)
    assert got.status_code == 200, got.text
    current = got.json()["current"]
    assert current["item_rows"] == 2
    assert current["done_rows"] == 1

    # 非 .xlsx 拒绝
    bad = admin.post(
        f"{base}/preview",
        files={"file": ("c.csv", b"a,b", "text/csv")},
        headers={"Idempotency-Key": "admin-key-67890"},
    )
    assert bad.status_code == 415
