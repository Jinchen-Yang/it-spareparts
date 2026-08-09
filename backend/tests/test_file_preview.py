"""文件在线预览：agent_files.preview 把 xlsx 解析成行数据；预览端点与 download 同一归属校验（防 IDOR）。"""
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import permissions, security
from app.auth import hash_password
from app.main import app
from app.models.system import SysUser
from app.services import agent_files


def _owner(username: str):
    return agent_files.verified_artifact_owner(security.UserContext(
        user_id=username,
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    ))


def _xlsx_bytes() -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = "报价"
    ws.append(["型号", "数量", "单价"]); ws.append(["PN-A", 2, 100]); ws.append(["PN-B", 1, 200])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_preview_xlsx_returns_table(db):
    fid = agent_files.save_upload(_xlsx_bytes(), "报价单.xlsx", _owner("alice"))["file_id"]
    pv = agent_files.preview(fid)
    assert pv["kind"] == "table" and pv["filename"] == "报价单.xlsx"
    sh = pv["sheets"][0]
    assert sh["name"] == "报价"
    assert sh["rows"][0] == ["型号", "数量", "单价"]       # 表头
    assert sh["rows"][1][0] == "PN-A"                      # 单元格统一转字符串


def test_preview_non_xlsx_kind(db):
    fid = agent_files.save_upload(b"hi", "note.txt", _owner("alice"))["file_id"]
    assert agent_files.preview(fid)["kind"] == "other"


def test_agent_files_dir_persisted_under_raw():
    # agent_files 必须在 raw_file_dir(持久卷)之下，否则容器重建会清掉生成文件 → 下载 404。
    from pathlib import Path
    from app.config import get_settings
    from app.services.agent_files import _dir
    assert _dir().resolve().parent == Path(get_settings().raw_file_dir).resolve()


def test_preview_corrupt_xlsx_raises_fileerror(db):
    # 落盘损坏(绕过上传校验)：preview 须转成 FileError(端点据此返 404)，不能裸冒 500
    fid = agent_files.save_upload(_xlsx_bytes(), "ok.xlsx", _owner("alice"))["file_id"]
    agent_files._data_path(fid, "xlsx").write_bytes(b"PK\x03\x04 corrupt not a real xlsx")
    with pytest.raises(agent_files.FileError):
        agent_files.preview(fid)


def _mk_login(db, c, username):
    db.add(SysUser(username=username, role="sales", password_hash=hash_password("pw123456")))
    db.commit()
    return c.post("/api/auth/login", json={"username": username, "password": "pw123456"}).json()["token"]


def test_preview_endpoint_owner_acl(db):
    c = TestClient(app)
    alice = _mk_login(db, c, "alice_p")
    bob = _mk_login(db, c, "bob_p")
    up = c.post("/api/agent/upload", headers={"Authorization": f"Bearer {alice}"},
                files={"file": ("q.xlsx", _xlsx_bytes(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert up.status_code == 200, up.text
    base = f"/api/agent/files/{up.json()['file_id']}/preview"
    assert c.get(base, headers={"Authorization": f"Bearer {alice}"}).status_code == 200   # 本人可预览
    assert c.get(base, headers={"Authorization": f"Bearer {bob}"}).status_code == 403      # 他人 403
