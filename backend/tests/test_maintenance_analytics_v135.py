"""v1.35 开支统计：有效销售口径（项目主档优先）与按项目汇总。

覆盖发布验收 A2（销售口径：主档优先 / 人工改过即权威含清空 / 主档空回退
订单源 / 无项目用订单源 / btrim 归一；spend-trend 与 pn-ranking 同口径）
与 A1（by_project 与 summary 恒等、标签正确、全筛选收窄、行键范围收敛、
成本三态信封、成本降序与上限）。
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

SPEND_URL = "/api/maintenance/analytics/spend-trend"
PN_URL = "/api/maintenance/analytics/pn-ranking"


def _set_order(db, line, **fields) -> FMaintenanceOrder:
    order = db.get(FMaintenanceOrder, line.order_id)
    for name, value in fields.items():
        setattr(order, name, value)
    return order


def _deactivate_assignment(db, line) -> None:
    """作废该单的活跃挂靠 → 行落无项目（unassigned）。"""
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


def _typed_project(db, tag: str, business_type: str | None):
    proj = _project(db, tag)
    proj.business_type = business_type
    db.flush()
    return proj


def _qty(value) -> Decimal:
    return Decimal(value)


# ---------------------------------------------------------------- 有效销售


def test_effective_salesperson_project_master_wins_over_order_source(db):
    """A2：主档=王俊凯、订单源=王小环 → 汇总与筛选只认王俊凯（两页签一致）。"""
    proj = _typed_project(db, "ESP-WIN", "整体维保")
    part = _part(db, "ESP-WIN")
    line = _line(
        db,
        proj,
        part,
        order_no="WBDD-ESP-1",
        order_date=date(2026, 5, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("200.00"),
    )
    _set_order(db, line, salesperson="王小环")
    proj.salesperson = "王俊凯"
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert [
        (r["salesperson"], _qty(r["qty"]), r["order_count"])
        for r in out["by_salesperson"]
    ] == [("王俊凯", Decimal("2"), 1)]
    assert out["by_salesperson"][0]["cost_inc"]["value"] == "200.00"

    hit = ana.spend_trend(db, range_="all", can_cost=True, salesperson="王俊凯")
    assert _qty(hit["summary"]["qty"]) == 2
    assert hit["summary"]["order_count"] == 1
    miss = ana.spend_trend(db, range_="all", can_cost=True, salesperson="王小环")
    assert miss["buckets"] == []
    assert miss["summary"]["order_count"] == 0
    assert miss["by_salesperson"] == []

    pn_hit = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, salesperson="王俊凯"
    )
    assert pn_hit["total"] == 1
    pn_miss = ana.pn_ranking(
        db, range_="all", sort="qty", can_cost=True, salesperson="王小环"
    )
    assert pn_miss["total"] == 0


def test_effective_salesperson_override_cleared_is_authoritative(db):
    """A2：主档被人工清空（override）→ 未标注（null），不回退订单源旧名。"""
    proj = _typed_project(db, "ESP-CLR", "整体维保")
    part = _part(db, "ESP-CLR")
    line = _line(
        db,
        proj,
        part,
        order_no="WBDD-ESP-CLR",
        order_date=date(2026, 5, 1),
        qty=Decimal("3"),
        cost_inc=Decimal("30.00"),
    )
    _set_order(db, line, salesperson="王小环")
    proj.salesperson = None
    proj.salesperson_override_active = True
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert [(r["salesperson"], _qty(r["qty"])) for r in out["by_salesperson"]] == [
        (None, Decimal("3"))
    ]

    miss = ana.spend_trend(db, range_="all", can_cost=True, salesperson="王小环")
    assert miss["summary"]["order_count"] == 0
    assert (
        ana.pn_ranking(
            db, range_="all", sort="qty", can_cost=True, salesperson="王小环"
        )["total"]
        == 0
    )


def test_effective_salesperson_fallback_unassigned_and_btrim(db):
    """A2：主档空且未改过 → 回退订单源；无项目 → 订单源；两侧 btrim 归一。"""
    blank_master = _typed_project(db, "ESP-FB", "整体维保")
    padded_master = _typed_project(db, "ESP-PAD", "整体维保")
    part = _part(db, "ESP-FB")
    fallback = _line(
        db,
        blank_master,
        part,
        order_no="WBDD-ESP-FB",
        order_date=date(2026, 5, 1),
        qty=Decimal("1"),
        cost_inc=Decimal("10.00"),
    )
    unassigned = _line(
        db,
        blank_master,
        part,
        order_no="WBDD-ESP-UN",
        order_date=date(2026, 5, 2),
        qty=Decimal("2"),
        cost_inc=Decimal("20.00"),
    )
    padded = _line(
        db,
        padded_master,
        part,
        order_no="WBDD-ESP-PAD",
        order_date=date(2026, 5, 3),
        qty=Decimal("4"),
        cost_inc=Decimal("40.00"),
    )
    _set_order(db, fallback, salesperson="王小环")
    _set_order(db, unassigned, salesperson=" 王小环 ")  # 订单源带空格 → btrim
    _set_order(db, padded, salesperson="赵六")
    blank_master.salesperson = "   "  # 纯空白（btrim 后空）且未改过 → 回退
    padded_master.salesperson = "  王俊凯  "  # 主档带空格 → btrim
    _deactivate_assignment(db, unassigned)
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert [
        (r["salesperson"], _qty(r["qty"]), r["order_count"])
        for r in out["by_salesperson"]
    ] == [("王俊凯", Decimal("4"), 1), ("王小环", Decimal("3"), 2)]

    assert (
        _qty(
            ana.spend_trend(db, range_="all", can_cost=True, salesperson="俊凯")[
                "summary"
            ]["qty"]
        )
        == 4
    )
    assert (
        _qty(
            ana.spend_trend(db, range_="all", can_cost=True, salesperson="小环")[
                "summary"
            ]["qty"]
        )
        == 3
    )
    assert (
        ana.pn_ranking(db, range_="all", sort="qty", can_cost=True, salesperson="小环")[
            "total"
        ]
        == 1
    )


# ---------------------------------------------------------------- 按项目汇总


def test_by_project_reconciles_summary_labels_and_cost_order(db):
    """A1：按项目汇总与 summary 恒等（含未归属行）；标签与成本降序正确。"""
    overall = _typed_project(db, "BP-OVR", "整体维保")
    refit = _typed_project(db, "BP-RFT", "拆改配服务")
    unlabeled = _typed_project(db, "BP-NUL", None)
    other = _typed_project(db, "BP-OTH", "单次维修")
    part = _part(db, "BP-ALL")
    _line(
        db,
        overall,
        part,
        order_no="WBDD-BP-1",
        order_date=date(2026, 6, 1),
        qty=Decimal("1"),
        cost_inc=Decimal("100.00"),
    )
    _line(
        db,
        overall,
        part,
        order_no="WBDD-BP-2",
        order_date=date(2026, 6, 2),
        qty=Decimal("2"),
        cost_inc=Decimal("200.00"),
    )
    _line(
        db,
        refit,
        part,
        order_no="WBDD-BP-3",
        order_date=date(2026, 6, 3),
        qty=Decimal("4"),
        return_qty=Decimal("1"),
        cost_inc=Decimal("400.00"),
    )
    _line(
        db,
        unlabeled,
        part,
        order_no="WBDD-BP-4",
        order_date=date(2026, 6, 4),
        qty=Decimal("5"),
        cost_inc=Decimal("500.00"),
    )
    _line(
        db,
        other,
        part,
        order_no="WBDD-BP-5",
        order_date=date(2026, 6, 5),
        qty=Decimal("6"),
        cost_inc=Decimal("600.00"),
    )
    unassigned = _line(
        db,
        overall,
        part,
        order_no="WBDD-BP-U",
        order_date=date(2026, 6, 6),
        qty=Decimal("8"),
        cost_inc=Decimal("800.00"),
    )
    _deactivate_assignment(db, unassigned)
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    rows = out["by_project"]
    by_pid = {r["project_id"]: r for r in rows}
    assert set(by_pid) == {
        overall.project_id,
        refit.project_id,
        unlabeled.project_id,
        other.project_id,
        None,
    }

    ov = by_pid[overall.project_id]
    assert ov["display_name"] == "分析测试BP-OVR"
    assert ov["business_type_code"] == "overall"
    assert ov["business_type_label"] == "整体维保"
    assert ov["order_count"] == 2
    assert _qty(ov["qty"]) == 3 and _qty(ov["effective_qty"]) == 3
    assert ov["cost_inc"]["value"] == "300.00"
    rf = by_pid[refit.project_id]
    assert rf["business_type_code"] == "refit"
    assert rf["business_type_label"] == "拆改配服务"
    assert _qty(rf["effective_qty"]) == 3  # 4 - 1
    assert by_pid[unlabeled.project_id]["business_type_label"] == "未标注"
    assert by_pid[other.project_id]["business_type_label"] == "非维保"
    un = by_pid[None]
    assert un["display_name"] == "未归属（无项目）"
    assert un["business_type_code"] == "unassigned"
    assert un["business_type_label"] == "未归属"
    assert un["order_count"] == 1

    # 与 summary 恒等（数量/单数/金额），含未归属行
    assert sum(_qty(r["qty"]) for r in rows) == _qty(out["summary"]["qty"]) == 26
    assert sum(r["order_count"] for r in rows) == out["summary"]["order_count"] == 6
    assert sum(Decimal(r["cost_inc"]["value"]) for r in rows) == Decimal(
        out["summary"]["total_cost_inc"]["value"]
    )
    assert out["summary"]["total_cost_inc"]["value"] == "2600.00"
    # 含税成本降序：800 > 600 > 500 > 400 > 300
    assert [r["cost_inc"]["value"] for r in rows] == [
        "800.00",
        "600.00",
        "500.00",
        "400.00",
        "300.00",
    ]
    assert ov["cost_share_pct"] == 11.5  # 300 / 2600
    assert un["cost_share_pct"] == 30.8  # 800 / 2600


def test_by_project_filters_narrow_like_summary(db):
    """A1：业务类型/项目/销售/时间全部筛选同样收窄 by_project。"""
    overall = _typed_project(db, "BPF-OVR", "整体维保")
    spare = _typed_project(db, "BPF-SPR", "备件维保")
    part = _part(db, "BPF")
    first = _line(
        db,
        overall,
        part,
        order_no="WBDD-BPF-A",
        order_date=date(2026, 6, 1),
        qty=Decimal("1"),
        cost_inc=Decimal("100.00"),
    )
    _set_order(db, first, salesperson="王小环")
    overall.salesperson = "王俊凯"
    second = _line(
        db,
        spare,
        part,
        order_no="WBDD-BPF-B",
        order_date=date(2026, 7, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("200.00"),
    )
    _set_order(db, second, salesperson="李四")
    unassigned = _line(
        db,
        overall,
        part,
        order_no="WBDD-BPF-U",
        order_date=date(2026, 6, 2),
        qty=Decimal("4"),
        cost_inc=Decimal("400.00"),
    )
    _set_order(db, unassigned, salesperson="王小环")
    _deactivate_assignment(db, unassigned)
    db.commit()

    def pids(**filters) -> set[str | None]:
        filters.setdefault("range_", "all")
        return {
            r["project_id"]
            for r in ana.spend_trend(db, can_cost=True, **filters)["by_project"]
        }

    # 显式分类不混入未归属：只剩整体维保项目行
    assert pids(business_type="overall") == {overall.project_id}
    assert pids(project_ids=[spare.project_id]) == {spare.project_id}
    # 有效销售口径：sp=王俊凯 只剩主档项目行（未归属行回退订单源王小环，落选）
    assert pids(salesperson="王俊凯") == {overall.project_id}
    assert pids(salesperson="王小环") == {None}
    # 时间窗收窄：六月只有整体维保 + 未归属
    assert pids(
        range_="custom",
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    ) == {overall.project_id, None}


def test_by_project_scope_isolation_via_api(db):
    """A3：own_maintenance_projects_only 只看本人项目行，未归属行一并排除。"""
    own = _typed_project(db, "BPS-OWN", "整体维保")
    other = _typed_project(db, "BPS-OTH", "整体维保")
    part = _part(db, "BPS")
    own_line = _line(
        db,
        own,
        part,
        order_no="WBDD-BPS-A",
        order_date=date(2026, 5, 1),
        qty=Decimal("2"),
        cost_inc=Decimal("20.00"),
    )
    _line(
        db,
        other,
        part,
        order_no="WBDD-BPS-B",
        order_date=date(2026, 5, 1),
        qty=Decimal("9"),
        cost_inc=Decimal("90.00"),
    )
    unassigned = _line(
        db,
        other,
        part,
        order_no="WBDD-BPS-U",
        order_date=date(2026, 5, 1),
        qty=Decimal("64"),
        cost_inc=Decimal("640.00"),
    )
    _set_order(db, own_line, salesperson="王小环")
    _deactivate_assignment(db, unassigned)
    db.commit()

    client = client_for(
        db,
        username="v135-scoped",
        overrides={
            "page_maintenance": True,
            "own_maintenance_projects_only": True,
            "data_purchase_cost": False,
        },
    )
    user = db.scalar(select(SysUser).where(SysUser.username == "v135-scoped"))
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

    resp = client.get(SPEND_URL, params={"range": "all"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body["by_project"]
    assert [r["project_id"] for r in rows] == [own.project_id]
    assert rows[0]["display_name"] == "分析测试BPS-OWN"
    assert _qty(rows[0]["qty"]) == 2 and rows[0]["order_count"] == 1
    # 无成本权限：金额 restricted、占比 —，数量照常可读
    assert rows[0]["cost_inc"]["state"] == "restricted"
    assert rows[0]["cost_inc"]["value"] is None
    assert rows[0]["cost_ex"]["state"] == "restricted"
    assert rows[0]["cost_share_pct"] is None
    assert _qty(body["summary"]["qty"]) == 2


def test_by_project_restricted_envelopes_keep_qty(db):
    """A3：无成本权限时 by_project 金额三态 restricted，数量/单数不受影响。"""
    proj = _typed_project(db, "BPR", "备件维保")
    part = _part(db, "BPR")
    _line(
        db,
        proj,
        part,
        order_no="WBDD-BPR-1",
        order_date=date(2026, 5, 1),
        qty=Decimal("3"),
        cost_inc=Decimal("30.00"),
    )
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=False)
    row = out["by_project"][0]
    assert row["cost_inc"]["state"] == row["cost_ex"]["state"] == "restricted"
    assert row["cost_inc"]["value"] is None
    assert row["cost_share_pct"] is None
    assert _qty(row["qty"]) == 3 and row["order_count"] == 1
    # 键集与 ready 完全一致（防字段存在性侧信道）
    assert set(row["cost_inc"]) == {"state", "value", "as_of"}


def test_by_project_cost_sort_order_and_row_cap(db, monkeypatch):
    """A1：含税成本降序；上限 200（小数据 + 压低常量断言截断）。"""
    part = _part(db, "BPC")
    costs = ["500.00", "100.00", "300.00", "200.00", "400.00"]
    for i, cost in enumerate(costs):
        proj = _typed_project(db, f"BPC-{i}", "整体维保")
        _line(
            db,
            proj,
            part,
            order_no=f"WBD-BPC-{i}",
            order_date=date(2026, 6, 1),
            qty=Decimal("1"),
            cost_inc=Decimal(cost),
        )
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=True)
    assert [r["cost_inc"]["value"] for r in out["by_project"]] == [
        "500.00",
        "400.00",
        "300.00",
        "200.00",
        "100.00",
    ]

    monkeypatch.setattr(ana, "_PROJECT_LIMIT", 3)
    capped = ana.spend_trend(db, range_="all", can_cost=True)
    assert [r["cost_inc"]["value"] for r in capped["by_project"]] == [
        "500.00",
        "400.00",
        "300.00",
    ]


def test_by_project_restricted_sorts_by_qty_then_name(db):
    """A3：无成本权限按数量降序、同名升序——不泄露成本序。"""
    part = _part(db, "BPT")
    for tag, qty, cost in (
        ("BPT-A", Decimal("5"), Decimal("10.00")),
        ("BPT-B", Decimal("5"), Decimal("900.00")),
        ("BPT-C", Decimal("1"), Decimal("500.00")),
    ):
        proj = _typed_project(db, tag, "整体维保")
        _line(
            db,
            proj,
            part,
            order_no=f"WBD-{tag}",
            order_date=date(2026, 6, 1),
            qty=qty,
            cost_inc=cost,
        )
    db.commit()

    out = ana.spend_trend(db, range_="all", can_cost=False)
    assert [r["display_name"] for r in out["by_project"]] == [
        "分析测试BPT-A",
        "分析测试BPT-B",
        "分析测试BPT-C",
    ]
    assert all(r["cost_inc"]["state"] == "restricted" for r in out["by_project"])
