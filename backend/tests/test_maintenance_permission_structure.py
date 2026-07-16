"""维保页结构性权限：隐藏金额之外，分类、排序、筛选与 Agent 截断也不能泄漏。"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import permissions, security
from app.agent import tools
from app.auth import hash_password
from app.etl import loader
from app.main import app
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_cost
from tests import factories as f


@pytest.fixture()
def maintenance_permission_data(db):
    batch = SysImportBatch(
        filename="maintenance-permission.xlsx", file_type="maintenance",
        file_hash="maintenance-permission-structure",
    )
    db.add(batch)
    db.flush()

    specs = [
        # 项目名字母序 A/M/Z 与成本序 Z/M/A、利润状态序 Z/M/A 相反；
        # 出库日期序 A/M/Z，用来证明受限结果没有沿用隐藏分类/金额排序。
        ("A", "A-low", date(2026, 3, 30), "100"),
        ("M", "M-mid", date(2026, 3, 20), "950"),
        ("Z", "Z-high", date(2026, 3, 10), "1200"),
    ]
    for tag, project, out_date, cost in specs:
        loader.load(db, f.sales_result(
            {f"S-{tag}": f.sales_head(
                f"S-{tag}", order_no=f"XS-{tag}", amount_ex_tax=Decimal("1000"),
            )},
            [f.sales_line(f"S-{tag}", f"SL-{tag}", f"PN-{tag}", qty="1", price="1")],
        ), batch.id, date(2026, 6, 1))
        loader.load(db, f.purchase_result(
            {f"P-{tag}": f.purchase_head(
                f"P-{tag}", on=date(2026, 3, 5), source_type="维保需求",
                linked_maintenance_order_no=f"WBDD-{tag}",
            )},
            [f.purchase_line(
                f"P-{tag}", f"PL-{tag}", f"PN-{tag}", qty="1", price=cost,
            )],
        ), batch.id, date(2026, 6, 1))
        loader.load(db, f.maintenance_result(
            {f"M-{tag}": f.maintenance_head(
                f"M-{tag}", order_no=f"WBDD-{tag}", on=out_date,
                sales_order=f"XS-{tag}", project=project,
            )},
            [f.maintenance_line(
                f"M-{tag}", f"ML-{tag}", f"PN-{tag}", qty="1",
            )],
        ), batch.id, date(2026, 6, 1))
    db.commit()
    maintenance_cost.recompute(db)
    return specs


def _limited_client(db, username: str, *, cost: bool, profit: bool) -> TestClient:
    base = permissions.effective("readonly", None)
    overrides = {
        "page_maintenance": True,
        "data_purchase_cost": cost,
        "data_profit": profit,
    }
    effective = permissions.effective_from_snapshot(base, overrides)
    assert permissions.combo_errors(effective) == []
    db.add(SysUser(
        username=username, role="readonly", display_name=username,
        password_hash=hash_password("pw123456"), is_active=True,
        template_code="readonly", template_version=1, template_perms=base,
        perm_overrides=overrides, permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def _ctx(*, cost: bool, profit: bool) -> security.UserContext:
    perms = permissions.effective("readonly", {
        "page_maintenance": True,
        "data_purchase_cost": cost,
        "data_profit": profit,
    })
    return security.UserContext(
        user_id="limited", role="readonly", permissions=perms, is_authenticated=True,
    )


def test_api_profit_blind_board_removes_status_filter_counts_and_ranking(
    db, maintenance_permission_data,
):
    client = _limited_client(db, "maint-profit-blind", cost=True, profit=False)
    response = client.get("/api/maintenance/board", params={"status": "red"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["profit_restricted"] is True
    assert data["ranking_restricted"] is True
    assert data["effective_sort"] == "last_out"
    assert data["status_filter_applied"] is False
    assert "status_counts" not in data
    assert len(data["rows"]) == 3                 # red 筛选被忽略，不泄漏命中集合
    assert all("status" not in row for row in data["rows"])
    assert [row["contract"] for row in data["rows"]] == ["XS-A", "XS-M", "XS-Z"]


def test_api_cost_blind_projects_use_neutral_sort_before_any_consumer_truncation(
    db, maintenance_permission_data,
):
    client = _limited_client(db, "maint-cost-blind", cost=False, profit=False)
    response = client.get("/api/maintenance/projects")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ranking_restricted"] is True
    assert data["effective_sort"] == "project"
    assert [row["project"] for row in data["rows"]] == ["A-low", "M-mid", "Z-high"]
    assert all(row["cost_total"] is None for row in data["rows"])


def test_agent_uses_same_board_collapse_and_neutral_project_truncation(
    db, maintenance_permission_data,
):
    profit_blind_ctx = _ctx(cost=True, profit=False)
    profit_blind = tools.dispatch(
        db, "get_maintenance_board", {"status": "red"},
        profit_blind_ctx,
    )
    assert profit_blind["profit_restricted"] is True
    assert "status_counts" not in profit_blind
    assert len(profit_blind["rows"]) == 3
    assert all("status" not in row for row in profit_blind["rows"])

    cost_blind = tools.dispatch(
        db, "get_maintenance_projects", {"top": 2},
        _ctx(cost=False, profit=False),
    )
    assert cost_blind["ranking_restricted"] is True
    assert cost_blind["effective_sort"] == "project"
    assert [row["project"] for row in cost_blind["rows"]] == ["A-low", "M-mid"]
    assert all(row["cost_total"] is None for row in cost_blind["rows"])
    assert "按项目名" in cost_blind["note"] and "成本最高" not in cost_blind["note"]
    skills = tools.dispatch(db, "list_skills", {}, profit_blind_ctx)["skills"]
    assert "maintenance_health_check" not in {item["skill"] for item in skills}
