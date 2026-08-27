"""M3-2：看板七端点契约、六态信封、未归属桶（plan v1.3 §4.4/§4.5）。"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app import config
from app.config import get_settings
from app.models.maintenance import FMaintenanceOrder
from app.models.sales import FSalesOrder
from app.models.system import SysImportBatch
from app.services import maintenance_boss_board as board
from tests.boss_board_helpers import (
    add_ckd,
    assign,
    boss_client,
    import_wbdd,
    make_project,
    purchaser_client,
    set_costs,
)


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def test_health_returns_four_sources(db):
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/health").json()
    assert set(body["sources"]) == {"wbdd", "ckd", "return_order", "rkd_inbound"}
    assert all(s["readiness"] == "not_imported" for s in body["sources"].values())


def test_summary_window_defaults_and_envelopes(db, tmp_path):
    import_wbdd(db, tmp_path, orders=2, lines_per_order=2)
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    assert body["window"] == {"from": "2026-01-01", "to": "2026-12-31"}
    assert body["orders_ytd"]["state"] == "ready"
    assert body["orders_ytd"]["value"] == 2
    assert body["lines_ytd"]["value"] == 4
    # 环比基期：等长窗口紧邻前移
    assert body["prev_window"]["window"]["to"] == "2025-12-31"
    # 时间窗字段名不写死年份
    assert "y2026" not in str(body)


def test_summary_cost_bundle_five_fields(db, tmp_path):
    import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    set_costs(db, source="direct", amount="100.00")
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    bundle = body["known_apply_cost_inc_tax"]
    assert bundle["state"] == "ready"
    value = bundle["value"]
    assert set(value) == {"actual_amount", "estimated_amount", "known_amount",
                          "missing_lines", "coverage_pct", "quality"}
    assert float(value["actual_amount"]) == 200.0
    assert value["missing_lines"] == 0
    assert value["coverage_pct"] == 100.0
    assert value["quality"] == "actual_only"


def test_cost_bundle_incomplete_when_missing_prices(db, tmp_path):
    """缺价必须显式 incomplete / missing_lines，绝不按 0 计（铁律 5 精神）。"""
    import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/summary",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()
    assert body["known_apply_cost_inc_tax"]["state"] == "partial"
    value = body["known_apply_cost_inc_tax"]["value"]
    assert value["missing_lines"] == 2
    assert value["quality"] == "incomplete"
    assert value["coverage_pct"] == 0.0


def test_cost_bundle_without_active_lines_is_unknown_not_real_zero():
    """有单头但零有效明细与空项目都不能被 SQL 的 0 单位元伪装成已知成本。"""
    bundle = board._bundle_from_row(0, 0, 0, 0, 0)
    assert bundle["state"] == "partial"
    assert bundle["value"] == {
        "actual_amount": 0,
        "estimated_amount": 0,
        "known_amount": None,
        "missing_lines": 0,
        "coverage_pct": None,
        "quality": "incomplete",
    }


def test_project_with_order_head_but_no_lines_never_gets_zero_or_green(db):
    project = make_project(db, code="只有需求单头的项目")
    _contract(db, project, "1000.00", contract_no="HT-HEAD-ONLY")
    batch = SysImportBatch(
        filename="head-only.xlsx",
        file_type="maintenance",
        file_hash=uuid.uuid4().hex * 2,
        status="success",
    )
    db.add(batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id=f"HEAD-{uuid.uuid4()}",
        order_no="WBDD-HEAD-ONLY",
        order_date=date(2026, 8, 1),
        data_status=config.ACTIVE_STATUS,
        linked_sales_order_no="HT-HEAD-ONLY",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.commit()
    assign(db, order, project)

    card = boss_client(db, username="head-without-lines").get(
        f"/api/maintenance/boss-board/projects/{project.project_id}"
    ).json()

    assert card["orders_ytd"]["value"] == 1
    assert card["lines_ytd"]["value"] == 0
    assert card["known_apply_cost_inc_tax"]["state"] == "partial"
    assert card["known_apply_cost_inc_tax"]["value"]["known_amount"] is None
    assert card["cost_ratio_pct"]["value"] is None
    assert card["card_status"] is None


def test_cost_bundle_reuses_strict_normalized_inc_quality(db, tmp_path):
    """legacy 不完整必须 missing；含税换算估算不得冒充 actual。"""
    from decimal import Decimal

    from app.models.maintenance import FMaintenanceLine

    import_wbdd(db, tmp_path, orders=1, lines_per_order=4)
    lines = db.execute(
        select(FMaintenanceLine).order_by(FMaintenanceLine.id)
    ).scalars().all()
    facts = (
        ("direct", "inc", "100.00", "100.00", []),
        ("direct", "ex", "100.00", "113.00", ["inc_tax_estimated"]),
        ("sales_ref", "ex", "50.00", "56.50", []),
        # normalized 列即使夹带金额，legacy 原始事实不完整仍须 fail-closed。
        ("direct", "inc", None, "99.00", []),
    )
    for line, (source, basis, legacy, normalized, flags) in zip(lines, facts):
        line.cost_source = source
        line.cost_tax_basis = basis
        line.cost_amount = Decimal(legacy) if legacy is not None else None
        line.cost_amount_inc_tax = Decimal(normalized)
        line.anomaly_flags = flags
    db.commit()

    bundle = boss_client(db, username="strict-inc").get(
        "/api/maintenance/boss-board/summary",
        params={"from": "2026-01-01", "to": "2026-12-31"},
    ).json()["known_apply_cost_inc_tax"]

    assert bundle["state"] == "partial"
    value = bundle["value"]
    assert float(value["actual_amount"]) == 100.0
    assert float(value["estimated_amount"]) == 169.5
    assert float(value["known_amount"]) == 269.5
    assert value["missing_lines"] == 1
    assert value["coverage_pct"] == 75.0
    assert value["quality"] == "incomplete"


def test_attention_registers_only_the_two_signed_kinds(db):
    """M0-A 已拍板（AB-2）：只注册 ①超预算 ③待返件多，未获选的四类不得自行加。"""
    body = boss_client(db).get("/api/maintenance/boss-board/attention").json()
    assert body["registered_kinds"] == ["budget_remaining", "pending_return"]
    # 业务只给口径没给分界线 → 这是排序取前 N，不是阈值告警，显式回传避免误读
    assert body["threshold"] is None
    assert body["ranking"]
    assert "pending_decision" not in body


def test_projects_list_includes_unassigned_bucket(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=2, lines_per_order=1)
    assign(db, orders[0], proj)          # 一单归属，一单留未归属
    client = boss_client(db)
    body = client.get("/api/maintenance/boss-board/projects").json()
    rows = body["rows"]
    bucket = next(r for r in rows if r["project_id"] == board.UNASSIGNED_BUCKET)
    assert bucket["orders_ytd"]["value"] == 1
    proj_row = next(r for r in rows if r["project_id"] == proj.project_id)
    assert proj_row["orders_ytd"]["value"] == 1
    assert proj_row["lifecycle"] == "ongoing"
    assert body["total"] == 1            # total 只数真实项目；桶是附加行


def test_project_card_exact_endpoint_uses_stable_id(db):
    target = make_project(db, code="同名项目")
    make_project(db, code="同名项目-另一个")

    response = boss_client(db, username="exact-project-card").get(
        f"/api/maintenance/boss-board/projects/{target.project_id}"
    )

    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == target.project_id


def test_projects_default_lifetime_includes_undated_orders(db, tmp_path):
    """默认 lifetime 母集保留 order_date=NULL；显式窗口仍只收日期命中行。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db, amount="42.00")
    orders[0].order_date = None
    db.commit()
    client = boss_client(db, username="lifetime-undated")

    lifetime_rows = client.get(
        "/api/maintenance/boss-board/projects"
    ).json()["rows"]
    lifetime = next(
        row for row in lifetime_rows if row["project_id"] == proj.project_id
    )
    assert lifetime["orders_ytd"]["value"] == 1
    assert lifetime["lines_ytd"]["value"] == 1
    assert float(
        lifetime["known_apply_cost_inc_tax"]["value"]["actual_amount"]
    ) == 42.0

    window_rows = client.get(
        "/api/maintenance/boss-board/projects",
        params={"from": "2026-01-01", "to": "2026-12-31"},
    ).json()["rows"]
    windowed = next(
        row for row in window_rows if row["project_id"] == proj.project_id
    )
    assert windowed["orders_ytd"]["value"] == 0
    assert windowed["lines_ytd"]["value"] == 0


