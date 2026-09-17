"""维保数据分析「开支统计」：时间粒度 × 业务类型拆分 × 销售汇总（2026-09-16）。

覆盖发布验收 A1–A6：日/周/月/年分桶（周一起周、跨年不重不漏）、分类拆分
（未归属 / 未标注 / 四类标准 / 自定义 other）、分类与汇总精确对账、全字段筛选
与业务类型 AND、行键范围收敛、成本三态信封、按销售汇总（含 NULL 空白组）、
粒度 422 与空结果形状。
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysUser
from app.services import maintenance_analytics as ana
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _line, _part, _project

URL = "/api/maintenance/analytics/spend-trend"
REPAIR = "报修供货"
STOCK = "补库供货"


def _project_with_type(db, tag: str, business_type: str | None):
    proj = _project(db, tag)
    proj.business_type = business_type
    db.flush()
    return proj


def _set_order(db, line, **fields) -> FMaintenanceOrder:
    order = db.get(FMaintenanceOrder, line.order_id)
    for name, value in fields.items():
        setattr(order, name, value)
    return order


def _deactivate_assignment(db, line) -> None:
    """作废该单的活跃挂靠 → 行落 unassigned（无活跃项目）。"""
    order = db.get(FMaintenanceOrder, line.order_id)
    assignment = db.scalar(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id
        )
    )
    assignment.is_active = False
    assignment.version += 1
    assignment.archived_by = "test"
    assignment.archived_at = datetime.now(timezone.utc)
    db.flush()


def _category(out, code):
    return next(row for row in out["by_business_type"] if row["code"] == code)


def _qty(value) -> Decimal:
    return Decimal(value)


def test_granularity_buckets_day_week_month_year_with_boundaries(db):
    """A1：日/周/月/年分桶；周一开周、跨年周桶键仍取周一；各粒度桶合计一致。"""
    proj = _project(db, "GRAN")
    part = _part(db, "SPEND-GRAN")
    for order_no, order_date in (
        ("WBDD-G-1", date(2025, 12, 28)),  # 周日：属 12-22 周
        ("WBDD-G-2", date(2025, 12, 29)),  # 周一：属 12-29 周
        ("WBDD-G-3", date(2025, 12, 31)),  # 跨年头，仍属 12-29 周
        ("WBDD-G-4", date(2026, 1, 1)),  # 跨年头，仍属 12-29 周
        ("WBDD-G-5", date(2026, 2, 1)),  # 周日：属 2026-01-26 周
    ):
        _line(
            db,
            proj,
            part,
            order_no=order_no,
            order_date=order_date,
            qty=Decimal("1"),
            cost_inc=Decimal("10.00"),
        )
    db.commit()

    def keys(granularity: str) -> list[str]:
        out = ana.spend_trend(db, range_="all", granularity=granularity, can_cost=True)
        return [b["bucket"] for b in out["buckets"]]

    assert keys("day") == [
        "2025-12-28",
        "2025-12-29",
        "2025-12-31",
        "2026-01-01",
        "2026-02-01",
    ]
    assert keys("week") == ["2025-12-22", "2025-12-29", "2026-01-26"]
    assert keys("month") == ["2025-12-01", "2026-01-01", "2026-02-01"]
    assert keys("year") == ["2025-01-01", "2026-01-01"]

    for granularity in ana.GRANULARITIES:
        out = ana.spend_trend(db, range_="all", granularity=granularity, can_cost=True)
        assert out["granularity"] == granularity
        assert _qty(out["summary"]["qty"]) == 5
        assert out["summary"]["total_cost_inc"]["value"] == "50.00"
    week = ana.spend_trend(db, range_="all", granularity="week", can_cost=True)
    cross_year_week = next(b for b in week["buckets"] if b["bucket"] == "2025-12-29")
    assert _qty(cross_year_week["qty"]) == 3


def test_category_split_unassigned_unlabeled_other_and_reconcile(db):
    """A2：六档 + 未归属拆分；空/空白项目类型归未标注；分类合计 == summary。"""
    part = _part(db, "SPEND-CAT")
    cases = [
        ("OVR", "整体维保", "1", "100.00"),
        ("SPR", "备件维保", "2", "200.00"),
        ("CMP", "算力运维", "4", "400.00"),
        ("RFT", "拆改配服务", "8", "800.00"),
        ("OTH", "单次维修", "16", "160.00"),
        ("NUL", None, "32", "320.00"),
        ("BLK", "   ", "64", "640.00"),
    ]
    for tag, business_type, qty, cost in cases:
        proj = _project_with_type(db, f"SPLIT-{tag}", business_type)
        _line(
            db,
            proj,
            part,
            order_no=f"WBDD-SPLIT-{tag}",
            order_date=date(2026, 6, 1),
            qty=Decimal(qty),
            cost_inc=Decimal(cost),
        )
    unassigned = _line(
        db,
        _project_with_type(db, "SPLIT-UN", "整体维保"),
        part,
        order_no="WBDD-SPLIT-UN",
        order_date=date(2026, 6, 1),
        qty=Decimal("128"),
        cost_inc=Decimal("1280.00"),
    )
    _deactivate_assignment(db, unassigned)
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert _qty(_category(out, "overall")["qty"]) == 1
    assert _qty(_category(out, "spare")["qty"]) == 2
    assert _qty(_category(out, "computing")["qty"]) == 4
    assert _qty(_category(out, "refit")["qty"]) == 8
    assert _qty(_category(out, "other")["qty"]) == 16
    assert _qty(_category(out, "unlabeled")["qty"]) == 96  # NULL 32 + 空白 64
    assert _qty(_category(out, "unassigned")["qty"]) == 128
    assert _category(out, "overall")["label"] == "整体维保"
    assert _category(out, "other")["label"] == "非维保"
    assert _category(out, "unlabeled")["label"] == "未标注"
    assert _category(out, "unassigned")["label"] == "未归属"
    # 含税成本降序（unassigned 1280 > unlabeled 960 > refit 800 > …）
    assert [r["code"] for r in out["by_business_type"]] == [
        "unassigned",
        "unlabeled",
        "refit",
        "computing",
        "spare",
        "other",
        "overall",
    ]
    assert _category(out, "refit")["cost_share_pct"] == 20.5

    summary = out["summary"]
    assert (
        summary["order_count"]
        == sum(r["order_count"] for r in out["by_business_type"])
        == 8
    )
    assert (
        _qty(summary["qty"])
        == sum(_qty(r["qty"]) for r in out["by_business_type"])
        == 255
    )
    assert summary["missing_lines"] == sum(
        r["missing_lines"] for r in out["by_business_type"]
    )
    assert (
        summary["total_cost_inc"]["value"]
        == str(sum(Decimal(r["cost_inc"]["value"]) for r in out["by_business_type"]))
        == "3900.00"
    )
    bucket = out["buckets"][0]
    assert set(bucket["by_business_type"]) == {
        r["code"] for r in out["by_business_type"]
    }
    assert _qty(bucket["qty"]) == 255
    assert (
        str(sum(Decimal(b["cost_inc"]["value"]) for b in out["buckets"])) == "3900.00"
    )

    # 显式分类筛选（含六档全选）不混入未归属：未归属单的项目类型虽是整体维保，
    # 但没有活跃挂靠，只能进默认 all。
    only_overall = ana.spend_trend(
        db, range_="all", business_type="overall", can_cost=True
    )
    assert {r["code"] for r in only_overall["by_business_type"]} == {"overall"}
    assert _qty(only_overall["summary"]["qty"]) == 1


def test_filters_intersect_business_type_and_each_other(db):
    """A3：项目/客户/销售/单号/需求类型/仓库/取价来源与业务类型 AND 叠加。"""
    spare = _project_with_type(db, "FLT-SPR", "备件维保")
    overall = _project_with_type(db, "FLT-OVR", "整体维保")
    part = _part(db, "SPEND-FLT")
    first = _line(
        db,
        spare,
        part,
        order_no="WBDD-FLT-A",
        order_date=date(2026, 5, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("20.00"),
    )
    second = _line(
        db,
        overall,
        part,
        order_no="WBDD-FLT-B",
        order_date=date(2026, 5, 2),
        qty=Decimal("5"),
        cost_inc=Decimal("50.00"),
    )
    third = _line(
        db,
        spare,
        part,
        order_no="WBDD-FLT-C",
        order_date=date(2026, 5, 3),
        qty=Decimal("7"),
    )
    _set_order(
        db,
        first,
        end_customer="甲客户",
        salesperson="销售甲",
        demand_type=REPAIR,
        warehouse="WH-A",
    )
    _set_order(
        db,
        second,
        end_customer="乙客户",
        salesperson="销售乙",
        demand_type=STOCK,
        warehouse="WH-B",
    )
    _set_order(
        db,
        third,
        end_customer="甲客户",
        salesperson="销售甲",
        demand_type=REPAIR,
        warehouse="WH-B",
    )
    db.commit()

    both = ana.spend_trend(db, range_="all", can_cost=True)
    assert _qty(both["summary"]["qty"]) == 14

    only = ana.spend_trend(
        db,
        range_="all",
        can_cost=True,
        business_type="spare",
        project_ids=[spare.project_id],
        customer="甲",
        salesperson="销售甲",
        order_no="FLT-A",
        demand_types=[REPAIR],
        warehouses=["WH-A"],
        cost_sources=["linked"],
    )
    assert _qty(only["summary"]["qty"]) == 2
    assert [b["bucket"] for b in only["buckets"]] == ["2026-05-01"]

    missing = ana.spend_trend(db, range_="all", can_cost=True, cost_sources=["missing"])
    assert _qty(missing["summary"]["qty"]) == 7
    assert missing["summary"]["missing_lines"] == 1

    exclusively = ana.spend_trend(
        db, range_="all", can_cost=True, project_ids=[overall.project_id]
    )
    assert _qty(exclusively["summary"]["qty"]) == 5

    # AND 语义：任一条冲突即空（业务类型无冲突但仓库对不上）
    conflict = ana.spend_trend(
        db,
        range_="all",
        can_cost=True,
        business_type="overall",
        customer="甲",
        warehouses=["WH-A"],
    )
    assert conflict["buckets"] == []
    assert conflict["summary"]["order_count"] == 0


def test_api_row_scope_hides_out_of_scope_and_unassigned(db):
    """A4：own_maintenance_projects_only 只看本人范围，未归属行一并排除。"""
    own = _project_with_type(db, "SCOPE-OWN", "整体维保")
    other = _project_with_type(db, "SCOPE-OTH", "整体维保")
    part = _part(db, "SPEND-SCOPE")
    own_line = _line(
        db,
        own,
        part,
        order_no="WBDD-SCOPE-A",
        order_date=date(2026, 5, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("20.00"),
    )
    _line(
        db,
        other,
        part,
        order_no="WBDD-SCOPE-B",
        order_date=date(2026, 5, 1),
        qty=Decimal("9"),
        cost_inc=Decimal("90.00"),
    )
    unassigned = _line(
        db,
        other,
        part,
        order_no="WBDD-SCOPE-U",
        order_date=date(2026, 5, 1),
        qty=Decimal("64"),
        cost_inc=Decimal("640.00"),
    )
    _set_order(db, own_line, salesperson="销售甲")
    _deactivate_assignment(db, unassigned)
    db.commit()

    client = client_for(
        db,
        username="spend-scoped",
        overrides={
            "page_maintenance": True,
            "own_maintenance_projects_only": True,
            "data_purchase_cost": False,
        },
    )
    user = db.scalar(select(SysUser).where(SysUser.username == "spend-scoped"))
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id=str(uuid.uuid4()),
            project_id=own.project_id,
            responsibility_type="primary_manager",
            user_id=user.id,
            version=1,
            assigned_by="test",
            assignment_reason="test",
        )
    )
    db.commit()

    resp = client.get(URL, params={"range": "all"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert _qty(body["summary"]["qty"]) == 2
    assert body["summary"]["order_count"] == 1
    assert [b["bucket"] for b in body["buckets"]] == ["2026-05-01"]
    assert "unassigned" not in {r["code"] for r in body["by_business_type"]}
    # 无成本权限：金额三态 restricted，数量/单数照常可读
    bucket = body["buckets"][0]
    assert bucket["cost_inc"]["state"] == "restricted"
    assert bucket["cost_inc"]["value"] is None
    assert bucket["cost_ex"]["state"] == "restricted"
    assert bucket["by_business_type"]["overall"]["state"] == "restricted"
    assert _qty(bucket["qty"]) == 2
    assert body["summary"]["total_cost_inc"]["state"] == "restricted"
    assert body["by_salesperson"][0]["cost_inc"]["state"] == "restricted"
    assert body["by_business_type"][0]["cost_share_pct"] is None


def test_restricted_cost_envelopes_keep_qty_and_counts(db):
    """A4/A5：无成本权限时所有金额三态 restricted，数量与行数不受影响。"""
    proj = _project_with_type(db, "RESTR", "备件维保")
    part = _part(db, "SPEND-RESTR")
    _line(
        db,
        proj,
        part,
        order_no="WBDD-RESTR-1",
        order_date=date(2026, 5, 1),
        qty=Decimal("3"),
        cost_inc=Decimal("30.00"),
    )
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=False)
    bucket = out["buckets"][0]
    assert bucket["cost_inc"]["state"] == bucket["cost_ex"]["state"] == "restricted"
    assert bucket["cost_inc"]["value"] is None
    assert bucket["by_business_type"]["spare"]["state"] == "restricted"
    assert _qty(bucket["qty"]) == 3 and bucket["order_count"] == 1
    assert out["summary"]["total_cost_inc"]["state"] == "restricted"
    assert out["summary"]["total_cost_ex"]["state"] == "restricted"
    assert out["by_business_type"][0]["cost_inc"]["state"] == "restricted"
    assert out["by_business_type"][0]["cost_share_pct"] is None
    assert out["by_salesperson"][0]["cost_inc"]["state"] == "restricted"
    assert _qty(out["summary"]["qty"]) == 3
    # 键集与 ready 完全一致（防字段存在性侧信道）
    assert set(bucket["cost_inc"]) == {"state", "value", "as_of"}


def test_wbdd_not_imported_returns_not_imported_envelopes(db):
    """A5：WBDD 未导入 → 金额 not_imported（不渲染 0），wbdd_ready=false。"""
    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert out["summary"]["wbdd_ready"] is False
    assert out["summary"]["total_cost_inc"]["state"] == "not_imported"
    assert out["summary"]["total_cost_inc"]["value"] is None
    assert out["summary"]["total_cost_ex"]["state"] == "not_imported"
    assert out["buckets"] == []
    assert out["by_business_type"] == []
    assert out["by_salesperson"] == []
    assert out["summary"]["bucket_count"] == 0

    hidden = ana.spend_trend(db, range_="all", can_cost=False)
    assert hidden["summary"]["total_cost_inc"]["state"] == "not_imported"


def test_salesperson_grouping_null_blank_and_sp_filter(db):
    """A6：按销售汇总；NULL 与纯空白并为一档（JSON null）；sp 收窄整个结果。"""
    proj = _project_with_type(db, "SP", "整体维保")
    part = _part(db, "SPEND-SP")
    zhang_one = _line(
        db,
        proj,
        part,
        order_no="WBDD-SP-1",
        order_date=date(2026, 5, 1),
        qty=Decimal("1"),
        cost_inc=Decimal("100.00"),
    )
    zhang_two = _line(
        db,
        proj,
        part,
        order_no="WBDD-SP-2",
        order_date=date(2026, 5, 2),
        qty=Decimal("1"),
        cost_inc=Decimal("300.00"),
    )
    li = _line(
        db,
        proj,
        part,
        order_no="WBDD-SP-3",
        order_date=date(2026, 5, 3),
        qty=Decimal("2"),
        cost_inc=Decimal("200.00"),
    )
    none_line = _line(
        db,
        proj,
        part,
        order_no="WBDD-SP-4",
        order_date=date(2026, 5, 4),
        qty=Decimal("4"),
    )
    blank_line = _line(
        db,
        proj,
        part,
        order_no="WBDD-SP-5",
        order_date=date(2026, 5, 5),
        qty=Decimal("8"),
    )
    _set_order(db, zhang_one, salesperson="张三")
    _set_order(db, zhang_two, salesperson="张三")
    _set_order(db, li, salesperson="李四")
    _set_order(db, none_line, salesperson=None)
    _set_order(db, blank_line, salesperson="   ")
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert [
        (r["salesperson"], _qty(r["qty"]), r["order_count"])
        for r in out["by_salesperson"]
    ] == [
        ("张三", Decimal("2"), 2),
        ("李四", Decimal("2"), 1),
        (None, Decimal("12"), 2),
    ]
    assert _qty(out["by_salesperson"][0]["cost_inc"]["value"]) == 400
    assert out["by_salesperson"][0]["cost_share_pct"] == 66.7
    assert out["by_salesperson"][2]["cost_share_pct"] == 0.0

    filtered = ana.spend_trend(db, range_="all", can_cost=True, salesperson="张三")
    assert [r["salesperson"] for r in filtered["by_salesperson"]] == ["张三"]
    assert _qty(filtered["summary"]["qty"]) == 2
    assert filtered["summary"]["total_cost_inc"]["value"] == "400.00"


def test_api_granularity_validation_and_response_shape(db):
    """粒度白名单 422；响应键集与 no-store 缓存头；未导入时三态信封。"""
    client = client_for(db, username="spend-api", overrides={"page_maintenance": True})
    for value in ("quarter", "weeks", ""):
        bad = client.get(URL, params={"granularity": value})
        assert bad.status_code == 422, value
        assert bad.json()["detail"][0]["loc"] == ["query", "granularity"]

    ok = client.get(URL, params={"range": "all", "granularity": "day"})
    assert ok.status_code == 200, ok.text
    assert ok.headers["cache-control"] == "no-store"
    body = ok.json()
    assert set(body) == {
        "granularity",
        "window",
        "buckets",
        "by_business_type",
        "by_project",
        "by_salesperson",
        "summary",
    }
    assert body["granularity"] == "day"
    assert body["window"] == {"range": "all", "date_from": None, "date_to": None}
    assert body["buckets"] == []
    assert body["by_business_type"] == []
    assert body["by_salesperson"] == []
    assert body["summary"]["wbdd_ready"] is False
    assert body["summary"]["total_cost_inc"]["state"] == "not_imported"

    no_page = client_for(
        db, username="spend-no-page", overrides={"page_maintenance": False}
    )
    assert no_page.get(URL).status_code == 403


def test_empty_result_shape(db):
    """窗口内无数据：桶/拆分表为空，summary 保持恒定形状（不省略键）。"""
    proj = _project_with_type(db, "EMPTY", "整体维保")
    part = _part(db, "SPEND-EMPTY")
    _line(
        db,
        proj,
        part,
        order_no="WBDD-EMPTY",
        order_date=date(2026, 3, 1),
        qty=Decimal("1"),
    )
    db.commit()

    out = ana.spend_trend(
        db,
        range_="custom",
        date_from=date(2030, 1, 1),
        date_to=date(2030, 1, 31),
        can_cost=True,
    )
    assert out["granularity"] == "month"
    assert out["window"] == {
        "range": "custom",
        "date_from": "2030-01-01",
        "date_to": "2030-01-31",
    }
    assert out["buckets"] == []
    assert out["by_business_type"] == []
    assert out["by_salesperson"] == []
    assert out["summary"] == {
        "bucket_count": 0,
        "order_count": 0,
        "qty": "0",
        "effective_qty": "0",
        "missing_lines": 0,
        "total_cost_inc": {"state": "ready", "value": "0.00", "as_of": None},
        "total_cost_ex": {"state": "ready", "value": "0.00", "as_of": None},
        "wbdd_ready": True,
    }
