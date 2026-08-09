"""HTTP contract for warehouse import/search/ambiguity resolution."""

from __future__ import annotations

from datetime import date
import io

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAuditEvent,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseImportBatch,
)
from app.models.system import SysImportBatch, SysUser


SHIPMENT_PREFIX = "D107407Fvxu6voev32rlg4pkdu6nvdc83"


def _content() -> bytes:
    headers = [
        ("ObjectId", "数据ID(不可修改)"),
        ("SeqNo", "出库单号(必填)"),
        ("F0000001", "出库日期(必填)"),
        ("F0000032", "出库类别(必填)"),
        ("F0000061", "出库备件/整机(必填)"),
        ("Status", "数据状态"),
        ("F0000151", "维保需求单(备件)(必填)"),
        (f"{SHIPMENT_PREFIX}.ObjectId", "备件明细.数据ID(不可修改)"),
        (f"{SHIPMENT_PREFIX}.F0000031", "备件明细.备件PN(必填)"),
        (f"{SHIPMENT_PREFIX}.F0000044", "备件明细.备件SN号(必填)"),
        (f"{SHIPMENT_PREFIX}.F0000011", "备件明细.出库数量"),
    ]
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([code for code, _label in headers])
    sheet.append([label for _code, label in headers])
    sheet.append([
        "SYN-API-DOC", "SYN-API-SHIP", "2026-08-01", "维保", "备件", "已完成",
        "SYN-API-WBDD", "SYN-API-LINE", "SYN-API-PN", "SYN-API-SN", 1,
    ])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _seed(db) -> None:
    source = SysImportBatch(
        filename="synthetic-api-source.xlsx",
        file_type="maintenance",
        file_hash="synthetic-api-source-hash",
        status="success",
    )
    db.add(source)
    db.flush()
    db.add(FMaintenanceOrder(
        raw_order_id="SYN-API-WBDD",
        order_no="SYN-API-WBDD",
        order_date=date(2026, 8, 1),
        import_batch_id=source.id,
    ))
    db.add(DimPart(
        pn_std="SYN-API-PN", status="active", master_source="import", locked_fields=[]
    ))
    db.commit()


def _real_admin(db) -> TestClient:
    db.add(SysUser(
        username="warehouse-api-admin",
        role="admin",
        display_name="合成仓库管理员",
        password_hash=hash_password("synthetic-password-123"),
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={
        "username": "warehouse-api-admin",
        "password": "synthetic-password-123",
    })
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_preview_apply_post_search_and_resolution_end_to_end(db):
    _seed(db)
    client = _real_admin(db)
    content = _content()
    before_batches = db.scalar(select(func.count()).select_from(MaintenanceWarehouseImportBatch))

    preview = client.post(
        "/api/maintenance/warehouse-imports/preview",
        files={"file": ("synthetic-api.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert preview.status_code == 200, preview.text
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseImportBatch)) == before_batches
    plan = preview.json()

    applied = client.post(
        f"/api/maintenance/warehouse-imports/{plan['import_id']}/apply",
        data={"preview_token": plan["preview_token"], "reason": "合成 API 导入验证"},
        files={"file": ("synthetic-api.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["writes"]["documents"] == 1

    documents = client.post(
        "/api/maintenance/warehouse-documents/search",
        json={"q": "SYN-API-SHIP", "page": 1, "page_size": 20},
    )
    assert documents.status_code == 200, documents.text
    assert documents.headers["cache-control"] == "no-store"
    assert documents.json()["total"] == 1
    assert documents.json()["items"][0]["line_count"] == 1
    assert client.get("/api/maintenance/warehouse-documents/search?q=SYN-API").status_code == 405

    ambiguities = client.post(
        "/api/maintenance/warehouse-ambiguities/search",
        json={"status": "open", "page": 1, "page_size": 20},
    )
    assert ambiguities.status_code == 200, ambiguities.text
    unknown = next(
        item for item in ambiguities.json()["items"]
        if item["ambiguity_type"] == "unknown_version"
    )
    resolved = client.post(
        f"/api/maintenance/warehouse-ambiguities/{unknown['ambiguity_id']}/resolve",
        json={
            "version": unknown["version"],
            "reason": "合成审核：确认可选列变化",
            "decision": "acknowledge",
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseAuditEvent)) == 2


def test_apply_rejects_shared_admin_before_any_write(db):
    _seed(db)
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    content = _content()
    preview = client.post(
        "/api/maintenance/warehouse-imports/preview",
        files={"file": ("synthetic-api.xlsx", content)},
    )
    assert preview.status_code == 200
    plan = preview.json()

    response = client.post(
        f"/api/maintenance/warehouse-imports/{plan['import_id']}/apply",
        data={"preview_token": plan["preview_token"], "reason": "不应落库"},
        files={"file": ("synthetic-api.xlsx", content)},
    )
    assert response.status_code == 403
    assert db.scalar(select(func.count()).select_from(MaintenanceWarehouseDocument)) == 0


def test_overlong_post_search_is_not_reflected(db, caplog):
    client = _real_admin(db)
    sentinel = "SYN-PRIVATE-WAREHOUSE-QUERY-" + "x" * 256
    response = client.post(
        "/api/maintenance/warehouse-documents/search",
        json={"q": sentinel, "page": 1, "page_size": 20},
    )
    assert response.status_code == 422
    assert sentinel not in response.text
    assert sentinel not in caplog.text