def test_project_row_fact_envelopes_not_imported_never_zero(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path)
    assign(db, orders[0], proj)
    client = boss_client(db)
    row = next(r for r in client.get("/api/maintenance/boss-board/projects").json()["rows"]
               if r["project_id"] == proj.project_id)
    for field in ("shipped_qty", "returned_good_qty", "returned_bad_qty"):
        assert row[field]["state"] == "not_imported"
        assert row[field]["value"] is None       # 未导入绝不显示 0


def test_project_row_shipped_ready_after_ckd(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path)
    assign(db, orders[0], proj)
    add_ckd(db, wbdd_no=orders[0].order_no, qty="7")
    client = boss_client(db)
    row = next(r for r in client.get("/api/maintenance/boss-board/projects").json()["rows"]
               if r["project_id"] == proj.project_id)
    assert row["shipped_qty"]["state"] == "ready"
    assert float(row["shipped_qty"]["value"]) == 7.0
    assert row["returned_good_qty"]["state"] == "not_imported"


def test_get_projects_with_q_rejects_and_search_works(db, tmp_path):
    make_project(db, code="平安银行整体维保")
    make_project(db, code="其他工程")
    client = boss_client(db)
    bad = client.get("/api/maintenance/boss-board/projects", params={"q": "平安"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "use_search_endpoint"
    found = client.post("/api/maintenance/boss-board/projects/search",
                        json={"q": "平安"}).json()
    assert [r["project_code"] for r in found["rows"]] == ["平安银行整体维保"]


def test_order_and_line_drilldown(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="50.00")
    client = boss_client(db)
    od = client.get(
        f"/api/maintenance/boss-board/projects/{proj.project_id}/orders").json()
    assert od["total"] == 1
    row = od["rows"][0]
    assert row["order_no"] == orders[0].order_no
    assert row["line_count"] == 2
    assert row["self_report"]["head_shipped_qty"] is not None
    # M4-4：无判定并排——响应不含任何 mismatch/diff 键
    assert "mismatch" not in str(row) and "diff" not in str(row)
    lines = client.get(
        f"/api/maintenance/boss-board/orders/{orders[0].raw_order_id}/lines").json()
    assert lines["total"] == 2
    line = lines["rows"][0]
    # 流转状态列原样（铁律 3）
    assert float(line["supplied_qty"]) == 2.0
    assert line["consumed_qty"] is None
    assert line["known_apply_cost_inc_tax"]["state"] == "ready"


def test_order_line_evidence_is_strict_and_manual_uses_net_quantity(db, tmp_path):
    from decimal import Decimal

    from app.models.maintenance import (
        FMaintenanceLine,
        MaintenanceManualCostOverride,
    )

    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=3)
    assign(db, orders[0], proj)
    lines = db.execute(select(FMaintenanceLine).order_by(FMaintenanceLine.id)).scalars().all()

    lines[0].cost_source = "direct"
    lines[0].cost_tax_basis = "inc"
    lines[0].cost_amount = Decimal("0")
    lines[0].cost_amount_inc_tax = Decimal("0")
    lines[0].unit_cost_inc_tax = Decimal("0")

    lines[1].cost_source = "future_source"
    lines[1].cost_tax_basis = "inc"
    lines[1].cost_amount = Decimal("999")
    lines[1].cost_amount_inc_tax = Decimal("999")
    lines[1].unit_cost_inc_tax = Decimal("999")

    lines[2].cost_source = None
    lines[2].qty = Decimal("3")
    lines[2].return_qty = Decimal("1")
    db.add(MaintenanceManualCostOverride(
        line_id=lines[2].id,
        unit_cost_ex_tax=Decimal("8"),
        unit_cost_inc_tax=Decimal("9.04"),
        active=True,
        updated_by="test",
    ))
    db.commit()

    payload = boss_client(db, username="strict-line-evidence").get(
        f"/api/maintenance/boss-board/orders/{orders[0].raw_order_id}/lines"
    ).json()["rows"]
    by_id = {row["raw_line_id"]: row for row in payload}

    assert by_id[lines[0].raw_line_id]["known_apply_cost_inc_tax"] == {
        "state": "ready", "value": "0.00", "as_of": None,
    }
    assert by_id[lines[1].raw_line_id]["known_apply_cost_inc_tax"]["state"] == "partial"
    assert by_id[lines[1].raw_line_id]["known_apply_cost_inc_tax"]["value"] is None
    assert by_id[lines[2].raw_line_id]["known_apply_cost_inc_tax"]["value"] == "18.08"
    assert by_id[lines[2].raw_line_id]["cost_source"]["value"] == "manual"


def test_unassigned_bucket_drilldown(db, tmp_path):
    import_wbdd(db, tmp_path, orders=1)
    client = boss_client(db)
    body = client.get(
        f"/api/maintenance/boss-board/projects/{board.UNASSIGNED_BUCKET}/orders").json()
    assert body["total"] == 1


def test_flag_off_hides_all_board_endpoints(db):
    client = boss_client(db)
    settings = get_settings()
    settings.maintenance_boss_dashboard_enabled = False
    try:
        for path in ("/api/maintenance/boss-board/health",
                     "/api/maintenance/boss-board/summary",
                     "/api/maintenance/boss-board/attention",
                     "/api/maintenance/boss-board/projects"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/maintenance/boss-board/projects/search",
                           json={"q": "x"}).status_code == 404
        # 稳定端点不受影响
        assert client.get("/api/maintenance/projects").status_code == 200
    finally:
        settings.maintenance_boss_dashboard_enabled = True


# ---------- AB-2：需关注队列两个注册 kind ----------

def _contract(
    db,
    project,
    amount_inc_tax,
    *,
    included=True,
    effective_from=None,
    effective_to=None,
    contract_no="HT-001",
    contract_id=None,
):
    """台账合同行（金额口径 REQUIREMENTS #8：正式列=amount_inc_tax）。"""
    import uuid
    from datetime import date as _date
    from decimal import Decimal as _D

    from app.models.maintenance_project import MaintenanceProjectContract

    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=project.project_id,
        contract_id=contract_id or f"C-{uuid.uuid4().hex[:8]}",
        contract_no=contract_no,
        amount_inc_tax=(
            _D(amount_inc_tax) if amount_inc_tax is not None else None
        ), included_in_total=included,
        status_mapping_state="mapped", status_mapping_version="v1",
        effective_from=effective_from or _date(2026, 1, 1),
        effective_to=effective_to, source="ledger", version=1))
    db.commit()


