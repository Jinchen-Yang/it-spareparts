"""Independent post-green probes: real PG scope, effective rows and transaction seams.

Synthetic records are independent of implementation-side foundation fixtures.
Mutation probes replace in-memory boundaries only, never production source files.
"""
from datetime import UTC, date, datetime, timedelta
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import config, security
from app.agent import tools
from app.config import get_settings
from app.etl import loader
from app.models.maintenance import (
    FMaintenanceLine, FMaintenanceOrder, MaintenanceDemandDeleteIntent,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
from app.models.maintenance_project_operations import MaintenanceProjectOperationAudit
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysAccessLog, SysImportBatch, SysUser
from app.services import maintenance_assigned_lines as assigned
from app.services import query_filters
from app.services import maintenance_project_access as access
from app.services import maintenance_project_audit as audit
from app.services import maintenance_row_workbench as rows
from app.services.maintenance_row_filter_registry import parse_filters
from tests import factories


def context(user, *, scoped=False, salesperson=None, profit=False):
    return security.UserContext(user_id=user.username, role=user.role,
        salesperson_name=salesperson, is_authenticated=True,
        permissions={"page_maintenance": True, "own_maintenance_projects_only": scoped,
                     "data_profit": profit, "data_purchase_cost": profit})


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    monkeypatch.setattr(get_settings(), "maintenance_cutover_enabled", True)
    a, b = [MaintenanceProject(project_id=str(uuid4()), project_code=key,
                display_name="Identical visible name", lifecycle_status="ongoing",
                salesperson="canonical-A" if key == "A" else "canonical-B")
            for key in ("A", "B")]
    user = SysUser(username="independent-viewer", display_name="viewer", role="readonly",
                   password_hash="synthetic-never-used", is_active=True, permissions={})
    batch = SysImportBatch(filename="independent-access.xlsx", file_type="maintenance",
                           file_hash=uuid4().hex, status="success")
    db.add_all([a, b, user, batch])
    db.flush()
    keys = ("old", "new", "foreign", "void-header", "void-line", "tomb", "archived", "unassigned", "null-date")
    heads, lines = {}, []
    for key in keys:
        heads[key] = factories.maintenance_head(key, project="Identical visible name",
            on=date(2026, 1, 1) if key == "old" else date(2026, 1, 3))
        for i in range(2 if key == "new" else 1):
            line = factories.maintenance_line(key, f"{key}-{i}", f"PN-{key}-{i}")
            line["line_no"] = None
            lines.append(line)
    loader.load(db, factories.maintenance_result(heads, lines), batch.id, date(2026, 1, 5))
    orders = {o.raw_order_id: o for o in db.scalars(select(FMaintenanceOrder))}
    db_lines = {line.raw_line_id: line for line in db.scalars(select(FMaintenanceLine))}
    now = datetime.now(UTC)
    for key, order in orders.items():
        if key == "unassigned":
            continue
        db.add(MaintenanceSourceOrderAssignment(assignment_id=str(uuid4()),
            source_order_id=key, project_id=b.project_id if key == "foreign" else a.project_id,
            is_active=key != "archived", version=1, created_by="fixture",
            created_at=now - timedelta(days=1),
            archived_by="fixture" if key == "archived" else None,
            archived_at=now if key == "archived" else None))
    orders["void-header"].data_status = "已取消"
    orders["null-date"].order_date = None
    db_lines["void-line-0"].is_active = False
    assignment = MaintenanceProjectUserAssignment(assignment_id=str(uuid4()),
        project_id=a.project_id, responsibility_type="viewer", user_id=user.id,
        source_manager_text=user.username, version=1, assigned_by="fixture",
        assignment_reason="independent scope probe")
    intent = MaintenanceDemandDeleteIntent(intent_id=str(uuid4()), idempotency_key=uuid4().hex,
        request_digest="a" * 64, selection_digest="b" * 64, status="executed",
        reason="synthetic", operated_by="fixture", header_count=1, line_count=1,
        created_at=now, expires_at=now + timedelta(days=1), executed_at=now)
    db.add_all([assignment, intent])
    db.flush()
    db.add(MaintenanceDemandTombstone(source_order_id="tomb", delete_intent_id=intent.intent_id,
        version_digest="c" * 64, deleted_by="fixture", delete_reason="synthetic", deleted_at=now))
    db.commit()
    return a, b, user, assignment, db_lines


def raw_ids(db, stmt):
    return [row[0].raw_line_id for row in db.execute(stmt)]


def test_revocation_rechecks_viewer_and_account_without_caching(db, world):
    a, b, user, assignment, _ = world
    ctx = context(user, scoped=True)
    assert access.resolve_visible_project_ids(db, ctx) == {a.project_id}
    assert set(raw_ids(db, rows.query(db, ctx))) == {"old-0", "new-0", "new-1", "null-date-0"}
    assignment.archived_at = datetime.now(UTC)
    assignment.archived_by = "fixture"
    assignment.archive_reason = "revoked"
    assignment.version += 1
    db.commit()
    assert access.resolve_visible_project_ids(db, ctx) == set()
    assert raw_ids(db, rows.query(db, ctx)) == []
    with pytest.raises(HTTPException) as denied:
        rows.query(db, ctx, project_id=a.project_id, direct_project=True)
    assert denied.value.status_code == 403
    db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()),
        project_id=a.project_id, responsibility_type="viewer", user_id=user.id,
        source_manager_text=user.username, version=1, assigned_by="fixture",
        assignment_reason="new generation after revocation"))
    user.is_active = False
    db.commit()
    assert access.resolve_visible_project_ids(db, ctx) == set()
    with pytest.raises(HTTPException) as missing:
        access.enforce_maintenance_project_access(db, project_id=str(uuid4()), ctx=ctx)
    assert missing.value.status_code == 404


