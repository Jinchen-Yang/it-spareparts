"""真实 PostgreSQL 下的跨项目搜索契约。"""
from datetime import UTC, datetime
from uuid import uuid4
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text

from app import config
from app.agent import tools
from app.api import maintenance_movement_search as api
from app.auth import current_role
from app.db import get_db
from app.models.maintenance import MaintenanceDemandDeleteIntent, MaintenanceDemandTombstone
from app.models.maintenance_bad_return import MaintenanceBadReturn, MaintenanceBadReturnLine
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_project_operations import MaintenanceSiteIssue
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.security import UserContext, get_current_user_context
from app.services.maintenance_movement_search import MovementSearchQuery, movement_statements, search_movements
from tests.test_maintenance_analytics import _project, _part
from tests.test_maintenance_return_metrics import _issue, _receipt
from tests.test_maintenance_return_receipts_api import _wbdd


@pytest.fixture()
def facts(db, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    db.add(SysUser(username="movement-admin", role="admin", password_hash="synthetic"))
    projects = [_project(db, f"MOVE-{i}") for i in range(2)]
    part = _part(db, "AB-123")
    records = []
    for project in projects:
        issue = _issue(db, project, part, "2")
        issue.serial_number, issue.remark = "SN-123", "现场 更换说明"
        receipt = _receipt(db, project, part, "1")
        receipt.serial_numbers = ["SN-123", "SECOND-SN"]
        receipt.note, receipt.evidence_ref = "收到 返还说明", "EV-123"
        receipt.source_payload = {"source_metadata": {"sn": "SOURCE-SN-123"}, "secret": "NEVER-OUTPUT"}
        order = _wbdd(db, project=project, order_no=f"WBDD-{uuid4()}")
        order.express_no, order.express_no2, order.receiver = "TRACK-123", "TRACK-SECOND", "NEVER-OUTPUT"
        bad = MaintenanceBadReturn(return_id=str(uuid4()), return_no=f"HJFH-{uuid4().hex[:20]}",
            project_id=project.project_id, status="in_transit", created_by="movement-admin",
            submitted_at=datetime.now(UTC), in_transit_at=datetime.now(UTC),
            logistics_reference="BAD-TRACK", note="坏件 物流备注")
        db.add(bad)
        records.append((issue, receipt, order, bad))
    db.commit()
    return projects, records, UserContext(user_id="movement-admin", role="admin", is_authenticated=True)


def search(db, ctx, q, **kwargs):
    return search_movements(db, MovementSearchQuery(q=q, **kwargs), ctx)


@pytest.mark.parametrize("entity,field,q,matched", [
    ("issue", "pn", "ab-123", "pn"), ("issue", "sn", "SN-123", "serial_number"),
    ("issue", "remark", "更换说明", "remark"), ("receipt", "pn", "AB123", "pn"),
    ("receipt", "sn", "SECOND-SN", "serial_numbers"),
    ("receipt", "sn", "SOURCE-SN-123", "source_serial_number"),
    ("receipt", "remark", "返还说明", "note"), ("receipt", "evidence", "EV-123", "evidence_ref"),
    ("demand_order", "evidence", "TRACK-123", "express_no"),
    ("demand_order", "evidence", "TRACK-SECOND", "express_no2"),
    ("bad_return", "evidence", "BAD-TRACK", "logistics_reference"),
    ("bad_return", "remark", "物流备注", "note"),
    ("bad_return", "evidence", "物流备注", "note"),
])
def test_each_field_in_each_project(db, facts, entity, field, q, matched):
    projects, _, ctx = facts
    result = search(db, ctx, q, entity=entity, field=field)
    assert {r["project"]["id"] for r in result["items"]} == {p.project_id for p in projects}
    assert len(result["items"]) == 2
    assert all(matched in row["matched_fields"] for row in result["items"])
    assert not result["has_more"]
    for forbidden in ("unit_cost", "cost_amount", "source_payload", "receiver", "NEVER-OUTPUT"):
        assert forbidden not in json.dumps(result)
    if entity == "receipt":
        assert all(r["demand_ref"] is None for r in result["items"])
    if entity == "demand_order":
        assert all(r["record_ref"]["tab"] == "parts-orders" for r in result["items"])


def test_normalization_exact_arrays_and_literal_safety(db, facts):
    _, records, ctx = facts
    records[0][0].remark = "  ＡＢＣ\t\n　测试  "
    db.commit()
    assert len(search(db, ctx, "abc 测试", field="remark", match_mode="exact")["items"]) == 1
    assert len(search(db, ctx, "  ａｂ－１２３  ", field="pn")["items"]) == 4
    assert search(db, ctx, "SN123", field="sn", match_mode="exact")["items"] == []
    assert len(search(db, ctx, "SECOND-SN", field="sn", match_mode="exact")["items"]) == 2
    for token in ("%", "_", "\\", "*?", "' OR 1=1 --", "A%", "A_"):
        records[0][1].evidence_ref = f"prefix{token}suffix"
        db.commit()
        result = search(db, ctx, token, field="evidence", entity="receipt")
        assert len(result["items"]) == (0 if len(token) < 3 else 1)
        result = search(db, ctx, f"prefix{token}", field="evidence", entity="receipt")
        assert [r["record_id"] for r in result["items"]] == [records[0][1].rkd_line_id]
    records[0][1].evidence_ref = "%"
    db.commit()
    assert len(search(db, ctx, "%", field="evidence")["items"]) == 1


def test_short_words_and_validation(db, facts):
    assert search(db, facts[2], "AB", field="pn")["items"] == []
    for args in ({"q": " "}, {"q": "x", "field": "remark"}, {"q": "x" * 257},
                 {"q": "x", "field": "sql"}, {"q": "x", "page_size": 101},
                 {"q": "x", "statuses": ["void"], "entity": "receipt"}, {"q": "x", "user_id": "admin"}):
        with pytest.raises(ValidationError):
            MovementSearchQuery(**args)


def test_status_source_scope_and_immediate_edits(db, facts):
    projects, records, ctx = facts
    line = records[0][0]
    head = db.get(MaintenanceSiteIssue, line.issue_id)
    head.normalized_status = "draft"
    records[0][1].line_status, records[0][1].voided_at, records[0][1].voided_by = "voided", datetime.now(UTC), "synthetic"
    projects[1].is_active = False
    db.commit()
    assert search(db, ctx, "AB-123")["items"] == []
    historical = search(db, ctx, "AB-123", entity="issue", statuses=["draft"])
    assert len(historical["items"]) == 1 and historical["items"][0]["document"]["status"] == "draft"
    assert len(search(db, ctx, "AB-123", entity="receipt", statuses=["voided"])["items"]) == 1
    head.source = "direct_api"
    db.commit()
    assert search(db, ctx, "AB-123", entity="issue", statuses=["draft"])["items"] == []
    head.source, head.normalized_status, line.is_active = "page_manual", "confirmed", False
    db.commit()
    assert search(db, ctx, "AB-123", entity="issue")["items"] == []
    line.is_active, line.pn = True, "NEW-PN"
    db.commit()
    assert len(search(db, ctx, "NEW-PN")["items"]) == 1


@pytest.mark.parametrize("role,scoped,attached,hidden,expected", [
    ("admin", False, None, False, 2), ("boss", False, None, False, 2),
    ("sales", True, None, False, 1), ("maintenance_manager", False, "primary_manager", False, 1),
    ("purchaser", False, "viewer", False, 1), ("sales", False, None, False, 0),
    ("maintenance_manager", False, None, False, 0), ("sales", True, None, True, 1),
])
def test_permission_matrix(db, facts, role, scoped, attached, hidden, expected):
    projects, _, _ = facts
    perms = {"page_maintenance": True, "data_profit": not hidden, "data_purchase_cost": not hidden, "own_maintenance_projects_only": scoped}
    user = SysUser(username=f"matrix-{role}-{uuid4().hex[:8]}", role=role, password_hash="synthetic", permissions=perms)
    db.add(user)
    db.flush()
    projects[0].salesperson = "本人销售"
    if attached:
        db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()), project_id=projects[0].project_id,
            responsibility_type=attached, user_id=user.id, assigned_by="synthetic", assignment_reason="合成权限测试"))
    db.commit()
    ctx = UserContext(user_id=user.username, role=role, salesperson_name="本人销售", permissions=perms, is_authenticated=True)
    for q, entity in (("TRACK-123", "demand_order"), ("EV-123", "receipt"), ("BAD-TRACK", "bad_return"), ("SN-123", "issue")):
        result = search(db, ctx, q, entity=entity)
        assert len(result["items"]) == (0 if hidden and entity == "issue" else expected)
        if expected < 2:
            assert projects[1].project_id not in json.dumps(result)
        if hidden:
            assert result["unavailable_entities"] == ["issue"]
    assert search(db, ctx, "AB-123", project_ids=[])["items"] == []
    if expected < 2:
        assert search(db, ctx, "AB-123", project_ids=[projects[1].project_id])["items"] == []
    user.is_active = False
    db.commit()
    assert search(db, ctx, "AB-123")["items"] == []


