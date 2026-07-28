"""维保导出下载文件名：ASCII 响应头 + RFC 5987 UTF-8 文件名。"""
import csv
import io
from datetime import date
from decimal import Decimal
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.api import maintenance as maintenance_api
from app.auth import hash_password
from app.etl import loader
from app.main import app
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_cost
from tests import factories as f


def _admin_client(db) -> TestClient:
    db.add(SysUser(
        username="maintenance_export_admin",
        role="admin",
        password_hash=hash_password("pw123456"),
        is_active=True,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_admin", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _readonly_client(db) -> TestClient:
    db.add(SysUser(
        username="maintenance_export_readonly",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_readonly", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _cost_blind_maintenance_client(db) -> TestClient:
    base = permissions.effective("readonly", None)
    overrides = {"page_maintenance": True, "data_purchase_cost": False}
    effective = permissions.effective_from_snapshot(base, overrides)
    db.add(SysUser(
        username="maintenance_export_cost_blind",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
        template_code="readonly",
        template_version=1,
        template_perms=base,
        perm_overrides=overrides,
        permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_cost_blind", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _profit_blind_maintenance_client(db) -> TestClient:
    base = permissions.effective("readonly", None)
    overrides = {
        "page_maintenance": True,
        "data_purchase_cost": True,
        "data_profit": False,
    }
    effective = permissions.effective_from_snapshot(base, overrides)
    db.add(SysUser(
        username="maintenance_export_profit_blind",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
        template_code="readonly",
        template_version=1,
        template_perms=base,
        perm_overrides=overrides,
        permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maintenance_export_profit_blind", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def test_chinese_project_lines_csv_uses_ascii_header_and_utf8_filename(db):
    client = _admin_client(db)
    project = "华北核心网维保项目"

    response = client.get("/api/maintenance/lines/export", params={"project": project})

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    expected_name = f"maintenance_lines_{project}.csv"
    assert disposition == (
        'attachment; filename="maintenance_lines.csv"; '
        f"filename*=UTF-8''{quote(expected_name, safe='!#$&+-.^_`|~')}"
    )


def test_project_lines_csv_fail_closes_invalid_cost_and_exports_explicit_tier(db):
    batch = SysImportBatch(
        filename="invalid-cost.csv.xlsx",
        file_type="maintenance",
        file_hash="invalid-cost-csv",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M1": f.maintenance_head(
                    "M1",
                    project="明细严格分层项目",
                    on=date(2026, 7, 1),
                ),
            },
            [f.maintenance_line("M1", "ML1", "PN-INVALID", qty="1")],
        ),
        batch.id,
        date(2026, 7, 2),
    )
    line = db.execute(select(FMaintenanceLine)).scalar_one()
    line.cost_source = "future_source"
    line.cost_tax_basis = "inc"
    line.unit_cost = Decimal("999.00")
    line.cost_amount = Decimal("999.00")
    db.commit()

    def csv_row(response):
        parsed = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert len(parsed) == 2
        return dict(zip(parsed[0], parsed[1], strict=True))

    admin_row = csv_row(
        _admin_client(db).get(
            "/api/maintenance/lines/export",
            params={"project": "明细严格分层项目"},
        ),
    )
    assert admin_row["单价"] == ""
    assert admin_row["金额"] == ""
    assert admin_row["成本事实层级"] == "成本缺失"
    assert admin_row["成本来源"] == "future_source"

    cost_blind_row = csv_row(
        _cost_blind_maintenance_client(db).get(
            "/api/maintenance/lines/export",
            params={"project": "明细严格分层项目"},
        ),
    )
    assert cost_blind_row["成本事实层级"] == ""
    assert cost_blind_row["成本来源"] == ""


def test_project_lines_csv_sanitizes_every_dynamic_text_cell(db):
    batch = SysImportBatch(
        filename="formula-cells.xlsx",
        file_type="maintenance",
        file_hash="formula-cells",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M-FORMULA": f.maintenance_head(
                    "M-FORMULA",
                    order_no="=ORDER()",
                    project="CSV统一净化",
                    on=date(2026, 7, 1),
                    demand_type="  +DEMAND()",
                    business_type="@BUSINESS()",
                ),
            },
            [
                f.maintenance_line(
                    "M-FORMULA",
                    "ML-FORMULA",
                    "=PN()",
                    description="\x01=DESCRIPTION()",
                ),
            ],
        ),
        batch.id,
        date(2026, 7, 2),
    )
    order = db.execute(select(FMaintenanceOrder)).scalar_one()
    order.warehouse = "\t=WAREHOUSE()"
    line = db.execute(select(FMaintenanceLine)).scalar_one()
    line.linked_purchase_order_no = "\r@PURCHASE()"
    line.anomaly_flags = ["=FLAG()"]
    db.commit()

    response = _admin_client(db).get(
        "/api/maintenance/lines/export",
        params={"project": "CSV统一净化"},
    )

    assert response.status_code == 200, response.text
    parsed = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    row = dict(zip(parsed[0], parsed[1], strict=True))
    for header in ("维保单号", "需求类型", "业务类型", "出库仓库", "PN", "描述",
                   "关联采购单", "异常标记"):
        assert row[header].startswith("'"), (header, row[header])
    assert "\x01" not in row["描述"]


def test_project_summary_csv_sanitizes_formula_after_controls_and_whitespace(db, monkeypatch):
    def fake_projects(*_args, **_kwargs):
        return {
            "rows": [{
                "project": "\x01  =PROJECT()",
                "lifecycle_status": "ongoing",
                "maint_end": "2027-01-01",
                "lines": 1,
                "qty": 1,
                "actual_cost_inc": 1,
                "actual_cost_ex": 0,
                "estimated_cost_inc": 0,
                "estimated_cost_ex": 0,
                "actual_lines": 1,
                "estimated_lines": 0,
                "missing_cost_lines": 0,
                "known_cost_total": 1,
                "cost_quality": "actual_only",
                "cost_inc": 1,
                "cost_ex": 0,
                "cost_total": 1,
                "coverage_pct": 100,
                "by_source": {
                    "direct": 1, "window": 0, "month_avg": 0,
                    "trace_avg": 0, "sales_ref": 0, "none": 0,
                },
                "months": 1,
                "sales_orders": ["\t@CONTRACT()"],
                "contract_amount": 10,
                "contract_shared": False,
            }],
        }

    monkeypatch.setattr(maintenance_cost, "projects_aggregate", fake_projects)
    response = _admin_client(db).get("/api/maintenance/export", params={"lifecycle": "all"})

    assert response.status_code == 200, response.text
    parsed = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    row = dict(zip(parsed[0], parsed[1], strict=True))
    assert row["项目"].startswith("'")
    assert "\x01" not in row["项目"]
    assert row["关联销售订单"].startswith("'")


def test_project_lines_csv_rejects_over_one_million_before_row_materialization(
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        maintenance_cost,
        "project_line_count",
        lambda *_args, **_kwargs: 1_000_001,
    )

    def must_not_iterate(*_args, **_kwargs):
        raise AssertionError("row iterator must not start after count preflight rejection")

    monkeypatch.setattr(maintenance_cost, "iter_project_lines", must_not_iterate)
    response = _admin_client(db).get(
        "/api/maintenance/lines/export",
        params={"project": "超量项目"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "CSV 数据行超过 1000000 行上限"


def test_project_lines_csv_rejects_escaped_cell_over_excel_limit(db, monkeypatch):
    monkeypatch.setattr(
        maintenance_cost,
        "project_line_count",
        lambda *_args, **_kwargs: 1,
    )
    row = {
        "order_date": "2026-07-01",
        "order_no": "WBDD-1",
        "demand_type": "报修供货",
        "business_type": "备件维保",
        "warehouse": "仓库",
        "pn_std": "PN-1",
        "description": "=" + "x" * 32_766,
        "qty": 1,
        "return_qty": 0,
        "unit_cost": 1,
        "cost_amount": 1,
        "cost_tier": "actual",
        "cost_source": "direct",
        "confidence": "high",
        "cost_tax_basis": "inc",
        "price_month": "2026-07",
        "trace_months": 0,
        "price_distance_days": 0,
        "linked_purchase_order_no": None,
        "anomaly_flags": [],
    }
    class ClosingRows:
        def __init__(self):
            self._rows = iter([row])
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._rows)

        def close(self):
            self.closed = True

    rows = ClosingRows()
    monkeypatch.setattr(
        maintenance_cost,
        "iter_project_lines",
        lambda *_args, **_kwargs: rows,
    )

    response = _admin_client(db).get(
        "/api/maintenance/lines/export",
        params={"project": "超长单元格"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "CSV 单元格超过 32767 字符安全上限"
    assert rows.closed is True


def test_project_lines_csv_rejects_total_dynamic_text_budget_before_response(
    db,
    monkeypatch,
):
    monkeypatch.setattr(maintenance_api, "_MAX_CSV_DYNAMIC_TEXT_BYTES", 10)
    monkeypatch.setattr(
        maintenance_cost,
        "project_line_count",
        lambda *_args, **_kwargs: 0,
    )
    response = _admin_client(db).get(
        "/api/maintenance/lines/export",
        params={"project": "文本预算"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "CSV 动态文本超过 64 MiB 安全上限"


def test_project_lines_csv_rejects_total_encoded_output_budget(db, monkeypatch):
    monkeypatch.setattr(maintenance_api, "_MAX_CSV_OUTPUT_BYTES", 10)
    monkeypatch.setattr(
        maintenance_cost,
        "project_line_count",
        lambda *_args, **_kwargs: 0,
    )
    response = _admin_client(db).get(
        "/api/maintenance/lines/export",
        params={"project": "文件预算"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "CSV 文件超过 512 MiB 安全上限"


def test_project_line_iterator_closes_server_result_when_consumer_stops(monkeypatch):
    class Result:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return iter([(object(), object())])

        def close(self):
            self.closed = True

    result = Result()

    class DB:
        def execute(self, _statement):
            return result

    monkeypatch.setattr(
        maintenance_cost,
        "_serialize_project_line",
        lambda *_args, **_kwargs: {"row": 1},
    )
    rows = maintenance_cost.iter_project_lines(DB(), "项目")
    assert next(rows) == {"row": 1}
    rows.close()
    assert result.closed is True


def test_chinese_contract_workbook_uses_ascii_header_and_utf8_filename(db):
    client = _admin_client(db)
    contract = "北京联通核心网维保合同"

    response = client.get(
        "/api/maintenance/export-workbook",
        params={"contract": contract},
    )

    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK"
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    expected_name = f"project_workbook_{contract}.xlsx"
    assert disposition == (
        'attachment; filename="project_workbook.xlsx"; '
        f"filename*=UTF-8''{quote(expected_name, safe='!#$&+-.^_`|~')}"
    )


def test_project_filename_cannot_inject_headers_or_paths(db):
    client = _admin_client(db)
    project = "华北\r\nX-Injected: yes/../../escape"

    response = client.get("/api/maintenance/lines/export", params={"project": project})

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    disposition.encode("ascii")
    assert "\r" not in disposition and "\n" not in disposition
    encoded_name = disposition.split("filename*=UTF-8''", 1)[1]
    decoded_name = unquote(encoded_name)
    assert decoded_name.startswith("maintenance_lines_华北_")
    assert not any(char in decoded_name for char in '\r\n/\\:*?"<>|')


def test_project_summary_csv_uses_same_dual_filename_contract(db):
    client = _admin_client(db)

    response = client.get("/api/maintenance/export", params={"lifecycle": "all"})

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        'attachment; filename="maintenance_projects.csv"; '
        "filename*=UTF-8''maintenance_projects.csv"
    )


def test_project_summary_csv_uses_layered_cost_truth_and_keeps_masked_sources_blank(
    db,
    monkeypatch,
):
    def fake_projects(*_args, **_kwargs):
        return {
            "rows": [{
                "project": "成本分层项目",
                "lifecycle_status": "ongoing",
                "maint_end": "2027-01-01",
                "lines": 3,
                "qty": 3,
                "actual_cost_inc": 100,
                "actual_cost_ex": 20,
                "estimated_cost_inc": 30,
                "estimated_cost_ex": 40,
                "actual_lines": 1,
                "estimated_lines": 1,
                "missing_cost_lines": 1,
                "known_cost_total": 190,
                "cost_quality": "incomplete",
                "cost_inc": 130,
                "cost_ex": 60,
                "cost_total": 190,
                "coverage_pct": 66.67,
                "by_source": {
                    "direct": 1,
                    "window": 0,
                    "month_avg": 0,
                    "trace_avg": 1,
                    "sales_ref": 0,
                    "none": 1,
                },
                "months": 1,
                "sales_orders": ["XS-CSV"],
                "contract_amount": 1000,
                "contract_shared": False,
            }],
        }

    def row_by_header(response):
        parsed = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert len(parsed) == 2
        return dict(zip(parsed[0], parsed[1], strict=True))

    monkeypatch.setattr(maintenance_cost, "projects_aggregate", fake_projects)

    admin_row = row_by_header(
        _admin_client(db).get("/api/maintenance/export", params={"lifecycle": "all"}),
    )
    assert admin_row["实际采购参考-含税"] == "100"
    assert admin_row["估算参考-不含税"] == "40"
    assert admin_row["缺失成本行数"] == "1"
    assert admin_row["已知成本参考(混合原值)"] == "190"
    assert admin_row["成本完整性"] == "成本不完整，需补数据"
    assert admin_row["实际·专属采购(行)"] == "1"
    assert admin_row["成本缺失(行)"] == "1"

    profit_blind_row = row_by_header(
        _profit_blind_maintenance_client(db).get(
            "/api/maintenance/export",
            params={"lifecycle": "all"},
        ),
    )
    assert profit_blind_row["实际采购参考-含税"] == "100"
    assert profit_blind_row["估算参考-不含税"] == "40"
    assert profit_blind_row["成本完整性"] == "成本不完整，需补数据"
    assert profit_blind_row["合同额(含税参考)"] == ""

    masked_row = row_by_header(
        _cost_blind_maintenance_client(db).get(
            "/api/maintenance/export",
            params={"lifecycle": "all"},
        ),
    )
    for header in (
        "实际采购参考-含税",
        "估算参考-不含税",
        "实际参考行数",
        "估算参考行数",
        "缺失成本行数",
        "已知成本参考(混合原值)",
        "成本完整性",
        "覆盖率%",
        "实际·专属采购(行)",
        "预估·追溯均价(行)",
        "成本缺失(行)",
    ):
        assert masked_row[header] == ""


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/maintenance/export", {"lifecycle": "all"}),
        ("/api/maintenance/lines/export", {"project": "中文项目"}),
        ("/api/maintenance/export-workbook", {"contract": "中文合同"}),
    ],
)
def test_export_endpoints_keep_anonymous_401_and_no_page_403(db, path, params):
    anonymous = TestClient(app)
    assert anonymous.get(path, params=params).status_code == 401

    readonly = _readonly_client(db)
    assert readonly.get(path, params=params).status_code == 403


def test_cost_blind_user_can_export_masked_csv_but_workbook_stays_403(db):
    client = _cost_blind_maintenance_client(db)

    csv_response = client.get(
        "/api/maintenance/lines/export",
        params={"project": "中文项目"},
    )
    workbook_response = client.get(
        "/api/maintenance/export-workbook",
        params={"contract": "中文合同"},
    )

    assert csv_response.status_code == 200, csv_response.text
    assert workbook_response.status_code == 403
    assert workbook_response.json()["detail"] == "无成本及利润查看权限，不能导出项目成本工作簿"