def test_canonical_sales_direct_scope_is_not_directory_scope(db, world):
    a, b, user, _, _ = world
    ctx = context(user, salesperson="canonical-B")
    assert access.resolve_visible_project_ids(db, ctx) == {a.project_id}
    assert raw_ids(db, rows.query(db, ctx, project_id=b.project_id)) == []
    assert raw_ids(db, rows.query(db, ctx, project_id=b.project_id, direct_project=True)) == ["foreign-0"]
    for role in ("admin", "boss"):
        ctx.role = role
        assert access.resolve_visible_project_ids(db, ctx) is None
        assert "foreign-0" in raw_ids(db, rows.query(db, ctx))
    ctx.role = "readonly"
    with pytest.raises(ValueError, match="requires project_id"):
        rows.query(db, ctx, direct_project=True)


def test_viewer_cannot_probe_amount_predicates_or_metadata(db, world):
    a, _, user, _, _ = world
    ctx = context(user)
    assert not rows.can_view_amounts(db, ctx, a.project_id)
    assert all(not field.amount for field in rows.visible_fields(db, ctx, a.project_id))
    amount = next(field.field for field in rows.FIELDS.values() if field.amount and field.type == "number")
    filters = parse_filters(json.dumps([{"field": amount, "op": "range", "min": 0, "max": 999999}]))
    with pytest.raises(HTTPException) as denied:
        rows.query(db, ctx, project_id=a.project_id, direct_project=True, filters=filters)
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "row_filter_amount_forbidden"


@pytest.mark.parametrize("cutover", [False, True])
def test_effective_set_scope_intersection_and_paging_on_readonly_pg(db, world, monkeypatch, cutover):
    a, b, user, _, line_map = world
    monkeypatch.setattr(get_settings(), "maintenance_cutover_enabled", cutover)
    # Read IDs before opening the read-only transaction (commit expires ORM attributes).
    aid, bid = a.project_id, b.project_id
    expected = {"old-0", "new-0", "new-1", "null-date-0"} | (set() if cutover else {"tomb-0"})
    db.rollback()
    db.execute(text("SET TRANSACTION READ ONLY"))
    stmt = assigned.assigned_lines_query(project_id=None, window=None, allowed_project_ids={aid})
    assert set(raw_ids(db, stmt)) == expected
    assert raw_ids(db, assigned.assigned_lines_query(project_id=aid, window=None, allowed_project_ids={bid})) == []
    assert raw_ids(db, assigned.assigned_lines_query(project_id=None, window=None, allowed_project_ids=set())) == []
    full = raw_ids(db, stmt)
    pages = sum((raw_ids(db, stmt.limit(1).offset(i)) for i in range(len(full))), [])
    assert pages == full and len(set(pages)) == len(full)
    ascending = assigned.assigned_lines_query(project_id=aid, window=None, export_order=True)
    assert raw_ids(db, ascending)[0] == "old-0"
    assert raw_ids(db, ascending)[-1] == "null-date-0"
    exact = assigned.assigned_lines_query(project_id=aid, window=(date(2026, 1, 3), date(2026, 1, 3)))
    assert set(raw_ids(db, exact)) == {"new-0", "new-1"} | (set() if cutover else {"tomb-0"})
    # Verify the DB restriction itself, not only an empty ORM dirty set.
    with pytest.raises(DBAPIError) as rejected:
        db.execute(text("UPDATE maintenance_project SET display_name = display_name"))
    assert rejected.value.orig.sqlstate == "25006"
    db.rollback()


