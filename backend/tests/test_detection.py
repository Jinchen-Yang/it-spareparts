"""检测单测试（板块 D 第二片，D-2/D-23）。

覆盖：SN 采集与台账生成（一 SN 一物）、生命周期映射（好件 in_stock/坏件 bad_stock）、
实物 PN 以实物为准修正留痕、分批检测累计限额、SN 重复拒绝（请求内/台账）、
SN 数量必须等于实收、无 SN 低值件、权限失败关闭。
"""
from __future__ import annotations

import io
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app import auth
from app.api import circulation
from app.auth import hash_password
from app.models.circulation import CirculationSnItem, DetectionSheet, RecycleBatch
from app.models.dimensions import DimPart
from app.models.system import SysUser
from app.services import recycle_import as recycler

COLUMNS = ("备件号", "物料描述", "库房代码", "货位编码", "2WR",
           "中文品名", "备件状态", "公司信息", "单价", "总价")


def _mk_recycle_batch(db, *, pn="PNSYN-0001", qty=5, cond="好件", part_id=None) -> str:
    """经正式导入管道建一个回收批次，返回 batch_id 与聚合行 line_id。"""
    wb = Workbook(); ws = wb.active
    ws.append(list(COLUMNS))
    ws.append([pn, "测试件", "IT_BJDC", "A-01", qty, "硬盘", cond, "厂商甲", 100, 100 * qty])
    buf = io.BytesIO(); wb.save(buf)
    report = recycler.parse_recycle_workbook(buf.getvalue(), db)
    batch, _ = recycler.apply_recycle_import(db, report, source_filename="r.xlsx", imported_by="t")
    line = db.execute(
        select(recycler.RecycleLine).where(recycler.RecycleLine.batch_id == batch.batch_id)
    ).scalars().one()
    return batch.batch_id, line.line_id


def _client(db, *, username="detect_admin", role="admin", with_action=True) -> TestClient:
    permissions = {"page_maintenance": True}
    if with_action:
        permissions["action_recycle_manage"] = True
    db.add(SysUser(username=username, role=role, display_name="检测管理员",
                   password_hash=hash_password("detect-password-123"),
                   permissions=permissions, is_active=True))
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(circulation.router, prefix="/api")
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": "detect-password-123"})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _detect(client, batch_id, line_id, *, qty=2, cond="好件", handling="检测",
            sns=("SN-001", "SN-002"), actual_pn=None, inspector="检测员甲"):
    payload = {"batch_id": batch_id, "inspector": inspector, "items": [{
        "line_id": line_id, "received_qty": qty, "actual_condition": cond,
        "handling": handling, "sns": list(sns),
    }]}
    if actual_pn:
        payload["items"][0]["actual_pn_raw"] = actual_pn
    return client.post("/api/circulation/recycle-imports/detection-sheets", json=payload)


# ── 正向 ──


def test_detect_good_items_creates_sn_ledger_in_stock(db):
    _seed = DimPart(id=9801, pn_std="PNSYN-0001", description="测试件")
    db.add(_seed); db.commit()
    batch_id, line_id = _mk_recycle_batch(db, qty=3)
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=2, sns=["SN-A", "SN-B"])
    assert resp.status_code == 200, resp.text
    sns = db.execute(select(CirculationSnItem)).scalars().all()
    assert sorted(s.sn for s in sns) == ["SN-A", "SN-B"]
    assert all(s.lifecycle_status == "in_stock" for s in sns)
    assert all(s.pn_std == "PNSYN-0001" and s.part_id == 9801 for s in sns)
    assert all(s.detection_item_id for s in sns)


def test_detect_bad_items_lands_bad_stock(db):
    batch_id, line_id = _mk_recycle_batch(db, pn="PNSYN-0002", qty=2, cond="坏件")
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=1, cond="坏件",
                   sns=["SN-BAD-1"], handling="检测")
    assert resp.status_code == 200, resp.text
    sn = db.execute(select(CirculationSnItem)).scalars().one()
    assert sn.lifecycle_status == "bad_stock"


