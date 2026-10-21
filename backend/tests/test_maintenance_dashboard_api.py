"""维保数据看板 API：权限矩阵、项目范围、参数校验与导出（R33 / D-0022）。

关键口径：
- 数量端点对 maintenance_manager / purchaser 开放，但响应与导出不得出现
  任何金额字段（键都不出现，不是 0/null）——递归扫描断言。
- 金额端点（valuation）= 登录 + page_maintenance + 项目范围 + 利润门；
- readonly 兜底模板有 data_profit 但无 page_maintenance → 仍 403（利润门单独不够）。
- 受限账号手填未授权项目 ID → 空交集 → 空结果，绝不回退全量。
"""

import csv
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.dimensions import DimPart
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectUserAssignment,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_dashboard as dash
from app.services.maintenance_dashboard_valuation_policy import POLICY_VERSION
from tests.test_maintenance_return_metrics import _issue, _receipt


def _part(db, pn, *, unit=None, description=""):
    part = DimPart(pn_std=pn, unit=unit, description=description)
    db.add(part)
    db.flush()
    return part


def _project(db, code):
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=code, display_name=code,
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    return project

BASE = "/api/maintenance/analytics/maintenance-dashboard"
PASSWORD = "mdd-api-password-1"
TODAY = date(2026, 10, 21)
UTC = timezone.utc
# 无金额权响应里递归不允许出现的键片段（金额域键集）。
FORBIDDEN_KEY_PARTS = ("valuation", "value_inc", "value_ex", "amount", "price",
                       "factor", "reference", "coverage", "policy")


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr(dash, "business_today", lambda: TODAY)


