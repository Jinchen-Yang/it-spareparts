"""DEV-13A：维保需求号未匹配归因（只读、互斥、脱敏）。"""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.auth import hash_password
from app.etl import loader
from app.main import app
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_match_audit
from tests import factories as f


@pytest.fixture()
def batch(db):
    row = SysImportBatch(filename="match-audit.xlsx", file_type="maintenance", file_hash="dev13")
    db.add(row)
    db.flush()
    return row


def _load_fixture(db, batch) -> None:
    """1 条精确命中 + 六个未匹配桶各 1 条。"""
    maintenance_orders = {
        "M-EXACT": f.maintenance_head("M-EXACT", order_no="WBDD-EXACT", on=date(2026, 1, 2)),
        "M-EMPTY": f.maintenance_head("M-EMPTY", order_no="WBDD-EMPTY", on=date(2026, 1, 2)),
        "M-FORMAT": f.maintenance_head("M-FORMAT", order_no="WBDD-20260101-0001", on=date(2026, 1, 2)),
        "M-DUP": f.maintenance_head("M-DUP", order_no="WBDD-20260101-0002", on=date(2026, 1, 2)),
        "M-PN": f.maintenance_head("M-PN", order_no="WBDD-20260101-0003", on=date(2026, 1, 2)),
        "M-MISSING": f.maintenance_head("M-MISSING", order_no="WBDD-20260101-0004", on=date(2026, 1, 2)),
        "M-OTHER": f.maintenance_head("M-OTHER", order_no="WBDD-20260101-0005", on=date(2026, 1, 2)),
    }
    maintenance_lines = [
        f.maintenance_line("M-EXACT", "ML-EXACT", "PN-EXACT"),
        f.maintenance_line("M-EMPTY", "ML-EMPTY", "PN-EMPTY"),
        f.maintenance_line("M-FORMAT", "ML-FORMAT", "PN-FORMAT"),
        f.maintenance_line("M-DUP", "ML-DUP", "PN-DUP"),
        f.maintenance_line("M-PN", "ML-PN", "PN-WANTED"),
        f.maintenance_line("M-MISSING", "ML-MISSING", "PN-MISSING"),
        f.maintenance_line("M-OTHER", "ML-OTHER", "PN-OTHER"),
    ]
    loader.load(db, f.maintenance_result(maintenance_orders, maintenance_lines), batch.id,
                date(2026, 7, 16))

    purchase_orders = {
        "P-EXACT": f.purchase_head("P-EXACT", linked_maintenance_order_no=" wbdd-exact "),
        "P-FORMAT": f.purchase_head("P-FORMAT", linked_maintenance_order_no="WBDD 20260101 0001"),
        "P-DUP-A": f.purchase_head("P-DUP-A", linked_maintenance_order_no="WBDD 20260101 0002"),
        "P-DUP-B": f.purchase_head("P-DUP-B", linked_maintenance_order_no="WBDD/20260101/0002"),
        "P-PN": f.purchase_head("P-PN", linked_maintenance_order_no="WBDD-20260101-0003"),
        # 同需求号同 PN，但 qty=0，不是现行直配池的合格候选 → other。
        "P-OTHER": f.purchase_head("P-OTHER", linked_maintenance_order_no="WBDD-20260101-0005"),
    }
    purchase_lines = [
        f.purchase_line("P-EXACT", "PL-EXACT", "PN-EXACT"),
        f.purchase_line("P-FORMAT", "PL-FORMAT", "PN-FORMAT"),
        f.purchase_line("P-DUP-A", "PL-DUP-A", "PN-DUP"),
        f.purchase_line("P-DUP-B", "PL-DUP-B", "PN-DUP"),
        f.purchase_line("P-PN", "PL-PN", "PN-ACTUAL"),
        f.purchase_line("P-OTHER", "PL-OTHER", "PN-OTHER", qty="0"),
    ]
    loader.load(db, f.purchase_result(purchase_orders, purchase_lines), batch.id,
                date(2026, 7, 16))
    # 导入层会拦空单号；归因服务仍须对数据库历史脏值给出明确桶。
    empty = db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.raw_order_id == "M-EMPTY"))
    empty.order_no = " "
    db.commit()


def _by_key(report: dict) -> dict[str, dict]:
    return {row["code"]: row for row in report["buckets"]}


