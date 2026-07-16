"""DEV-05C1 确定性金额疑点检测器。"""
from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.auth import hash_password
from app.db import engine
from app.main import app
from app.etl import loader, mapping, pipeline
from app.models.data_quality import FactDataQualityIssue
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.models.system import SysImportBatch, SysUser
from app.models.system import SysAuditLog
from app.services import data_quality
from app.services import dashboard
from app.services import data_quality_amount_mismatch as detector
from tests import factories as f


def _xlsx(tmp_path, *, side: str, filename: str, qty="2", unit_price="10",
          line_amount="25") -> str:
    config = mapping.MAPPINGS[side]
    cn = {
        **{internal: source for source, internal in config["head"].items()},
        **{internal: source for source, internal in config["line"].items()},
    }
    prefix = "CG" if side == mapping.PURCHASE else "XS"
    row = {
        "raw_order_id": f"{prefix}-RAW-1",
        "order_no": f"{prefix}-DQ-1",
        "order_date": "2026-07-16",
        "data_status": "已生效",
        "raw_line_id": f"{prefix}-LINE-1",
        "line_no": 1,
        "pn_raw": f"{prefix}-PN-1",
        "qty": Decimal(qty),
        "unit_price": Decimal(unit_price),
        "line_amount": Decimal(line_amount),
    }
    if side == mapping.SALES:
        row["business_type"] = "配件销售"
    path = tmp_path / filename
    pd.DataFrame([{cn[key]: value for key, value in row.items()}]).to_excel(path, index=False)
    return str(path)


def test_purchase_import_creates_one_named_amount_mismatch_issue_and_reimport_is_idempotent(
    db, tmp_path,
):
    path = _xlsx(tmp_path, side=mapping.PURCHASE, filename="mismatch.xlsx")
    first_batch = pipeline.run_import(
        db, path, "mismatch.xlsx", uploaded_by="刘朝红", mode="skip",
    )
    db.commit()

    issue = db.scalar(select(FactDataQualityIssue))
    assert issue is not None
    assert issue.rule_code == "amount_mismatch"
    assert issue.rule_version == "etl-v1"
    assert issue.status == "open"
    assert issue.detected_by == "刘朝红"
    assert issue.evidence == {
        "qty": "2",
        "unit_price": "10",
        "line_amount": "25",
        "expected_amount": "20",
        "absolute_difference": "5",
        "tolerance": "0.05",
        "current_match": True,
        "detection_source": "etl_import",
    }
    assert first_batch.report_json["data_quality_detection"] == {
        "scanned": 1, "matched": 1, "created": 1,
        "refreshed": 0, "unchanged": 0, "source_changed": 0,
    }

    same = tmp_path / "mismatch-again.xlsx"
    shutil.copy(path, same)
    second_batch = pipeline.run_import(
        db, str(same), "mismatch.xlsx", uploaded_by="刘朝红", mode="upsert",
    )
    db.commit()
    db.expire_all()

    issues = db.scalars(select(FactDataQualityIssue)).all()
    assert len(issues) == 1
    assert issues[0].id == issue.id
    assert issues[0].version == 1
    assert second_batch.report_json["data_quality_detection"]["unchanged"] == 1
    assert db.scalar(select(func.count()).select_from(SysAuditLog).where(
        SysAuditLog.entity_type == "data_quality_issue",
        SysAuditLog.entity_id == issue.id,
    )) == 1


def test_corrected_source_invalidates_old_conclusion_and_keeps_new_evidence(db, tmp_path):
    original = _xlsx(
        tmp_path, side=mapping.PURCHASE, filename="source-before.xlsx",
        qty="2", unit_price="10", line_amount="25",
    )
    pipeline.run_import(db, original, "source-before.xlsx", uploaded_by="刘朝红")
    db.commit()
    issue = db.scalar(select(FactDataQualityIssue))
    decided = data_quality.decide_issue(
        db, issue_id=issue.id, decision="confirmed_source_error", version=issue.version,
        note="已按原始凭证确认", operated_by="数据维护员甲",
    )
    assert decided["status"] == "confirmed_source_error"

    corrected = _xlsx(
        tmp_path, side=mapping.PURCHASE, filename="source-corrected.xlsx",
        qty="2", unit_price="10", line_amount="20",
    )
    batch = pipeline.run_import(
        db, corrected, "source-corrected.xlsx", uploaded_by="王小环", mode="upsert",
    )
    db.commit()
    db.expire_all()

    current = db.get(FactDataQualityIssue, issue.id)
    assert current.status == "source_changed"
    assert current.version == 3
    assert current.detected_by == "王小环"
    assert current.evidence == {
        "qty": "2",
        "unit_price": "10",
        "line_amount": "20",
        "expected_amount": "20",
        "absolute_difference": "0",
        "tolerance": "0.05",
        "current_match": False,
        "detection_source": "etl_import",
    }
    assert batch.report_json["data_quality_detection"] == {
        "scanned": 1, "matched": 0, "created": 0,
        "refreshed": 0, "unchanged": 0, "source_changed": 1,
    }
    logs = db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "data_quality_issue",
        SysAuditLog.entity_id == issue.id,
    ).order_by(SysAuditLog.id)).all()
    assert [log.action for log in logs] == ["create", "decision", "source_changed"]
    assert logs[-1].operated_by == "王小环"


