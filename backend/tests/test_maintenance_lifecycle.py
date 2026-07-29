"""DEV-12：维保项目生命周期状态的统一契约。"""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.auth import hash_password
from app.business_time import business_today
from app.etl import loader
from app.main import app
from app.models.maintenance import FMaintenanceLine
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_cost
from tests import factories as f


@pytest.fixture()
def lifecycle_data(db):
    """五个同级聚合：昨天、今天、未来、空日期、部分空日期。"""
    as_of = business_today()
    batch = SysImportBatch(
        filename="maintenance-lifecycle.xlsx",
        file_type="maintenance",
        file_hash="maintenance-lifecycle",
    )
    db.add(batch)
    db.flush()

    specs = [
        ("END", "项目-结束", "XS-END", date(2026, 3, 1), as_of - timedelta(days=1)),
        ("TODAY", "项目-今天", "XS-TODAY", date(2026, 3, 2), as_of),
        ("FUTURE", "项目-未来", "XS-FUTURE", date(2026, 4, 1), as_of + timedelta(days=1)),
        ("MISS", "项目-缺失", "XS-MISS", date(2026, 3, 4), None),
        ("MIX-A", "项目-混合", "XS-MIX", date(2026, 3, 5), as_of + timedelta(days=30)),
        ("MIX-B", "项目-混合", "XS-MIX", date(2026, 3, 6), None),
    ]
    orders = {}
    lines = []
    for i, (tag, project, contract, out_date, maint_end) in enumerate(specs, 1):
        orders[tag] = f.maintenance_head(
            tag,
            order_no=f"WBDD-{tag}",
            on=out_date,
            project=project,
            sales_order=contract,
            maint_end=maint_end,
        )
        lines.append(f.maintenance_line(tag, f"ML-{tag}", f"PN-{tag}", qty="1"))
    loader.load(db, f.maintenance_result(orders, lines), batch.id, as_of)
    db.flush()

    # 直接使用已落库的正式成本结果，验证生命周期筛选不改变金额口径。
    for i, line in enumerate(
        db.execute(select(FMaintenanceLine).order_by(FMaintenanceLine.id)).scalars(), 1
    ):
        line.unit_cost = Decimal(i * 10)
        line.cost_amount = Decimal(i * 10)
        line.cost_source = "direct"
        line.cost_tax_basis = "ex"
        line.confidence = "high"
    db.commit()
    return as_of


def _rows_by_project(data: dict) -> dict[str, dict]:
    return {row["project"]: row for row in data["rows"]}


def test_projects_lifecycle_boundaries_and_missing_dominates(db, lifecycle_data):
    as_of = lifecycle_data
    data = maintenance_cost.projects_aggregate(
        db, lifecycle="all", as_of=as_of,
    )
    rows = _rows_by_project(data)

    assert data["as_of"] == as_of.isoformat()
    assert data["lifecycle_filter"] == "all"
    assert data["lifecycle_counts"] == {"ongoing": 2, "ended": 1, "missing": 2}
    assert (rows["项目-结束"]["lifecycle_status"], rows["项目-结束"]["maint_end"]) == (
        "ended", (as_of - timedelta(days=1)).isoformat(),
    )
    assert rows["项目-今天"]["lifecycle_status"] == "ongoing"
    assert rows["项目-未来"]["lifecycle_status"] == "ongoing"
    assert (rows["项目-缺失"]["lifecycle_status"], rows["项目-缺失"]["maint_end"]) == (
        "missing", None,
    )
    # 同项目任一底层单期限为空，不能被另一张有日期的单掩盖。
    assert (rows["项目-混合"]["lifecycle_status"], rows["项目-混合"]["maint_end"]) == (
        "missing", None,
    )


def test_projects_default_ongoing_counts_precede_lifecycle_filter_and_keep_amounts(
    db, lifecycle_data,
):
    as_of = lifecycle_data
    all_data = maintenance_cost.projects_aggregate(db, lifecycle="all", as_of=as_of)
    ongoing = maintenance_cost.projects_aggregate(db, lifecycle="ongoing", as_of=as_of)

    assert {row["project"] for row in ongoing["rows"]} == {"项目-今天", "项目-未来"}
    assert ongoing["lifecycle_filter"] == "ongoing"
    assert ongoing["lifecycle_counts"] == all_data["lifecycle_counts"]
    all_rows = _rows_by_project(all_data)
    for row in ongoing["rows"]:
        assert row["cost_total"] == all_rows[row["project"]]["cost_total"]
        assert row["coverage_pct"] == all_rows[row["project"]]["coverage_pct"]