def client_for(db, *, username, role="readonly", overrides=None) -> TestClient:
    base = permissions.effective(role, None)
    effective = permissions.effective_from_snapshot(base, overrides or {})
    db.add(SysUser(
        username=username, role=role, display_name=username,
        password_hash=hash_password(PASSWORD), is_active=True,
        template_code=role, template_version=1, template_perms=base,
        perm_overrides=overrides or {}, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _assign_manager(db, client_username: str, project: MaintenanceProject):
    user = db.scalar(select(SysUser).where(SysUser.username == client_username))
    db.add(MaintenanceProjectUserAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        responsibility_type="primary_manager", user_id=user.id,
        source_manager_text=client_username, version=1, assigned_by="t",
        assignment_reason="test",
    ))
    db.commit()


def _purchase_ref(db, part, *, order_date=date(2026, 10, 18)):
    batch = SysImportBatch(
        filename="mdd-api-cg.xlsx", file_type="purchase",
        file_hash=uuid.uuid4().hex, status="success",
    )
    db.add(batch)
    db.flush()
    order = FPurchaseOrder(
        raw_order_id=f"mdd-api-cg-{uuid.uuid4().hex[:8]}", order_no="MDDA-CG-1",
        purchaser="t", data_status="已生效", order_date=order_date,
        tax_rate=Decimal("0.13"), is_tax_inclusive=True, import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(FPurchaseLine(
        raw_line_id=f"mdd-api-cg-l-{uuid.uuid4().hex[:8]}", order_id=order.id,
        line_no=1, part_id=part.id, pn_std=part.pn_std, qty=Decimal(2),
        unit_price=Decimal("113.00"), unit="件", import_batch_id=batch.id,
    ))
    db.flush()
    return order


@pytest.fixture()
def seeded(db):
    """两个项目 + 坏件实收（期内）+ 领用 + 采购参考（113 含税 / 100 未税）。"""
    p1 = _project(db, "MDDA-P1")
    p2 = _project(db, "MDDA-P2")
    part = _part(db, "MDDA-PN-1", unit="件")
    _purchase_ref(db, part)
    _receipt(db, p1, part, "4", condition="坏品",
             occurred_at=datetime(2026, 10, 18, 8, tzinfo=UTC))
    _receipt(db, p2, part, "2", condition="废品",
             occurred_at=datetime(2026, 10, 18, 9, tzinfo=UTC))
    _issue(db, p1, part, "5", issue_date=date(2026, 10, 18))
    db.commit()
    return p1, p2, part


def _forbidden_keys(node, path="payload") -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if any(part in str(key) for part in FORBIDDEN_KEY_PARTS):
                found.add(f"{path}.{key}")
            found |= _forbidden_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found |= _forbidden_keys(item, f"{path}[{index}]")
    return found


def test_unauthenticated_401(db):
    client = TestClient(app)
    for endpoint in ("/summary", "/projects", "/parts", "/valuation",
                     "/receipts", "/quantity-checks", "/export", "/options"):
        response = client.get(f"{BASE}{endpoint}")
        assert response.status_code == 401, (endpoint, response.status_code)


def test_page_permission_required_even_with_profit(db, seeded):
    """readonly 有 data_profit 但无 page_maintenance → 数量/金额端点都 403。"""
    client = client_for(db, username="mdd-readonly", role="readonly")
    assert client.get(f"{BASE}/summary").status_code == 403
    assert client.get(f"{BASE}/valuation").status_code == 403


def test_admin_sees_amounts_and_policy_version(db, seeded):
    p1, _p2, part = seeded
    client = client_for(db, username="mdd-admin", role="admin")
    summary = client.get(f"{BASE}/summary")
    assert summary.status_code == 200
    assert summary.headers["cache-control"] == "no-store"
    body = summary.json()
    assert body["range"] == {"start": "2026-10-15", "end": "2026-10-21"}
    valuation = body["cards"]["valuation"]
    # 坏品 4×113×0.20=90.40 + 废品 2×113×0.05=11.30 → 101.70（含税）；
    # 未税：坏品 80.00 + 废品 10.00 → 90.00。
    assert valuation["bad_estimated_value"]["inc_tax"] == "101.70"
    assert valuation["bad_estimated_value"]["ex_tax"] == "90.00"
    assert valuation["policy_version"] == POLICY_VERSION
    assert valuation["coverage_pct"] == 100.0

    valuation_page = client.get(f"{BASE}/valuation")
    assert valuation_page.status_code == 200
    page = valuation_page.json()
    assert page["policy"]["policy_version"] == POLICY_VERSION
    assert page["policy"]["condition_factors"]["坏件"] == "0.20"
    assert page["cards"]["bad_estimated_value"]["inc_tax"] == "101.70"

    parts = client.get(f"{BASE}/parts").json()["rows"]
    row = next(item for item in parts if item["pn"] == "MDDA-PN-1")
    assert row["valuation"]["bad_estimated_value_inc_tax"] == "101.70"
    assert row["references"]["chosen"] == "purchase"
    assert row["references"]["purchase"]["inc_tax"] == "113.00"

    receipts = client.get(f"{BASE}/receipts").json()["rows"]
    bad = next(item for item in receipts if item["test_result"] == "坏品")
    assert bad["valuation"]["state"] == "ready"
    assert bad["valuation"]["factor"] == "0.20"


def test_maintenance_manager_quantity_only_no_amount_leak(db, seeded):
    p1, _p2, _part = seeded
    username = "mdd-manager"
    client = client_for(db, username=username, role="maintenance_manager")
    _assign_manager(db, username, p1)

    options = client.get(f"{BASE}/options")
    assert options.status_code == 200
    options_body = options.json()
    assert options_body["amounts_visible"] is False
    assert "valuation_policy" not in options_body
    assert "bad_value_desc" not in options_body["sorts"]["projects"]
    assert [p["project_code"] for p in options_body["projects"]] == ["MDDA-P1"]

    summary = client.get(f"{BASE}/summary")
    assert summary.status_code == 200
    assert _forbidden_keys(summary.json()) == set()
    assert summary.json()["cards"]["receipt_qty"]["total"] == "4.000"

    projects = client.get(f"{BASE}/projects")
    assert projects.status_code == 200
    assert _forbidden_keys(projects.json()) == set()
    assert [r["project_code"] for r in projects.json()["rows"]] == ["MDDA-P1"]

    parts = client.get(f"{BASE}/parts")
    assert parts.status_code == 200
    assert _forbidden_keys(parts.json()) == set()

    receipts = client.get(f"{BASE}/receipts")
    assert receipts.status_code == 200
    assert _forbidden_keys(receipts.json()) == set()

    # 金额端点 403；金额排序 422（不静默降级）。
    assert client.get(f"{BASE}/valuation").status_code == 403
    denied = client.get(f"{BASE}/projects",
                        params={"sort": "bad_value_desc"})
    assert denied.status_code == 422
    assert denied.json()["detail"]["code"] == "sort_requires_profit_permission"

    # 导出（数量列完整、无任何金额列/估价元数据）。
    export = client.get(f"{BASE}/export",
                        params={"kind": "parts", "format": "csv"})
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.content.decode("utf-8"))))
    header = rows[0]
    assert "坏件实收" in header
    for forbidden in ("坏件估算价值-含税", "估价政策版本", "参考价-含税", "覆盖率%"):
        assert forbidden not in header


def test_purchaser_full_scope_quantity_only(db, seeded):
    client = client_for(db, username="mdd-purchaser", role="purchaser")
    projects = client.get(f"{BASE}/projects")
    assert projects.status_code == 200
    body = projects.json()
    assert _forbidden_keys(body) == set()
    assert {r["project_code"] for r in body["rows"]} == {"MDDA-P1", "MDDA-P2"}
    assert client.get(f"{BASE}/valuation").status_code == 403
    checks = client.get(f"{BASE}/quantity-checks")
    assert checks.status_code == 200
    assert _forbidden_keys(checks.json()) == set()


