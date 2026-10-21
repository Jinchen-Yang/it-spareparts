"""期间下单与每日已计消耗（M01/M03，R34 / D-0024）：独立小模块，只读不改成本。

- M01：制单日期入期的有效需求单（已生效 + 未墓碑 + 活跃挂靠）有效行
  Σcoalesce(qty,0)，另列去重单数；不扣退货、不扣免返（M-AC01）。
- M03：每天每项目实际领用（映射有效 + confirmed/corrected + 行有效）数量全计；
  金额只报既有 cost_amount 双税已知小计，另报未计成本量／覆盖／来源
  （M-AC02）；只读，不回算、不减估值、不覆盖成本链。
"""

import uuid
from datetime import date, timezone
from decimal import Decimal

from app.services import maintenance_dashboard_consumption as consumption
from tests.test_maintenance_analytics import _line, _project
from tests.test_maintenance_analytics import _part
from tests.test_maintenance_return_metrics import _issue

UTC = timezone.utc
START = date(2026, 10, 13)
END = date(2026, 10, 19)


def _projects(db, *projects):
    return {project.project_id: project for project in projects}


def _order_of(db, line):
    from sqlalchemy import select

    from app.models.maintenance import FMaintenanceOrder

    return db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.id == line.order_id))


def _costed_issue(db, project, part, qty, *, issue_date, unit_cost_ex,
                  cost_source="direct_purchase", status="confirmed", active=True):
    line = _issue(db, project, part, qty, status=status, issue_date=issue_date)
    if unit_cost_ex is not None:
        unit_inc = (unit_cost_ex * Decimal("1.13")).quantize(Decimal("0.01"))
        line.unit_cost = unit_cost_ex
        line.unit_cost_ex_tax = unit_cost_ex
        line.unit_cost_inc_tax = unit_inc
        line.cost_amount = (Decimal(qty) * unit_cost_ex).quantize(Decimal("0.01"))
        line.cost_amount_ex_tax = line.cost_amount
        line.cost_amount_inc_tax = (
            Decimal(qty) * unit_inc
        ).quantize(Decimal("0.01"))
        line.cost_source = cost_source
    db.flush()
    return line


# ---------- M01 期间下单 ----------

def test_period_orders_counts_original_qty_and_distinct_orders(db):
    """下单按制单日期、原 qty、另计单数；退货/免返不扣（M-AC01）。"""
    project = _project(db, "M01-A")
    part = _part(db, "M01-PN")
    # 两张单（期内制单），一张 qty 10（return_qty 2 不扣）、一张 qty 5。
    _line(db, project, part, order_no="WBDD-M01-1", order_date=date(2026, 10, 14),
          qty=Decimal("10"), return_qty=Decimal("2"))
    _line(db, project, part, order_no="WBDD-M01-2", order_date=date(2026, 10, 15),
          qty=Decimal("5"))
    # 期前/期末之后制单不进期间。
    _line(db, project, part, order_no="WBDD-M01-EARLY", order_date=date(2026, 10, 12),
          qty=Decimal("7"))
    _line(db, project, part, order_no="WBDD-M01-LATE", order_date=date(2026, 10, 20),
          qty=Decimal("8"))
    db.commit()

    payload = consumption.period_orders(
        db, start=START, end=END, projects=_projects(db, project)
    )
    assert payload["cards"]["order_qty"] == "15.000"
    assert payload["cards"]["order_count"] == 2
    (row,) = payload["rows"]
    assert row["project_id"] == project.project_id
    assert row["order_qty"] == "15.000"
    assert row["order_count"] == 2