def test_card_contract_fallback_accepts_only_consistent_successful_sales_facts(
    db,
):
    """Fallback is batched and fails closed on missing/ambiguous economics."""

    order_nos = {
        "valid": "XSDD-FALLBACK-VALID",
        "failed": "XSDD-FALLBACK-FAILED",
        "conflict": "XSDD-FALLBACK-CONFLICT",
        "same": "XSDD-FALLBACK-SAME",
        "half_up": "XSDD-FALLBACK-HALF-UP",
    }
    projects = {
        key: make_project(db, code=f"销售回退-{key}")
        for key in order_nos
    }
    maintenance_batch = SysImportBatch(
        filename="fallback-maintenance.xlsx",
        file_type="maintenance",
        file_hash=uuid.uuid4().hex * 2,
        status="success",
    )
    db.add(maintenance_batch)
    db.flush()
    maintenance_orders: dict[str, FMaintenanceOrder] = {}
    for key, order_no in order_nos.items():
        maintenance_order = FMaintenanceOrder(
            raw_order_id=f"WBDD-FALLBACK-{key}-{uuid.uuid4()}",
            order_no=f"WBDD-FALLBACK-{key}",
            order_date=date(2026, 8, 1),
            linked_sales_order_no=order_no,
            data_status="已生效",
            import_batch_id=maintenance_batch.id,
        )
        db.add(maintenance_order)
        maintenance_orders[key] = maintenance_order
    db.commit()
    for key, maintenance_order in maintenance_orders.items():
        assign(db, maintenance_order, projects[key])

    success_batch = SysImportBatch(
        filename="fallback-sales-success.xlsx",
        file_type="sales",
        file_hash=uuid.uuid4().hex * 2,
        status="success",
    )
    failed_batch = SysImportBatch(
        filename="fallback-sales-failed.xlsx",
        file_type="sales",
        file_hash=uuid.uuid4().hex * 2,
        status="failed",
    )
    db.add_all([success_batch, failed_batch])
    db.flush()

    def add_sales_fact(
        tag: str,
        order_no: str,
        amount_ex_tax: str,
        tax_rate: str,
        *,
        batch: SysImportBatch = success_batch,
        data_status: str = "已生效",
    ) -> None:
        db.add(FSalesOrder(
            raw_order_id=f"SO-FALLBACK-{tag}-{uuid.uuid4()}",
            order_no=order_no,
            amount_ex_tax=Decimal(amount_ex_tax),
            tax_rate=Decimal(tax_rate),
            data_status=data_status,
            import_batch_id=batch.id,
        ))

    add_sales_fact("valid", order_nos["valid"], "100.00", "0.13")
    # Inactive rows cannot outvote the active successful fact.
    add_sales_fact(
        "valid-inactive",
        order_nos["valid"],
        "999.00",
        "0.13",
        data_status="已作废",
    )
    # A failed import batch supplies no economic evidence.
    add_sales_fact(
        "failed",
        order_nos["failed"],
        "200.00",
        "0.13",
        batch=failed_batch,
    )
    add_sales_fact("conflict-a", order_nos["conflict"], "100.00", "0.13")
    add_sales_fact("conflict-b", order_nos["conflict"], "120.00", "0.13")
    add_sales_fact("same-a", order_nos["same"], "100.00", "0.13")
    add_sales_fact("same-b", order_nos["same"], "100.00", "0.13")
    add_sales_fact("half-up", order_nos["half_up"], "1.00", "0.0050")
    db.commit()

    sales_queries: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = " ".join(statement.lower().split())
        if "from f_sales_order " in normalized:
            sales_queries.append(normalized)

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        cards = board._card_contracts(
            db, [project.project_id for project in projects.values()]
        )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)

    valid = cards[projects["valid"].project_id]
    assert valid["amount_inc_tax"] == Decimal("113.00")
    assert valid["contract_incomplete"] is False

    failed = cards[projects["failed"].project_id]
    assert failed["amount_inc_tax"] is None
    assert failed["contract_incomplete"] is True

    conflict = cards[projects["conflict"].project_id]
    assert conflict["amount_inc_tax"] is None
    assert conflict["contract_incomplete"] is True

    same = cards[projects["same"].project_id]
    assert same["amount_inc_tax"] == Decimal("113.00")
    assert same["contract_incomplete"] is False

    # Decimal's default HALF_EVEN would return 1.00; financial policy is the
    # same HALF_UP boundary as PostgreSQL round(numeric, 2).
    half_up = cards[projects["half_up"].project_id]
    assert half_up["amount_inc_tax"] == Decimal("1.01")
    assert half_up["contract_incomplete"] is False

    # All fallback projects are resolved by one joined sales-evidence query.
    assert len(sales_queries) == 1