def _http(db, ctx):
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_context] = lambda: ctx
    app.dependency_overrides[current_role] = lambda: ctx.role
    return TestClient(app)


def test_http_tool_equivalence_page_gate_and_limits(db, facts):
    ctx = facts[2]
    args = {"q": "AB-123", "page_size": 2}
    response = _http(db, ctx).post("/api/maintenance/movements/search", json=args)
    assert response.status_code == 200, response.text
    actual = response.json()
    tool = tools.dispatch(db, "search_maintenance_movements", args, ctx)
    actual.pop("as_of"); tool.pop("as_of")
    assert actual == tool
    assert tools.dispatch(db, "search_maintenance_movements", {"q": "AB-123", "page_size": 100}, ctx)["page_size"] == 50
    denied = UserContext(user_id="none", role="sales", permissions={"page_maintenance": False}, is_authenticated=True)
    assert _http(db, denied).post("/api/maintenance/movements/search", json=args).status_code == 403
    assert "error" in tools.dispatch(db, "search_maintenance_movements", args, denied)
    assert _http(db, ctx).post("/api/maintenance/movements/search", json={"q": " "}).status_code == 422
    assert "error" in tools.dispatch(db, "search_maintenance_movements", {"q": " "}, ctx)
    assert search(db, UserContext(user_id=None, role="admin"), "AB-123")["items"] == []