def test_lifecycle_filter_preserves_cross_status_shared_contract_warning(db):
    as_of = business_today()
    batch = SysImportBatch(
        filename="maintenance-lifecycle-shared.xlsx",
        file_type="maintenance",
        file_hash="maintenance-lifecycle-shared",
        status="success",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.sales_result(
            {"S": f.sales_head(
                "S", order_no="XS-SHARED", amount_ex_tax=Decimal("1000"),
                tax_rate=Decimal("0.13"),
            )},
            [f.sales_line("S", "SL", "PN-SHARED", qty="1", price="1130")],
        ),
        batch.id,
        as_of,
    )
    orders = {
        "ONGOING": f.maintenance_head(
            "ONGOING", project="项目-仍在维保", sales_order="XS-SHARED",
            maint_end=as_of,
        ),
        "ENDED": f.maintenance_head(
            "ENDED", project="项目-已经结束", sales_order="XS-SHARED",
            maint_end=as_of - timedelta(days=1),
        ),
        "OTHER": f.maintenance_head(
            "OTHER", project="项目-其他合同", sales_order="XS-OTHER",
            maint_end=as_of - timedelta(days=1),
        ),
    }
    loader.load(
        db,
        f.maintenance_result(orders, [
            f.maintenance_line("ONGOING", "ML-ONGOING", "PN-O"),
            f.maintenance_line("ENDED", "ML-ENDED", "PN-E"),
            f.maintenance_line("OTHER", "ML-OTHER", "PN-X"),
        ]),
        batch.id,
        as_of,
    )
    db.flush()
    for line in db.execute(select(FMaintenanceLine)).scalars():
        line.cost_amount = Decimal("100")
        line.cost_source = "direct"
        line.cost_tax_basis = "ex"
        line.confidence = "high"
    db.commit()

    default = maintenance_cost.projects_aggregate(db, lifecycle="ongoing", as_of=as_of)
    assert [row["project"] for row in default["rows"]] == ["项目-仍在维保"]
    row = default["rows"][0]
    assert row["contract_shared"] is True
    assert row["contract_amount"] == 1130.0

    all_rows = _rows_by_project(
        maintenance_cost.projects_aggregate(db, lifecycle="all", as_of=as_of)
    )
    assert all_rows["项目-仍在维保"]["contract_shared"] is True
    assert all_rows["项目-已经结束"]["contract_shared"] is True
    assert all_rows["项目-仍在维保"]["contract_amount"] == row["contract_amount"]

    full_board = maintenance_cost.board(db, lifecycle="all", as_of=as_of)
    shared_full = next(r for r in full_board["rows"] if r["contract"] == "XS-SHARED")
    searched_board = maintenance_cost.board(
        db, q_text="仍在维保", lifecycle="all", as_of=as_of,
    )
    assert [r["contract"] for r in searched_board["rows"]] == ["XS-SHARED"]
    shared_searched = searched_board["rows"][0]
    assert {p["project"] for p in shared_searched["projects"]} == {
        "项目-仍在维保", "项目-已经结束",
    }
    assert shared_searched["spent_parts"] == shared_full["spent_parts"] == 200.0
    assert shared_searched["status"] == shared_full["status"]


def test_projects_search_and_outbound_date_filter_precede_lifecycle_counts(db, lifecycle_data):
    as_of = lifecycle_data
    searched = maintenance_cost.projects_aggregate(
        db, q_text="混合", lifecycle="all", as_of=as_of,
    )
    assert searched["lifecycle_counts"] == {"ongoing": 0, "ended": 0, "missing": 1}

    through_march = maintenance_cost.projects_aggregate(
        db, date_to=date(2026, 3, 31), lifecycle="all", as_of=as_of,
    )
    assert through_march["lifecycle_counts"] == {"ongoing": 1, "ended": 1, "missing": 2}
    assert "项目-未来" not in _rows_by_project(through_march)