def test_attention_budget_item_red_when_spend_exceeds_contract(db, tmp_path):
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="500.00")          # 2 行 × 500 = 1000 已知支出
    _contract(db, proj, "600.00")           # 预算 600 → 超支
    body = boss_client(db, username="attn-boss").get(
        "/api/maintenance/boss-board/attention").json()
    item = next(i for i in body["items"] if i["kind"] == "budget_remaining")
    assert item["project_id"] == proj.project_id
    assert item["value"]["status"] == "red"
    assert item["evidence_link"].endswith(f"/projects/{proj.project_id}/orders")


def test_attention_budget_ignores_contracts_not_included_in_total(db, tmp_path):
    """included_in_total=false 的合同不进预算——不从状态文本猜（REQUIREMENTS #31）。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db, amount="500.00")
    _contract(db, proj, "600.00", included=False)
    body = boss_client(db, username="attn-boss2").get(
        "/api/maintenance/boss-board/attention").json()
    assert not [i for i in body["items"] if i["kind"] == "budget_remaining"]


def test_attention_budget_uses_only_current_complete_contracts(db, tmp_path):
    """过期不累计；缺金额、重复版本、跨项目冲突均 fail-closed。"""
    from datetime import date
    from decimal import Decimal

    complete = make_project(db, code="当前完整合同项目")
    orders = import_wbdd(
        db, tmp_path, project="当前完整合同项目", orders=1, lines_per_order=1
    )
    assign(db, orders[0], complete)
    set_costs(db, amount="500.00")
    # 关注队列与项目卡都使用截至今日的 lifetime 母集，空日期不能漏掉。
    orders[0].order_date = None
    db.commit()
    _contract(
        db,
        complete,
        "1000.00",
        effective_from=date(2025, 1, 1),
        effective_to=date(2026, 1, 1),
        contract_no="HT-EXPIRED",
    )
    _contract(db, complete, "400.00", contract_no="HT-CURRENT")

    incomplete = make_project(db, code="当前合同缺额项目")
    _contract(db, incomplete, "400.00", contract_no="HT-KNOWN")
    _contract(db, incomplete, None, contract_no="HT-MISSING")

    duplicate = make_project(db, code="当前合同重复项目")
    _contract(
        db,
        duplicate,
        "200.00",
        contract_no="HT-DUP-A",
        contract_id="C-DUPLICATE",
        effective_from=date(2026, 1, 1),
    )
    _contract(
        db,
        duplicate,
        "200.00",
        contract_no="HT-DUP-B",
        contract_id="C-DUPLICATE",
        effective_from=date(2026, 2, 1),
    )

    conflict_a = make_project(db, code="跨项目冲突A")
    conflict_b = make_project(db, code="跨项目冲突B")
    _contract(
        db, conflict_a, "200.00", contract_id="C-CROSS-A", contract_no="HT-CROSS"
    )
    _contract(
        db, conflict_b, "200.00", contract_id="C-CROSS-B", contract_no="HT-CROSS"
    )

    assert board._attention_budget(db) == {
        complete.project_id: Decimal("400.00"),
    }
    cards = board._card_contracts(db, [
        complete.project_id,
        incomplete.project_id,
        duplicate.project_id,
        conflict_a.project_id,
        conflict_b.project_id,
    ])
    assert cards[complete.project_id]["amount_inc_tax"] == Decimal("400.00")
    assert cards[complete.project_id]["contract_incomplete"] is False
    assert cards[incomplete.project_id]["contract_incomplete"] is True
    assert cards[duplicate.project_id]["amount_inc_tax"] is None
    assert cards[duplicate.project_id]["contract_incomplete"] is True
    assert cards[conflict_a.project_id]["amount_inc_tax"] is None
    assert cards[conflict_a.project_id]["contract_shared"] is True
    assert cards[conflict_a.project_id]["contract_incomplete"] is True
    budget_stats = board._budget_overspend_stats()
    overspend = dict(db.execute(
        select(budget_stats.c.project_id, budget_stats.c.overspend)
    ).all())
    assert overspend == {complete.project_id: Decimal("100.00")}

    body = boss_client(db, username="current-budget").get(
        "/api/maintenance/boss-board/attention"
    ).json()
    budget_items = [
        item for item in body["items"] if item["kind"] == "budget_remaining"
    ]
    assert [item["project_id"] for item in budget_items] == [complete.project_id]
    assert float(budget_items[0]["value"]["budget_inc_tax"]) == 400.0
    assert budget_items[0]["value"]["status"] == "red"


def test_attention_budget_item_hidden_without_cost_permission(db, tmp_path):
    """预算条目是金额派生物：「它在不在队列里」本身就泄露成本排名 → 整条略去。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="500.00")
    _contract(db, proj, "600.00")
    body = boss_client(db, username="attn-nocost", with_cost=False).get(
        "/api/maintenance/boss-board/attention").json()
    assert not [i for i in body["items"] if i["kind"] == "budget_remaining"]
    assert 600 not in _all_numbers(body) and 1000 not in _all_numbers(body)


