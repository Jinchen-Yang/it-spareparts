"""页面权限必须同时驱动前端入口与后端 API 准入。

这组测试覆盖权限中心最容易漂移的两类故障：
1. 已授予 page_*，页面可见但 API 仍被旧的 require_admin 拒绝；
2. 已撤销 page_*，菜单隐藏但 API 仍可被直接调用。
"""
import csv
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.exc import DataError, ProgrammingError

from app import permissions
from app.api import imports as imports_api
from app.auth import hash_password
from app.config import get_settings
from app.main import app
from app.models.dimensions import DimPart
from app.models.system import SysAuditLog, SysImportJob, SysUser


@pytest.fixture()
def admin_client(db):
    db.add(SysUser(username="admin", role="admin", display_name="管理员",
                   password_hash=hash_password("adminpw")))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "admin", "password": "adminpw"})
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _account(admin_client: TestClient, username: str, template: str,
             overrides: dict[str, bool] | None = None) -> TestClient:
    created = admin_client.post("/api/accounts", json={
        "username": username,
        "password": "pw123456",
        "template_code": template,
        "overrides": overrides or {},
    })
    assert created.status_code == 201, created.text

    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    client.login_payload = login.json()  # type: ignore[attr-defined]
    return client


def _inventory_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["产品库存ID", "产品名称(PN)", "库存数量", "仓库"])
    sheet.append(["PERM-INV-1", "PERM-PN-1", 5, "总仓"])
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize(("username", "template"), [
    ("王小环", "purchaser"),
    ("刘朝红", "sales"),
])
def test_page_import_grant_unlocks_every_import_endpoint(
    db, admin_client, username, template,
):
    client = _account(admin_client, username, template, {"page_import": True})
    assert client.login_payload["permissions"]["page_import"] is True  # type: ignore[attr-defined]

    calls = [
        ("post", "/api/import/upload",
         {"files": {"file": ("bad.txt", b"x", "text/plain")}}, 400),
        ("post", "/api/import/precheck",
         {"files": [("files", ("bad.txt", b"x", "text/plain"))]}, 200),
        ("post", "/api/import/upload-batch",
         {"files": [("files", ("bad.txt", b"x", "text/plain"))]}, 400),
        ("get", "/api/import/jobs", {}, 200),
        ("get", "/api/import/jobs/999999", {}, 404),
        ("get", "/api/import/batches", {}, 200),
        ("get", "/api/import/batches/999999", {}, 404),
    ]
    for method, path, kwargs, expected in calls:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == expected, f"{method.upper()} {path}: {response.text}"


