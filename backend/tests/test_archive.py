"""循环档案测试（板块 D 第三片，D-3/D-22）。

覆盖：附件上传自动建档、照片/检测报告齐全性判定、上架/强制上架/下架、
强制上架权限失败关闭、删除附件回退、文件类型白名单、SN 台账查询。
"""
from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app import auth
from app.api import circulation
from app.auth import hash_password
from app.models.circulation import CirculationSnItem
from app.models.system import SysUser
from app.services import recycle_import as recycler

COLUMNS = ("备件号", "物料描述", "库房代码", "货位编码", "2WR",
           "中文品名", "备件状态", "公司信息", "单价", "总价")
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
PDF = b"%PDF-1.4 fake-pdf-bytes"


def _client(db, *, username="arch_admin", role="admin", perms=None) -> TestClient:
    permissions = perms if perms is not None else {
        "page_maintenance": True,
        "action_recycle_manage": True,
        "action_recycle_force_list": True,
    }
    db.add(SysUser(username=username, role=role, display_name="档案管理员",
                   password_hash=hash_password("archive-password-123"),
                   permissions=permissions, is_active=True))
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(circulation.router, prefix="/api")
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": "archive-password-123"})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _upload(client, pn, kind, filename, content):
    return client.post(f"/api/circulation/recycle-imports/archives/{pn}/attachments",
                       files={"file": (filename, content)},
                       data={"kind": kind})


def _mk_sn(db, sn, pn="PNSYN-0001", status="in_stock"):
    db.add(CirculationSnItem(sn=sn, pn_std=pn, lifecycle_status=status))
    db.commit()


# ── 上传与齐全性 ──


def test_upload_photo_auto_creates_archive_pending(db):
    client = _client(db)
    resp = _upload(client, "PNSYN-0001", "photo", "front.png", PNG)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pn_std"] == "PNSYN-0001"
    assert body["photo_count"] == 1 and body["report_count"] == 0
    assert body["requirements_met"] is False
    assert body["listing_status"] == "pending"


def test_wrong_ext_rejected(db):
    client = _client(db)
    resp = _upload(client, "PNSYN-0001", "photo", "not-image.pdf", PDF)
    assert resp.status_code == 422
    resp = _upload(client, "PNSYN-0001", "report", "fake-report.png", PNG)
    assert resp.status_code == 422


# ── 上架 / 强制上架 / 下架 ──