def test_stable_pagination_dedup_and_scope_recheck(db, facts):
    projects, records, ctx = facts
    records[0][1].note = "AB-123"
    db.commit()
    all_items = search(db, ctx, "AB-123")["items"]
    pages = [search(db, ctx, "AB-123", page=i, page_size=1) for i in range(1, 6)]
    assert [p["items"][0] for p in pages[:4]] == all_items
    assert not pages[3]["has_more"] and pages[4]["items"] == []
    assert len({(r["entity_type"], r["record_id"]) for r in all_items}) == 4
    row = next(r for r in all_items if r["record_id"] == records[0][1].rkd_line_id)
    assert set(row["matched_fields"]) == {"pn", "note"}
    projects[0].is_active = False
    db.commit()
    assert all(r["project"]["id"] == projects[1].project_id for r in search(db, ctx, "AB-123")["items"])


def test_demand_reassignment_unassigned_and_tombstones(db, facts):
    projects, records, ctx = facts
    issue, receipt, order, _ = records[0]
    issue.source_order_id = receipt.source_order_id = order.raw_order_id
    db.commit()
    assert all(r["demand_ref"]["number"] == order.order_no for r in search(db, ctx, "AB-123", project_ids=[projects[0].project_id])["items"])
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id))
    assignment.is_active, assignment.archived_at, assignment.archived_by = False, datetime.now(UTC), "synthetic"
    assignment.version += 1
    db.flush()
    assignment = MaintenanceSourceOrderAssignment(assignment_id=str(uuid4()), source_order_id=order.raw_order_id,
        project_id=projects[1].project_id, created_by="synthetic")
    db.add(assignment)
    db.commit()
    assert all(r["demand_ref"] is None for r in search(db, ctx, "AB-123", project_ids=[projects[0].project_id])["items"])
    assignment.is_active, assignment.archived_at, assignment.archived_by = False, datetime.now(UTC), "synthetic"
    assignment.version += 1
    intent = MaintenanceDemandDeleteIntent(intent_id=str(uuid4()), idempotency_key=str(uuid4()),
        request_digest="x" * 64, selection_digest="y" * 64, status="executed", reason="合成作废",
        operated_by="synthetic", header_count=1, line_count=0, created_at=datetime.now(UTC), expires_at=datetime.now(UTC))
    db.add(intent)
    db.flush()
    db.add(MaintenanceDemandTombstone(source_order_id=records[1][2].raw_order_id, delete_intent_id=intent.intent_id, version_digest="synthetic",
        deleted_by="synthetic", delete_reason="合成作废", deleted_at=datetime.now(UTC)))
    db.commit()
    assert search(db, ctx, "TRACK-123", entity="demand_order")["items"] == []