def test_actual_pn_correction_leaves_nominal_trace(db):
    batch_id, line_id = _mk_recycle_batch(db, pn="0302A79L", qty=1)
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=1, sns=["SN-X1"], actual_pn="RS33M2C9S")
    assert resp.status_code == 200, resp.text
    detail = client.get("/api/circulation/recycle-imports/detection-sheets").json()["sheets"][0]
    sheet_id = detail["sheet_id"]
    body = client.get(f"/api/circulation/recycle-imports/detection-sheets/{sheet_id}").json()
    item = body["items"][0]
    assert item["pn_corrected"] is True
    assert item["nominal_pn_raw"] == "0302A79L"      # 修正留痕：标称保留
    assert item["actual_pn_raw"] == "RS33M2C9S"      # 实物为准
    assert body["items"] and item["sns"] == ["SN-X1"]
    sn = db.execute(select(CirculationSnItem)).scalars().one()
    assert sn.pn_std == "RS33M2C9S"                  # SN 挂实物 PN


# ── 校验拒绝 ──


def test_reject_duplicate_sn_within_request(db):
    batch_id, line_id = _mk_recycle_batch(db, qty=3)
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=2, sns=["SN-DUP", "SN-DUP"])
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]["message"]


def test_reject_sn_already_in_ledger(db):
    batch_id, line_id = _mk_recycle_batch(db, qty=3)
    db.add(CirculationSnItem(sn="SN-EXIST", pn_std="PNSYN-0001")); db.commit()
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=1, sns=["SN-EXIST"])
    assert resp.status_code == 422
    assert "已存在" in resp.json()["detail"]["message"]


def test_reject_sn_count_mismatch_qty(db):
    batch_id, line_id = _mk_recycle_batch(db, qty=3)
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=2, sns=["SN-ONLY-1"])
    assert resp.status_code == 422
    # 数量与 SN 个数不一致（SN 提供时必须一一对应）
    assert "SN" in resp.json()["detail"]["message"] or "实收" in resp.json()["detail"]["message"]


def test_reject_cumulative_over_line_qty(db):
    batch_id, line_id = _mk_recycle_batch(db, qty=2)
    client = _client(db)
    assert _detect(client, batch_id, line_id, qty=2, sns=["SN-1", "SN-2"]).status_code == 200
    resp = _detect(client, batch_id, line_id, qty=1, sns=["SN-3"])
    assert resp.status_code == 422
    assert "超过清单数量" in resp.json()["detail"]["message"]


def test_reject_line_from_other_batch(db):
    batch_id, line_id = _mk_recycle_batch(db, qty=2)
    other_batch, _ = _mk_recycle_batch(db, pn="PNSYN-0009", qty=2)
    client = _client(db)
    resp = _detect(client, other_batch, line_id, qty=1, sns=["SN-Z"])
    assert resp.status_code == 422
    assert "不属于批次" in resp.json()["detail"]["message"]


def test_allow_sn_less_low_value_items(db):
    """低值件无 SN：只记检测明细，不生成 SN 台账。"""
    batch_id, line_id = _mk_recycle_batch(db, qty=2)
    client = _client(db)
    resp = _detect(client, batch_id, line_id, qty=2, sns=[])
    assert resp.status_code == 200, resp.text
    assert db.execute(select(CirculationSnItem)).scalars().all() == []


def test_detect_nonexistent_batch_422(db):
    client = _client(db)
    resp = _detect(client, "no-such-batch", "no-such-line", qty=1, sns=["SN-Q"])
    assert resp.status_code == 422


# ── 权限 ──


def test_detect_forbidden_without_action(db):
    batch_id, line_id = _mk_recycle_batch(db, qty=2)
    client = _client(db, username="detect_noperm", role="readonly", with_action=False)
    resp = _detect(client, batch_id, line_id, qty=1, sns=["SN-P"])
    assert resp.status_code == 403