def test_changed_source_that_still_matches_invalidates_old_conclusion(db, tmp_path):
    original = _xlsx(
        tmp_path, side=mapping.PURCHASE, filename="still-before.xlsx",
        qty="2", unit_price="10", line_amount="25",
    )
    pipeline.run_import(db, original, "still-before.xlsx", uploaded_by="刘朝红")
    db.commit()
    issue = db.scalar(select(FactDataQualityIssue))
    data_quality.decide_issue(
        db, issue_id=issue.id, decision="confirmed_valid", version=issue.version,
        note="按旧文件核实正确", operated_by="数据维护员甲",
    )

    changed = _xlsx(
        tmp_path, side=mapping.PURCHASE, filename="still-after.xlsx",
        qty="2", unit_price="10", line_amount="26",
    )
    batch = pipeline.run_import(
        db, changed, "still-after.xlsx", uploaded_by="王小环", mode="upsert",
    )
    db.commit()
    db.expire_all()

    current = db.get(FactDataQualityIssue, issue.id)
    assert current.status == "source_changed"
    assert current.version == 3
    assert current.evidence["current_match"] is True
    assert current.evidence["absolute_difference"] == "6"
    assert current.detected_by == "王小环"
    assert batch.report_json["data_quality_detection"]["refreshed"] == 1
    assert batch.report_json["data_quality_detection"]["source_changed"] == 1
    logs = db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "data_quality_issue",
        SysAuditLog.entity_id == issue.id,
    ).order_by(SysAuditLog.id)).all()
    assert [log.action for log in logs] == ["create", "decision", "source_changed"]