def test_board_uses_same_contract_lifecycle_and_default(db, lifecycle_data):
    as_of = lifecycle_data
    all_data = maintenance_cost.board(db, lifecycle="all", as_of=as_of)
    rows = {row["contract"]: row for row in all_data["rows"]}
    assert all_data["lifecycle_counts"] == {"ongoing": 2, "ended": 1, "missing": 2}
    assert rows["XS-END"]["lifecycle_status"] == "ended"
    assert rows["XS-TODAY"]["lifecycle_status"] == "ongoing"
    assert rows["XS-MIX"]["lifecycle_status"] == "missing"
    assert rows["XS-MIX"]["maint_end"] is None

    default = maintenance_cost.board(db, lifecycle="ongoing", as_of=as_of)
    assert {row["contract"] for row in default["rows"]} == {"XS-TODAY", "XS-FUTURE"}
    assert default["lifecycle_counts"] == all_data["lifecycle_counts"]
    assert default["as_of"] == as_of.isoformat()


def test_board_combines_outbound_date_expense_gate_and_lifecycle_filters(
    db,
    lifecycle_data,
):
    data = maintenance_cost.board(
        db,
        date_to=date(2026, 3, 31),
        status="expense_data_unavailable",
        lifecycle="missing",
        as_of=lifecycle_data,
    )
    assert {row["contract"] for row in data["rows"]} == {"XS-MISS", "XS-MIX"}
    assert data["lifecycle_counts"] == {"ongoing": 1, "ended": 1, "missing": 2}
    assert data["status_counts"] == {"red": 0, "yellow": 0, "green": 0, "no_budget": 0}
    assert data["decision_status_counts"]["expense_data_unavailable"] == 2


def test_board_project_search_matches_projects_scope(db, lifecycle_data):
    projects = maintenance_cost.projects_aggregate(
        db, q_text="混合", lifecycle="all", as_of=lifecycle_data,
    )
    board = maintenance_cost.board(
        db, q_text="混合", lifecycle="all", as_of=lifecycle_data,
    )
    assert {row["project"] for row in projects["rows"]} == {"项目-混合"}
    assert {row["contract"] for row in board["rows"]} == {"XS-MIX"}
    assert board["lifecycle_counts"] == {"ongoing": 0, "ended": 0, "missing": 1}


def test_board_search_does_not_merge_unrelated_unlinked_projects(db):
    as_of = business_today()
    batch = SysImportBatch(
        filename="maintenance-unlinked-search.xlsx",
        file_type="maintenance",
        file_hash="maintenance-unlinked-search",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "TARGET": f.maintenance_head(
                    "TARGET", project="无合同-目标项目", sales_order=None,
                    maint_end=as_of,
                ),
                "OTHER": f.maintenance_head(
                    "OTHER", project="无合同-其他项目", sales_order=None,
                    maint_end=as_of - timedelta(days=1),
                ),
            },
            [
                f.maintenance_line("TARGET", "ML-TARGET", "PN-TARGET"),
                f.maintenance_line("OTHER", "ML-OTHER", "PN-OTHER"),
            ],
        ),
        batch.id,
        as_of,
    )
    db.commit()

    result = maintenance_cost.board(
        db, q_text="目标项目", lifecycle="all", as_of=as_of,
    )
    assert len(result["rows"]) == 1
    assert result["rows"][0]["contract"] is None
    assert [p["project"] for p in result["rows"][0]["projects"]] == ["无合同-目标项目"]
    assert result["lifecycle_counts"] == {"ongoing": 1, "ended": 0, "missing": 0}


def test_business_today_uses_shanghai_boundary_not_container_timezone():
    # UTC 16:00 已是北京时间次日 00:00；容器即使保持 UTC 也必须跨业务日。
    assert business_today(datetime(2026, 7, 15, 15, 59, tzinfo=timezone.utc)) == date(2026, 7, 15)
    assert business_today(datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)) == date(2026, 7, 16)