def _all_numbers(payload) -> list:
    out = []
    if isinstance(payload, dict):
        for v in payload.values():
            out.extend(_all_numbers(v))
    elif isinstance(payload, list):
        for v in payload:
            out.extend(_all_numbers(v))
    elif isinstance(payload, (int, float)) and not isinstance(payload, bool):
        out.append(payload)
    return out


def test_attention_pending_return_is_not_imported_when_rkd_absent(db, tmp_path):
    """铁律 5：RKD 未导入 → 回收量无法知道，返件率不得按 0 算。"""
    from decimal import Decimal

    from app.models.maintenance import FMaintenanceLine

    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    for ln in db.execute(select(FMaintenanceLine)).scalars():
        ln.return_qty = Decimal("5")        # 应返 5（退货列在聚合白名单内）
    db.commit()
    body = boss_client(db, username="attn-ret").get(
        "/api/maintenance/boss-board/attention").json()
    item = next(i for i in body["items"] if i["kind"] == "pending_return")
    assert item["value"]["demand_return_qty"] == "5.000"
    for field in ("recovered_return_qty", "pending_return_qty", "return_rate_pct"):
        assert item["value"][field]["state"] == "not_imported", field
        assert item["value"][field]["value"] is None, field


def test_attention_never_aggregates_status_columns(db):
    """铁律 3 回归：待返/已返是流转状态列，队列口径只能用白名单列与三源事实。"""
    from sqlalchemy.dialects import postgresql

    from app.services import maintenance_boss_board as b

    for stmt in (b._budget_overspend_stats(),):
        sql = str(stmt.compile(dialect=postgresql.dialect(),
                               compile_kwargs={"literal_binds": True}))
        for column in b.STATUS_ONLY_COLUMNS:
            assert column not in sql, column