def test_detector_joins_the_import_transaction_instead_of_committing_early(db, tmp_path):
    path = _xlsx(
        tmp_path, side=mapping.PURCHASE, filename="atomic.xlsx",
        qty="2", unit_price="10", line_amount="25",
    )
    pipeline.run_import(db, path, "atomic.xlsx", uploaded_by="刘朝红")
    assert db.scalar(select(func.count()).select_from(FactDataQualityIssue)) == 1

    db.rollback()

    assert db.scalar(select(func.count()).select_from(FactDataQualityIssue)) == 0
    assert db.scalar(select(func.count()).select_from(FPurchaseLine)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == 0


def test_detector_accepts_seventy_thousand_raw_ids_without_parameter_expansion(db):
    batch = SysImportBatch(
        filename="70k.xlsx", file_type="purchase", file_hash="detector-70k",
        uploaded_by="大批导入员", status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.purchase_result(
            {"O-70K": f.purchase_head("O-70K", on=date(2026, 7, 16))},
            [f.purchase_line("O-70K", "L-REAL", "PN-REAL", qty="2", price="10")],
        ),
        batch.id,
        date(2026, 7, 16),
    )
    db.commit()

    ids = ["L-REAL", *(f"L-MISSING-{index}" for index in range(69_999))]
    result = detector.detect_imported_lines(
        db, file_type=mapping.PURCHASE, raw_line_ids=ids,
        detected_by="大批导入员",
    )

    assert result["scanned"] == 1
    assert result["matched"] == 0


def test_one_thousand_normal_lines_use_two_read_queries(db):
    batch = SysImportBatch(
        filename="1000.xlsx", file_type="purchase", file_hash="detector-1000",
        uploaded_by="批量导入员", status="success",
    )
    db.add(batch)
    db.flush()
    lines = [
        f.purchase_line("O-1000", f"L-{index}", f"PN-{index}", qty="2", price="10")
        for index in range(1000)
    ]
    loader.load(
        db,
        f.purchase_result(
            {"O-1000": f.purchase_head("O-1000", on=date(2026, 7, 16))}, lines,
        ),
        batch.id,
        date(2026, 7, 16),
    )
    db.commit()
    statements: list[str] = []

    def count_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        result = detector.detect_imported_lines(
            db, file_type=mapping.PURCHASE,
            raw_line_ids=[f"L-{index}" for index in range(1000)],
            detected_by="批量导入员",
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert result["scanned"] == 1000
    assert result["matched"] == 0
    assert len(statements) == 2, statements


def test_one_thousand_lines_with_ten_new_issues_use_declared_query_budget(db):
    batch = SysImportBatch(
        filename="1000-mixed.xlsx", file_type="purchase", file_hash="detector-1000-mixed",
        uploaded_by="批量导入员", status="success",
    )
    db.add(batch)
    db.flush()
    lines = []
    for index in range(1000):
        line = f.purchase_line("O-MIXED", f"LM-{index}", f"PNM-{index}",
                               qty="2", price="10")
        if index < 10:
            line["line_amount"] = Decimal("25")
        lines.append(line)
    loader.load(
        db,
        f.purchase_result(
            {"O-MIXED": f.purchase_head("O-MIXED", on=date(2026, 7, 16))}, lines,
        ),
        batch.id,
        date(2026, 7, 16),
    )
    db.commit()
    statements: list[str] = []

    def count_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        result = detector.detect_imported_lines(
            db, file_type=mapping.PURCHASE,
            raw_line_ids=[f"LM-{index}" for index in range(1000)],
            detected_by="批量导入员",
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert result["scanned"] == 1000
    assert result["matched"] == 10
    assert result["created"] == 10
    # 固定读预算 2 条（事实 + 已有疑点）；每条新写入最多 4 条
    # （advisory lock、并发复核、疑点 INSERT、审计 INSERT）。
    assert len(statements) == 2 + 4 * 10, statements


def test_second_issue_write_failure_rolls_back_the_whole_import_transaction(db):
    batch = SysImportBatch(
        filename="atomic-nth.xlsx", file_type="purchase", file_hash="atomic-nth",
        uploaded_by="原子导入员", status="processing",
    )
    db.add(batch)
    db.flush()
    lines = []
    for index in range(2):
        line = f.purchase_line("O-NTH", f"L-NTH-{index}", f"PN-NTH-{index}",
                               qty="2", price="10")
        line["line_amount"] = Decimal("25")
        lines.append(line)
    loader.load(
        db,
        f.purchase_result(
            {"O-NTH": f.purchase_head("O-NTH", on=date(2026, 7, 16))}, lines,
        ),
        batch.id,
        date(2026, 7, 16),
    )
    inserts = 0

    def fail_second_issue(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal inserts
        if statement.startswith("INSERT INTO fact_data_quality_issue"):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("simulated second issue write failure")

    event.listen(engine, "before_cursor_execute", fail_second_issue)
    try:
        with pytest.raises(RuntimeError, match="second issue"):
            detector.detect_imported_lines(
                db, file_type=mapping.PURCHASE,
                raw_line_ids=["L-NTH-0", "L-NTH-1"],
                detected_by="原子导入员",
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_issue)
        db.rollback()

    assert db.scalar(select(func.count()).select_from(FactDataQualityIssue)) == 0
    assert db.scalar(select(func.count()).select_from(FPurchaseLine)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportBatch)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == 0


def test_sales_import_uses_shared_tolerance_and_explicit_system_identity(db, tmp_path):
    boundary = _xlsx(
        tmp_path, side=mapping.SALES, filename="sales-boundary.xlsx",
        qty="2", unit_price="10", line_amount="20.05",
    )
    first = pipeline.run_import(db, boundary, "sales-boundary.xlsx")
    db.commit()
    assert first.report_json["data_quality_detection"]["matched"] == 0
    assert db.scalar(select(func.count()).select_from(FactDataQualityIssue)) == 0

    over = _xlsx(
        tmp_path, side=mapping.SALES, filename="sales-over.xlsx",
        qty="2", unit_price="10", line_amount="20.06",
    )
    second = pipeline.run_import(db, over, "sales-over.xlsx", mode="upsert")
    db.commit()
    issue = db.scalar(select(FactDataQualityIssue))
    assert second.report_json["data_quality_detection"]["matched"] == 1
    assert issue.side == "sales"
    assert issue.detected_by == detector.SYSTEM_DETECTOR
    assert issue.evidence["absolute_difference"] == "0.06"


def _seed_history_for_preview(db):
    batch = SysImportBatch(
        filename="历史回扫.xlsx", file_type="purchase", file_hash="history-preview",
        uploaded_by="历史导入员", status="success",
    )
    db.add(batch)
    db.flush()
    purchase_bad = f.purchase_line("P1", "PL-BAD", "PN-P-BAD", qty="2", price="10")
    purchase_bad["line_amount"] = Decimal("25")
    loader.load(
        db,
        f.purchase_result(
            {"P1": f.purchase_head("P1", on=date(2026, 1, 1))},
            [purchase_bad, f.purchase_line("P1", "PL-OK", "PN-P-OK", qty="2", price="10")],
        ),
        batch.id,
        date(2026, 7, 16),
    )
    sales_bad = f.sales_line("S1", "SL-BAD", "PN-S-BAD", qty="3", price="10")
    sales_bad["line_amount"] = Decimal("35")
    loader.load(
        db,
        f.sales_result(
            {"S1": f.sales_head("S1", on=date(2026, 2, 1))},
            [sales_bad, f.sales_line("S1", "SL-OK", "PN-S-OK", qty="3", price="10")],
        ),
        batch.id,
        date(2026, 7, 16),
    )
    db.commit()
    purchase_line = db.scalar(select(FPurchaseLine).where(
        FPurchaseLine.raw_line_id == "PL-BAD",
    ))
    detector.detect_imported_lines(
        db, file_type=mapping.PURCHASE, raw_line_ids=["PL-BAD"],
        detected_by="历史导入员",
    )
    db.commit()
    return purchase_line


def test_history_preview_is_deterministic_and_read_only_for_business_numbers(db):
    _seed_history_for_preview(db)
    before_issue_count = db.scalar(select(func.count()).select_from(FactDataQualityIssue))
    before_audit_count = db.scalar(select(func.count()).select_from(SysAuditLog))
    before_kpi = dashboard.kpi(
        db, date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 12, 31),
    )
    before_purchase = db.execute(select(
        func.count(FPurchaseLine.id), func.sum(FPurchaseLine.line_amount),
    )).one()
    before_sales = db.execute(select(
        func.count(FSalesLine.id), func.sum(FSalesLine.line_amount),
    )).one()

    first = detector.preview_history(db, side=None, sample_limit=10)
    second = detector.preview_history(db, side=None, sample_limit=10)

    assert first == second
    assert first["summary"] == {
        "scanned": 4,
        "matched": 2,
        "existing": 1,
        "would_create": 1,
        "would_refresh": 0,
        "existing_no_longer_matches": 0,
    }
    assert [(sample["side"], sample["raw_line_id"], sample["action"])
            for sample in first["samples"]] == [
        ("purchase", "PL-BAD", "unchanged"),
        ("sales", "SL-BAD", "would_create"),
    ]
    assert db.scalar(select(func.count()).select_from(FactDataQualityIssue)) == before_issue_count
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == before_audit_count
    assert dashboard.kpi(
        db, date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 12, 31),
    ) == before_kpi
    assert db.execute(select(
        func.count(FPurchaseLine.id), func.sum(FPurchaseLine.line_amount),
    )).one() == before_purchase
    assert db.execute(select(
        func.count(FSalesLine.id), func.sum(FSalesLine.line_amount),
    )).one() == before_sales


def _login(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"},
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['token']}"
    return client


def test_automatic_issue_keeps_existing_page_and_price_visibility_gates(db, tmp_path):
    path = _xlsx(
        tmp_path, side=mapping.PURCHASE, filename="masked.xlsx",
        qty="2", unit_price="999", line_amount="2500",
    )
    pipeline.run_import(db, path, "masked.xlsx", uploaded_by="刘朝红")
    db.add_all([
        SysUser(
            username="dq_masked", role="readonly", display_name="金额受限",
            password_hash=hash_password("pw123456"),
            permissions={"page_governance": True, "data_purchase_cost": False},
        ),
        SysUser(
            username="dq_no_page", role="readonly", display_name="无治理页",
            password_hash=hash_password("pw123456"),
        ),
    ])
    db.commit()
    issue = db.scalar(select(FactDataQualityIssue))

    masked = _login("dq_masked")
    listed = masked.get("/api/data-quality/issues")
    assert listed.status_code == 200
    assert "999" not in listed.text and "2500" not in listed.text
    detail = masked.get(f"/api/data-quality/issues/{issue.id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["evidence"] is None
    assert payload["evidence_restricted"] is True
    assert payload["fact"]["unit_price"] is None
    assert payload["fact"]["line_amount"] is None
    assert "source_fingerprint" not in payload
    assert "999" not in detail.text and "2500" not in detail.text

    no_page = _login("dq_no_page")
    assert no_page.get("/api/data-quality/issues").status_code == 403