def test_period_orders_excludes_invalid_tombstoned_unassigned(db):
    project = _project(db, "M01-B")
    part = _part(db, "M01-PN-B")
    # 已生效 + 挂靠 → 计。
    _line(db, project, part, order_no="WBDD-M01-OK", order_date=date(2026, 10, 14),
          qty=Decimal("3"))
    # 数据状态非已生效 → 不计。
    voided = _line(db, project, part, order_no="WBDD-M01-VOID",
                   order_date=date(2026, 10, 14), qty=Decimal("4"))
    _order_of(db, voided).data_status = "已作废"
    # 墓碑（未恢复）→ 不计（沿 T4 summarize_demand_returns 口径，无条件生效）。
    tombstoned = _line(db, project, part, order_no="WBDD-M01-TOMB",
                       order_date=date(2026, 10, 15), qty=Decimal("5"))
    from datetime import datetime as _dt

    from app.models.maintenance import (
        MaintenanceDemandDeleteIntent,
        MaintenanceDemandTombstone,
    )

    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid.uuid4()), idempotency_key=f"m01-{uuid.uuid4()}",
        request_digest="x" * 64, selection_digest="y" * 64, status="executed",
        reason="测试墓碑", operated_by="t", header_count=1, line_count=1,
        created_at=_dt.now(UTC), expires_at=_dt.now(UTC),
    )
    db.add(intent)
    db.flush()
    db.add(MaintenanceDemandTombstone(
        source_order_id=_order_of(db, tombstoned).raw_order_id,
        delete_intent_id=intent.intent_id,
        version_digest="z" * 64, deleted_by="t", delete_reason="测试",
        deleted_at=_dt.now(UTC), version=1,
    ))
    # 行失效 → 该行不计。
    inactive = _line(db, project, part, order_no="WBDD-M01-INACT",
                     order_date=date(2026, 10, 16), qty=Decimal("6"), active=False)
    assert inactive is not None
    db.commit()

    payload = consumption.period_orders(
        db, start=START, end=END, projects=_projects(db, project)
    )
    assert payload["cards"]["order_qty"] == "3.000"
    assert payload["cards"]["order_count"] == 1


def test_period_orders_empty_scope_stays_empty(db):
    """空授权范围不扩全量（铁律）。"""
    project = _project(db, "M01-C")
    part = _part(db, "M01-PN-C")
    _line(db, project, part, order_no="WBDD-M01-C1", order_date=date(2026, 10, 14),
          qty=Decimal("10"))
    db.commit()
    payload = consumption.period_orders(db, start=START, end=END, projects={})
    assert payload["cards"]["order_qty"] == "0.000"
    assert payload["cards"]["order_count"] == 0
    assert payload["rows"] == []


# ---------- M03 每日每项目已计消耗 ----------

def test_daily_consumption_qty_full_cost_known_only(db):
    """同日两条领用仅一条有成本：数量均计，金额只报已知（M-AC02）。"""
    project = _project(db, "M03-A")
    part = _part(db, "M03-PN")
    _costed_issue(db, project, part, 4, issue_date=date(2026, 10, 15),
                  unit_cost_ex=Decimal("25.00"))
    _issue(db, project, part, 3, issue_date=date(2026, 10, 15))
    # 人工来源另一天。
    _costed_issue(db, project, part, 2, issue_date=date(2026, 10, 16),
                  unit_cost_ex=Decimal("10.00"), cost_source="manual")
    # 期外/无效不进。
    _costed_issue(db, project, part, 9, issue_date=date(2026, 10, 12),
                  unit_cost_ex=Decimal("1.00"))
    _costed_issue(db, project, part, 8, issue_date=date(2026, 10, 17),
                  unit_cost_ex=Decimal("1.00"), status="void")
    db.commit()

    payload = consumption.daily_consumption(
        db, start=START, end=END, projects=_projects(db, project),
        include_amounts=True,
    )
    cards = payload["cards"]
    assert cards["issued_qty"] == "9.000"
    # 已知小计：ex = 4×25 + 2×10 = 120.00；inc = 113.00 + 22.60 = 135.60。
    assert cards["cost_amount_ex_tax"] == "120.00"
    assert cards["cost_amount_inc_tax"] == "135.60"
    assert cards["uncosted_qty"] == "3.000"
    assert cards["coverage_pct"] == round(6 / 9 * 100, 1)
    assert set(cards["sources"]) == {"direct_purchase", "manual"}
    assert "只读" in cards["note"]

    by_date = {row["issue_date"]: row for row in payload["rows"]}
    assert set(by_date) == {"2026-10-15", "2026-10-16"}
    day15 = by_date["2026-10-15"]
    assert day15["issued_qty"] == "7.000"
    assert day15["cost_amount_ex_tax"] == "100.00"
    assert day15["cost_amount_inc_tax"] == "113.00"
    assert day15["uncosted_qty"] == "3.000"
    assert day15["sources"] == ["direct_purchase"]