# ---------- 项目卡墙字段（REQUIREMENTS #34/#35/#41/#43） ----------

def _card(db, client, project_id):
    rows = client.get("/api/maintenance/boss-board/projects",
                      params={"from": "2026-01-01", "to": "2026-12-31"}).json()["rows"]
    return next(r for r in rows if r["project_id"] == project_id)


def test_card_carries_contract_manager_and_amount(db, tmp_path):
    """2026-08-20 修正：project_manager＝项目经理（账号显示名），不再误用 CMO。"""
    from app.models.maintenance_project import MaintenanceProject
    from app.models.system import SysUser

    proj = make_project(db)
    record = db.get(MaintenanceProject, proj.project_id)
    record.cmo_name = "李CMO"
    record.project_manager_id = "pm-zhang"
    db.add(SysUser(username="pm-zhang", password_hash="x", role="admin", display_name="张项目经理", is_active=True))
    db.commit()
    _contract(db, proj, "100000.00")
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    row = _card(db, boss_client(db, username="card-boss"), proj.project_id)
    assert row["contract_nos"] == ["HT-001"]
    assert row["project_manager"] == "张项目经理"
    assert row["project_manager"] != "李CMO"
    assert row["contract_amount_inc_tax"]["value"] == "100000.00"


def test_card_carries_expense_and_requisition_costs(db, tmp_path):
    """2026-08-22 客户反馈：报销/已领用成本上卡——有成本权限出值，无权限 restricted。"""
    from datetime import date
    from decimal import Decimal

    from app.models.maintenance import FProjectExpense
    from app.models.maintenance_project import MaintenanceProjectContract
    from app.models.maintenance_project_operations import (
        MaintenanceProjectExpenseAttribution,
    )

    proj = make_project(db, code="合成项目C")
    _contract(db, proj, "100000.00")
    contract = db.scalar(select(MaintenanceProjectContract).where(
        MaintenanceProjectContract.project_id == proj.project_id,
    ))
    batch = SysImportBatch(
        filename="card-expense.xlsx",
        file_type="expense",
        file_hash=uuid.uuid4().hex.ljust(64, "0"),
        status="success",
    )
    db.add(batch)
    db.flush()
    raw_line_id = "card-expense-raw-1"
    db.add(FProjectExpense(
        raw_line_id=raw_line_id,
        bxd_no="BXD-1",
        line_no=1,
        data_status="已结束",
        expense_date=date(2026, 8, 1),
        linked_sales_order_no=contract.contract_no,
        amount=Decimal("100.00"),
        amount_ex_tax=Decimal("100.00"),
        amount_inc_tax=Decimal("113.00"),
        tax_basis="ex",
        tax_rate_used=Decimal("0.13"),
        import_batch_id=batch.id,
    ))
    db.add(MaintenanceProjectExpenseAttribution(
        expense_id="bxd:exp-1", project_id=proj.project_id,
        project_contract_id=contract.project_contract_id,
        raw_expense_line_id=raw_line_id,
        expense_ref="BXD-1", expense_date=date(2026, 8, 1),
        amount_ex_tax=Decimal("100.00"), amount_inc_tax=Decimal("113.00"),
        tax_rate_used=Decimal("0.13"), raw_status="已结束",
        status_mapping_state="mapped", normalized_status="approved",
        status_mapping_version="t", ownership_mapping_state="mapped",
        ownership_mapping_version="synthetic-card-expense-v1", version=1))
    db.commit()
    orders = import_wbdd(db, tmp_path, project="合成项目C", orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    row = _card(db, boss_client(db, username="card-cost", with_cost=True),
                proj.project_id)
    assert row["expense_cost_inc_tax"]["value"] == "113.00"
    assert row["requisition_cost_inc_tax"]["state"] == "ready"  # 0 也是已知值

    # 无成本权限：金额位 restricted（键集一致防侧信道）
    row_nocost = _card(db, boss_client(db, username="card-cost-n", with_cost=False),
                       proj.project_id)
    assert row_nocost["expense_cost_inc_tax"]["state"] == "restricted"
    assert row_nocost["requisition_cost_inc_tax"]["state"] == "restricted"


def test_card_carries_salesperson_ledger_first_mode_fallback(db, tmp_path):
    """2026-08-21 客户反馈：卡片显示销售——台账 salesperson 优先，XSDD 众数兜底。"""
    from app.models.maintenance_project import MaintenanceProject

    # 路径一：台账没给 salesperson → 回落需求单销售众数（夹具「销售人员」=合成销售）
    proj = make_project(db, code="合成项目B")
    orders = import_wbdd(db, tmp_path, project="合成项目B", orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    row = _card(db, boss_client(db, username="card-sales-fallback"), proj.project_id)
    assert row["salesperson"] == "合成销售"

    # 路径二：台账给了 salesperson → 台账事实源优先，不被动静
    record = db.get(MaintenanceProject, proj.project_id)
    record.salesperson = "台账销售"
    db.commit()
    row = _card(db, boss_client(db, username="card-sales-ledger"), proj.project_id)
    assert row["salesperson"] == "台账销售"


def test_card_status_is_green_yellow_red_by_cost_ratio(db, tmp_path):
    """#35：<80% 绿 / 80–100% 黄 / >100% 红。"""
    from app.services import maintenance_boss_board as b
    from decimal import Decimal as D

    assert b.card_status(D("79.9")) == "normal"
    assert b.card_status(D("80.0")) == "warning"
    assert b.card_status(D("100.0")) == "warning"
    assert b.card_status(D("100.1")) == "alert"
    # 算不出来不拿绿色冒充健康（铁律 5）
    assert b.card_status(None) is None


def test_card_status_alert_end_to_end(db, tmp_path):
    proj = make_project(db)
    _contract(db, proj, "100.00")
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="100.00")          # 2 行 × 100 = 200，合同 100 → 200%
    row = _card(db, boss_client(db, username="card-alert"), proj.project_id)
    assert row["card_status"] == "alert"
    assert row["cost_ratio_pct"]["value"] == "200.0"


def test_card_status_is_none_without_contract_amount(db, tmp_path):
    """无合同额 → 成本率算不出 → 不给三态（前端显示「数据不足」）。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db)
    row = _card(db, boss_client(db, username="card-nocontract"), proj.project_id)
    assert row["card_status"] is None
    assert row["cost_ratio_pct"]["value"] is None


def test_procured_qty_is_warehouse_shipped_plus_direct_ship(db, tmp_path):
    """#41 业务指定公式：维保备件采购数 = 库房发货 + 直采直发。"""
    from decimal import Decimal as D

    from app.models.maintenance import FMaintenanceLine

    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    for ln in db.execute(select(FMaintenanceLine)).scalars():
        ln.warehouse_shipped_qty = D("2")
        ln.direct_ship_qty = D("3")
    db.commit()
    row = _card(db, boss_client(db, username="card-procured"), proj.project_id)
    assert row["procured_qty"]["value"] == "10.000"      # (2+3) × 2 行


def test_card_money_fields_are_restricted_without_cost_permission(db, tmp_path):
    """成本+利润权限双缺时，所有金额位 restricted、三态 None。"""
    proj = make_project(db)
    _contract(db, proj, "100000.00")
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db)
    client = boss_client(db, username="card-nocost", with_cost=False,
                         with_profit=False)
    row = _card(db, client, proj.project_id)
    for field in ("contract_amount_inc_tax", "known_apply_cost_ex_tax",
                  "collection_preview_inc_tax", "cost_ratio_pct"):
        assert row[field]["state"] == "restricted", field
        assert row[field]["value"] is None, field
    # 三态本身也是成本派生物：无权限时不给，免得从颜色反推金额
    assert row["card_status"] is None


