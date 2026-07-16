"""DEV-13A：维保需求号未匹配归因（只读、互斥、脱敏）。"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

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
from app.services.maintenance_match_keys import exact_match_key
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
        "M-BLANK-EXACT": f.maintenance_head(
            "M-BLANK-EXACT", order_no="WBDD-BLANK-EXACT", on=date(2026, 1, 2)
        ),
        "M-FORMAT": f.maintenance_head("M-FORMAT", order_no="WBDD-20260101-0001", on=date(2026, 1, 2)),
        "M-DUP": f.maintenance_head("M-DUP", order_no="WBDD-20260101-0002", on=date(2026, 1, 2)),
        "M-PN": f.maintenance_head("M-PN", order_no="WBDD-20260101-0003", on=date(2026, 1, 2)),
        "M-MISSING": f.maintenance_head("M-MISSING", order_no="WBDD-20260101-0004", on=date(2026, 1, 2)),
        "M-OTHER": f.maintenance_head("M-OTHER", order_no="WBDD-20260101-0005", on=date(2026, 1, 2)),
    }
    maintenance_lines = [
        f.maintenance_line("M-EXACT", "ML-EXACT", "PN-EXACT"),
        f.maintenance_line("M-EMPTY", "ML-EMPTY", "PN-EMPTY"),
        f.maintenance_line("M-BLANK-EXACT", "ML-BLANK-EXACT", "PN-BLANK-EXACT"),
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
        # 现行 A0 把双方纯空白键视为精确命中；本诊断只复刻母集，不在此修正式成本语义。
        "P-BLANK": f.purchase_head("P-BLANK", linked_maintenance_order_no="   "),
        "P-FORMAT": f.purchase_head("P-FORMAT", linked_maintenance_order_no="WBDD 20260101 0001"),
        "P-DUP-A": f.purchase_head("P-DUP-A", linked_maintenance_order_no="WBDD 20260101 0002"),
        "P-DUP-B": f.purchase_head("P-DUP-B", linked_maintenance_order_no="WBDD/20260101/0002"),
        "P-PN": f.purchase_head("P-PN", linked_maintenance_order_no="WBDD-20260101-0003"),
        # 同需求号同 PN，但 qty=0，不是现行直配池的合格候选 → other。
        "P-OTHER": f.purchase_head("P-OTHER", linked_maintenance_order_no="WBDD-20260101-0005"),
    }
    purchase_lines = [
        f.purchase_line("P-EXACT", "PL-EXACT", "PN-EXACT"),
        f.purchase_line("P-BLANK", "PL-BLANK", "PN-BLANK-EXACT"),
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
    blank_exact = db.scalar(
        select(FMaintenanceOrder).where(FMaintenanceOrder.raw_order_id == "M-BLANK-EXACT")
    )
    blank_exact.order_no = "   "
    db.commit()


def _by_key(report: dict) -> dict[str, dict]:
    return {row["code"]: row for row in report["buckets"]}


EXPECTED_BUCKET_ORDER = [
    "empty_request_no",
    "normalizable_format",
    "duplicate_candidates",
    "other",
    "request_exists_pn_diff",
    "purchase_missing_request_no",
]


def _assert_all_buckets_are_zero(report: dict) -> None:
    assert [row["code"] for row in report["buckets"]] == EXPECTED_BUCKET_ORDER
    assert [row["line_count"] for row in report["buckets"]] == [0] * 6
    assert [row["share_of_unmatched"] for row in report["buckets"]] == [0.0] * 6
    assert all(row["samples"] == [] for row in report["buckets"])
    # 标准 JSON 不允许 NaN/Infinity；空分母必须产出显式 0，而不是非标准浮点值。
    assert "NaN" not in json.dumps(report, ensure_ascii=False, allow_nan=False)


def test_empty_and_all_exact_mother_sets_have_stable_zero_denominators(db, batch):
    empty = maintenance_match_audit.build_report(db, sample_limit=5)
    assert empty["scope"] == {
        "definition": "active_maintenance_since_cost_start",
        "maintenance_start_date": "2024-01-01",
        "total_line_count": 0,
        "exact_matched_line_count": 0,
        "unmatched_line_count": 0,
        "exact_match_rate": 0.0,
    }
    assert empty["repairable"]["line_count"] == 0
    assert empty["repairable"]["rate_of_unmatched"] == 0.0
    assert empty["invariant"] == {"bucket_sum": 0, "equals_unmatched": True}
    _assert_all_buckets_are_zero(empty)

    loader.load(db, f.maintenance_result(
        {"M-ALL-EXACT": f.maintenance_head(
            "M-ALL-EXACT", order_no="WBDD-ALL-EXACT", on=date(2026, 1, 2),
        )},
        [f.maintenance_line("M-ALL-EXACT", "ML-ALL-EXACT", "PN-ALL-EXACT")],
    ), batch.id, date(2026, 7, 16))
    loader.load(db, f.purchase_result(
        {"P-ALL-EXACT": f.purchase_head(
            "P-ALL-EXACT", linked_maintenance_order_no=" wbdd-all-exact ",
        )},
        [f.purchase_line("P-ALL-EXACT", "PL-ALL-EXACT", "PN-ALL-EXACT")],
    ), batch.id, date(2026, 7, 16))
    db.commit()

    all_exact = maintenance_match_audit.build_report(db, sample_limit=5)
    assert all_exact["scope"]["total_line_count"] == 1
    assert all_exact["scope"]["exact_matched_line_count"] == 1
    assert all_exact["scope"]["unmatched_line_count"] == 0
    assert all_exact["scope"]["exact_match_rate"] == 1.0
    assert all_exact["repairable"]["line_count"] == 0
    assert all_exact["repairable"]["rate_of_unmatched"] == 0.0
    assert all_exact["invariant"] == {"bucket_sum": 0, "equals_unmatched": True}
    _assert_all_buckets_are_zero(all_exact)


def test_six_buckets_are_exhaustive_mutually_exclusive_and_exact_hit_is_excluded(db, batch):
    _load_fixture(db, batch)

    report = maintenance_match_audit.build_report(db, sample_limit=5)

    assert report["scope"]["total_line_count"] == 8
    assert report["scope"]["exact_matched_line_count"] == 2
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
    seeded = db.scalar(select(FMaintenanceLine).where(
        FMaintenanceLine.raw_line_id == "ML-FORMAT",
    ))
    seeded.unit_cost = Decimal("12.34")
    seeded.cost_amount = Decimal("24.68")
    seeded.cost_source = "direct"
    seeded.linked_purchase_order_no = "P-COST-SNAPSHOT"
    seeded.cost_tax_basis = "ex"
    seeded.price_month = "2026-01"
    seeded.trace_months = 2
    seeded.price_distance_days = 3
    seeded.confidence = "high"
    seeded.anomaly_flags = ["snapshot-marker"]
    db.commit()
    cost_before = list(db.execute(select(
        FMaintenanceLine.id, FMaintenanceLine.unit_cost, FMaintenanceLine.cost_amount,
        FMaintenanceLine.cost_source, FMaintenanceLine.linked_purchase_order_no,
        FMaintenanceLine.cost_tax_basis, FMaintenanceLine.price_month,
        FMaintenanceLine.trace_months, FMaintenanceLine.price_distance_days,
        FMaintenanceLine.confidence, FMaintenanceLine.anomaly_flags,
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
    assert first_query_count == 3
    assert selects == 6
    payload = json.dumps(first, ensure_ascii=False)
    all_raw_values = {
        value.strip()
        for value in db.scalars(select(FMaintenanceOrder.order_no)).all()
        + db.scalars(select(FMaintenanceLine.pn_std)).all()
        + db.scalars(select(FPurchaseOrder.linked_maintenance_order_no)).all()
        + db.scalars(select(FPurchaseLine.pn_std)).all()
        if value and value.strip()
    }
    for raw in all_raw_values | {"测试供应商", "测试客户"}:
        assert raw not in payload
    assert all(sample["sample_ref"].startswith("MA-")
               for bucket in first["buckets"] for sample in bucket["samples"])

    cost_after = list(db.execute(select(
        FMaintenanceLine.id, FMaintenanceLine.unit_cost, FMaintenanceLine.cost_amount,
        FMaintenanceLine.cost_source, FMaintenanceLine.linked_purchase_order_no,
        FMaintenanceLine.cost_tax_basis, FMaintenanceLine.price_month,
        FMaintenanceLine.trace_months, FMaintenanceLine.price_distance_days,
        FMaintenanceLine.confidence, FMaintenanceLine.anomaly_flags,
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


def test_sample_limit_zero_never_constructs_sample_dicts(db, batch, monkeypatch):
    _load_fixture(db, batch)
    selects = 0

    def forbidden(*_args, **_kwargs):
        raise AssertionError("sample_limit=0 不得构造样例字典、候选预览或执行预览查询")

    def count_select(_conn, _cursor, statement, _params, _ctx, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    monkeypatch.setattr(maintenance_match_audit, "_sample", forbidden)
    monkeypatch.setattr(maintenance_match_audit, "_CandidatePreview", forbidden)
    event.listen(db.bind, "before_cursor_execute", count_select)
    try:
        report = maintenance_match_audit.build_report(db, sample_limit=0)
    finally:
        event.remove(db.bind, "before_cursor_execute", count_select)

    assert report["scope"]["unmatched_line_count"] == 6
    assert report["invariant"]["equals_unmatched"] is True
    assert all(bucket["samples"] == [] for bucket in report["buckets"])
    assert selects == 2


def test_shared_exact_key_preserves_cost_engine_edge_semantics():
    assert exact_match_key(None) is None
    assert exact_match_key("") is None
    assert exact_match_key("   ") == ""
    assert exact_match_key(" wbdd-AbC-1 ") == "WBDD-ABC-1"


def test_blank_cross_case_follows_existing_a0_mother_set(db, batch):
    _load_fixture(db, batch)
    report = maintenance_match_audit.build_report(db, sample_limit=10)
    blank_exact_id = db.scalar(
        select(FMaintenanceLine.id).where(FMaintenanceLine.raw_line_id == "ML-BLANK-EXACT")
    )
    blank_unmatched_id = db.scalar(
        select(FMaintenanceLine.id).where(FMaintenanceLine.raw_line_id == "ML-EMPTY")
    )
    returned_refs = {
        sample["sample_ref"]
        for bucket in report["buckets"]
        for sample in bucket["samples"]
    }

    # 双方空白+同 part 沿用现行 A0 命中，不进入未匹配；无采购命中的空白号才进 empty。
    assert maintenance_match_audit._sample_ref(blank_exact_id) not in returned_refs
    assert maintenance_match_audit._sample_ref(blank_unmatched_id) in returned_refs
    assert _by_key(report)["empty_request_no"]["line_count"] == 1


def test_mask_policy_and_response_field_whitelist(db, batch):
    assert maintenance_match_audit._masked("ABCD") == "****"
    assert maintenance_match_audit._masked("ABCDE") == "A***E"
    assert maintenance_match_audit._masked("ABCDEFGH") == "A******H"
    assert maintenance_match_audit._masked("ABCDEFGHI") == "AB*****HI"

    _load_fixture(db, batch)
    report = maintenance_match_audit.build_report(db, sample_limit=10)
    assert set(report) == {"restricted", "as_of", "scope", "repairable", "buckets", "invariant"}
    assert set(report["scope"]) == {
        "definition", "maintenance_start_date", "total_line_count",
        "exact_matched_line_count", "unmatched_line_count", "exact_match_rate",
    }
    assert set(report["repairable"]) == {"line_count", "rate_of_unmatched", "meaning"}
    assert set(report["invariant"]) == {"bucket_sum", "equals_unmatched"}
    for bucket in report["buckets"]:
        assert set(bucket) == {
            "code", "label", "line_count", "share_of_unmatched", "repairable", "samples",
        }
        for sample in bucket["samples"]:
            assert set(sample) == {
                "sample_ref", "maintenance_request_no", "maintenance_pn",
                "candidate_order_count", "candidates", "reason",
            }
            assert all(set(candidate) == {"request_no", "pn"}
                       for candidate in sample["candidates"])


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