def test_daily_consumption_without_amounts_hides_cost_keys(db):
    """无金额权：只有数量，金额/覆盖/来源键整个不出现（不是 0）。"""
    project = _project(db, "M03-B")
    part = _part(db, "M03-PN-B")
    _costed_issue(db, project, part, 4, issue_date=date(2026, 10, 15),
                  unit_cost_ex=Decimal("25.00"))
    db.commit()

    payload = consumption.daily_consumption(
        db, start=START, end=END, projects=_projects(db, project),
        include_amounts=False,
    )
    assert payload["cards"]["issued_qty"] == "4.000"
    assert "cost_amount_ex_tax" not in payload["cards"]
    assert "coverage_pct" not in payload["cards"]
    assert "sources" not in payload["cards"]
    (row,) = payload["rows"]
    assert row["issued_qty"] == "4.000"
    assert "cost_amount_ex_tax" not in row
    assert "sources" not in row


def test_daily_consumption_scope_and_multi_project(db):
    authorized = _project(db, "M03-IN")
    other = _project(db, "M03-OUT")
    part = _part(db, "M03-PN-S")
    _costed_issue(db, authorized, part, 4, issue_date=date(2026, 10, 15),
                  unit_cost_ex=Decimal("25.00"))
    _costed_issue(db, other, part, 6, issue_date=date(2026, 10, 15),
                  unit_cost_ex=Decimal("50.00"))
    db.commit()

    payload = consumption.daily_consumption(
        db, start=START, end=END, projects=_projects(db, authorized),
        include_amounts=True,
    )
    assert payload["cards"]["issued_qty"] == "4.000"
    assert payload["cards"]["cost_amount_ex_tax"] == "100.00"
    # 空范围不扩全量；全未计成本窗口金额未知 → 金额键缺失（B-F11）。
    empty = consumption.daily_consumption(
        db, start=START, end=END, projects={}, include_amounts=True
    )
    assert empty["cards"]["issued_qty"] == "0.000"
    assert "cost_amount_ex_tax" not in empty["cards"]
    assert "cost_amount_inc_tax" not in empty["cards"]
    assert empty["rows"] == []


def test_empty_window_daily_consumption_200_both_roles(db):
    """B3 独立验收反例：窗口内无领用（范围非空）必须 200 空 shape，不 500。

    根因：daily_consumption cards["unit"] 的 next(iter(window_units)) 在
    空 dict 上 StopIteration（空范围走早退分支，故仅"非空范围×空窗口"触发）。
    语义保持：空窗口合法 unit=None/空桶；F11 金额键不出现（未知≠0）；
    同窗口有记录时原正常输出不变（下方 10-17 断言）。
    """
    project = _project(db, "M03-EW")
    part = _part(db, "M03-PN-EW")
    part.unit = "件"  # 本文件 _part 无单位默认；给单一单位正向断言用
    db.flush()
    # 有数据的日子（10-17）与被查询的空日（10-16）分离。
    _costed_issue(db, project, part, 3, issue_date=date(2026, 10, 17),
                  unit_cost_ex=Decimal("25.00"))
    db.commit()

    from tests.test_maintenance_dashboard_api import (
        BASE,
        _assign_manager,
        client_for,
    )

    empty_day = {"range": "custom",
                 "date_from": "2026-10-16", "date_to": "2026-10-16"}

    # 金额账号（B3 冻结证据原例：有权 admin 500 → 应 200）。
    admin = client_for(db, username="mdd-admin-ew", role="admin")
    response = admin.get(f"{BASE}/consumption", params=empty_day)
    assert response.status_code == 200
    body = response.json()
    assert body["cards"] == {
        "issued_qty": "0.000", "unit": None, "unit_breakdown": [],
        "uncosted_qty": "0.000", "coverage_pct": None,
        "sources": [], "note": body["cards"]["note"],
    }
    assert "只读" in body["cards"]["note"]
    # F11：空窗口无已计行 → 金额键整个不存在（不是 0.00）。
    assert "cost_amount_ex_tax" not in body["cards"]
    assert "cost_amount_inc_tax" not in body["cards"]
    assert body["rows"] == []

    # 数量账号（非空授权范围同样命中空窗口路径）。
    username = "mdd-manager-ew"
    manager = client_for(db, username=username, role="maintenance_manager")
    _assign_manager(db, username, project)
    manager_response = manager.get(f"{BASE}/consumption", params=empty_day)
    assert manager_response.status_code == 200
    assert manager_response.json()["cards"] == {
        "issued_qty": "0.000", "unit": None, "unit_breakdown": [],
    }
    assert manager_response.json()["rows"] == []

    # 同项目同筛选集、有记录的窗口：原正常输出不变（单位/金额/覆盖）。
    data_day = admin.get(f"{BASE}/consumption", params={
        "range": "custom", "date_from": "2026-10-17", "date_to": "2026-10-17",
    })
    assert data_day.status_code == 200
    data_cards = data_day.json()["cards"]
    assert data_cards["issued_qty"] == "3.000"
    assert data_cards["unit"] == "件"
    assert data_cards["unit_breakdown"] == [
        {"unit": "件", "issued_qty": "3.000", "costed_qty": "3.000",
         "uncosted_qty": "0.000", "coverage_pct": 100.0}
    ]
    assert data_cards["cost_amount_ex_tax"] == "75.00"
    assert data_cards["cost_amount_inc_tax"] == "84.75"
    (data_row,) = data_day.json()["rows"]
    assert data_row["issued_qty"] == "3.000"
    assert data_row["unit"] == "件"


