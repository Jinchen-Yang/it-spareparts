"""维保数据看板数量口径（服务层）：期间、返还率、免返、退货分离与 T4。

口径见 docs/decisions/0022（R33 裁定）：
- 默认近 7 天含今天（上海日历日）；实收按 occurred_at 上海业务日入期。
- 同期返还率 = 期间实收 ÷ 期间实际领用；分母≤0 无率；领用>0 实收=0 → 0%；
  >100% 如实展示；不剔除免返项目，免返量单独一列不冲减。
- 退货 return_qty 只冲需求净量，与返件实收互不抵扣、不相加。
- T4 当前累计：逐单 N/R/max(N−R,0)/max(R−N,0)，不发布返还状态。
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services import maintenance_dashboard as dash
from tests.test_maintenance_analytics import _line, _part, _project
from tests.test_maintenance_return_metrics import _issue, _receipt

UTC = timezone.utc
TODAY = date(2026, 10, 21)


def _filters(db, *, start=date(2026, 10, 15), end=TODAY, scope=None, q=None,
             business_type=None):
    return dash._base_filters(
        db, scope=scope, start=start, end=end, q=q, business_type=business_type,
    )


# ---------- 时间范围解析 ----------

def test_resolve_date_range_presets():
    # 默认近 7 天含今天：今天−6 ～ 今天。
    assert dash.resolve_date_range("7d", None, None, today=TODAY) == (
        date(2026, 10, 15), TODAY
    )
    assert dash.resolve_date_range("15d", None, None, today=TODAY) == (
        date(2026, 10, 7), TODAY
    )
    assert dash.resolve_date_range("30d", None, None, today=TODAY) == (
        date(2026, 9, 22), TODAY
    )
    # 半年/一年按日历月（10-21 → 04-21 / 前一年 10-21）。
    assert dash.resolve_date_range("half_year", None, None, today=TODAY) == (
        date(2026, 4, 21), TODAY
    )
    assert dash.resolve_date_range("year", None, None, today=TODAY) == (
        date(2025, 10, 21), TODAY
    )
    # 日历月溢出收到月末：12-31 半年前 = 06-30。
    assert dash._shift_months(date(2026, 12, 31), 6) == date(2026, 6, 30)
    # all：无下界，上界仍为今天（不取未来事实）。
    assert dash.resolve_date_range("all", None, None, today=TODAY) == (None, TODAY)


def test_resolve_date_range_custom_and_errors():
    assert dash.resolve_date_range(
        "custom", date(2026, 10, 1), date(2026, 10, 20)
    ) == (date(2026, 10, 1), date(2026, 10, 20))
    with pytest.raises(dash.DashboardInputError) as missing:
        dash.resolve_date_range("custom", None, date(2026, 10, 20))
    assert missing.value.code == "custom_requires_dates"
    with pytest.raises(dash.DashboardInputError) as inverted:
        dash.resolve_date_range("custom", date(2026, 10, 2), date(2026, 10, 1))
    assert inverted.value.code == "invalid_date_range"
    with pytest.raises(dash.DashboardInputError) as bad_preset:
        dash.resolve_date_range("ytd", None, None)
    assert bad_preset.value.code == "invalid_range_preset"


def test_period_bounds_shanghai_midnights():
    lower, upper = dash._period_bounds(date(2026, 10, 15), date(2026, 10, 21))
    assert lower == datetime(2026, 10, 14, 16, tzinfo=UTC)      # 10-15 00:00 上海
    assert upper == datetime(2026, 10, 21, 16, tzinfo=UTC)      # 10-22 00:00 上海（半开）


# ---------- T1 总览：返还率三分支与退货分离 ----------

def test_summary_rate_formula_three_cases(db):
    none_project = _project(db, "MDD-R0")     # 无领用 → 无率
    zero_project = _project(db, "MDD-RZ")     # 有领用无实收 → 0%
    over_project = _project(db, "MDD-RO")     # >100% 如实展示
    part = _part(db, "MDD-RATE-PN")
    _receipt(db, none_project, part, "5",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    _issue(db, zero_project, part, "4", issue_date=date(2026, 10, 16))
    _receipt(db, over_project, part, "3",
             occurred_at=datetime(2026, 10, 17, 8, tzinfo=UTC))
    _issue(db, over_project, part, "2", issue_date=date(2026, 10, 17))
    db.commit()

    rows = {
        row["project_id"]: row
        for row in dash.build_project_rows(db, _filters(db, scope={
            none_project.project_id, zero_project.project_id, over_project.project_id,
        }), include_amounts=False)
    }
    none_row = rows[none_project.project_id]
    assert none_row["return_rate_pct"] is None
    assert none_row["rate_numerator"] == "5.000"
    assert none_row["rate_denominator"] == "0.000"
    assert rows[zero_project.project_id]["return_rate_pct"] == 0.0
    assert rows[over_project.project_id]["return_rate_pct"] == 150.0
    # 无金额权：行内不出现任何估价键。
    assert all("valuation" not in row for row in rows.values())


def test_return_qty_never_offsets_receipts(db):
    project = _project(db, "MDD-RET")
    part = _part(db, "MDD-RET-PN")
    # 需求行退货 5（return_qty 只冲需求净量）。
    _line(db, project, part, order_no="WBDD-MDD-RET", order_date=date(2026, 10, 10),
          qty=Decimal("10"), return_qty=Decimal("5"))
    # 返件实收 4（独立事实）。
    _receipt(db, project, part, "4",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    _issue(db, project, part, "10", issue_date=date(2026, 10, 16))
    db.commit()

    payload = dash.build_summary_payload(db, _filters(db), include_amounts=False)
    cards = payload["cards"]
    # 实收 4（不是 4−5，也不是 4+5）；领用 10；率 = 4/10。
    assert cards["receipt_qty"]["total"] == "4.000"
    assert cards["issued_qty"]["total"] == "10.000"
    assert cards["issued_qty"]["note"] == "实际领用量（未采集件况）"
    assert cards["return_rate_pct"] == 40.0
    assert "valuation" not in cards


def test_exempt_qty_separate_column_no_deduction(db):
    project = _project(db, "MDD-EXE")
    part = _part(db, "MDD-EXE-PN")
    # 免返项目：领用 7 全部免返（5 显式免返 + 2 项目默认免返）。
    exempt_line = _issue(db, project, part, "5", issue_date=date(2026, 10, 16),
                         no_return=True)
    _issue(db, project, part, "2", issue_date=date(2026, 10, 16))
    _receipt(db, project, part, "2",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    db.commit()
    project.no_return_default = True
    db.commit()

    rows = dash.build_project_rows(db, _filters(db), include_amounts=False)
    row = rows[0]
    # 免返量单独一列（7），不冲减实收/领用/率（率 = 2/7，不为 2/0）。
    assert row["exempt_issued_qty"] == "7.000"
    assert row["issued_qty"] == "7.000"
    assert row["receipt_qty"] == "2.000"
    assert row["return_rate_pct"] == pytest.approx(28.6)
    assert exempt_line.no_return is True


def test_undated_receipts_excluded_from_period_but_reported(db):
    project = _project(db, "MDD-UND")
    part = _part(db, "MDD-UND-PN")
    _receipt(db, project, part, "6",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    _receipt(db, project, part, "3", occurred_at=None)
    db.commit()

    payload = dash.build_summary_payload(db, _filters(db), include_amounts=False)
    assert payload["cards"]["receipt_qty"]["total"] == "6.000"
    assert any("3.000" in note and "无业务日期" in note for note in payload["notes"])


def test_daily_trend_shanghai_day_bucketing_and_zero_fill(db):
    project = _project(db, "MDD-TREND")
    part = _part(db, "MDD-TREND-PN")
    # 10-15 16:00 UTC = 上海 10-16 00:00 → 入 10-16 桶。
    _receipt(db, project, part, "4",
             occurred_at=datetime(2026, 10, 15, 16, tzinfo=UTC))
    _receipt(db, project, part, "1",
             occurred_at=datetime(2026, 10, 15, 15, 59, tzinfo=UTC))  # 上海 10-15
    _issue(db, project, part, "2", issue_date=date(2026, 10, 18))
    db.commit()

    trend = dash.daily_trend(db, _filters(db))
    by_day = {item["date"]: item for item in trend}
    assert [item["date"] for item in trend] == [
        date(2026, 10, day).isoformat() for day in range(15, 22)
    ]  # 7 天补零
    assert by_day["2026-10-15"]["receipt_qty"] == "1.000"
    assert by_day["2026-10-16"]["receipt_qty"] == "4.000"
    assert by_day["2026-10-18"]["issued_qty"] == "2.000"
    assert by_day["2026-10-17"] == {"date": "2026-10-17",
                                    "receipt_qty": "0.000", "issued_qty": "0.000"}


def test_summary_condition_structure_and_top_projects(db):
    project = _project(db, "MDD-COND")
    part = _part(db, "MDD-COND-PN")
    for qty, condition in (("4", "坏品"), ("2", "废品"), ("1", None)):
        _receipt(db, project, part, qty, condition=condition,
                 occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    db.commit()
    payload = dash.build_summary_payload(db, _filters(db), include_amounts=False)
    cards = payload["cards"]
    assert cards["receipt_qty"]["total"] == "7.000"
    assert cards["bad_receipt_qty"]["total"] == "6.000"
    assert cards["bad_receipt_qty"]["scrap"] == "2.000"
    structure = {item["test_result"]: item["qty"]
                 for item in payload["condition_structure"]}
    assert structure["坏品"] == "4.000"
    assert structure["废品"] == "2.000"
    assert structure["未填"] == "1.000"
    assert payload["top_projects"][0]["project_id"] == project.project_id


def test_issue_validity_conditions(db):
    project = _project(db, "MDD-VAL-ISSUE")
    part = _part(db, "MDD-VI-PN")
    _issue(db, project, part, "3", issue_date=date(2026, 10, 16))            # 有效
    _issue(db, project, part, "100", status="draft", issue_date=date(2026, 10, 16))
    _issue(db, project, part, "100", status="unknown", issue_date=date(2026, 10, 16))
    _issue(db, project, part, "100", active=False, issue_date=date(2026, 10, 16))
    db.commit()
    payload = dash.build_summary_payload(db, _filters(db), include_amounts=False)
    assert payload["cards"]["issued_qty"]["total"] == "3.000"


# ---------- 项目范围 ----------

def test_scope_empty_set_returns_nothing_not_full(db):
    project = _project(db, "MDD-SCOPE")
    part = _part(db, "MDD-SCOPE-PN")
    _receipt(db, project, part, "5",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    db.commit()
    # 空授权集：恒假条件，绝不回退全量。
    payload = dash.build_summary_payload(db, _filters(db, scope=set()),
                                         include_amounts=False)
    assert payload["cards"]["receipt_qty"]["total"] == "0.000"
    rows = dash.build_project_rows(db, _filters(db, scope=set()),
                                   include_amounts=False)
    assert rows == []


def test_project_rows_include_zero_rows_within_scope(db):
    busy = _project(db, "MDD-BUSY")
    idle = _project(db, "MDD-IDLE")
    part = _part(db, "MDD-ZR-PN")
    _receipt(db, busy, part, "5",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    db.commit()
    rows = dash.build_project_rows(
        db, _filters(db, scope={busy.project_id, idle.project_id}),
        include_amounts=False,
    )
    assert {row["project_code"] for row in rows} == {
        busy.project_code, idle.project_code,
    }
    idle_row = next(row for row in rows
                    if row["project_code"] == idle.project_code)
    assert idle_row["receipt_qty"] == "0.000"
    assert idle_row["return_rate_pct"] is None


def test_unlinked_receipts_by_project(db):
    project = _project(db, "MDD-UNLINK")
    part = _part(db, "MDD-UNLINK-PN")
    _receipt(db, project, part, "5",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))  # 未关联需求单
    db.commit()
    rows = dash.build_project_rows(db, _filters(db), include_amounts=False)
    assert rows[0]["unlinked_receipt_qty"] == "5.000"


# ---------- T4 需求单数量核对 ----------

def _demand(db, project, part, order_no, qty):
    line = _line(db, project, part, order_no=order_no,
                 order_date=date(2026, 10, 10), qty=Decimal(qty))
    db.flush()
    return line


def _order_of(db, line):
    from sqlalchemy import select

    from app.models.maintenance import FMaintenanceOrder

    return db.scalar(select(FMaintenanceOrder).where(
        FMaintenanceOrder.id == line.order_id))


def test_quantity_checks_current_cumulative(db):
    project = _project(db, "MDD-T4")
    part = _part(db, "MDD-T4-PN")
    order_line = _demand(db, project, part, "WBDD-MDD-T4-1", "10")
    over_line = _demand(db, project, part, "WBDD-MDD-T4-2", "3")
    _receipt(db, project, part, "4",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC),
             source_order_id=_order_of(db, order_line).raw_order_id)
    _receipt(db, project, part, "5",
             occurred_at=datetime(2026, 10, 17, 8, tzinfo=UTC),
             source_order_id=_order_of(db, over_line).raw_order_id)
    db.commit()

    payload = dash.build_quantity_checks_payload(
        db, None, projects=dash._project_rows(db, None),
        order_date_from=date(2026, 10, 1), order_date_to=date(2026, 10, 21),
    )
    rows = {row["order_no"]: row for row in payload["rows"]}
    assert rows["WBDD-MDD-T4-1"]["basis_state"] == "complete"
    assert rows["WBDD-MDD-T4-1"]["demand_qty"] == "10.000"
    assert rows["WBDD-MDD-T4-1"]["received_qty"] == "4.000"
    assert rows["WBDD-MDD-T4-1"]["difference_qty"] == "6.000"
    assert rows["WBDD-MDD-T4-1"]["over_received_qty"] == "0.000"
    assert rows["WBDD-MDD-T4-2"]["difference_qty"] == "0.000"
    assert rows["WBDD-MDD-T4-2"]["over_received_qty"] == "2.000"
    # 不发布「欠返/逾期/已返」状态：行内没有任何返还状态字段。
    for row in rows.values():
        assert not ({"status", "return_status", "overdue", "returned"} & set(row))
    cards = payload["cards"]
    assert cards["checked_demand_qty"] == "13.000"
    assert cards["received_qty"] == "9.000"
    assert cards["known_difference_qty"] == "6.000"
    assert cards["over_received_qty"] == "2.000"
    assert "当前累计" in cards["note"]


def test_quantity_checks_scoped_and_order_date_filter(db):
    kept = _project(db, "MDD-T4A")
    hidden = _project(db, "MDD-T4B")
    part = _part(db, "MDD-T4S-PN")
    kept_line = _demand(db, kept, part, "WBDD-MDD-T4-KEEP", "5")
    _demand(db, hidden, part, "WBDD-MDD-T4-HIDE", "7")
    old_line = _demand(db, kept, part, "WBDD-MDD-T4-OLD", "9")
    _order_of(db, old_line).order_date = date(2026, 9, 1)
    db.commit()
    payload = dash.build_quantity_checks_payload(
        db, {kept.project_id}, projects=dash._project_rows(db, {kept.project_id}),
        order_date_from=date(2026, 10, 1), order_date_to=date(2026, 10, 21),
    )
    # 范围外项目与制单日期在期外的单都不出现。
    assert {row["order_no"] for row in payload["rows"]} == {"WBDD-MDD-T4-KEEP"}
    assert payload["rows"][0]["demand_qty"] == "5.000"
    assert payload["rows"][0]["project_code"] == kept.project_code
    assert _order_of(db, kept_line).order_no == "WBDD-MDD-T4-KEEP"


def test_quantity_checks_unassigned_receipts_full_scope_only(db):
    project = _project(db, "MDD-T4U")
    part = _part(db, "MDD-T4U-PN")
    _demand(db, project, part, "WBDD-MDD-T4U", "2")
    _receipt(db, project, part, "3",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))  # 未关联
    db.commit()
    full = dash.build_quantity_checks_payload(
        db, None, projects=dash._project_rows(db, None),
        order_date_from=date(2026, 10, 1), order_date_to=date(2026, 10, 21),
    )
    assert full["cards"]["unassigned_receipt_qty"] == "3.000"
    assert full["unassigned_receipts"][0]["project_code"] == project.project_code
    scoped = dash.build_quantity_checks_payload(
        db, {project.project_id},
        projects=dash._project_rows(db, {project.project_id}),
        order_date_from=date(2026, 10, 1), order_date_to=date(2026, 10, 21),
    )
    # 受限账号不展示跨项目未关联实收（信息可能越权）。
    assert scoped["unassigned_receipts"] == []
    assert scoped["cards"]["unassigned_receipt_qty"] == "0.000"


# ---------- 排序与分页 ----------

def test_sorts_and_pagination(db):
    alpha = _project(db, "MDD-SRT-A")
    beta = _project(db, "MDD-SRT-B")
    part = _part(db, "MDD-SRT-PN")
    _receipt(db, beta, part, "5", condition="坏品",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    _receipt(db, alpha, part, "2", condition="废品",
             occurred_at=datetime(2026, 10, 17, 8, tzinfo=UTC))
    db.commit()
    rows = dash.build_project_rows(db, _filters(db), include_amounts=False)
    by_bad = dash.sort_project_rows(rows, "bad_receipt_qty_desc")
    assert [row["project_code"] for row in by_bad][:2] == [
        beta.project_code, alpha.project_code,
    ]
    by_code = dash.sort_project_rows(rows, "project_code_asc")
    assert [row["project_code"] for row in by_code] == sorted(
        row["project_code"] for row in rows
    )
    paged = dash.paginate(by_code, 1, 1)
    assert len(paged["rows"]) == 1
    assert paged["pagination"]["total"] == len(by_code)
    with pytest.raises(dash.DashboardInputError):
        dash.sort_project_rows(rows, "no_such_sort")
    with pytest.raises(dash.DashboardInputError):
        dash.sort_part_rows(
            dash.build_part_rows(db, _filters(db), include_amounts=False),
            "no_such_sort",
        )