def append_audit(db, project_id):
    audit.write_project_audit(db, project_id=project_id, entity_type="synthetic",
        entity_id=17, action="probe", operated_by="independent-reader", reason="",
        before={"value": 1}, after={"value": 2})


def test_audit_defers_flush_and_rolls_back_with_caller_savepoint(db, world):
    a, _, _, _, _ = world
    aid = a.project_id
    original = a.display_name
    with Session(db.get_bind()) as observer:
        with db.begin_nested() as savepoint:
            a.display_name = "not-committed"
            append_audit(db, aid)
            assert len(db.new) == 1
            pending = next(iter(db.new))
            assert pending.reason == "工作簿编辑" and pending.entity_id == "17"
            assert observer.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 0
            db.flush()
            assert observer.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 0
            savepoint.rollback()
        db.commit()
        assert observer.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 0
        assert observer.scalar(select(MaintenanceProject.display_name).where(MaintenanceProject.project_id == aid)) == original
    append_audit(db, aid)
    db.commit()
    with Session(db.get_bind()) as observer:
        stored = observer.scalar(select(MaintenanceProjectOperationAudit))
        assert stored.before_json == {"value": 1} and stored.after_json == {"value": 2}


def test_access_log_survives_business_rollback_but_project_audit_does_not(db, world, monkeypatch):
    a, _, user, _, _ = world
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", True)
    ctx = context(user)
    aid = a.project_id
    a.display_name = "uncommitted-business"
    append_audit(db, aid)
    db.flush()
    result = tools.dispatch(db, "independent_unknown_read", {}, ctx)
    assert result == {"error": "未知工具: independent_unknown_read"}
    db.rollback()
    with Session(db.get_bind()) as observer:
        assert observer.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)) == 0
        logs = observer.scalars(select(SysAccessLog).where(SysAccessLog.username == user.username)).all()
        assert [log.action for log in logs] == ["agent_tool:independent_unknown_read"]
        assert observer.scalar(select(MaintenanceProject.display_name).where(MaintenanceProject.project_id == aid)) == "Identical visible name"


@pytest.mark.parametrize("mutation", ["empty_scope_to_all", "ignore_header_status", "audit_eager_flush"])
def test_independent_oracles_kill_three_in_memory_mutations(db, world, monkeypatch, mutation):
    a, _, _, _, _ = world
    if mutation == "empty_scope_to_all":
        def oracle():
            assert raw_ids(db, rows.query(db, security.UserContext(user_id="nobody",
                role="readonly", is_authenticated=True, permissions={}))) == []
        oracle()
        monkeypatch.setattr(rows, "resolve_visible_project_ids", lambda *args: None)
    elif mutation == "ignore_header_status":
        def oracle():
            stmt = assigned.assigned_lines_query(project_id=a.project_id, window=None)
            assert "void-header-0" not in raw_ids(db, stmt)
        oracle()
        monkeypatch.setattr(query_filters, "active_orders", lambda stmt, model: stmt)
    else:
        real = audit.write_project_audit
        def oracle():
            append_audit(db, a.project_id)
            assert len(db.new) == 1
            db.rollback()
        oracle()
        def eager(*args, **kwargs):
            real(*args, **kwargs)
            args[0].flush()
        monkeypatch.setattr(audit, "write_project_audit", eager)
    with pytest.raises(AssertionError):
        oracle()
    db.rollback()
