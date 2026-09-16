"""PN 排名全字段筛选（2026-09-16）：项目/客户/销售/单号/需求类型/仓库/取价来源。

覆盖：筛选在聚合前收窄行集/汇总/坏件佐证、行键范围只收不放、ILIKE 转义、
需求类型码校验 422、取价来源行级四分类、filter_options 去重排序与范围。
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.system import SysUser
from app.services import maintenance_analytics as ana
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _line, _part, _project
from tests.test_maintenance_analytics_business_type import bad_return

URL = "/api/maintenance/analytics/pn-ranking"
OPTIONS_URL = "/api/maintenance/analytics/filter-options"


def _set_order(db, line, **fields) -> FMaintenanceOrder:
    order = db.get(FMaintenanceOrder, line.order_id)
    for name, value in fields.items():
        setattr(order, name, value)
    return order


def _assign(db, project, username: str) -> None:
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id=str(uuid.uuid4()),
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            version=1,
            assigned_by="test",
            assignment_reason="test",
        )
    )
    db.commit()


def test_project_filter_narrows_rows_summary_and_rkd(db):
    proj_a = _project(db, "PF-A")
    proj_b = _project(db, "PF-B")
    part = _part(db, "FILT-PF", "过滤备件")
    _line(
        db,
        proj_a,
        part,
        order_no="WBDD-PF-A",
        order_date=date(2026, 5, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("200.00"),
    )
    _line(
        db,
        proj_b,
        part,
        order_no="WBDD-PF-B",
        order_date=date(2026, 5, 1),
        qty=Decimal("7"),
        cost_inc=Decimal("700.00"),
    )
    bad_return(db, proj_a, part, "1")
    bad_return(db, proj_b, part, "5")
    db.commit()

    out = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, project_ids=[proj_a.project_id]
    )
    assert out["total"] == 1
    row = out["rows"][0]
    assert row["qty"] == Decimal("2")
    assert row["project_count"] == 1
    assert row["cost_inc"]["value"] == "200.00"
    assert row["bad_return_qty"] == Decimal("1")
    assert Decimal(out["summary"]["total_effective_qty"]) == 2
    assert out["summary"]["total_cost_inc"]["value"] == "200.00"
    assert Decimal(out["summary"]["total_bad_return_qty"]) == 1

    # project_ids 与 allowed_project_ids 取交集：只收不放
    scoped = ana.pn_ranking(
        db,
        range_="all",
        sort="qty",
        can_cost=True,
        allowed_project_ids={proj_a.project_id},
        project_ids=[proj_a.project_id, proj_b.project_id],
    )
    assert scoped["total"] == 1
    assert scoped["rows"][0]["qty"] == Decimal("2")
    assert scoped["rows"][0]["bad_return_qty"] == Decimal("1")
    denied = ana.pn_ranking(
        db,
        range_="all",
        sort="qty",
        can_cost=True,
        allowed_project_ids={proj_a.project_id},
        project_ids=[proj_b.project_id],
    )
    assert denied["total"] == 0


def test_api_project_filter_never_widens_scoped_account(db):
    proj_a = _project(db, "API-A")
    proj_b = _project(db, "API-B")
    part = _part(db, "FILT-API")
    _line(
        db,
        proj_a,
        part,
        order_no="WBDD-API-A",
        order_date=date(2026, 5, 2),
        qty=Decimal("2"),
    )
    _line(
        db,
        proj_b,
        part,
        order_no="WBDD-API-B",
        order_date=date(2026, 5, 2),
        qty=Decimal("9"),
    )
    db.commit()
    client = client_for(
        db,
        username="filters-scoped",
        overrides={
            "page_maintenance": True,
            "own_maintenance_projects_only": True,
            "data_purchase_cost": False,
        },
    )
    _assign(db, proj_a, "filters-scoped")

    own = client.get(
        URL, params={"range": "all", "sort": "qty", "project": proj_a.project_id}
    )
    assert own.status_code == 200, own.text
    assert Decimal(own.json()["rows"][0]["qty"]) == 2
    other = client.get(
        URL, params={"range": "all", "sort": "qty", "project": proj_b.project_id}
    )
    assert other.status_code == 200 and other.json()["total"] == 0
    both = client.get(
        URL,
        params={
            "range": "all",
            "sort": "qty",
            "project": f"{proj_a.project_id},{proj_b.project_id}",
        },
    )
    assert both.status_code == 200 and both.json()["total"] == 1
    assert Decimal(both.json()["rows"][0]["qty"]) == 2


def test_customer_salesperson_order_no_contains_and_escaping(db):
    proj = _project(db, "TXT")
    part = _part(db, "FILT-TXT")
    first = _line(
        db,
        proj,
        part,
        order_no="WBDD-PCT-1",
        order_date=date(2026, 4, 1),
        qty=Decimal("1"),
    )
    second = _line(
        db,
        proj,
        part,
        order_no="WBDD-XA",
        order_date=date(2026, 4, 2),
        qty=Decimal("1"),
    )
    third = _line(
        db,
        proj,
        part,
        order_no="WBDD-_A",
        order_date=date(2026, 4, 3),
        qty=Decimal("1"),
    )
    _set_order(db, first, end_customer="客户%100", salesperson="Alice")
    _set_order(db, second, end_customer="客户X100", salesperson="Bob")
    _set_order(db, third, end_customer="其他公司", salesperson="alice-wang")
    db.commit()

    def qty(**filters) -> Decimal:
        out = ana.pn_ranking(db, range_="all", sort="qty", can_cost=True, **filters)
        assert out["total"] == 1
        return out["rows"][0]["qty"]

    assert qty(customer="%100") == Decimal("1")  # 字面 %，不匹配 X100
    assert qty(customer="x100") == Decimal("1")  # 大小写不敏感
    assert qty(customer="客户") == Decimal("2")  # contains 命中前两单
    assert qty(salesperson="alice") == Decimal("2")  # Alice + alice-wang
    assert qty(salesperson="wang") == Decimal("1")
    assert qty(order_no="_A") == Decimal("1")  # 字面 _，不匹配 XA
    assert qty(order_no="wbdd-") == Decimal("3")


def test_demand_type_filter_and_api_code_validation(db):
    proj = _project(db, "DT")
    part = _part(db, "FILT-DT")
    repair = _line(
        db,
        proj,
        part,
        order_no="WBDD-DT-R",
        order_date=date(2026, 4, 1),
        qty=Decimal("1"),
    )
    stock = _line(
        db,
        proj,
        part,
        order_no="WBDD-DT-S",
        order_date=date(2026, 4, 2),
        qty=Decimal("2"),
    )
    _set_order(db, repair, demand_type="报修供货")
    _set_order(db, stock, demand_type="补库供货")
    db.commit()

    def qty(demand_types) -> Decimal:
        out = ana.pn_ranking(
            db, range_="all", sort="qty", can_cost=True, demand_types=demand_types
        )
        assert out["total"] == 1
        return out["rows"][0]["qty"]

    assert qty(["报修供货"]) == Decimal("1")
    assert qty(["补库供货"]) == Decimal("2")
    assert qty(["报修供货", "补库供货"]) == Decimal("3")

    client = client_for(db, username="filters-dt", overrides={"page_maintenance": True})
    ok = client.get(
        URL, params={"range": "all", "sort": "qty", "demand_type": "repair"}
    )
    assert ok.status_code == 200, ok.text
    assert Decimal(ok.json()["rows"][0]["qty"]) == 1
    both = client.get(
        URL, params={"range": "all", "sort": "qty", "demand_type": "repair,stock"}
    )
    assert both.status_code == 200
    assert Decimal(both.json()["rows"][0]["qty"]) == 3
    for value in ("foo", "repair,foo", "报修供货", "all,repair"):
        invalid = client.get(URL, params={"range": "all", "demand_type": value})
        assert invalid.status_code == 422, value
        assert invalid.json()["detail"][0]["loc"] == ["query", "demand_type"]


def test_warehouse_filter_in(db):
    proj = _project(db, "WH")
    part = _part(db, "FILT-WH")
    lines = [
        _line(
            db,
            proj,
            part,
            order_no=f"WBDD-WH-{i}",
            order_date=date(2026, 4, i),
            qty=Decimal(qty),
        )
        for i, qty in enumerate(("1", "2", "4"), start=1)
    ]
    _set_order(db, lines[0], warehouse="WH-A")
    _set_order(db, lines[1], warehouse="WH-A")
    _set_order(db, lines[2], warehouse="WH-B")
    db.commit()

    one = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, warehouses=["WH-A"]
    )
    assert one["rows"][0]["qty"] == Decimal("3")
    both = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, warehouses=["WH-A", "WH-B"]
    )
    assert both["rows"][0]["qty"] == Decimal("7")
    assert (
        ana.pn_ranking(
            db, range_="all", sort="qty", can_cost=True, warehouses=["WH-C"]
        )["total"]
        == 0
    )


def test_warehouse_filter_keeps_raw_padded_values(db):
    """历史带空格仓库值：候选与筛选都用库内原值，去空白会让它选不中自己。"""

    proj = _project(db, "WH-PAD")
    part = _part(db, "FILT-WHP")
    line = _line(
        db,
        proj,
        part,
        order_no="WBDD-WHP-1",
        order_date=date(2026, 4, 1),
        qty=Decimal("3"),
    )
    _set_order(db, line, warehouse=" 广州仓 ")
    db.commit()

    assert ana.filter_options(db) == {"warehouses": [" 广州仓 "]}
    padded = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, warehouses=[" 广州仓 "]
    )
    assert padded["total"] == 1 and padded["rows"][0]["qty"] == Decimal("3")
    trimmed = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, warehouses=["广州仓"]
    )
    assert trimmed["total"] == 0

    client = client_for(
        db, username="filters-wh-pad", overrides={"page_maintenance": True}
    )
    resp = client.get(
        URL, params={"range": "all", "sort": "qty", "warehouse": " 广州仓 "}
    )
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["rows"][0]["qty"]) == 3


def test_cost_source_categories_line_level_and_combined_or(db):
    proj = _project(db, "CS")
    part = _part(db, "FILT-CS")
    _line(
        db,
        proj,
        part,
        order_no="WBDD-CS-D",
        order_date=date(2026, 4, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("100.00"),
    )
    _line(
        db,
        proj,
        part,
        order_no="WBDD-CS-M",
        order_date=date(2026, 4, 2),
        qty=Decimal("3"),
    )
    estimated = _line(
        db,
        proj,
        part,
        order_no="WBDD-CS-E",
        order_date=date(2026, 4, 3),
        qty=Decimal("5"),
        cost_inc=Decimal("500.00"),
    )
    estimated.cost_source = "window"
    manual = _line(
        db,
        proj,
        part,
        order_no="WBDD-CS-A",
        order_date=date(2026, 4, 4),
        qty=Decimal("7"),
    )
    manual.cost_source = "manual"
    none_src = _line(
        db,
        proj,
        part,
        order_no="WBDD-CS-N",
        order_date=date(2026, 4, 5),
        qty=Decimal("11"),
    )
    none_src.cost_source = "none"
    blank_src = _line(
        db,
        proj,
        part,
        order_no="WBDD-CS-B",
        order_date=date(2026, 4, 6),
        qty=Decimal("13"),
    )
    # 空串在前端 costSourceCategory 里也是缺失（!source），筛选必须同口径
    blank_src.cost_source = ""
    db.commit()

    def row(cost_sources):
        out = ana.pn_ranking(
            db, range_="all", sort="qty", can_cost=True, cost_sources=cost_sources
        )
        assert out["total"] == 1
        return out["rows"][0]

    linked = row(["linked"])
    assert linked["qty"] == Decimal("2")
    assert linked["cost_inc"]["value"] == "100.00"
    missing = row(["missing"])
    # 行级语义：只算缺价行（NULL/none/空串），不含 direct/estimated/manual
    assert missing["qty"] == Decimal("27")
    assert missing["missing_lines"] == 3
    assert missing["cost_inc"]["value"] is None
    estimated_row = row(["estimated"])
    assert estimated_row["qty"] == Decimal("5")
    assert estimated_row["cost_inc"]["value"] == "500.00"
    manual_row = row(["manual"])
    assert manual_row["qty"] == Decimal("7")
    combined = row(["linked", "missing"])
    assert combined["qty"] == Decimal("29")
    assert combined["cost_inc"]["value"] == "100.00"


def test_filters_intersect_business_type_and_keyword(db):
    spare = _project(db, "BIZ-SPARE")
    spare.business_type = "备件维保"
    overall = _project(db, "BIZ-OVERALL")
    overall.business_type = "整体维保"
    part_a = _part(db, "FILT-BIZ-A", "硬盘A")
    part_b = _part(db, "FILT-BIZ-B", "硬盘B")
    first = _line(
        db,
        spare,
        part_a,
        order_no="WBDD-BIZ-A",
        order_date=date(2026, 6, 1),
        qty=Decimal("1"),
    )
    second = _line(
        db,
        overall,
        part_b,
        order_no="WBDD-BIZ-B",
        order_date=date(2026, 6, 1),
        qty=Decimal("2"),
    )
    _set_order(db, first, end_customer="甲客户", salesperson="销售甲")
    _set_order(db, second, end_customer="甲客户", salesperson="销售甲")
    db.commit()

    both = ana.pn_ranking(
        db,
        range_="all",
        sort="qty",
        can_cost=True,
        customer="甲",
        q="硬盘",
        business_type="all",
    )
    assert both["total"] == 2
    only_spare = ana.pn_ranking(
        db,
        range_="all",
        sort="qty",
        can_cost=True,
        customer="甲",
        q="硬盘",
        business_type="spare",
    )
    assert only_spare["total"] == 1
    assert only_spare["rows"][0]["pn"] == "FILT-BIZ-A"
    conflict = ana.pn_ranking(
        db,
        range_="all",
        sort="qty",
        can_cost=True,
        business_type="spare",
        project_ids=[overall.project_id],
    )
    assert conflict["total"] == 0


def test_parse_csv_items_normalizes_and_validates():
    parse = ana.parse_csv_items
    kwargs = {"max_items": 50, "max_len": 64, "field": "project"}
    assert parse(None, **kwargs) is None
    assert parse("  ", **kwargs) is None
    assert parse(",,", **kwargs) is None
    assert parse(" a , b ,a,, c ", **kwargs) == ["a", "b", "c"]
    assert parse("x" * 64, **kwargs) == ["x" * 64]
    with pytest.raises(ana.AnalyticsValidationError):
        parse("x" * 65, **kwargs)
    with pytest.raises(ana.AnalyticsValidationError):
        parse(",".join(f"p{i}" for i in range(51)), **kwargs)
    assert parse(",".join(["p"] * 60), **kwargs) == ["p"]


def test_api_filter_overflow_returns_422(db):
    client = client_for(
        db, username="filters-overflow", overrides={"page_maintenance": True}
    )
    too_many = client.get(
        URL,
        params={
            "range": "all",
            "project": ",".join(f"p{i}" for i in range(51)),
        },
    )
    assert too_many.status_code == 422
    assert "project" in str(too_many.json()["detail"])
    too_long = client.get(URL, params={"range": "all", "project": "x" * 65})
    assert too_long.status_code == 422
    too_many_wh = client.get(
        URL,
        params={
            "range": "all",
            "warehouse": ",".join(f"WH-{i}" for i in range(11)),
        },
    )
    assert too_many_wh.status_code == 422


def test_api_filter_params_wire_through_and_intersect(db):
    proj = _project(db, "WIRE")
    part = _part(db, "FILT-WIRE")
    first = _line(
        db,
        proj,
        part,
        order_no="WBDD-WIRE-A",
        order_date=date(2026, 4, 1),
        qty=Decimal("1"),
    )
    second = _line(
        db,
        proj,
        part,
        order_no="WBDD-WIRE-B",
        order_date=date(2026, 4, 2),
        qty=Decimal("2"),
        cost_inc=Decimal("200.00"),
    )
    _set_order(db, first, warehouse="WH-A", end_customer="甲客户", salesperson="销售甲")
    _set_order(
        db, second, warehouse="WH-B", end_customer="乙客户", salesperson="销售乙"
    )
    db.commit()
    client = client_for(
        db, username="filters-wire", overrides={"page_maintenance": True}
    )

    def qty(params) -> Decimal:
        resp = client.get(URL, params={"range": "all", "sort": "qty", **params})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return Decimal(body["rows"][0]["qty"]) if body["total"] else Decimal("0")

    assert qty({"warehouse": "WH-A"}) == 1
    assert qty({"cost_source": "missing"}) == 1
    assert qty({"customer": "甲"}) == 1
    assert qty({"sp": "销售甲"}) == 1
    assert qty({"order_no": "WIRE-A"}) == 1
    assert (
        qty(
            {
                "warehouse": "WH-A",
                "cost_source": "missing",
                "customer": "甲",
                "sp": "销售甲",
                "order_no": "WIRE-A",
            }
        )
        == 1
    )
    # AND 语义：仓库对但来源不对 → 空
    assert qty({"warehouse": "WH-A", "cost_source": "linked"}) == 0


def test_filter_options_distinct_sorted_scoped_and_excludes_empty(db):
    proj_a = _project(db, "FO-A")
    proj_b = _project(db, "FO-B")
    part = _part(db, "FILT-FO")
    for i, warehouse in enumerate(["WH-B", "WH-A", "WH-A", None, "", "   "]):
        line = _line(
            db,
            proj_a,
            part,
            order_no=f"WBDD-FO-A{i}",
            order_date=date(2026, 4, 1),
            qty=Decimal("1"),
        )
        _set_order(db, line, warehouse=warehouse)
    other = _line(
        db,
        proj_b,
        part,
        order_no="WBDD-FO-B",
        order_date=date(2026, 4, 1),
        qty=Decimal("1"),
    )
    _set_order(db, other, warehouse="WH-C")
    db.commit()

    assert ana.filter_options(db) == {"warehouses": ["WH-A", "WH-B", "WH-C"]}
    assert ana.filter_options(db, allowed_project_ids={proj_a.project_id}) == {
        "warehouses": ["WH-A", "WH-B"]
    }
    assert ana.filter_options(db, allowed_project_ids=set()) == {"warehouses": []}


def test_filter_options_endpoint_scope_and_page_gate(db):
    proj_a = _project(db, "OPT-A")
    proj_b = _project(db, "OPT-B")
    part = _part(db, "FILT-OPT")
    own = _line(
        db,
        proj_a,
        part,
        order_no="WBDD-OPT-A",
        order_date=date(2026, 4, 1),
        qty=Decimal("1"),
    )
    other = _line(
        db,
        proj_b,
        part,
        order_no="WBDD-OPT-B",
        order_date=date(2026, 4, 1),
        qty=Decimal("1"),
    )
    _set_order(db, own, warehouse="WH-OWN")
    _set_order(db, other, warehouse="WH-OTHER")
    db.commit()

    full = client_for(
        db, username="filters-opt-full", overrides={"page_maintenance": True}
    )
    resp = full.get(OPTIONS_URL)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"warehouses": ["WH-OTHER", "WH-OWN"]}

    scoped = client_for(
        db,
        username="filters-opt-scoped",
        overrides={"page_maintenance": True, "own_maintenance_projects_only": True},
    )
    _assign(db, proj_a, "filters-opt-scoped")
    scoped_resp = scoped.get(OPTIONS_URL)
    assert scoped_resp.status_code == 200
    assert scoped_resp.json() == {"warehouses": ["WH-OWN"]}

    no_page = client_for(
        db, username="filters-opt-none", overrides={"page_maintenance": False}
    )
    assert no_page.get(OPTIONS_URL).status_code == 403
