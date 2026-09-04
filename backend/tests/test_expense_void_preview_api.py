"""导入前作废预演：HTTP 层的字节绑定与令牌校验。"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app.auth import hash_password
from app.etl import pipeline
from app.main import app
from app.models.maintenance import FProjectExpense
from app.models.system import SysUser

_COLS = ["报销日期", "报销人员", "支出事由", "报销金额", "单号", "序号"]
R1 = ["2026-05-01", "甲", "备件", 5000, "BXD-1", 1]
R2 = ["2026-05-02", "乙", "差旅", 3000, "BXD-2", 1]
R3 = ["2026-05-03", "丙", "租金", 1200, "BXD-3", 1]
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _anchored(tmp_path, name, rows, anchor="XSDD-P"):
    wb = Workbook(); ws = wb.active; ws.title = "报销明细"
    ws.append(["销售订单", anchor]); ws.append(_COLS)
    for r in rows:
        ws.append(r)
    p = tmp_path / name; wb.save(str(p)); return p


@pytest.fixture()
def client(db):
    db.add(SysUser(username="void-admin", role="admin", display_name="预演管理员",
                   password_hash=hash_password("adminpw")))
    db.commit()
    c = TestClient(app)
    login = c.post("/api/auth/login", json={"username": "void-admin", "password": "adminpw"})
    assert login.status_code == 200, login.text
    c.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return c


def _seed(db, tmp_path):
    pipeline.run_import(db, str(_anchored(tmp_path, "seed.xlsx", [R1, R2, R3])), "seed.xlsx")
    db.commit()


def _preview(client, path, mode="upsert"):
    with open(path, "rb") as fh:
        return client.post("/api/import/expense-void-preview", params={"mode": mode},
                           files={"file": (path.name, fh, _XLSX)})


def _upload(client, path, token=None, mode="upsert"):
    with open(path, "rb") as fh:
        return client.post("/api/import/upload", params={"mode": mode},
                           files={"file": (path.name, fh, _XLSX)},
                           data={"preview_token": token} if token else None)


def _status(db, reason):
    return db.scalar(select(FProjectExpense.data_status)
                     .where(FProjectExpense.reason == reason))


def test_preview_then_upload_with_token(db, tmp_path, client):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    pv = _preview(client, page)
    assert pv.status_code == 200, pv.text
    body = pv.json()
    assert body["status"] == "ready" and body["void"]["rows"] == 1
    assert body["void"]["amount"] == "1200.00" and body["preview_token"]

    up = _upload(client, page, body["preview_token"])
    assert up.status_code == 200, up.text
    db.expire_all()
    assert _status(db, "租金") == "已作废"


def test_upload_of_armed_page_without_token_is_refused(db, tmp_path, client):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    up = _upload(client, page)
    assert up.status_code == 400 and up.headers["X-Error-Code"] == "void_preview_required"
    db.expire_all()
    assert _status(db, "租金") == "已结束"


def test_token_for_a_different_file_is_refused(db, tmp_path, client):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    token = _preview(client, page).json()["preview_token"]
    edited = _anchored(tmp_path, "edited.xlsx", [R1])          # 预演后又改了文件
    up = _upload(client, edited, token)
    assert up.status_code == 409 and up.headers["X-Error-Code"] == "void_preview_mismatch"
    db.expire_all()
    assert _status(db, "租金") == "已结束" and _status(db, "差旅") == "已结束"


def test_tampered_token_is_refused(db, tmp_path, client):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    token = _preview(client, page).json()["preview_token"]
    up = _upload(client, page, token[:-3] + "abc")
    assert up.status_code == 409 and up.headers["X-Error-Code"] == "void_preview_invalid"


def test_drift_between_preview_and_upload_is_refused(db, tmp_path, client):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    token = _preview(client, page).json()["preview_token"]
    victim = db.scalar(select(FProjectExpense).where(FProjectExpense.reason == "租金"))
    victim.amount = victim.amount_ex_tax = victim.amount + 1
    victim.amount_inc_tax = (victim.amount * Decimal("1.13")).quantize(Decimal("0.01"))
    db.commit()
    up = _upload(client, page, token)
    assert up.status_code == 409 and up.headers["X-Error-Code"] == "void_plan_drift"
    db.expire_all()
    assert _status(db, "租金") == "已结束"


def test_batch_with_two_armed_tokens_is_refused(db, tmp_path, client):
    _seed(db, tmp_path)
    a = _anchored(tmp_path, "a.xlsx", [R1, R2])
    b = _anchored(tmp_path, "b.xlsx", [R1, R3])
    ta = _preview(client, a).json()["preview_token"]
    tb = _preview(client, b).json()["preview_token"]
    with open(a, "rb") as fa, open(b, "rb") as fb:
        r = client.post("/api/import/upload-batch", params={"mode": "upsert"},
                        files=[("files", ("a.xlsx", fa, _XLSX)), ("files", ("b.xlsx", fb, _XLSX))],
                        data={"preview_tokens": [ta, tb]})
    assert r.status_code == 422 and r.headers["X-Error-Code"] == "void_preview_multiple"


def test_unreadable_file_previews_as_unreadable(db, tmp_path, client):
    p = tmp_path / "junk.xlsx"; p.write_bytes(b"not a workbook")
    r = _preview(client, p)
    assert r.status_code == 200 and r.json()["status"] == "unreadable"