def test_scope_hand_filled_foreign_project_sees_nothing(db, seeded):
    p1, p2, _part = seeded
    username = "mdd-manager-foreign"
    client = client_for(db, username=username, role="maintenance_manager")
    _assign_manager(db, username, p1)
    # 手填未授权项目 ID：交集为空 → 空结果，绝不回退全量。
    response = client.get(f"{BASE}/projects",
                          params={"project": p2.project_id})
    assert response.status_code == 200
    assert response.json()["rows"] == []
    leak = client.get(f"{BASE}/summary", params={"project": p2.project_id})
    assert leak.json()["cards"]["receipt_qty"]["total"] == "0.000"
    # 选自己可见的项目正常返回。
    own = client.get(f"{BASE}/projects", params={"project": p1.project_id})
    assert [r["project_code"] for r in own.json()["rows"]] == ["MDDA-P1"]


def test_invalid_params_422_not_500(db, seeded):
    client = client_for(db, username="mdd-admin-422", role="admin")
    for params in (
        {"range": "ytd"},
        {"range": "custom"},
        {"range": "custom", "date_from": "2026-10-20", "date_to": "2026-10-01"},
        {"page_size": 0},
        {"page": -1},
        {"sort": "no_such_sort"},
    ):
        response = client.get(f"{BASE}/projects", params=params)
        assert response.status_code == 422, (params, response.status_code)
    assert client.get(f"{BASE}/receipts",
                      params={"sort": "bad_value_desc"}).status_code == 422
    assert client.get(f"{BASE}/export",
                      params={"kind": "nope"}).status_code == 422
    assert client.get(f"{BASE}/export",
                      params={"kind": "parts", "format": "pdf"}).status_code == 422


def test_quantity_checks_endpoint_and_unassigned(db, seeded):
    p1, _p2, part = seeded
    # 未关联需求单的实收（全范围账号可见汇总）。
    client = client_for(db, username="mdd-admin-t4", role="admin")
    response = client.get(f"{BASE}/quantity-checks")
    assert response.status_code == 200
    body = response.json()
    assert body["cards"]["unassigned_receipt_qty"] == "6.000"
    assert body["cards"]["note"].startswith("当前累计")
    for row in body["rows"]:
        assert not ({"status", "return_status", "overdue"} & set(row))


def test_export_csv_formula_guard_and_policy_column(db, seeded):
    p1, _p2, _seed_part = seeded
    formula = _part(db, "=1+1", unit="件")
    negative = _part(db, "-5", unit="件")
    _receipt(db, p1, formula, "1", condition="坏品",
             occurred_at=datetime(2026, 10, 18, 10, tzinfo=UTC))
    _receipt(db, p1, negative, "1", condition="坏品",
             occurred_at=datetime(2026, 10, 18, 11, tzinfo=UTC))
    db.commit()
    client = client_for(db, username="mdd-admin-csv", role="admin")
    export = client.get(f"{BASE}/export",
                        params={"kind": "parts", "format": "csv"})
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    text = export.content.decode("utf-8")
    header = next(csv.reader(io.StringIO(text)))
    assert "坏件估算价值-含税" in header
    assert "估价政策版本" in header
    # 公式形态 PN 加撇号；纯负数 PN（如 -5）不误伤。
    assert "'=1+1" in text
    assert "'-5" not in text
    assert "-5" in text


def test_export_xlsx_numbers_and_amount_gating(db, seeded):
    from openpyxl import load_workbook

    client = client_for(db, username="mdd-admin-xlsx", role="admin")
    export = client.get(f"{BASE}/export",
                        params={"kind": "projects", "format": "xlsx"})
    assert export.status_code == 200
    assert export.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    book = load_workbook(io.BytesIO(export.content))
    sheet = book.active
    header = [cell.value for cell in sheet[1]]
    assert "坏件估算价值-含税" in header
    assert "估价政策版本" in header
    row2 = [cell.value for cell in sheet[2]]
    value_cell = sheet[2][header.index("坏件估算价值-含税")]
    assert isinstance(value_cell.value, (int, float))  # 数值写数值单元格
    assert row2[header.index("估价政策版本")] == POLICY_VERSION

    # 无金额权：XLSX 同样不含金额列。
    username = "mdd-manager-xlsx"
    manager = client_for(db, username=username, role="maintenance_manager")
    _assign_manager(db, username, seeded[0])
    plain = manager.get(f"{BASE}/export",
                        params={"kind": "projects", "format": "xlsx"})
    assert plain.status_code == 200
    plain_book = load_workbook(io.BytesIO(plain.content))
    plain_header = [cell.value for cell in plain_book.active[1]]
    assert "坏件估算价值-含税" not in plain_header
    assert "估价政策版本" not in plain_header
    assert "返件实收合计" in plain_header


def test_receipts_test_result_filter(db, seeded):
    client = client_for(db, username="mdd-admin-rf", role="admin")
    response = client.get(f"{BASE}/receipts", params={"test_result": "坏品"})
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["test_result"] == "坏品"