# ---------- 领用明细（下钻可核对） ----------

def test_consumption_issue_rows_line_level_evidence(db):
    project = _project(db, "M03-D")
    part = _part(db, "M03-PN-D")
    _costed_issue(db, project, part, 4, issue_date=date(2026, 10, 15),
                  unit_cost_ex=Decimal("25.00"), cost_source="purchase_window")
    _issue(db, project, part, 3, issue_date=date(2026, 10, 15))
    db.commit()

    rows = consumption.consumption_issue_rows(
        db, start=START, end=END, projects=_projects(db, project),
        include_amounts=True, issue_date=date(2026, 10, 15),
    )
    assert len(rows) == 2
    costed = next(row for row in rows if row["cost_source"] == "purchase_window")
    assert costed["pn"] == part.pn_std
    assert costed["qty"] == "4.000"
    assert costed["cost_amount_ex_tax"] == "100.00"
    assert costed["cost_amount_inc_tax"] == "113.00"
    assert costed["unit_cost_ex_tax"] == "25.00"
    uncosted = next(row for row in rows if row["cost_source"] is None)
    assert uncosted["qty"] == "3.000"
    assert "cost_amount_ex_tax" not in uncosted

    hidden = consumption.consumption_issue_rows(
        db, start=START, end=END, projects=_projects(db, project),
        include_amounts=False, issue_date=date(2026, 10, 15),
    )
    assert len(hidden) == 2
    assert "cost_amount_ex_tax" not in hidden[0]
    assert "cost_source" not in hidden[0]


def test_consumption_readonly_does_not_mutate_costs(db):
    """查询前后业务事实不变：成本金额与库存/写路径零改动（只读验证）。"""
    project = _project(db, "M03-E")
    part = _part(db, "M03-PN-E")
    line = _costed_issue(db, project, part, 4, issue_date=date(2026, 10, 15),
                         unit_cost_ex=Decimal("25.00"))
    db.commit()
    before = (
        line.cost_amount_inc_tax, line.cost_amount_ex_tax, line.cost_source,
        line.algorithm_version, line.is_active,
    )
    consumption.daily_consumption(
        db, start=START, end=END, projects=_projects(db, project),
        include_amounts=True,
    )
    consumption.consumption_issue_rows(
        db, start=START, end=END, projects=_projects(db, project),
        include_amounts=True,
    )
    db.refresh(line)
    after = (
        line.cost_amount_inc_tax, line.cost_amount_ex_tax, line.cost_source,
        line.algorithm_version, line.is_active,
    )
    assert before == after