def test_page_import_runs_the_real_precheck_batch_and_history_flow(
    db, admin_client, monkeypatch, tmp_path,
):
    """锁住 ImportPage 的真实调用链，并验证导入人按具体账号留痕。"""
    client = _account(admin_client, "王小环", "purchaser", {"page_import": True})
    payload = _inventory_xlsx()

    settings = get_settings()
    monkeypatch.setattr(settings, "raw_file_dir", str(tmp_path / "raw"))

    class InlineThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    # 只替换 imports 模块持有的命名空间，不污染 TestClient/解释器的全局 threading.Thread。
    monkeypatch.setattr(imports_api, "threading", SimpleNamespace(Thread=InlineThread))

    precheck = client.post(
        "/api/import/precheck",
        files=[("files", ("inventory.xlsx", payload,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
    )
    assert precheck.status_code == 200, precheck.text
    assert precheck.json()["files"][0]["file_type"] == "inventory"

    submitted = client.post(
        "/api/import/upload-batch",
        files=[("files", ("inventory.xlsx", payload,
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
    )
    assert submitted.status_code == 200, submitted.text
    job_id = submitted.json()["job_id"]

    job = client.get(f"/api/import/jobs/{job_id}")
    assert job.status_code == 200, job.text
    assert job.json()["status"] == "done"
    assert job.json()["created_by"] == "王小环"
    assert len(job.json()["batches"]) == 1

    batch_id = job.json()["batches"][0]["id"]
    detail = client.get(f"/api/import/batches/{batch_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["uploaded_by"] == "王小环"
    listed = client.get("/api/import/batches")
    assert listed.status_code == 200
    assert listed.json()[0]["uploaded_by"] == "王小环"


def test_page_import_denial_still_blocks_read_and_write(db, admin_client):
    client = _account(admin_client, "import-denied", "sales", {"page_import": False})
    assert client.get("/api/import/batches").status_code == 403
    denied = client.post(
        "/api/import/precheck",
        files=[("files", ("bad.txt", b"x", "text/plain"))],
    )
    assert denied.status_code == 403


@pytest.mark.parametrize(("username", "overrides", "expected_stats"), [
    ("recompute-profit-hidden", {
        "page_import": True,
        "page_governance": True,
        "data_purchase_cost": True,
        "data_profit": False,
    }, {"sales_lines": 10, "no_cost": 2, "neg_margin": None, "fallback": 4}),
    ("recompute-cost-hidden", {
        "page_import": True,
        "page_governance": True,
        "data_purchase_cost": False,
        "data_profit": True,
    }, {"sales_lines": 10, "no_cost": None, "neg_margin": 3, "fallback": None}),
])
def test_import_and_governance_recompute_stats_follow_data_permissions(
    db, admin_client, monkeypatch, username, overrides, expected_stats,
):
    """触发重算的响应也必须脱敏，不能用短键绕过成本/利润数据权限。"""
    client = _account(admin_client, username, "readonly", overrides)
    raw_stats = {"sales_lines": 10, "no_cost": 2, "neg_margin": 3, "fallback": 4}

    monkeypatch.setattr("app.api.imports.pipeline.run_import", lambda *_args, **_kwargs:
                        SimpleNamespace(id=88, file_type="purchase", status="success",
                                        report_json={"rows": 1}))
    monkeypatch.setattr("app.api.imports._post_import_refresh", lambda *_args, **_kwargs:
                        dict(raw_stats))
    uploaded = client.post(
        "/api/import/upload",
        files={"file": ("permission.xlsx", b"not-read-by-mock",
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["recompute"] == expected_stats

    monkeypatch.setattr("app.api.governance.merge.merge_parts", lambda *_args, **_kwargs: {
        "source_pn": "SOURCE-PN", "target_pn": "TARGET-PN",
    })
    monkeypatch.setattr("app.api.governance.profit_svc.recompute", lambda _db: dict(raw_stats))
    monkeypatch.setattr("app.api.governance.inventory_svc.backfill_costs", lambda _db: {
        "updated": 0,
    })
    monkeypatch.setattr("app.api.governance.maintenance_svc.recompute", lambda _db: {
        "updated": 0,
    })
    merged = client.post("/api/governance/parts/merge", json={
        "source_pn": "SOURCE-PN", "target_pn": "TARGET-PN",
    })
    assert merged.status_code == 200, merged.text
    assert merged.json()["recompute"] == expected_stats


def test_import_and_governance_never_return_internal_exception_details(
    db, admin_client, monkeypatch, tmp_path,
):
    client = _account(admin_client, "safe-errors", "readonly", {
        "page_import": True,
        "page_governance": True,
    })
    secret = "secret_finance_table SELECT internal_sql"

    def raise_internal_error(*_args, **_kwargs):
        raise ProgrammingError(
            "SELECT * FROM secret_finance_table",
            {},
            RuntimeError(secret),
        )

    monkeypatch.setattr("app.api.imports.pipeline.run_import", lambda *_args, **_kwargs:
                        SimpleNamespace(id=89, file_type="purchase", status="success",
                                        report_json={"rows": 1}))
    monkeypatch.setattr("app.api.imports.profit.recompute", raise_internal_error)
    monkeypatch.setattr("app.api.imports.maintenance_cost.recompute", raise_internal_error)
    monkeypatch.setattr("app.api.imports.inventory.backfill_costs", lambda _db: {"updated": 0})
    monkeypatch.setattr("app.api.imports.master_data.refresh", lambda _db: {"updated": 0})

    uploaded = client.post(
        "/api/import/upload",
        files={"file": ("safe-error.xlsx", b"not-read-by-mock",
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert secret not in uploaded.text
    assert "secret_finance_table" not in uploaded.text
    assert uploaded.json()["recompute"]["error"] == (
        "利润重算失败，请到利润页手动重算；"
        "维保项目成本重算失败，请到项目成本页手动重算"
    )

    job = SysImportJob(
        created_by="safe-errors", status="processing", mode="skip", total_files=1,
    )
    db.add(job)
    db.commit()
    job_id = job.id
    tmp = tmp_path / "worker-safe-error.xlsx"
    tmp.write_bytes(b"not-read-by-mock")
    monkeypatch.setattr("app.api.imports.pipeline.run_import", raise_internal_error)
    imports_api._process_import_job(
        job_id, [(str(tmp), "worker-safe-error.xlsx")], "skip", "safe-errors",
    )
    job_response = client.get(f"/api/import/jobs/{job_id}")
    assert job_response.status_code == 200, job_response.text
    assert secret not in job_response.text
    assert "secret_finance_table" not in job_response.text
    assert "请联系管理员查看服务端日志" in job_response.json()["note"]

    monkeypatch.setattr("app.api.governance.merge.merge_parts", lambda *_args, **_kwargs: {
        "source_pn": "SOURCE-PN", "target_pn": "TARGET-PN",
    })
    monkeypatch.setattr("app.api.governance.merge.unmerge", lambda *_args, **_kwargs: {
        "merge_log_id": 1,
    })
    monkeypatch.setattr("app.api.governance.profit_svc.recompute", raise_internal_error)
    for path, payload in (
        ("/api/governance/parts/merge", {
            "source_pn": "SOURCE-PN", "target_pn": "TARGET-PN",
        }),
        ("/api/governance/parts/unmerge", {"merge_log_id": 1}),
    ):
        response = client.post(path, json=payload)
        assert response.status_code == 200, response.text
        assert secret not in response.text
        assert "secret_finance_table" not in response.text
        assert response.json()["recompute_failed"] == (
            "治理操作已完成，但后置重算失败，请联系管理员处理"
        )


def test_page_governance_grant_allows_work_masks_sensitive_fields_and_audits_username(
    db, admin_client, monkeypatch,
):
    client = _account(admin_client, "治理专员", "readonly", {
        "page_governance": True,
        "data_purchase_cost": False,
        "data_profit": False,
    })

    monkeypatch.setattr("app.api.governance.governance.summary", lambda _db: {
        "parts_total": 1,
        "sales_no_cost": 8,
        "sales_neg_margin": 7,
        "sales_fallback_cost": 6,
    })
    summary = client.get("/api/governance/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["sales_no_cost"] is None
    assert summary.json()["sales_neg_margin"] is None
    assert summary.json()["sales_fallback_cost"] is None

    monkeypatch.setattr("app.api.governance.governance.list_parts", lambda *_args: {
        "kind": "nonstd", "total": 1, "page": 1, "page_size": 20,
        "items": [{"pn_std": "TEST-PN", "gross_margin": 0.42}],
    })
    parts = client.get("/api/governance/parts")
    assert parts.status_code == 200, parts.text
    assert parts.json()["items"][0]["gross_margin"] is None

    db.add(DimPart(pn_std="治理测试-PN"))
    db.commit()
    changed = client.put("/api/governance/exclude", json={
        "pn_std": "治理测试-PN", "excluded": True, "reason": "权限契约回归",
    })
    assert changed.status_code == 200, changed.text
    db.expire_all()
    audit = db.query(SysAuditLog).filter_by(entity_type="part", action="exclude").one()
    assert audit.operated_by == "治理专员"


def test_page_governance_bulk_writes_audit_the_real_username(
    db, admin_client, monkeypatch,
):
    client = _account(admin_client, "治理批处理专员", "readonly", {
        "page_governance": True,
    })
    monkeypatch.setattr("app.api.governance.master_data.refresh", lambda _db: {
        "quality_scored": 3,
    })
    monkeypatch.setattr("app.api.governance.match_candidates.generate", lambda _db: {
        "candidates_inserted": 2,
    })
    monkeypatch.setattr("app.api.governance.master_data.classify_backfill", lambda _db: {
        "scanned": 5,
        "parts_reclassified": 1,
    })

    refreshed = client.post("/api/governance/refresh")
    assert refreshed.status_code == 200, refreshed.text
    classified = client.post("/api/governance/classify-backfill")
    assert classified.status_code == 200, classified.text

    db.expire_all()
    audits = db.query(SysAuditLog).filter_by(entity_type="governance").all()
    assert {(row.action, row.operated_by) for row in audits} == {
        ("refresh", "治理批处理专员"),
        ("classify_backfill", "治理批处理专员"),
    }
    refresh_audit = next(row for row in audits if row.action == "refresh")
    assert refresh_audit.after_json == {
        "quality_scored": 3,
        "candidates": {"candidates_inserted": 2},
    }


def test_page_governance_refresh_is_audited_if_candidate_generation_fails(
    db, admin_client, monkeypatch,
):
    client = _account(admin_client, "治理失败审计专员", "readonly", {
        "page_governance": True,
    })
    monkeypatch.setattr("app.api.governance.master_data.refresh", lambda _db: {
        "quality_scored": 3,
    })

    def fail_candidates(candidate_db):
        # 用真实数据库异常把 endpoint Session 置为 failed，验证实现会 rollback 后再审计。
        candidate_db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr("app.api.governance.match_candidates.generate", fail_candidates)

    with pytest.raises(DataError, match="division by zero"):
        client.post("/api/governance/refresh")

    db.expire_all()
    audit = db.query(SysAuditLog).filter_by(
        entity_type="governance", action="refresh",
    ).one()
    assert audit.operated_by == "治理失败审计专员"
    assert audit.after_json == {
        "quality_scored": 3,
        "candidates": {"status": "failed", "error_type": "DataError"},
    }


@pytest.mark.parametrize(("username", "overrides"), [
    ("governance-profit-without-cost", {
        "page_governance": True,
        "data_purchase_cost": False,
        "data_profit": True,
    }),
    ("governance-cost-without-profit", {
        "page_governance": True,
        "data_purchase_cost": True,
        "data_profit": False,
    }),
])
def test_governance_parts_do_not_allow_cost_or_profit_inference(
    db, admin_client, monkeypatch, username, overrides,
):
    client = _account(admin_client, username, "readonly", overrides)
    monkeypatch.setattr("app.api.governance.governance.list_parts", lambda *_args: {
        "kind": "nonstd", "total": 1, "page": 1, "page_size": 20,
        "items": [{
            "pn_std": "SECURE-PN",
            "revenue": 100.0,
            "gross_margin": 0.4,
        }],
    })

    response = client.get("/api/governance/parts")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0] == {
        "pn_std": "SECURE-PN",
        "revenue": 100.0,
        "gross_margin": None,
    }


def test_page_governance_denial_blocks_read_and_write(db, admin_client):
    client = _account(admin_client, "governance-denied", "readonly", {
        "page_governance": False,
    })
    assert client.get("/api/governance/summary").status_code == 403
    assert client.put("/api/governance/exclude", json={
        "pn_std": "missing", "excluded": True,
    }).status_code == 403


def test_page_profit_allows_reports_but_keeps_recompute_admin_only(db, admin_client):
    boss = _account(admin_client, "profit-boss", "boss")
    assert boss.get("/api/profit", params={"dimension": "part"}).status_code == 200
    assert boss.get("/api/profit/export", params={"dimension": "part"}).status_code == 200
    assert boss.post("/api/profit/recompute").status_code == 403

    denied = _account(admin_client, "profit-denied", "sales", {"page_profit": False})
    assert denied.get("/api/profit", params={"dimension": "part"}).status_code == 403
    assert denied.get("/api/profit/export", params={"dimension": "part"}).status_code == 403


def test_profit_report_rejects_dimensions_that_would_bypass_data_scope(db, admin_client):
    scoped_sales = _account(admin_client, "profit-sales", "sales", {"page_profit": True})
    assert scoped_sales.get("/api/profit", params={"dimension": "part"}).status_code == 200
    for path in ("/api/profit", "/api/profit/export"):
        assert scoped_sales.get(path, params={"dimension": "salesperson"}).status_code == 403
        assert scoped_sales.get(path, params={"dimension": "customer"}).status_code == 403

    customer_blind = _account(admin_client, "profit-purchaser", "purchaser", {
        "page_profit": True,
        "data_customer": False,
    })
    assert customer_blind.get("/api/profit", params={"dimension": "customer"}).status_code == 403
    assert customer_blind.get("/api/profit/export", params={"dimension": "customer"}).status_code == 403


@pytest.mark.parametrize(("username", "overrides"), [
    ("profit-without-cost", {
        "page_profit": True,
        "data_purchase_cost": False,
        "data_profit": True,
    }),
    ("cost-without-profit", {
        "page_profit": True,
        "data_purchase_cost": True,
        "data_profit": False,
    }),
])
def test_profit_report_and_export_do_not_allow_cross_permission_inference(
    db, admin_client, monkeypatch, username, overrides,
):
    """成本和毛利互为可推导值；只授权一边时，利润页必须把两边一起收敛。"""
    client = _account(admin_client, username, "readonly", overrides)
    monkeypatch.setattr("app.api.profit.profit.aggregate", lambda *_args: {
        "dimension": "part",
        "rows": [{
            "dimension": "SECURE-PN",
            "revenue": 100.0,
            "revenue_costed": 100.0,
            "cost_moving_avg": 60.0,
            "gross_profit_moving": 40.0,
            "gross_margin_moving": 0.4,
            "cost_fifo": 70.0,
            "gross_profit_fifo": 30.0,
            "gross_margin_fifo": 0.3,
            "lines": 1,
            "no_cost": 0,
            "excluded_revenue": 0.0,
        }],
    })

    report = client.get("/api/profit", params={"dimension": "part"})
    assert report.status_code == 200, report.text
    row = report.json()["rows"][0]
    protected = (
        "cost_moving_avg", "gross_profit_moving", "gross_margin_moving",
        "cost_fifo", "gross_profit_fifo", "gross_margin_fifo",
    )
    assert all(row[key] is None for key in protected)
    assert row["revenue_costed"] is None
    assert row["no_cost"] is None

    assert client.get("/api/profit", params={
        "dimension": "part", "only_anomaly": True,
    }).status_code == 403

    exported = client.get("/api/profit/export", params={"dimension": "part"})
    assert exported.status_code == 200, exported.text
    csv_rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert csv_rows[1][1] == "100.0"
    assert csv_rows[1][2] == ""
    assert csv_rows[1][3:9] == ["", "", "", "", "", ""]
    assert csv_rows[1][10] == ""
    assert client.get("/api/profit/export", params={
        "dimension": "part", "only_anomaly": True,
    }).status_code == 403


def test_page_inventory_controls_reads_but_not_admin_updates(db, admin_client):
    denied = _account(admin_client, "inventory-denied", "sales", {"page_inventory": False})
    for path in ("/api/inventory", "/api/inventory/dynamic", "/api/inventory/warehouses"):
        assert denied.get(path).status_code == 403, path

    allowed = _account(admin_client, "inventory-allowed", "sales", {"page_inventory": True})
    for path in ("/api/inventory", "/api/inventory/dynamic", "/api/inventory/warehouses"):
        response = allowed.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
    assert allowed.put("/api/inventory/999999", json={"manual_qty": 1}).status_code == 403


@pytest.mark.parametrize("path", [
    "/api/import/batches",
    "/api/governance/summary",
    "/api/profit",
    "/api/inventory",
])
def test_page_gates_never_replace_hard_authentication(db, path):
    assert TestClient(app).get(path).status_code == 401


def test_permission_metadata_still_describes_the_enforced_contract():
    """防止后续又把这些键改成纯前端装饰。"""
    for key in ("page_import", "page_governance", "page_profit", "page_inventory"):
        assert key in permissions.PAGE_KEYS
        assert permissions.PERMISSION_META[key]["summary"]