def test_six_buckets_are_exhaustive_mutually_exclusive_and_exact_hit_is_excluded(db, batch):
    _load_fixture(db, batch)

    report = maintenance_match_audit.build_report(db, sample_limit=5)

    assert report["scope"]["total_line_count"] == 7
    assert report["scope"]["exact_matched_line_count"] == 1
    assert report["scope"]["unmatched_line_count"] == 6
    buckets = _by_key(report)
    assert set(buckets) == {
        "empty_request_no", "normalizable_format", "request_exists_pn_diff",
        "purchase_missing_request_no", "duplicate_candidates", "other",
    }
    assert {key: row["line_count"] for key, row in buckets.items()} == {
        "empty_request_no": 1,
        "normalizable_format": 1,
        "request_exists_pn_diff": 1,
        "purchase_missing_request_no": 1,
        "duplicate_candidates": 1,
        "other": 1,
    }
    assert report["invariant"] == {"bucket_sum": 6, "equals_unmatched": True}
    assert report["repairable"] == {
        "line_count": 1,
        "rate_of_unmatched": 0.166667,
        "meaning": "技术上可规整候选；只读，不自动修改",
    }


def test_report_is_deterministic_masked_fixed_query_count_and_has_no_side_effects(db, batch):
    _load_fixture(db, batch)
    cost_before = list(db.execute(select(
        FMaintenanceLine.id, FMaintenanceLine.unit_cost, FMaintenanceLine.cost_amount,
        FMaintenanceLine.cost_source, FMaintenanceLine.linked_purchase_order_no,
    ).order_by(FMaintenanceLine.id)))
    counts_before = (
        db.scalar(select(func.count()).select_from(FMaintenanceOrder)),
        db.scalar(select(func.count()).select_from(FMaintenanceLine)),
        db.scalar(select(func.count()).select_from(FPurchaseOrder)),
        db.scalar(select(func.count()).select_from(FPurchaseLine)),
    )
    selects = 0

    def count_select(_conn, _cursor, statement, _params, _ctx, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(db.bind, "before_cursor_execute", count_select)
    try:
        first = maintenance_match_audit.build_report(db, sample_limit=3)
        first_query_count = selects
        second = maintenance_match_audit.build_report(db, sample_limit=3)
    finally:
        event.remove(db.bind, "before_cursor_execute", count_select)

    assert first == second
    assert first_query_count == 2
    assert selects == 4
    payload = json.dumps(first, ensure_ascii=False)
    for raw in (
        "WBDD-EXACT", "WBDD-20260101-0001", "WBDD 20260101 0001",
        "PN-EXACT", "PN-FORMAT", "PN-ACTUAL",
        "测试供应商", "测试客户",
    ):
        assert raw not in payload
    assert all(sample["sample_ref"].startswith("MA-")
               for bucket in first["buckets"] for sample in bucket["samples"])

    cost_after = list(db.execute(select(
        FMaintenanceLine.id, FMaintenanceLine.unit_cost, FMaintenanceLine.cost_amount,
        FMaintenanceLine.cost_source, FMaintenanceLine.linked_purchase_order_no,
    ).order_by(FMaintenanceLine.id)))
    counts_after = (
        db.scalar(select(func.count()).select_from(FMaintenanceOrder)),
        db.scalar(select(func.count()).select_from(FMaintenanceLine)),
        db.scalar(select(func.count()).select_from(FPurchaseOrder)),
        db.scalar(select(func.count()).select_from(FPurchaseLine)),
    )
    assert cost_after == cost_before
    assert counts_after == counts_before
    assert not db.dirty and not db.new and not db.deleted


def _login(client: TestClient, db, username: str, role: str, permissions: dict | None = None) -> str:
    db.add(SysUser(
        username=username,
        role=role,
        is_active=True,
        password_hash=hash_password("pw123456"),
        permissions=permissions,
    ))
    db.commit()
    response = client.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_api_requires_auth_and_page_permission_and_returns_only_masked_samples(db, batch):
    _load_fixture(db, batch)
    client = TestClient(app)
    assert client.get("/api/maintenance/match-audit").status_code == 401

    denied_token = _login(client, db, "audit_denied", "readonly")
    denied = client.get(
        "/api/maintenance/match-audit",
        headers={"Authorization": f"Bearer {denied_token}"},
    )
    assert denied.status_code == 403

    allowed_token = _login(client, db, "audit_admin", "admin")
    allowed = client.get(
        "/api/maintenance/match-audit?sample_limit=2",
        headers={"Authorization": f"Bearer {allowed_token}"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["invariant"]["equals_unmatched"] is True
    assert "WBDD-20260101-0001" not in allowed.text
    assert "PN-FORMAT" not in allowed.text
