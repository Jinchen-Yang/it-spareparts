"""DEV-05A 行级数据疑点地基：模型、状态机、权限与 HTTP 契约。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAuditLog, SysImportBatch, SysUser
from app.services import data_quality
from app.services import dashboard


def _seed_purchase(db):
    batch = SysImportBatch(
        filename="采购疑点.xlsx", file_type="purchase", file_hash="dq-purchase",
        uploaded_by="导入员", status="success",
    )
    part = DimPart(pn_std="DQ-PN-001", description="疑点测试盘")
    db.add_all([batch, part])
    db.flush()
    order = FPurchaseOrder(
        raw_order_id="dq-order-raw", order_no="CG-DQ-001", purchaser="采购甲",
        order_date=date(2026, 7, 1), data_status="已生效", import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    line = FPurchaseLine(
        raw_line_id="dq-line-raw", order_id=order.id, line_no=1,
        part_id=part.id, pn_std=part.pn_std, description=part.description,
        qty=Decimal("2"), unit="块", unit_price=Decimal("999.00"),
        line_amount=Decimal("1998.00"), import_batch_id=batch.id,
    )
    db.add(line)
    db.commit()
    return batch, part, order, line


def _admin_client(db) -> TestClient:
    db.add(SysUser(
        username="dq_admin", role="admin", display_name="疑点管理员",
        password_hash=hash_password("pw123456"),
    ))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "dq_admin", "password": "pw123456"})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _login(username: str) -> TestClient:
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def _create_issue(db, line, **overrides):
    args = dict(
        side="purchase", line_id=line.id, rule_code="unit_price_outlier",
        rule_version="1", evidence={"unit_price": "999.00", "median": "100.00"},
        source_fingerprint="fp-1", detected_by="test-detector",
    )
    args.update(overrides)
    return data_quality.create_or_refresh_issue(db, **args)


def test_permission_defaults_and_dependency_are_fail_closed():
    assert permissions.ROLE_TEMPLATES["admin"]["action_data_quality_review"] is True
    for role in ("boss", "sales", "purchaser", "readonly"):
        assert permissions.ROLE_TEMPLATES[role]["action_data_quality_review"] is False
    assert permissions.ACTION_PAGE_DEPENDENCIES["action_data_quality_review"] == "page_governance"
    assert permissions.ACTION_DATA_DEPENDENCIES["action_data_quality_review"] == "data_purchase_cost"
    invalid = permissions.effective("readonly", {
        "action_data_quality_review": True, "page_governance": False,
        "data_purchase_cost": False,
    })
    assert permissions.combo_errors(invalid)


def test_create_refresh_is_idempotent_and_source_change_invalidates_decision(db):
    _, _, _, line = _seed_purchase(db)
    first = _create_issue(db, line)
    assert first["status"] == "open" and first["version"] == 1

    same = _create_issue(db, line)
    assert same["id"] == first["id"] and same["version"] == 1

    decided = data_quality.decide_issue(
        db, issue_id=first["id"], decision="confirmed_valid", version=1,
        note="实物与原单核对无误", operated_by="复核员甲",
    )
    assert decided["status"] == "confirmed_valid" and decided["version"] == 2

    changed = _create_issue(
        db, line, source_fingerprint="fp-2",
        evidence={"unit_price": "1099.00", "median": "100.00"},
    )
    assert changed["status"] == "source_changed" and changed["version"] == 3
    assert db.scalar(select(FactDataQualityIssue).where(FactDataQualityIssue.id == first["id"])).reviewed_by == "复核员甲"


def test_decision_reopen_conflict_note_and_audit(db):
    _, _, _, line = _seed_purchase(db)
    issue = _create_issue(db, line)
    with pytest.raises(data_quality.DataQualityValidationError):
        data_quality.decide_issue(
            db, issue_id=issue["id"], decision="confirmed_source_error",
            version=1, note="  ", operated_by="复核员乙",
        )
    done = data_quality.decide_issue(
        db, issue_id=issue["id"], decision="confirmed_source_error",
        version=1, note="源文件金额录入错误", operated_by="复核员乙",
    )
    with pytest.raises(data_quality.DataQualityConflictError):
        data_quality.reopen_issue(
            db, issue_id=issue["id"], version=1,
            note="旧页面重复提交", operated_by="复核员丙",
        )
    opened = data_quality.reopen_issue(
        db, issue_id=issue["id"], version=done["version"],
        note="收到新凭证，重新核对", operated_by="复核员丙",
    )
    assert opened["status"] == "open" and opened["version"] == 3
    logs = db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "data_quality_issue",
        SysAuditLog.entity_id == issue["id"],
    ).order_by(SysAuditLog.id)).all()
    assert [row.operated_by for row in logs][-2:] == ["复核员乙", "复核员丙"]
    assert logs[-1].before_json["status"] == "confirmed_source_error"
    assert logs[-1].after_json["status"] == "open"


def test_http_list_detail_filters_masking_and_state_errors(db):
    _, _, _, line = _seed_purchase(db)
    issue = _create_issue(db, line)
    client = _admin_client(db)

    listed = client.get("/api/data-quality/issues", params={"q": "CG-DQ-001"})
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["fact"]["pn_std"] == "DQ-PN-001"
    assert item["fact"]["unit_price"] == "999.00"
    assert item["fact"]["purchaser"] == "采购甲"
    assert item["fact"]["batch"]["filename"] == "采购疑点.xlsx"

    detail = client.get(f"/api/data-quality/issues/{issue['id']}")
    assert detail.status_code == 200
    assert detail.json()["evidence"]["unit_price"] == "999.00"

    blank = client.post(f"/api/data-quality/issues/{issue['id']}/decision", json={
        "decision": "confirmed_valid", "version": 1, "note": " ",
    })
    assert blank.status_code == 422
    ok = client.post(f"/api/data-quality/issues/{issue['id']}/decision", json={
        "decision": "confirmed_valid", "version": 1, "note": "原单正确",
    })
    assert ok.status_code == 200
    invalid_state = client.post(f"/api/data-quality/issues/{issue['id']}/decision", json={
        "decision": "confirmed_source_error", "version": 2, "note": "重复结论",
    })
    assert invalid_state.status_code == 409
    stale = client.post(f"/api/data-quality/issues/{issue['id']}/reopen", json={
        "version": 1, "note": "旧版本",
    })
    assert stale.status_code == 409
    assert client.get("/api/data-quality/issues/999999").status_code == 404


def test_same_fact_line_multiple_rules_all_keep_fact_summary(db):
    _, _, _, line = _seed_purchase(db)
    first = _create_issue(db, line)
    second = _create_issue(
        db, line, rule_code="quantity_unit_suspect",
        evidence={"qty": "2", "unit": "块"},
    )
    client = _admin_client(db)
    response = client.get("/api/data-quality/issues", params={"q": "DQ-PN-001"})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["id"] for item in response.json()["items"]} == {first["id"], second["id"]}
    for item in response.json()["items"]:
        assert item["fact"]["pn_std"] == "DQ-PN-001"
        assert item["fact"]["order_no"] == "CG-DQ-001"
        assert item["fact"]["unit_price"] == "999.00"


def test_account_validation_masking_and_write_gate(db):
    _, _, _, line = _seed_purchase(db)
    issue = _create_issue(db, line)
    admin = _admin_client(db)

    illegal_template = admin.post("/api/role-templates", json={
        "name": "盲判坏模板", "base_role": "readonly",
        "permissions": {
            "page_governance": True,
            "action_data_quality_review": True,
            "data_purchase_cost": False,
        },
    })
    assert illegal_template.status_code == 400, illegal_template.text

    illegal = admin.post("/api/accounts", json={
        "username": "dq_illegal", "password": "pw123456", "template_code": "readonly",
        "overrides": {
            "page_governance": True,
            "action_data_quality_review": True,
            "data_purchase_cost": False,
            "data_profit": False,
        },
    })
    assert illegal.status_code == 400, illegal.text

    created = admin.post("/api/accounts", json={
        "username": "dq_reader", "password": "pw123456", "template_code": "readonly",
        "overrides": {
            "page_governance": True, "data_purchase_cost": False, "data_profit": False,
        },
    })
    assert created.status_code == 201, created.text
    reader = _login("dq_reader")
    listed = reader.get("/api/data-quality/issues")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["fact"]["unit_price"] is None
    assert listed.json()["items"][0]["fact"]["line_amount"] is None
    detail = reader.get(f"/api/data-quality/issues/{issue['id']}")
    assert detail.status_code == 200
    assert detail.json()["evidence"] is None
    assert detail.json()["evidence_restricted"] is True
    assert all(entry["before"] is None and entry["after"] is None
               for entry in detail.json()["audit"])
    denied = reader.post(f"/api/data-quality/issues/{issue['id']}/decision", json={
        "decision": "confirmed_valid", "version": 1, "note": "不能盲判",
    })
    assert denied.status_code == 403

    reviewer_created = admin.post("/api/accounts", json={
        "username": "dq_reviewer", "password": "pw123456", "template_code": "readonly",
        "overrides": {
            "page_governance": True,
            "data_purchase_cost": True,
            "action_data_quality_review": True,
        },
    })
    assert reviewer_created.status_code == 201, reviewer_created.text
    reviewer = _login("dq_reviewer")
    allowed = reviewer.post(f"/api/data-quality/issues/{issue['id']}/decision", json={
        "decision": "confirmed_valid", "version": 1, "note": "已核对原始凭证",
    })
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["reviewed_by"] == "dq_reviewer"


def test_issue_workflow_does_not_mutate_source_facts_or_financial_fields(db):
    _, _, _, line = _seed_purchase(db)
    before = (line.qty, line.unit_price, line.line_amount, tuple(line.anomaly_flags))
    before_kpi = dashboard.kpi(
        db, date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 12, 31),
    )
    issue = _create_issue(db, line)
    assert dashboard.kpi(
        db, date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 12, 31),
    ) == before_kpi
    valid = data_quality.decide_issue(
        db, issue_id=issue["id"], decision="confirmed_valid", version=1,
        note="确认正确也不改变统计", operated_by="复核员甲",
    )
    opened = data_quality.reopen_issue(
        db, issue_id=issue["id"], version=valid["version"],
        note="重新核实", operated_by="复核员甲",
    )
    data_quality.decide_issue(
        db, issue_id=issue["id"], decision="confirmed_source_error", version=opened["version"],
        note="确认源错误仍只记录结论，本阶段不改统计", operated_by="复核员甲",
    )
    db.refresh(line)
    assert (line.qty, line.unit_price, line.line_amount, tuple(line.anomaly_flags)) == before
    assert dashboard.kpi(
        db, date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 12, 31),
    ) == before_kpi


def test_sales_side_summary_and_filters(db):
    batch = SysImportBatch(
        filename="销售疑点.xlsx", file_type="sales", file_hash="dq-sales",
        uploaded_by="销售导入员", status="success",
    )
    part = DimPart(pn_std="DQ-SALE-PN", description="销售疑点盘")
    db.add_all([batch, part])
    db.flush()
    order = FSalesOrder(
        raw_order_id="dq-sale-order-raw", order_no="XS-DQ-001", salesperson="销售甲",
        data_status="已生效", import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    line = FSalesLine(
        raw_line_id="dq-sale-line-raw", order_id=order.id, line_no=1,
        part_id=part.id, pn_std=part.pn_std, description=part.description,
        qty=Decimal("1"), unit="块", unit_price=Decimal("888.00"),
        line_amount=Decimal("888.00"), import_batch_id=batch.id,
    )
    db.add(line)
    db.commit()
    issue = data_quality.create_or_refresh_issue(
        db, side="sales", line_id=line.id, rule_code="sale_price_outlier",
        rule_version="1", evidence={"unit_price": "888.00"},
        source_fingerprint="sales-fp", detected_by="test-detector",
    )
    client = _admin_client(db)
    response = client.get("/api/data-quality/issues", params={
        "side": "sales", "rule_code": "sale_price_outlier", "q": "XS-DQ-001",
    })
    assert response.status_code == 200
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["id"] == issue["id"]
    assert item["fact"]["salesperson"] == "销售甲"
    assert item["fact"]["pn_std"] == "DQ-SALE-PN"