def test_list_blocked_until_requirements_met_then_listed(db):
    client = _client(db)
    _upload(client, "PNSYN-0001", "photo", "front.png", PNG)
    # 资料不全：正常上架 422（提示缺检测报告），强制上架可用
    resp = client.post("/api/circulation/recycle-imports/archives/PNSYN-0001/listing",
                       json={"action": "list"})
    assert resp.status_code == 422
    assert "检测报告" in resp.json()["detail"]["message"]
    _upload(client, "PNSYN-0001", "report", "report.pdf", PDF)
    resp = client.post("/api/circulation/recycle-imports/archives/PNSYN-0001/listing",
                       json={"action": "list"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["listing_status"] == "listed"


def test_force_list_requires_permission_and_reason(db):
    # 无 action_recycle_force_list 权限的账号
    # 注意：admin 恒有全量动作，403 场景必须用 readonly 角色
    client = _client(db, username="arch_noforce", role="readonly", perms={
        "page_maintenance": True, "action_recycle_manage": True})
    _upload(client, "PNSYN-0002", "photo", "front.png", PNG)
    resp = client.post("/api/circulation/recycle-imports/archives/PNSYN-0002/listing",
                       json={"action": "force_list", "reason": "客户急等着看"})
    assert resp.status_code == 403
    # 有权限但不填原因
    client2 = _client(db, username="arch_force")
    _upload(client2, "PNSYN-0002", "photo", "front.png", PNG)
    resp = client2.post("/api/circulation/recycle-imports/archives/PNSYN-0002/listing",
                        json={"action": "force_list"})
    assert resp.status_code == 422
    assert "原因" in resp.json()["detail"]["message"]
    # 有权限 + 原因 → 强制上架并留痕
    resp = client2.post("/api/circulation/recycle-imports/archives/PNSYN-0002/listing",
                        json={"action": "force_list", "reason": "客户急等着看，报告后补"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["listing_status"] == "force_listed"
    assert body["force_listed_by"] == "arch_force"
    assert body["force_reason"] == "客户急等着看，报告后补"
    # 资料已齐全时强制上架无意义 → 422 引导走正常上架
    _upload(client2, "PNSYN-0002", "report", "report.pdf", PDF)
    resp = client2.post("/api/circulation/recycle-imports/archives/PNSYN-0002/listing",
                        json={"action": "force_list", "reason": "重复强制"})
    assert resp.status_code == 422


def test_delist_then_relist(db):
    client = _client(db)
    _upload(client, "PNSYN-0003", "photo", "front.png", PNG)
    _upload(client, "PNSYN-0003", "report", "report.pdf", PDF)
    assert client.post("/api/circulation/recycle-imports/archives/PNSYN-0003/listing",
                       json={"action": "list"}).json()["listing_status"] == "listed"
    assert client.post("/api/circulation/recycle-imports/archives/PNSYN-0003/listing",
                       json={"action": "delist"}).json()["listing_status"] == "delisted"
    assert client.post("/api/circulation/recycle-imports/archives/PNSYN-0003/listing",
                       json={"action": "list"}).json()["listing_status"] == "listed"


def test_delete_report_after_list_demotes_to_pending(db):
    client = _client(db)
    _upload(client, "PNSYN-0004", "photo", "front.png", PNG)
    att = _upload(client, "PNSYN-0004", "report", "report.pdf", PDF).json()
    report_id = att["attachments"][-1]["attachment_id"]
    assert client.post("/api/circulation/recycle-imports/archives/PNSYN-0004/listing",
                       json={"action": "list"}).json()["listing_status"] == "listed"
    resp = client.delete(
        f"/api/circulation/recycle-imports/archives/PNSYN-0004/attachments/{report_id}")
    assert resp.status_code == 200
    body = client.get("/api/circulation/recycle-imports/archives/PNSYN-0004").json()
    assert body["listing_status"] == "pending"      # 资料不再齐全 → 回退
    assert body["report_count"] == 0


# ── SN 台账查询（D-4）──


def test_sn_ledger_query_filters(db):
    _mk_sn(db, "SN-Q-1", pn="PNSYN-Q1", status="in_stock")
    _mk_sn(db, "SN-Q-2", pn="PNSYN-Q1", status="bad_stock")
    _mk_sn(db, "SN-Q-3", pn="PNSYN-Q2", status="in_stock")
    client = _client(db)
    all_rows = client.get("/api/circulation/recycle-imports/sn-items").json()["sn_items"]
    assert {r["sn"] for r in all_rows} >= {"SN-Q-1", "SN-Q-2", "SN-Q-3"}
    by_pn = client.get("/api/circulation/recycle-imports/sn-items",
                       params={"pn_std": "PNSYN-Q1"}).json()["sn_items"]
    assert {r["sn"] for r in by_pn} == {"SN-Q-1", "SN-Q-2"}
    by_status = client.get("/api/circulation/recycle-imports/sn-items",
                           params={"lifecycle_status": "bad_stock"}).json()["sn_items"]
    assert {r["sn"] for r in by_status} == {"SN-Q-2"}
    bad = client.get("/api/circulation/recycle-imports/sn-items",
                     params={"lifecycle_status": "haha"})
    assert bad.status_code == 422


# ── 权限 ──


def test_archive_upload_forbidden_without_manage(db):
    client = _client(db, username="arch_noperm", role="readonly",
                     perms={"page_maintenance": True})
    resp = _upload(client, "PNSYN-0005", "photo", "front.png", PNG)
    assert resp.status_code == 403
