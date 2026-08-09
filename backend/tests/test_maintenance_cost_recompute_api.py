"""Late-arriving evidence recomputation for stable-project cost gaps."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import auth, permissions
from app.api import maintenance_project_operations
from app.auth import hash_password
from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysUser
from app.services import maintenance_consumption_cost
from tests.test_maintenance_project_operations_api import (
    _batch,
    _client,
    _create_legacy_site_issue_fixture,
    _project,
)


def _count_recompute_queries(
    db,
    client: TestClient,
    *,
    project_id: str,
) -> tuple[dict, int, list[str]]:
    engine = db.get_bind()
    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = client.post(
            f"/api/maintenance/projects/stable/{project_id}/cost-gaps/recompute",
            json={"reason": "验证批量重算查询与锁范围"},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert response.status_code == 200, response.text
    return response.json(), len(statements), statements


def _add_unresolved_lines(db, *, project_id: str, count: int) -> None:
    part = DimPart(pn_std=f"PN-BATCH-{project_id}")
    db.add(part)
    db.flush()
    issue = MaintenanceSiteIssue(
        issue_id=f"issue-{project_id}",
        project_id=project_id,
        issue_no=f"ISSUE-{project_id}",
        issue_date=date(2026, 6, 10),
        raw_status="synthetic-confirmed",
        status_mapping_state="mapped",
        normalized_status="confirmed",
        status_mapping_version="synthetic-map-v1",
    )
    db.add(issue)
    db.add_all(
        MaintenanceSiteIssueLine(
            issue_line_id=f"line-{project_id}-{index:03d}",
            issue_id=issue.issue_id,
            line_no=index + 1,
            part_id=part.id,
            pn=part.pn_std,
            quantity=Decimal("1"),
            algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
        )
        for index in range(count)
    )
    db.commit()


def _create_gap(db, *, project_id: str, suffix: str) -> DimPart:
    part = DimPart(pn_std=f"PN-SYNTH-{suffix}")
    db.add(part)
    db.commit()
    payload = _create_legacy_site_issue_fixture(
        db,
        project_id=project_id,
        body={
            "issue_no": f"ISSUE-SYNTH-{suffix}",
            "issue_date": "2026-06-10",
            "raw_status": "synthetic-confirmed",
            "status_mapping_state": "mapped",
            "normalized_status": "confirmed",
            "status_mapping_version": "synthetic-issue-map-v1",
            "lines": [{
                "issue_line_id": f"issue-line-{suffix}",
                "line_no": 1,
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": "2",
            }],
            "reason": "建立合成缺价领用行",
        },
    )
    assert payload["lines"][0]["cost_source"] is None
    return part


def _add_late_purchase(
    db,
    *,
    part: DimPart,
    suffix: str,
    unit_price: int,
    order_date: date = date(2026, 6, 13),
) -> FPurchaseOrder:
    batch = _batch(db, suffix.lower())
    order = FPurchaseOrder(
        raw_order_id=f"PO-H-{suffix}",
        order_no=f"PO-{suffix}",
        order_date=order_date,
        data_status="已生效",
        is_tax_inclusive=False,
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        FPurchaseLine(
            raw_line_id=f"PO-L-{suffix}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            qty=4,
            unit_price=unit_price,
            import_batch_id=batch.id,
        )
    )
    db.commit()
    return order


def _add_late_sale(
    db,
    *,
    part: DimPart,
    suffix: str,
    unit_price_inc_tax: int,
    order_date: date = date(2026, 6, 12),
) -> FSalesOrder:
    batch = _batch(db, suffix.lower())
    order = FSalesOrder(
        raw_order_id=f"SO-H-{suffix}",
        order_no=f"SO-{suffix}",
        order_date=order_date,
        data_status="已生效",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()
    db.add(
        FSalesLine(
            raw_line_id=f"SO-L-{suffix}",
            order_id=order.id,
            part_id=part.id,
            pn_std=part.pn_std,
            qty=4,
            unit_price=unit_price_inc_tax,
            import_batch_id=batch.id,
        )
    )
    db.commit()
    return order


def _limited_client(
    db,
    *,
    username: str,
    action_manage: bool,
    purchase_cost: bool,
) -> TestClient:
    base = permissions.effective("readonly", None)
    overrides = {
        "page_maintenance": True,
        "action_maintenance_project_manage": action_manage,
        "data_purchase_cost": purchase_cost,
        "data_profit": False,
    }
    effective = permissions.effective_from_snapshot(base, overrides)
    db.add(
        SysUser(
            username=username,
            role="readonly",
            display_name="合成受限维保用户",
            password_hash=hash_password("synthetic-password-123"),
            template_code="readonly",
            template_version=1,
            template_perms=base,
            perm_overrides=overrides,
            permissions=effective,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_project_operations.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def test_sales_fallback_persists_only_valid_samples_when_result_contains_over_limit(
    monkeypatch,
):
    valid = SimpleNamespace(
        id=101,
        qty=Decimal("2"),
        unit_price=Decimal("113"),
        order_no="SO-VALID",
        order_date=date(2026, 6, 12),
    )
    over_limit = SimpleNamespace(
        id=102,
        qty=Decimal("1"),
        unit_price=Decimal("1000000000000"),
        order_no="SO-OVER-LIMIT",
        order_date=date(2026, 6, 13),
    )

    class SalesResultSession:
        def execute(self, _statement):
            return [valid, over_limit]

    monkeypatch.setattr(
        maintenance_consumption_cost,
        "_direct_purchase",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        maintenance_consumption_cost,
        "_purchase_window",
        lambda *_args, **_kwargs: None,
    )
    line = MaintenanceSiteIssueLine(
        issue_line_id="issue-line-sales-valid-filter",
        issue_id="issue-sales-valid-filter",
        line_no=1,
        part_id=1,
        pn="PN-SALES-VALID-FILTER",
        quantity=Decimal("3"),
    )

    maintenance_consumption_cost.resolve_line(
        SalesResultSession(),
        issue_date=date(2026, 6, 10),
        line=line,
    )

    assert line.cost_source == "sales_window"
    assert line.unit_cost == Decimal("100.00")
    assert line.cost_amount == Decimal("300.00")
    assert line.reference_sample_count == 1
    assert line.reference_sample_ids == ["sales:101"]
    assert [sample["document_no"] for sample in line.reference_samples] == [
        "SO-VALID"
    ]


def test_recompute_persists_t_plus_three_purchase_once_with_line_audit(db):
    project = _project(db, project_id="project-late-cost-recompute")
    client = _client(db, username="late_cost_recompute_admin")
    part = _create_gap(db, project_id=project.project_id, suffix="LATE-PURCHASE")
    state_before = db.get(MaintenanceProjectWorkbookState, project.project_id)
    revision_before = state_before.revision
    data_version_before = state_before.data_version
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"

    still_missing = client.post(path, json={"reason": "领用当天先检查一次系统价格"})
    assert still_missing.status_code == 200, still_missing.text
    assert still_missing.json() == {
        "resolved": 0,
        "remaining": 1,
        "data_version": data_version_before,
    }
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        action="auto_recompute",
    ).count() == 0

    _add_late_purchase(db, part=part, suffix="LATE-RECOMPUTE", unit_price=25)
    recomputed = client.post(
        path,
        json={"reason": "采购单在领用后 3 天到达，重新匹配系统价格"},
    )

    assert recomputed.status_code == 200, recomputed.text
    assert recomputed.json()["resolved"] == 1
    assert recomputed.json()["remaining"] == 0
    assert recomputed.json()["data_version"] != data_version_before
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-LATE-PURCHASE")
    assert line.cost_source == "purchase_window"
    assert line.unit_cost == 25
    assert line.cost_amount == 50
    assert line.reference_sample_ids == [line.reference_samples[0]["sample_id"]]
    assert line.reference_samples[0]["distance_days"] == 3
    assert line.algorithm_version == "site-issue-cost-v1"
    assert line.version == 2
    state_after = db.get(MaintenanceProjectWorkbookState, project.project_id)
    assert state_after.revision == revision_before + 1
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_type="site_issue_cost",
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).one()
    assert audit.before_json["cost_source"] is None
    assert audit.before_json["version"] == 1
    assert audit.after_json["cost_source"] == "purchase_window"
    assert audit.after_json["version"] == 2

    repeated = client.post(path, json={"reason": "重复点击不应再次写入"})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {
        "resolved": 0,
        "remaining": 0,
        "data_version": state_after.data_version,
    }
    db.expire_all()
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before + 1
    assert db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).count() == 1


def test_recompute_rejects_archived_project_without_mutation(db):
    project = _project(db, project_id="project-archived-cost-recompute")
    client = _client(db, username="archived_cost_recompute_admin")
    _create_gap(db, project_id=project.project_id, suffix="ARCHIVED-GAP")
    project.is_active = False
    db.commit()
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    revision_before = state.revision

    response = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute",
        json={"reason": "归档项目不得重算"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "项目主档已归档"
    db.expire_all()
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before
    assert db.get(MaintenanceSiteIssueLine, "issue-line-ARCHIVED-GAP").version == 1


def test_recompute_requires_manage_action_and_purchase_cost_visibility(db):
    project = _project(db, project_id="project-cost-recompute-permissions")
    without_action = _limited_client(
        db,
        username="cost_recompute_without_action",
        action_manage=False,
        purchase_cost=True,
    )
    without_cost = _limited_client(
        db,
        username="cost_recompute_without_cost",
        action_manage=True,
        purchase_cost=False,
    )
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"

    assert without_action.post(path, json={"reason": "缺少管理动作权限"}).status_code == 403
    assert without_cost.post(path, json={"reason": "缺少成本查看权限"}).status_code == 403
    assert db.get(MaintenanceProjectWorkbookState, project.project_id) is None


def test_manual_fill_persists_new_auto_evidence_instead_of_rolling_it_back(db):
    project = _project(db, project_id="project-manual-auto-race")
    client = _client(db, username="manual_auto_race_admin")
    part = _create_gap(db, project_id=project.project_id, suffix="MANUAL-AUTO-RACE")
    listed = client.get(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps"
    )
    assert listed.status_code == 200, listed.text
    stale_gap = listed.json()["rows"][0]
    revision_before = db.get(MaintenanceProjectWorkbookState, project.project_id).revision
    _add_late_purchase(
        db,
        part=part,
        suffix="MANUAL-AUTO-RACE",
        unit_price=30,
        order_date=date(2026, 6, 12),
    )

    response = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": stale_gap["line_id"],
            "version": stale_gap["version"],
            "unit_cost_ex_tax": "99.00",
            "evidence": "人工证据不应覆盖后到采购",
            "reason": "保存前发现系统证据",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["manual_applied"] is False
    assert response.json()["resolution"] == "automatic_evidence"
    assert response.json()["cost_source"] == "purchase_window"
    assert response.json()["unit_cost"] == "30.00"
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, stale_gap["line_id"])
    assert line.manual_unit_cost is None
    assert line.manual_evidence is None
    assert line.cost_source == "purchase_window"
    assert line.version == 2
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before + 1
    audit = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).one()
    assert audit.before_json["cost_source"] is None
    assert audit.after_json["cost_source"] == "purchase_window"


def test_recompute_upgrades_sales_window_to_purchase_window(db):
    project = _project(db, project_id="project-sales-to-purchase")
    client = _client(db, username="sales_to_purchase_admin")
    part = _create_gap(db, project_id=project.project_id, suffix="SALES-TO-PURCHASE")
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"
    _add_late_sale(
        db,
        part=part,
        suffix="SALES-FIRST",
        unit_price_inc_tax=113,
    )
    first = client.post(path, json={"reason": "先按销售窗口匹配"})
    assert first.status_code == 200, first.text
    assert first.json()["resolved"] == 1
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-SALES-TO-PURCHASE")
    assert line.cost_source == "sales_window"
    assert line.unit_cost == 100
    assert line.version == 2

    _add_late_purchase(db, part=part, suffix="PURCHASE-LATER", unit_price=70)
    upgraded = client.post(path, json={"reason": "后到采购证据优先于销售"})

    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["resolved"] == 1
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, line.issue_line_id)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost == 70
    assert line.version == 3
    audits = db.query(MaintenanceProjectOperationAudit).filter_by(
        project_id=project.project_id,
        entity_id=line.issue_line_id,
        action="auto_recompute",
    ).order_by(MaintenanceProjectOperationAudit.id).all()
    assert [row.after_json["cost_source"] for row in audits] == [
        "sales_window",
        "purchase_window",
    ]
    assert [row.reason for row in audits] == [
        "先按销售窗口匹配",
        "后到采购证据优先于销售",
    ]
    assert all(
        row.before_json["as_of"] == business_today().isoformat()
        and row.after_json["as_of"] == business_today().isoformat()
        for row in audits
    )


def test_recompute_updates_purchase_weight_when_new_window_sample_arrives(db):
    project = _project(db, project_id="project-purchase-weight-refresh")
    client = _client(db, username="purchase_weight_refresh_admin")
    part = _create_gap(db, project_id=project.project_id, suffix="WEIGHT-REFRESH")
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"
    _add_late_purchase(db, part=part, suffix="WEIGHT-A", unit_price=20)
    first = client.post(path, json={"reason": "首次采购窗口加权"})
    assert first.status_code == 200, first.text
    state_after_first = db.get(MaintenanceProjectWorkbookState, project.project_id)
    first_revision = state_after_first.revision
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-WEIGHT-REFRESH")
    assert line.unit_cost == 20
    assert line.reference_sample_count == 1

    _add_late_purchase(
        db,
        part=part,
        suffix="WEIGHT-B",
        unit_price=40,
        order_date=date(2026, 6, 14),
    )
    updated = client.post(path, json={"reason": "纳入新到采购样本重算均价"})

    assert updated.status_code == 200, updated.text
    assert updated.json()["resolved"] == 1
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, line.issue_line_id)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost == 30
    assert line.reference_sample_count == 2
    assert line.version == 3
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == first_revision + 1


def test_recompute_upgrades_manual_cost_to_automatic_purchase(db):
    project = _project(db, project_id="project-manual-to-purchase")
    client = _client(db, username="manual_to_purchase_admin")
    part = _create_gap(db, project_id=project.project_id, suffix="MANUAL-TO-PURCHASE")
    filled = client.patch(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps",
        json={
            "line_id": "issue-line-MANUAL-TO-PURCHASE",
            "version": 1,
            "unit_cost_ex_tax": "12.50",
            "evidence": "已审批人工价格证据",
            "reason": "暂时人工补价",
        },
    )
    assert filled.status_code == 200, filled.text
    assert filled.json()["cost_source"] == "manual"
    _add_late_purchase(db, part=part, suffix="AUTO-AFTER-MANUAL", unit_price=18)

    upgraded = client.post(
        f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute",
        json={"reason": "后到采购证据替换人工口径"},
    )

    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["resolved"] == 1
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-MANUAL-TO-PURCHASE")
    assert line.cost_source == "purchase_window"
    assert line.unit_cost == 18
    assert line.manual_unit_cost == 12.5
    assert line.manual_evidence == "已审批人工价格证据"
    assert line.version == 3


def test_recompute_never_downgrades_purchase_to_sales_when_evidence_disappears(db):
    project = _project(db, project_id="project-no-cost-downgrade")
    client = _client(db, username="no_cost_downgrade_admin")
    part = _create_gap(db, project_id=project.project_id, suffix="NO-DOWNGRADE")
    path = f"/api/maintenance/projects/stable/{project.project_id}/cost-gaps/recompute"
    purchase = _add_late_purchase(db, part=part, suffix="STRONG-PURCHASE", unit_price=60)
    _add_late_sale(db, part=part, suffix="WEAK-SALE", unit_price_inc_tax=113)
    assert client.post(path, json={"reason": "首次采用采购"}).json()["resolved"] == 1
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, "issue-line-NO-DOWNGRADE")
    version_before = line.version
    state = db.get(MaintenanceProjectWorkbookState, project.project_id)
    revision_before = state.revision
    purchase.data_status = "已作废"
    db.commit()

    repeated = client.post(path, json={"reason": "弱证据不得覆盖采购"})

    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["resolved"] == 0
    db.expire_all()
    line = db.get(MaintenanceSiteIssueLine, line.issue_line_id)
    assert line.cost_source == "purchase_window"
    assert line.unit_cost == 60
    assert line.version == version_before
    assert db.get(MaintenanceProjectWorkbookState, project.project_id).revision == revision_before


def test_recompute_batches_evidence_reads_and_does_not_lock_every_issue_line(db):
    one = _project(db, project_id="project-recompute-scale-one")
    forty = _project(db, project_id="project-recompute-scale-forty")
    client = _client(db, username="recompute_scale_admin")
    _add_unresolved_lines(db, project_id=one.project_id, count=1)
    _add_unresolved_lines(db, project_id=forty.project_id, count=40)

    one_payload, one_query_count, one_statements = _count_recompute_queries(
        db,
        client,
        project_id=one.project_id,
    )
    forty_payload, forty_query_count, forty_statements = _count_recompute_queries(
        db,
        client,
        project_id=forty.project_id,
    )

    assert one_payload["resolved"] == forty_payload["resolved"] == 0
    assert one_payload["remaining"] == 1
    assert forty_payload["remaining"] == 40
    assert forty_query_count <= one_query_count + 1
    issue_line_reads = [
        statement
        for statement in [*one_statements, *forty_statements]
        if "maintenance_site_issue_line" in statement.lower()
    ]
    assert issue_line_reads
    assert all("for update" not in statement.lower() for statement in issue_line_reads)
