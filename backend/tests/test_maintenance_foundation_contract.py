"""Public foundation contracts through real dispatch/runtime and isolated PostgreSQL.

The fake provider replaces only the external model; dispatch, family composition,
query permissions, business reads and independent access-log writes remain real.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app import config, permissions, security
from app.agent import maintenance_tools, provider, runtime, tools
from app.agent.tool_catalog import ToolCatalog
from app.etl import loader
from app.models.maintenance import FMaintenanceLine, MaintenanceManualCostOverride
from app.models.system import SysAccessLog, SysImportBatch
from tests import factories


def _ctx(*, own=False, money=True):
    graph = permissions.effective("sales" if own else "readonly", None)
    graph.update({"page_maintenance": True, "own_customers_only": own,
                  "data_profit": money, "data_purchase_cost": money})
    return security.UserContext(user_id="foundation-reader", role="sales" if own else "readonly",
                                salesperson_name="Owner", permissions=graph,
                                is_authenticated=True)


@pytest.mark.parametrize("outcome", ["success", "internal", "business", "denied", "unknown"])
def test_new_family_roundtrips_through_runtime_with_real_dispatch(monkeypatch, outcome):
    family = ToolCatalog()
    spec = {"type": "function", "function": {"name": "read_foundation_extension",
            "description": "Synthetic read-only extension", "parameters": {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"]}}}
    ctx = _ctx()
    seen_contexts = []

    @family.register(spec)
    def extension(db, args, current_user):
        seen_contexts.append(current_user)
        if outcome == "internal":
            raise ProgrammingError("SELECT SYNTHETIC_SECRET FROM PRIVATE_TABLE", {},
                                   Exception("SYNTHETIC_PASSWORD"))
        if outcome == "business":
            return {"error": "合成业务参数无效"}
        if outcome == "denied":
            return {"error": "无权限：合成拒绝"}
        return {"actor": current_user.user_id, "value": args["value"]}

    composed = ToolCatalog()
    composed.include(tools.catalog)
    composed.include(family)
    specs, handlers = composed.export()
    monkeypatch.setattr(tools, "TOOLS", specs)
    monkeypatch.setattr(tools, "_REGISTRY", handlers)
    audits, messages_seen = [], []
    monkeypatch.setattr(security, "record_access_log", lambda *args: audits.append(args))
    name = "unregistered_foundation_extension" if outcome == "unknown" else "read_foundation_extension"
    expected = {
        "success": {"actor": ctx.user_id, "value": 7},
        "internal": {"error": "工具执行失败，请换个方式或稍后重试", "retriable": True, "kind": "internal"},
        "business": {"error": "合成业务参数无效"},
        "denied": {"error": "无权限：合成拒绝"},
        "unknown": {"error": f"未知工具: {name}"},
    }[outcome]

    def fake_stream(messages, declarations=None):
        messages_seen.append(deepcopy(messages))
        assert declarations == specs
        if len(messages_seen) == 1:
            assert declarations[-1] == spec
            yield "result", provider.ChatResult(content="", tool_calls=[provider.ToolCall(
                id="foundation-call", name=name, arguments='{"value":7}')])
        else:
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "foundation-call"
            assert json.loads(messages[-1]["content"]) == expected
            assert not any(marker in messages[-1]["content"] for marker in (
                "SYNTHETIC_SECRET", "PRIVATE_TABLE", "SYNTHETIC_PASSWORD"))
            yield "result", provider.ChatResult(content="finished", tool_calls=[])

    monkeypatch.setattr(provider, "chat_stream", fake_stream)
    events = list(runtime.run_stream(None, [{"role": "user", "content": "synthetic"}], ctx))
    assert len(messages_seen) == 2
    assert len(audits) == 1 and audits[0] == (ctx, f"agent_tool:{name}", "agent", {"value": 7})
    assert seen_contexts == ([] if outcome == "unknown" else [ctx])
    assert next(event for event in events if event["type"] == "tool_done")["ok"] is (outcome == "success")
    assert events[-1]["answer"] == "finished"


@pytest.mark.parametrize("name,args,service", [
    ("get_maintenance_board", {"status": "red"}, "board"),
    ("get_maintenance_projects", {"top": -1}, "projects_aggregate"),
    ("get_maintenance_lines", {"project": "private", "page": 3}, "project_lines"),
])
def test_page_denial_precedes_business_reads(monkeypatch, name, args, service):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    monkeypatch.setattr(security, "record_access_log", lambda *args: None)
    ctx = _ctx()
    ctx.permissions["page_maintenance"] = False

    def unexpected_read(*args, **kwargs):
        pytest.fail("page permission must be checked before querying business data")

    monkeypatch.setattr(maintenance_tools.maintenance_cost, service, unexpected_read)
    assert "无权限" in tools.dispatch(None, name, args, ctx)["error"]


@pytest.fixture
def seeded_cost_rows(db):
    batch = SysImportBatch(filename="foundation-synthetic.xlsx", file_type="maintenance",
                           file_hash=uuid4().hex, status="success")
    db.add(batch)
    db.flush()
    heads, lines = {}, []
    # Alphabetical order differs from hidden cost order. Two salespeople share
    # one visible project name, so a project-name-only filter would leak a row.
    for index, (project, salesperson, amount) in enumerate((
        ("A-project", "Owner", "10"), ("Z-project", "Owner", "90"),
        ("A-project", "Foreign", "800"), ("Foreign-project", "Foreign", "900"),
    )):
        raw = f"foundation-order-{index}"
        head = factories.maintenance_head(raw, project=project, sales_order=f"XSDD-{index}")
        head["salesperson"] = salesperson
        heads[raw] = head
        lines.append(factories.maintenance_line(raw, f"foundation-line-{index}",
                                              f"FOUNDATION-PN-{index}", qty="2"))
    loader.load(db, factories.maintenance_result(heads, lines), batch.id, date(2026, 6, 1))
    amounts = ("10", "90", "800", "900")
    for index, amount in enumerate(amounts):
        line = db.scalar(select(FMaintenanceLine).where(
            FMaintenanceLine.raw_line_id == f"foundation-line-{index}"))
        db.add(MaintenanceManualCostOverride(
            line_id=line.id, unit_cost_ex_tax=Decimal(amount),
            unit_cost_inc_tax=Decimal(amount) * Decimal("1.13"),
            reason="合成价格", active=True, updated_by="fixture"))
    db.commit()


@pytest.mark.parametrize("own,money", [(False, True), (False, False), (True, True), (True, False)])
def test_maintenance_family_is_read_only_and_preserves_scope_and_money(db, monkeypatch, seeded_cost_rows, own, money):
    monkeypatch.setattr(config, "ENABLE_RBAC", True)
    monkeypatch.setattr(config, "ENABLE_ACCESS_LOG", True)
    ctx = _ctx(own=own, money=money)
    # PostgreSQL rejects any business-table write on this session. Access logs
    # deliberately use the existing independent short transaction.
    db.execute(text("SET TRANSACTION READ ONLY"))
    projects = tools.dispatch(db, "get_maintenance_projects", {"top": 1}, ctx)
    assert "error" not in projects and len(projects["rows"]) == 1
    if not money:
        assert projects["ranking_restricted"] is True
        assert projects["rows"][0]["project"] == "A-project"
        for key in ("actual_cost_inc", "actual_cost_ex", "known_cost_total", "contract_amount"):
            assert projects["rows"][0][key] is None
    else:
        assert projects["rows"][0]["project"] == ("Z-project" if own else "Foreign-project")
    all_projects = tools.dispatch(db, "get_maintenance_projects", {}, ctx)
    assert {row["project"] for row in all_projects["rows"]} == (
        {"A-project", "Z-project"} if own else {"A-project", "Z-project", "Foreign-project"})
    detail = tools.dispatch(db, "get_maintenance_lines", {"project": "A-project"}, ctx)
    assert "error" not in detail and detail["total"] == (1 if own else 2)
    assert {row["pn_std"] for row in detail["rows"]} == (
        {"FOUNDATION-PN-0"} if own else {"FOUNDATION-PN-0", "FOUNDATION-PN-2"})
    if not money:
        for row in detail["rows"]:
            for key in ("unit_cost", "cost_amount", "cost_amount_inc_tax", "cost_amount_ex_tax"):
                assert row[key] is None
    board = tools.dispatch(db, "get_maintenance_board", {"status": "red"}, ctx)
    if own:
        assert "受限销售" in board["error"]
    elif not money:
        assert len(board["rows"]) == 4  # Hidden status cannot select/count only red rows.
        assert "status_counts" not in board
        assert "decision_status_counts" not in board
        for row in board["rows"]:
            for key in ("budget", "remaining", "remaining_pct"):
                assert row[key] is None
            assert "status" not in row and "decision_status" not in row
        assert board["status_filter_applied"] is False
        assert board["effective_sort"] == "last_out"
    else:
        assert "error" not in board
    assert not db.dirty and not db.new and not db.deleted
    logs = db.scalars(select(SysAccessLog).where(SysAccessLog.username == ctx.user_id)).all()
    assert len(logs) == 4
    assert {log.action for log in logs} == {
        "agent_tool:get_maintenance_projects", "agent_tool:get_maintenance_lines",
        "agent_tool:get_maintenance_board"}
