"""D-0051: list grain, source evidence, matching products and permissions."""
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.dimensions import DimSupplier
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import (
    MaintenanceProject, MaintenanceProjectContract, MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import MaintenanceProjectExpenseAttribution
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysUser
from app.security import UserContext, is_scoped_maintenance
from app.services.sales_analytics import recorded_expenses
from tests.boss_board_helpers import client_for
from tests.test_purchase_analytics_api import ds  # noqa: F401 -- shared contract fixture
from tests.test_sales_analytics_api import _client, seeded  # noqa: F401

D = Decimal
SA = "/api/sales/analytics"
PA = "/api/purchases/analytics"


def _contract(db, order_no):
    project = MaintenanceProject(project_id="feedback-project", project_code="FEEDBACK",
        display_name="实际业务项目名称", lifecycle_status="active")
    db.add(project)
    db.flush()
    contract = MaintenanceProjectContract(project_contract_id="feedback-contract",
        project_id=project.project_id, contract_id="feedback-source", contract_no=order_no,
        status_mapping_state="mapped", status_mapping_version="test-v1", included_in_total=True,
        effective_from=date(2020, 1, 1), source="test")
    db.add(contract)
    db.flush()
    return project, contract


def test_purchase_lookup_uses_same_scope_and_current_batch_products(db, ds):
    supplier = DimSupplier(name_raw="供%_应商", name_normalized="供%_应商")
    db.add(supplier)
    db.flush()
    ds.poa.supplier_id = supplier.id
    _contract(db, ds.poa.linked_sales_order_no)
    c = client_for(db, username="feedback-purchase", role="boss")
    filters = {"supplier": "%_", "order_no": "CG-PA", "linked_sales_order_no": "XS-001"}
    summary = c.get(PA + "/summary", params=filters)
    assert summary.status_code == 200, summary.text
    assert summary.json()["kpi"]["orders"] == 1
    assert summary.json()["kpi"]["amount_paid"] == 1130
    response = c.get(PA + "/orders", params=filters)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    row = data["items"][0]
    assert row["supplier"] == "供%_应商"
    assert row["linked_sales_order_no"] == "XS-001"
    assert row["project_names"] == ["实际业务项目名称"]
    assert [p["pn_std"] for p in row["products"]] == ["PN-DQ", "PN-FL"]
    assert row["amount_paid"] == 1130
    matched = c.get(PA + "/orders", params={**filters, "category": "电器件"}).json()
    assert [p["pn_std"] for p in matched["items"][0]["products"]] == ["PN-DQ"]
    assert matched["items"][0]["amount_paid"] == 500
    assert c.get(PA + "/orders", params={"supplier": "不存在"}).json()["total"] == 0
    detail = c.get(f"{PA}/orders/{ds.poa.id}").json()
    assert detail["project_names"] == row["project_names"]
    assert detail["supplier"] == row["supplier"]


def test_purchase_supplier_filter_denied_and_value_hidden(db, ds):
    supplier = DimSupplier(name_raw="隐藏供应商", name_normalized="隐藏供应商")
    db.add(supplier)
    db.flush()
    ds.poa.supplier_id = supplier.id
    c = client_for(db, username="feedback-no-supplier", role="boss", overrides={"data_supplier": False})
    for endpoint in ("summary", "orders"):
        assert c.get(f"{PA}/{endpoint}", params={"supplier": "隐藏"}).status_code == 403
    row = c.get(PA + "/orders", params={"order_no": "CG-PA"}).json()["items"][0]
    assert row["supplier"] is None


def test_sales_products_are_paired_without_expanding_orders(db, seeded):
    order = seeded["orders"][0]
    part_id = db.scalar(select(FSalesLine.part_id).where(FSalesLine.order_id == order.id).limit(1))
    db.add(FSalesLine(raw_line_id="feedback-product", order_id=order.id, line_no=90,
        part_id=part_id,
        pn_std="PN-SECOND", category_major="第二类", qty=D("2"), unit_price=D("56.50"),
        line_amount=D("113"), counts_revenue=False, import_batch_id=order.import_batch_id))
    c = _client(db, "feedback-sales-products", role="boss")
    data = c.get(SA + "/orders").json()
    assert data["total"] == 5
    row = next(item for item in data["items"] if item["order_id"] == order.id)
    product = next(p for p in row["products"] if p["pn"] == "PN-SECOND")
    assert product["unit_price"] == 56.5
    filtered = c.get(SA + "/orders", params={"category": "第二类"}).json()
    assert filtered["total"] == 1
    assert len(filtered["items"][0]["products"]) == 1
    assert filtered["items"][0]["amount_inc_tax"] == 113
    export = c.get(SA + "/orders/export", params={"category": "第二类"})
    assert export.status_code == 200, export.text
    assert "PN-SECOND" in export.text and "56.5" in export.text
    hidden = _client(db, "feedback-sales-no-price", role="boss", overrides={"data_purchase_cost": False})
    hidden_rows = hidden.get(SA + "/orders").json()["items"]
    assert all(p["unit_price"] is None for r in hidden_rows for p in r["products"])


def _expenses(db, seeded):
    order = seeded["orders"][0]
    project, contract = _contract(db, order.order_no)
    raw = FProjectExpense(raw_line_id="feedback-expense", bxd_no="BXD-FEEDBACK",
        linked_sales_order_no=order.order_no, data_status="已结束", expense_date=date(2026, 1, 1),
        amount=D("100"), amount_ex_tax=D("100"), amount_inc_tax=D("113"),
        import_batch_id=order.import_batch_id)
    db.add(raw)
    db.flush()
    for key, raw_id, amount in [("source-copy", raw.raw_line_id, "100"), ("manual", None, "-10")]:
        db.add(MaintenanceProjectExpenseAttribution(expense_id=key,
            project_id=project.project_id, project_contract_id=contract.project_contract_id,
            raw_expense_line_id=raw_id, expense_ref=key, expense_date=date(2026, 1, 2),
            amount_ex_tax=D(amount), amount_inc_tax=D(amount) * D("1.13"),
            raw_status="已结束", status_mapping_state="mapped", normalized_status="approved",
            status_mapping_version="test-v1", ownership_mapping_state="mapped"))
    # 相同XSDD多个销售身份不能使同一费用被重复累加。
    db.add(FSalesOrder(raw_order_id="feedback-duplicate", order_no=order.order_no,
        data_status="已生效", import_batch_id=order.import_batch_id))
    db.flush()


def test_recorded_expenses_deduplicate_sources_and_order_numbers(db, seeded):
    _expenses(db, seeded)
    c = _client(db, "feedback-expense-boss", role="boss")
    response = c.get(SA + "/summary")
    assert response.status_code == 200, response.text
    evidence = response.json()["recorded_expenses"]
    assert evidence["total"] == 2
    assert evidence["amount_inc_tax"] == 101.7
    detail = c.get(SA + "/expenses", params={"page_size": 1}).json()
    assert detail["total"] == 2 and len(detail["items"]) == 1
    next_page = c.get(SA + "/expenses", params={"page_size": 1, "page": 2}).json()
    assert detail["items"][0]["id"] != next_page["items"][0]["id"]
    empty = c.get(SA + "/summary", params={"business_type": "不存在"}).json()["recorded_expenses"]
    assert empty["status"] == "no_evidence" and empty["amount_inc_tax"] is None


@pytest.mark.parametrize("permission", ["data_profit", "data_purchase_cost"])
def test_recorded_expenses_do_not_leak_count_without_amount_permission(db, seeded, permission):
    _expenses(db, seeded)
    c = _client(db, "feedback-hidden-expenses", role="boss", overrides={permission: False})
    data = c.get(SA + "/summary").json()["recorded_expenses"]
    assert data["status"] == "restricted" and data["total"] is None
    assert data["amount_inc_tax"] is None and data["items"] == []
    assert c.get(SA + "/expenses").status_code == 403


def test_recorded_expenses_boss_remains_full_scope(db, seeded):
    _expenses(db, seeded)
    c = _client(db, "feedback-expenses-full-boss", role="boss",
                overrides={"own_maintenance_projects_only": True})
    data = c.get(SA + "/expenses").json()
    # admin/boss 恒全范围是全站既有规则，不能让新费用入口另造相反口径。
    assert data["total"] == 2 and data["amount_inc_tax"] == 101.7


def test_recorded_expenses_obey_project_scope(db, seeded):
    _expenses(db, seeded)
    username = "feedback-expenses-scoped-manager"
    c = _client(db, username, role="maintenance_manager", overrides={
        "own_maintenance_projects_only": True, "page_sales_analysis": True,
        "data_purchase_cost": True, "data_profit": True,
    })
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    ctx = UserContext(user_id=username, role=user.role, permissions=user.permissions,
                      is_authenticated=True)
    assert is_scoped_maintenance(ctx)
    # 销售分析的角色硬门仍拒绝非admin/boss，即使单独授予页面权限。
    assert c.get(SA + "/expenses").status_code == 403
    # 服务层也必须尊重项目范围，以免未来被其他入口复用时越权。
    data = recorded_expenses(db, ctx, with_items=True)
    assert data["total"] == 0 and data["items"] == []
    assert data["amount_inc_tax"] is None
    assignment = MaintenanceProjectUserAssignment(
        assignment_id="feedback-expense-assignment", project_id="feedback-project",
        responsibility_type="viewer", user_id=user.id, assigned_by="test",
        assignment_reason="费用范围回归",
    )
    db.add(assignment)
    db.flush()
    visible = recorded_expenses(db, ctx, with_items=True)
    assert visible["total"] == 2 and visible["amount_inc_tax"] == 101.7
    assert {row["source"] for row in visible["items"]} == {"source", "manual"}
    assignment.archived_at = datetime.now(UTC)
    assignment.archived_by = "test"
    assignment.archive_reason = "撤销费用查看范围"
    assignment.version += 1
    db.flush()
    revoked = recorded_expenses(db, ctx, with_items=True)
    assert revoked["total"] == 0 and revoked["items"] == []
    assert revoked["amount_inc_tax"] is None