def test_card_contract_fields_restricted_without_profit_permission(db, tmp_path):
    """有成本无利润（真实采购员视角）：合同额/回款 restricted，成本可见，成本率/三态不可算。"""
    proj = make_project(db)
    _contract(db, proj, "100000.00")
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db, amount="5000.00")
    client = purchaser_client(db, username="card-purchaser")
    row = _card(db, client, proj.project_id)
    # 利润组字段受限
    for field in ("contract_amount_inc_tax", "collection_preview_inc_tax"):
        assert row[field]["state"] == "restricted", field
        assert row[field]["value"] is None, field
    # 成本组字段可见
    assert row["known_apply_cost_inc_tax"]["state"] == "ready"
    assert float(row["known_apply_cost_inc_tax"]["value"]["known_amount"]) == 5000.0
    # 成本率需要合同额，算不出 → restricted；三态 None
    assert row["cost_ratio_pct"]["state"] == "restricted"
    assert row["cost_ratio_pct"]["value"] is None
    assert row["card_status"] is None


def test_profit_override_without_cost_fails_closed(db, tmp_path):
    """data_profit 依赖成本权限：仅开利润 override 时财务字段仍全部 fail-close。"""
    proj = make_project(db)
    _contract(db, proj, "100000.00")
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=1)
    assign(db, orders[0], proj)
    set_costs(db, amount="5000.00")
    client = boss_client(db, username="card-profit-only",
                         with_cost=False, with_profit=True)
    row = _card(db, client, proj.project_id)
    assert row["contract_amount_inc_tax"]["state"] == "restricted"
    assert row["contract_amount_inc_tax"]["value"] is None
    assert row["collection_preview_inc_tax"]["state"] == "restricted"
    assert row["collection_preview_inc_tax"]["value"] is None
    assert row["known_apply_cost_ex_tax"]["state"] == "restricted"
    assert row["cost_ratio_pct"]["state"] == "restricted"
    assert row["cost_ratio_pct"]["value"] is None
    assert row["card_status"] is None