def test_bad_return_line_pn_no_duplicate_document(db, facts):
    from tests.test_maintenance_bad_returns_api import _client, _one_required_obligation
    client = _client(db, username="movement-bad-admin")
    _, obligation = _one_required_obligation(db, client, project_id="movement-bad-project", suffix="movement")
    header = MaintenanceBadReturn(return_id=str(uuid4()), return_no="HJFH-MOVEMENT", project_id=obligation.project_id,
        status="in_transit", created_by="synthetic", submitted_at=datetime.now(UTC),
        in_transit_at=datetime.now(UTC), logistics_reference=obligation.pn, note=obligation.pn)
    db.add(header); db.flush()
    db.add(MaintenanceBadReturnLine(return_line_id=str(uuid4()), return_id=header.return_id,
        line_no=1, obligation_id=obligation.obligation_id, part_id=obligation.part_id, pn=obligation.pn, quantity=1))
    db.commit()
    result = search(db, facts[2], obligation.pn, entity="bad_return")
    assert len(result["items"]) == 1
    row = result["items"][0]
    assert row["project"]["id"] == obligation.project_id
    assert set(row["matched_fields"]) == {"pn", "logistics_reference", "note"}
    assert row["quantity"] == "1.000"
    evidence = search(db, facts[2], obligation.pn, entity="bad_return", field="evidence")["items"][0]
    assert set(evidence["matched_fields"]) == {"pn", "logistics_reference", "note"}


def test_explain_small_fixture(db, facts):
    for entity in ("issue", "receipt", "demand_order", "bad_return"):
        query = MovementSearchQuery(q="AB-123", entity=entity)
        statement = movement_statements(query, [p.project_id for p in facts[0]])[0]
        compiled = statement.compile(db.bind, compile_kwargs={"literal_binds": True})
        plan = db.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + str(compiled))).scalars().all()
        print("\n" + entity + "\n" + "\n".join(plan))
        assert any("Execution Time" in line for line in plan)