def test_lifecycle_filters_keep_query_count_constant(db, lifecycle_data):
    engine = db.get_bind()

    def statement_count(callable_):
        count = 0

        def before_execute(_conn, _cursor, statement, _params, _context, _many):
            nonlocal count
            if statement.lstrip().upper().startswith("SELECT"):
                count += 1

        event.listen(engine, "before_cursor_execute", before_execute)
        try:
            callable_()
        finally:
            event.remove(engine, "before_cursor_execute", before_execute)
        return count

    project_all = statement_count(
        lambda: maintenance_cost.projects_aggregate(
            db, lifecycle="all", as_of=lifecycle_data,
        )
    )
    project_one = statement_count(
        lambda: maintenance_cost.projects_aggregate(
            db, q_text="结束", lifecycle="ended", as_of=lifecycle_data,
        )
    )
    board_all = statement_count(
        lambda: maintenance_cost.board(db, lifecycle="all", as_of=lifecycle_data)
    )
    board_one = statement_count(
        lambda: maintenance_cost.board(db, lifecycle="missing", as_of=lifecycle_data)
    )
    assert project_all == project_one <= 2
    # 合同费用快照完整性是独立证据水位，新增一条固定查询但不随合同数增长。
    assert board_all == board_one <= 4


def _admin_client(db) -> TestClient:
    db.add(SysUser(
        username="maint_lifecycle_admin",
        role="admin",
        is_active=True,
        password_hash=hash_password("pw123456"),
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "maint_lifecycle_admin", "password": "pw123456"},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})
    return client


def test_api_defaults_validation_and_export_share_lifecycle_contract(db, lifecycle_data):
    client = _admin_client(db)
    today = business_today()

    projects = client.get("/api/maintenance/projects")
    assert projects.status_code == 200, projects.text
    pdata = projects.json()
    assert pdata["as_of"] == today.isoformat()
    assert pdata["lifecycle_filter"] == "ongoing"
    assert all(row["lifecycle_status"] == "ongoing" for row in pdata["rows"])

    board = client.get("/api/maintenance/board")
    assert board.status_code == 200, board.text
    assert board.json()["lifecycle_filter"] == "ongoing"
    assert all(row["lifecycle_status"] == "ongoing" for row in board.json()["rows"])

    searched_projects = client.get(
        "/api/maintenance/projects", params={"q": "混合", "lifecycle": "all"},
    )
    searched_board = client.get(
        "/api/maintenance/board", params={"q": "混合", "lifecycle": "all"},
    )
    assert {row["project"] for row in searched_projects.json()["rows"]} == {"项目-混合"}, searched_projects.text
    assert {row["contract"] for row in searched_board.json()["rows"]} == {"XS-MIX"}, searched_board.text

    invalid = client.get("/api/maintenance/projects", params={"lifecycle": "unknown"})
    assert invalid.status_code == 422

    exported = client.get("/api/maintenance/export", params={"lifecycle": "all"})
    assert exported.status_code == 200, exported.text
    table = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert table[0][:3] == ["项目", "期限状态", "维保终止日期"]
    assert len(table) == 6
    by_project = {row[0]: row for row in table[1:]}
    assert by_project["项目-结束"][1:3] == [
        "已结束", (lifecycle_data - timedelta(days=1)).isoformat(),
    ]
    assert by_project["项目-混合"][1:3] == ["期限缺失", ""]

    default_export = client.get("/api/maintenance/export")
    default_table = list(csv.reader(io.StringIO(default_export.content.decode("utf-8-sig"))))
    assert {row[0] for row in default_table[1:]} == {"项目-今天", "项目-未来"}
    assert {row[1] for row in default_table[1:]} == {"进行中"}


def test_lifecycle_filter_does_not_bypass_existing_authentication(db, lifecycle_data):
    anonymous = TestClient(app)
    assert anonymous.get(
        "/api/maintenance/projects", params={"lifecycle": "all"},
    ).status_code == 401
    assert anonymous.get(
        "/api/maintenance/board", params={"lifecycle": "missing"},
    ).status_code == 401
    assert anonymous.get(
        "/api/maintenance/export", params={"lifecycle": "ended"},
    ).status_code == 401