def test_search_matches_xsdd_contract_no(db, tmp_path):
    """#37：搜索要能命中项目单号（XSDD 合同号）。"""
    proj = make_project(db, "看不出名字的项目")
    _contract(db, proj, "1000.00")
    client = boss_client(db, username="card-search")
    body = client.post("/api/maintenance/boss-board/projects/search",
                       json={"q": "HT-001"}).json()
    assert proj.project_id in {r["project_id"] for r in body["rows"]}


def test_card_status_filter(db, tmp_path):
    proj = make_project(db)
    _contract(db, proj, "100.00")
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    set_costs(db, amount="100.00")          # → alert
    client = boss_client(db, username="card-filter")
    alert = client.get("/api/maintenance/boss-board/projects",
                       params={"card_status": "alert"}).json()["rows"]
    assert proj.project_id in {r["project_id"] for r in alert}
    normal = client.get("/api/maintenance/boss-board/projects",
                        params={"card_status": "normal"}).json()["rows"]
    assert proj.project_id not in {r["project_id"] for r in normal}


def _assert_cost_contract_422(response):
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "cost_contract_permission_required"
    assert "成本及合同财务数据权限" in detail["message"]


def test_cost_ratio_sort_rejects_missing_either_permission(db):
    """缺成本或利润任一权限时，成本率排序 422（防顺序侧信道）。"""
    proj = make_project(db)
    # GET 缺利润
    _assert_cost_contract_422(
        purchaser_client(db, username="sort-purchaser-get").get(
            "/api/maintenance/boss-board/projects",
            params={"sort": "cost_ratio"})
    )
    # GET 缺成本
    _assert_cost_contract_422(
        boss_client(db, username="sort-nocost-get",
                    with_cost=False, with_profit=True).get(
            "/api/maintenance/boss-board/projects",
            params={"sort": "cost_ratio"})
    )
    # POST 缺利润
    _assert_cost_contract_422(
        purchaser_client(db, username="sort-purchaser-post").post(
            "/api/maintenance/boss-board/projects/search",
            json={"q": "x", "sort": "cost_ratio"})
    )
    # POST 缺成本
    _assert_cost_contract_422(
        boss_client(db, username="sort-nocost-post",
                    with_cost=False, with_profit=True).post(
            "/api/maintenance/boss-board/projects/search",
            json={"q": "x", "sort": "cost_ratio"})
    )
    # 双权限账号可用
    assert boss_client(db, username="sort-boss").get(
        "/api/maintenance/boss-board/projects",
        params={"sort": "cost_ratio"}).status_code == 200


def test_card_status_filter_rejects_missing_either_permission(db):
    """缺成本或利润任一权限时，三态筛选 422（防集合侧信道）。"""
    proj = make_project(db)
    # GET 缺利润
    _assert_cost_contract_422(
        purchaser_client(db, username="filter-purchaser-get").get(
            "/api/maintenance/boss-board/projects",
            params={"card_status": "alert"})
    )
    # GET 缺成本
    _assert_cost_contract_422(
        boss_client(db, username="filter-nocost-get",
                    with_cost=False, with_profit=True).get(
            "/api/maintenance/boss-board/projects",
            params={"card_status": "alert"})
    )
    # POST 缺利润
    _assert_cost_contract_422(
        purchaser_client(db, username="filter-purchaser-post").post(
            "/api/maintenance/boss-board/projects/search",
            json={"q": "x", "card_status": "alert"})
    )
    # POST 缺成本
    _assert_cost_contract_422(
        boss_client(db, username="filter-nocost-post",
                    with_cost=False, with_profit=True).post(
            "/api/maintenance/boss-board/projects/search",
            json={"q": "x", "card_status": "alert"})
    )
    # 双权限账号可用
    assert boss_client(db, username="filter-boss").get(
        "/api/maintenance/boss-board/projects",
        params={"card_status": "alert"}).status_code == 200


def test_bucket_row_has_the_same_card_keys(db, tmp_path):
    """桶行与项目行键集必须一致（同构数组）。"""
    proj = make_project(db)
    orders = import_wbdd(db, tmp_path, orders=2)
    assign(db, orders[0], proj)
    rows = boss_client(db, username="card-shape").get(
        "/api/maintenance/boss-board/projects").json()["rows"]
    bucket = next(r for r in rows if r["project_id"] == board.UNASSIGNED_BUCKET)
    real = next(r for r in rows if r["project_id"] == proj.project_id)
    assert set(bucket) == set(real)
    assert bucket["contract_nos"] == [] and bucket["project_manager"] is None