def test_auto_tracking_issue_numbers_and_real_import_sn(db, facts):
    projects, records, ctx = facts
    for field, q in (("express_no", "TRACK-123"), ("express_no2", "TRACK-SECOND"), ("logistics_reference", "BAD-TRACK")):
        result = search(db, ctx, q)
        assert len(result["items"]) == 2
        assert all(field in row["matched_fields"] for row in result["items"])
    head = db.get(MaintenanceSiteIssue, records[0][0].issue_id)
    head.display_no = "LY-20261027-0001"
    db.commit()
    for q, field in ((head.issue_no, "issue_no"), (head.display_no, "display_no")):
        assert search(db, ctx, q, entity="issue")["items"][0]["matched_fields"] == [field]
    imported = _receipt(db, projects[0], None, "1", pn="IMPORTED-PN", source="rkd_import")
    imported.source_payload = {"source_metadata": {"sn": "REAL-IMPORT-SN"}, "secret": "NEVER-OUTPUT"}
    db.commit()
    row = search(db, ctx, "REAL-IMPORT-SN", field="sn")["items"][0]
    assert row["serials"] == [] and row["source_serial_number"] == "REAL-IMPORT-SN"
    imported.source_payload = {"source_metadata": {"sn": {"secret": "NO-RAW-JSON"}}}
    db.commit()
    assert search(db, ctx, "NO-RAW-JSON")["items"] == []


def test_bad_return_default_draft_and_void_exclusion(db, facts):
    records, ctx = facts[1], facts[2]
    draft, void = records[0][3], records[1][3]
    draft.status, draft.submitted_at, draft.in_transit_at, draft.logistics_reference = "draft", None, None, None
    void.status, void.voided_at = "void", datetime.now(UTC)
    db.commit()
    assert search(db, ctx, "物流备注", entity="bad_return")["items"] == []
    assert len(search(db, ctx, "物流备注", entity="bad_return", statuses=["draft", "void"])["items"]) == 2


def test_query_busy_timeout_and_excerpt_are_explicit(db, facts, monkeypatch):
    from fastapi import HTTPException
    from app.services import maintenance_movement_search as service
    ctx, records = facts[2], facts[1]
    records[0][1].note = "长备注" * 150
    db.commit()
    row = search(db, ctx, "长备注", entity="receipt")["items"][0]
    assert row["truncated"] is True and len(row["excerpt"]) == 240
    for _ in range(4):
        assert service._SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as busy:
            search(db, ctx, "AB-123")
        assert busy.value.status_code == 429
    finally:
        for _ in range(4):
            service._SLOTS.release()
    previous = db.scalar(text("SELECT current_setting('statement_timeout')"))
    def timeout(db, query, ctx):
        db.execute(text("SELECT set_config('statement_timeout', '1', true)"))
        db.execute(text("SELECT pg_sleep(0.02)"))
    monkeypatch.setattr(service, "_search", timeout)
    with pytest.raises(HTTPException) as failure:
        search(db, ctx, "AB-123")
    assert failure.value.status_code == 503
    assert db.scalar(text("SELECT current_setting('statement_timeout')")) == previous


def test_local_benchmark_reproducible_small_scale(db, facts):
    from math import ceil
    from time import perf_counter
    ctx = facts[2]
    for label, args in (
        ("full_scope", {"q": "AB-123"}),
        ("no_match", {"q": "NO-SUCH-MOVEMENT"}),
        ("short_remark", {"q": "现场", "field": "remark"}),
        ("one_project", {"q": "AB-123", "project_ids": [facts[0][0].project_id]}),
        ("large_page", {"q": "123", "page_size": 100}),
    ):
        query = MovementSearchQuery(**args)
        samples = []
        for _ in range(20):
            start = perf_counter()
            result = search_movements(db, query, ctx)
            samples.append((perf_counter() - start) * 1000)
        p95 = sorted(samples)[ceil(0.95 * len(samples)) - 1]
        print(f"BENCH {label}: n=20, projects=2, issue_lines=2, receipts=2, demands=2, bad_returns=2, rows={len(result['items'])}, p95_ms={p95:.3f}, min_ms={min(samples):.3f}, max_ms={max(samples):.3f}")
        assert len(samples) == 20
