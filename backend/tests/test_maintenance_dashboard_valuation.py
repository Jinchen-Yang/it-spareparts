"""维保数据看板估价（R34 / D-0024）：三域并列、可空试算比例、7 天锚点窗。

- 三域 = 采购 / 销售（市场参考，采购优先销售回退）+ 领用已计成本（独立视角，
  不暗替）；估值按域分别给出，不相加、不混均价。
- 「价值保留比例」是可空试算参数 [0,1]：无输入 → 坏品行「待试算」，绝不算 0，
  也不代选默认；成品行按参考全值另列。
- 窗口 = 含统计结束日的 7 个上海日历日；不取锚点后交易。
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.config import get_settings
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch
from app.services import maintenance_dashboard as dash
from app.services.maintenance_dashboard_valuation import (
    batch_price_references,
    value_line,
)
from tests.test_maintenance_analytics import _project
from tests.test_maintenance_return_metrics import _issue, _receipt

ANCHOR = date(2026, 10, 19)
UTC = timezone.utc


def _part(db, pn, *, unit=None, description=""):
    part = DimPart(pn_std=pn, unit=unit, description=description)
    db.add(part)
    db.flush()
    return part


def _filters(db, *, start=date(2026, 10, 13), end=ANCHOR, scope=None, q=None):
    return dash._base_filters(db, scope=scope, start=start, end=end, q=q)


def _batch(db, file_type="purchase") -> SysImportBatch:
    batch = SysImportBatch(
        filename=f"mdd-{file_type}.xlsx", file_type=file_type,
        file_hash=uuid.uuid4().hex, status="success",
    )
    db.add(batch)
    db.flush()
    return batch


def _purchase(db, part, key, *, order_date=ANCHOR, unit="件",
              qty=Decimal(2), price=Decimal("113.00"),
              tax_rate=Decimal("0.13"), is_tax_inclusive=True):
    order = FPurchaseOrder(
        raw_order_id=key, order_no=f"MDD-CG-{key}", purchaser="t",
        data_status="已生效", order_date=order_date, tax_rate=tax_rate,
        is_tax_inclusive=is_tax_inclusive, import_batch_id=_batch(db).id,
    )
    db.add(order)
    db.flush()
    db.add(FPurchaseLine(
        raw_line_id=f"{key}-l1", order_id=order.id, line_no=1, part_id=part.id,
        pn_std=part.pn_std, qty=qty, unit_price=price, unit=unit,
        import_batch_id=order.import_batch_id,
    ))
    db.flush()
    return order


def _sale(db, part, key, *, order_date=ANCHOR, unit="件",
          qty=Decimal(2), price=Decimal("113.00"), counts_revenue=True):
    order = FSalesOrder(
        raw_order_id=key, order_no=f"MDD-XS-{key}", salesperson="t",
        business_type="备件销售", data_status="已生效", order_date=order_date,
        tax_rate=Decimal("0.13"), import_batch_id=_batch(db, "sales").id,
    )
    db.add(order)
    db.flush()
    db.add(FSalesLine(
        raw_line_id=f"{key}-l1", order_id=order.id, line_no=1, part_id=part.id,
        pn_std=part.pn_std, qty=qty, unit_price=price, unit=unit,
        counts_revenue=counts_revenue, import_batch_id=order.import_batch_id,
    ))
    db.flush()
    return order


def _costed_issue(db, project, part, qty, *, issue_date=date(2026, 10, 16),
                  unit_cost_ex=Decimal("25.00"), cost_source="direct_purchase",
                  status="confirmed"):
    """领用行 + 完整已计成本证据（满足 dual_tax CHECK 全有或全无）。"""
    line = _issue(db, project, part, qty, status=status, issue_date=issue_date)
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


# ---------- 行金额（qty × 参考价 × 比例，HALF_UP 两位） ----------

def test_value_line_rounds_half_up_per_line():
    # 含税 3×0.05×0.2=0.030；未税 3×0.04×0.2=0.024 → HALF_UP 两位 = 0.02。
    inc, ex = value_line(Decimal(3), Decimal("0.2"), (Decimal("0.05"), Decimal("0.04")))
    assert inc == Decimal("0.03")
    assert ex == Decimal("0.02")
    # 0.005 边界：HALF_UP 入位。
    inc, _ = value_line(Decimal(1), Decimal("1"), (Decimal("0.005"), Decimal("0.005")))
    assert inc == Decimal("0.01")


# ---------- 批量三域参考 ----------

def test_batch_references_purchase_preferred_sales_fallback(db):
    both = _part(db, "MDD-REF-BOTH", unit="件")
    _purchase(db, both, "ref-both-cg")           # 113.00 含税 / 100.00 未税
    _sale(db, both, "ref-both-xs")
    mismatch = _part(db, "MDD-REF-MISMATCH", unit="件")
    _purchase(db, mismatch, "ref-mm-cg", unit="箱")  # 采购单位 ≠ 主数据
    _sale(db, mismatch, "ref-mm-xs", unit="件")      # 销售同单位可用
    refs = batch_price_references(
        db, {both.id, mismatch.id}, anchor=ANCHOR, scope=None,
        settings=get_settings(),
    )
    assert refs[both.id].chosen == "purchase"
    assert refs[both.id].chosen_reason is None
    assert refs[both.id].purchase.available
    assert (str(refs[both.id].purchase.inc_tax), str(refs[both.id].purchase.ex_tax)) == (
        "113.00", "100.00"
    )
    # 采购域单位不一致 → 不可用；销售同单位 → 回退可用（原因显式给出）。
    assert refs[mismatch.id].purchase.available is False
    assert refs[mismatch.id].purchase.unavailable_reason == "unit_mismatch"
    assert refs[mismatch.id].chosen == "sales"
    assert refs[mismatch.id].chosen_reason == "purchase_fallback"
    assert refs[mismatch.id].price == (Decimal("113.00"), Decimal("100.00"))
    # 成本域独立：无领用样本 → 不可用但并列呈现（不影响市场域选择）。
    assert refs[both.id].cost.available is False
    assert refs[both.id].cost.unavailable_reason == "no_samples"


def test_batch_references_unit_rules_and_both_unavailable(db):
    missing_master = _part(db, "MDD-U-MASTER", unit=None)
    _sale(db, missing_master, "u-master-xs", unit="件")
    missing_sample = _part(db, "MDD-U-SAMPLE", unit="件")
    _sale(db, missing_sample, "u-sample-xs", unit=None)
    mixed = _part(db, "MDD-U-MIXED", unit="件")
    _sale(db, mixed, "u-mixed-xs-1", unit="件")
    _sale(db, mixed, "u-mixed-xs-2", unit="板")
    none = _part(db, "MDD-U-NONE", unit="件")   # 两域都无样本
    refs = batch_price_references(
        db, {missing_master.id, missing_sample.id, mixed.id, none.id},
        anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    assert refs[missing_master.id].sales.unavailable_reason == "unit_missing_master"
    assert refs[missing_sample.id].sales.unavailable_reason == "unit_missing_sample"
    assert refs[mixed.id].sales.unavailable_reason == "unit_mixed_samples"
    for part_id in (missing_master.id, missing_sample.id, mixed.id):
        assert refs[part_id].chosen is None
    assert refs[none.id].chosen is None
    assert refs[none.id].chosen_reason == "no_samples"


def test_batch_references_window_is_seven_days_no_future_trades(db):
    """R34-01：窗口 = [anchor-6, anchor]；7 天前与锚点后交易都不取。"""
    part = _part(db, "MDD-WINDOW", unit="件")
    _purchase(db, part, "win-future", order_date=ANCHOR + timedelta(days=1))
    _purchase(db, part, "win-stale", order_date=ANCHOR - timedelta(days=7))
    _purchase(db, part, "win-edge", order_date=ANCHOR - timedelta(days=6))
    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    assert refs[part.id].chosen == "purchase"
    assert refs[part.id].purchase.available
    assert refs[part.id].purchase.latest_order_date == ANCHOR - timedelta(days=6)
    assert refs[part.id].purchase.oldest_order_date == ANCHOR - timedelta(days=6)
    assert refs[part.id].purchase.order_count == 1


# ---------- 成本域（领用已计成本，R34-02） ----------

def test_cost_domain_qty_weighted_with_uncosted_and_coverage(db):
    project = _project(db, "MDD-COST")
    part = _part(db, "MDD-COST-PN", unit="件")
    # 10-16：qty4 × ex25（inc 28.25）→ 100.00/113.00；10-17：qty6 × ex50 → 300.00/339.00。
    _costed_issue(db, project, part, 4, issue_date=date(2026, 10, 16),
                  unit_cost_ex=Decimal("25.00"))
    _costed_issue(db, project, part, 6, issue_date=date(2026, 10, 17),
                  unit_cost_ex=Decimal("50.00"), cost_source="purchase_window")
    # 未计成本行 qty3：数量计入、金额不计。
    _issue(db, project, part, 3, issue_date=date(2026, 10, 18))
    # 窗外领用（10-10）不进成本域。
    _costed_issue(db, project, part, 5, issue_date=date(2026, 10, 10),
                  unit_cost_ex=Decimal("99.00"))
    db.commit()

    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    cost = refs[part.id].cost
    assert cost.available is True
    # 数量加权：inc = (113+339)/10 = 45.20；ex = (100+300)/10 = 40.00。
    assert (format(cost.unit_cost_inc_tax, ".2f"),
            format(cost.unit_cost_ex_tax, ".2f")) == ("45.20", "40.00")
    assert cost.costed_qty == Decimal("10")
    assert cost.uncosted_qty == Decimal("3")
    assert cost.issue_qty == Decimal("13")
    assert cost.coverage_pct == round(10 / 13 * 100, 1)
    assert set(cost.sources) == {"direct_purchase", "purchase_window"}
    payload = cost.payload()
    assert "锚点" in payload["note"]
    assert payload["available"] is True


def test_cost_domain_respects_authorized_scope(db):
    """R34-02：成本域只取授权项目内领用；空范围不得扩全量。"""
    allowed = _project(db, "MDD-COST-IN")
    denied = _project(db, "MDD-COST-OUT")
    part = _part(db, "MDD-SCOPE-PN", unit="件")
    _costed_issue(db, allowed, part, 4, unit_cost_ex=Decimal("25.00"))
    _costed_issue(db, denied, part, 6, unit_cost_ex=Decimal("50.00"))
    db.commit()

    scoped = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope={allowed.project_id},
        settings=get_settings(),
    )
    cost = scoped[part.id].cost
    assert cost.available is True
    # 只含授权项目行：inc = 113/4 = 28.25。
    assert format(cost.unit_cost_inc_tax, ".2f") == "28.25"
    assert cost.costed_qty == Decimal("4")

    empty = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=set(), settings=get_settings(),
    )
    assert empty[part.id].cost.available is False
    assert empty[part.id].cost.unavailable_reason == "no_samples"


def test_cost_domain_lines_without_cost_is_not_recorded(db):
    """有领用但全部未计成本 → cost_not_recorded（不与无领用混同）。"""
    project = _project(db, "MDD-NOCOST")
    part = _part(db, "MDD-NOCOST-PN", unit="件")
    _issue(db, project, part, 3, issue_date=date(2026, 10, 16))
    db.commit()
    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    cost = refs[part.id].cost
    assert cost.available is False
    assert cost.unavailable_reason == "cost_not_recorded"
    assert cost.payload() == {
        "available": False, "unavailable_reason": "cost_not_recorded",
    }


def test_cost_domain_excludes_void_and_unmapped_issues(db):
    project = _project(db, "MDD-COST-VOID")
    part = _part(db, "MDD-COSTV-PN", unit="件")
    _costed_issue(db, project, part, 4, status="void",
                  unit_cost_ex=Decimal("25.00"))
    _issue(db, project, part, 6, status="unknown",
           issue_date=date(2026, 10, 17))   # unmapped
    db.commit()
    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    assert refs[part.id].cost.available is False


# ---------- 行级估价（build_valuation，R34 比例语义） ----------

def test_build_valuation_three_domains_with_ratio(db):
    """比例 0.5：坏品行三域分别给值；主展示=采购优先，成本不暗替。"""
    project = _project(db, "MDD-3DOM")
    part = _part(db, "MDD-3DOM-PN", unit="件")
    _purchase(db, part, "dom-cg")                       # 113.00 / 100.00
    _sale(db, part, "dom-xs")
    _costed_issue(db, project, part, 10, unit_cost_ex=Decimal("40.00"))
    _receipt(db, project, part, "2", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    line = next(iter(bundle.line_values.values()))
    assert line["state"] == "ready"
    assert line["chosen"] == "purchase"
    assert line["factor"] == "0.5"
    # 采购 2×113×0.5=113.00 / 2×100×0.5=100.00（主展示口径）。
    assert (line["value_inc_tax"], line["value_ex_tax"]) == ("113.00", "100.00")
    domains = line["domains"]
    assert domains["purchase"]["value_inc_tax"] == "113.00"
    assert domains["sales"]["value_inc_tax"] == "113.00"
    # 成本域独立：2×45.20×0.5=45.20 / 2×40×0.5=40.00（inc: 40×1.13=45.20）。
    assert domains["cost"]["value_inc_tax"] == "45.20"
    assert domains["cost"]["value_ex_tax"] == "40.00"
    # 合计只取主展示域，不相加三域。
    assert bundle.total_bad_inc == Decimal("113.00")
    assert bundle.total_bad_ex == Decimal("100.00")


def test_build_valuation_without_ratio_is_awaiting_not_zero(db):
    """无比例：坏品行待试算（不算 0、不代选默认）；成品行按全值另列。"""
    project = _project(db, "MDD-AWAIT")
    part = _part(db, "MDD-AWAIT-PN", unit="件")
    _purchase(db, part, "await-cg")
    _receipt(db, project, part, "2", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    _receipt(db, project, part, "5", condition="成品",
             occurred_at=datetime(2026, 10, 15, 9, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db), ratio=None)
    states = {line["classification"]: line for line in bundle.line_values.values()}
    assert states["bad"]["state"] == "awaiting_ratio"
    assert "value_inc_tax" not in states["bad"]
    # 成品不折算：5×100=500.00（未税）照常估。
    assert states["finished"]["state"] == "ready"
    assert states["finished"]["value_ex_tax"] == "500.00"
    assert bundle.total_bad_inc == Decimal("0")
    assert bundle.total_finished_ex == Decimal("500.00")
    assert bundle.awaiting_ratio_bad_qty == Decimal("2")
    assert bundle.estimated_bad_qty == Decimal("0")


def test_build_valuation_line_rounding_then_sum(db):
    project = _project(db, "MDD-VAL")
    part = _part(db, "MDD-VAL-PN", unit="件")
    _purchase(db, part, "val-cg", qty=Decimal(1), price=Decimal("0.05"),
              is_tax_inclusive=False)
    # 3 条 坏品 各 qty 1：行 ex = 1×0.05×0.2 = 0.01；合计 0.03（逐行取整口径）。
    for index in range(3):
        _receipt(
            db, project, part, "1", condition="坏品",
            occurred_at=datetime(2026, 10, 15, 8 + index, tzinfo=UTC),
        )
    db.commit()

    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.2"))
    assert bundle.total_bad_ex == Decimal("0.03")
    assert bundle.estimated_bad_qty == Decimal("3")


def test_unknown_condition_not_valued_and_pending_never_zero(db):
    """件况未知不估价：行级标待估；绝不出现在价值合计里（不是按 0 元估价）。"""
    project = _project(db, "MDD-UNK")
    part = _part(db, "MDD-UNK-PN", unit="件")
    _purchase(db, part, "unk-cg")
    _receipt(db, project, part, "4", condition=None,
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    _receipt(db, project, part, "3", condition="其他",
             occurred_at=datetime(2026, 10, 15, 9, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    assert bundle.estimated_bad_qty == Decimal("0")
    assert bundle.pending_bad_qty == Decimal("0")
    assert bundle.total_bad_inc == Decimal("0")
    reasons = {
        (item["state"], item.get("reason"))
        for item in bundle.line_values.values()
    }
    assert reasons == {("pending", "unknown_condition")}

    rows = dash.build_part_rows(
        db, _filters(db), include_amounts=True, ratio=Decimal("0.5")
    )
    row = rows[0]
    assert row["receipt_qty"] == "7.000"
    assert row["bad_receipt_qty"] == "0.000"
    valuation = row["valuation"]
    assert valuation["estimated_bad_qty"] == "0.000"
    assert valuation["pending_bad_qty"] == "0.000"
    assert valuation["coverage_pct"] is None  # M02=0 → 无比例


def test_bad_condition_without_price_is_pending_not_zero(db):
    """坏件实收但无合格市场参考且无成本参考 → 待估坏件量单独呈现，不算 0 元。"""
    project = _project(db, "MDD-NOPX")
    part = _part(db, "MDD-NOPX-PN", unit="件")
    _purchase(db, part, "nopx-cg", unit="箱")   # 单位不合 → 采购不可用
    _sale(db, part, "nopx-xs", unit="板")       # 单位不合 → 销售不可用
    _receipt(db, project, part, "4", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    assert bundle.estimated_bad_qty == Decimal("0")
    assert bundle.total_bad_inc == Decimal("0")
    assert bundle.pending_bad_qty == Decimal("4")
    assert bundle.project_pending_bad_qty[project.project_id] == Decimal("4")
    (bad_line,) = bundle.line_values.values()
    assert bad_line["state"] == "pending"
    assert "unit_mismatch" in bad_line["reason"]

    rows = dash.build_part_rows(
        db, _filters(db), include_amounts=True, ratio=Decimal("0.5")
    )
    valuation = rows[0]["valuation"]
    assert valuation["pending_bad_qty"] == "4.000"
    assert valuation["coverage_pct"] == 0.0  # 已估 0 ÷ 坏件实收 4


def test_cost_domain_alone_does_not_substitute_market(db):
    """只有成本参考（无市场样本）：坏品行仍待估——成本不暗替市场参考。"""
    project = _project(db, "MDD-COSTONLY")
    part = _part(db, "MDD-COSTONLY-PN", unit="件")
    _costed_issue(db, project, part, 10, unit_cost_ex=Decimal("40.00"))
    _receipt(db, project, part, "2", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    (line,) = bundle.line_values.values()
    assert line["state"] == "pending"
    assert line["reason"] == "no_samples"
    # 但成本域参考值并列给出（独立视角）。
    assert line["domains"]["cost"]["available"] is True
    assert line["domains"]["cost"]["value_inc_tax"] == "45.20"
    assert bundle.pending_bad_qty == Decimal("2")


def test_cost_only_market_missing_keeps_cost_domain_visible_everywhere(db):
    """预审反例 1：市场两域缺样、成本域有效——成本域的 PN 级与总览三域
    试算值都必须并列可见；某域缺样本不能抹掉其它域（R34-05）。"""
    project = _project(db, "MDD-COSTVIS")
    part = _part(db, "MDD-COSTVIS-PN", unit="件")
    _costed_issue(db, project, part, 10, unit_cost_ex=Decimal("40.00"))
    _receipt(db, project, part, "2", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()

    filters = _filters(db)
    bundle = dash.build_valuation(db, filters, ratio=Decimal("0.5"))
    # 行级（既有行为）：主展示待估，成本域并列给值。
    (line,) = bundle.line_values.values()
    assert line["state"] == "pending"
    assert line["domains"]["cost"]["value_inc_tax"] == "45.20"
    # 总览三域合计：成本域可见（2×45.20×0.5=45.20）。
    assert bundle.total_bad_domains["cost"]["inc"] == Decimal("45.20")
    assert bundle.total_bad_domains["cost"]["ex"] == Decimal("40.00")

    # PN 级（T3 行）：待估主卡 + 成本域并列值。
    rows = dash.build_part_rows(db, filters, include_amounts=True,
                                ratio=Decimal("0.5"))
    valuation = rows[0]["valuation"]
    assert valuation["state"] == "pending"
    assert valuation["pending_bad_qty"] == "2.000"
    assert "bad_estimated_value_inc_tax" not in valuation
    assert valuation["domains"]["cost"]["value_inc_tax"] == "45.20"
    assert valuation["domains"]["cost"]["value_ex_tax"] == "40.00"
    assert "purchase" not in valuation["domains"]

    # 总览卡：市场待估，domains.cost 并列。
    summary = dash.build_summary_payload(db, filters, include_amounts=True,
                                         ratio=Decimal("0.5"))
    card = summary["cards"]["valuation"]
    assert card["pending_bad_qty"] == "2.000"
    assert card["domains"]["cost"]["value_inc_tax"] == "45.20"


def test_finished_lines_do_not_pollute_bad_domain_aggregates(db):
    """成品行域值另列：不得混入坏件三域聚合（与总览口径一致）。"""
    project = _project(db, "MDD-FINMIX")
    part = _part(db, "MDD-FINMIX-PN", unit="件")
    _purchase(db, part, "finmix-cg")                       # 113.00 / 100.00
    _receipt(db, project, part, "2", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    _receipt(db, project, part, "5", condition="成品",
             occurred_at=datetime(2026, 10, 15, 9, tzinfo=UTC))
    db.commit()

    filters = _filters(db)
    bundle = dash.build_valuation(db, filters, ratio=Decimal("0.5"))
    # 坏件域值 = 2×113×0.5 = 113.00；成品 5×113×0.5=282.50 不得混入。
    assert bundle.total_bad_domains["purchase"]["inc"] == Decimal("113.00")

    rows = dash.build_part_rows(db, filters, include_amounts=True,
                                ratio=Decimal("0.5"))
    valuation = rows[0]["valuation"]
    assert valuation["domains"]["purchase"]["value_inc_tax"] == "113.00"
    # 成品另列（全值，不混坏件主卡）。
    assert valuation["finished_estimated_value_inc_tax"] == "565.00"
    assert valuation["finished_estimated_value_ex_tax"] == "500.00"


def test_cost_domain_requires_master_unit(db):
    """预审反例 2（P-B02）：领用行无单位列，唯一单位证据是主数据单位；
    主单位 None/空串/纯空白 → 无法证明同单位，成本域不给参考（不猜单位）；
    控制例：补上主单位「件」即恢复可估（排除缺市场价对测试的掩盖）。"""
    project = _project(db, "MDD-COSTU")
    part = _part(db, "MDD-COSTU-PN", unit=None)   # 主单位缺失
    _costed_issue(db, project, part, 4, unit_cost_ex=Decimal("25.00"))
    db.commit()

    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    cost = refs[part.id].cost
    assert cost.available is False
    assert cost.unavailable_reason == "unit_missing_master"
    assert cost.payload() == {
        "available": False, "unavailable_reason": "unit_missing_master",
    }

    # 空串 / 纯空白同样视为缺失（只归一空白与大小写，不猜）。
    for blank in ("", "   "):
        part.unit = blank
        db.commit()
        refs = batch_price_references(
            db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
        )
        assert refs[part.id].cost.available is False, blank
        assert refs[part.id].cost.unavailable_reason == "unit_missing_master", blank

    # 行级：成本域不给值（available False），坏品行待估原因仍是市场口径。
    _receipt(db, project, part, "2", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    (line,) = bundle.line_values.values()
    assert line["state"] == "pending"
    assert line["domains"]["cost"]["available"] is False
    assert line["domains"]["cost"]["reason"] == "unit_missing_master"

    # 控制例：同一 PN 补主单位「件」→ 成本域恢复可估（4×25 → 28.25/25.00）。
    part.unit = "件"
    db.commit()
    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, scope=None, settings=get_settings(),
    )
    cost = refs[part.id].cost
    assert cost.available is True
    assert format(cost.unit_cost_inc_tax, ".2f") == "28.25"
    assert format(cost.unit_cost_ex_tax, ".2f") == "25.00"


def test_valuation_pending_reason_pn_unresolved(db):
    project = _project(db, "MDD-PNUN")
    _part(db, "MDD-PNUN-MASTER", unit="件")
    _receipt(db, project, None, "2", pn="MDD-PNUN-GHOST", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    assert bundle.line_values and all(
        item["reason"] == "pn_unresolved" for item in bundle.line_values.values()
    )
    assert bundle.pending_bad_qty == Decimal("2")


def test_valuation_resolved_by_unique_canonical_pn_fallback(db):
    project = _project(db, "MDD-PNFB")
    part = _part(db, "MDD-PNFB-PN", unit="件")
    _purchase(db, part, "pnfb-cg")
    _receipt(db, project, None, "2", pn=" mdd-pnfb-pn ", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    assert bundle.line_values and all(
        item["state"] == "ready" for item in bundle.line_values.values()
    )
    assert bundle.estimated_bad_qty == Decimal("2")


def test_valuation_excludes_out_of_period_and_voided(db):
    project = _project(db, "MDD-PER")
    part = _part(db, "MDD-PER-PN", unit="件")
    _purchase(db, part, "per-cg")
    _receipt(db, project, part, "9", condition="坏品",
             occurred_at=datetime(2026, 10, 12, 10, tzinfo=UTC))   # 期前（上海 10-12 18:00）
    _receipt(db, project, part, "8", condition="坏品",
             occurred_at=datetime(2026, 10, 19, 16, tzinfo=UTC))   # 期末次日凌晨（上海）
    _receipt(db, project, part, "7", condition="坏品", status="voided",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    _receipt(db, project, part, "6", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))    # 期内
    db.commit()
    bundle = dash.build_valuation(db, _filters(db), ratio=Decimal("0.5"))
    assert bundle.estimated_bad_qty == Decimal("6")
    assert bundle.pending_bad_qty == Decimal("0")


# ---------- T3 行组装：估价与三域参考并列 ----------

def test_part_rows_valuation_and_references_payload(db):
    project = _project(db, "MDD-T3")
    part = _part(db, "MDD-T3-PN", unit="件")
    _purchase(db, part, "t3-cg")
    _sale(db, part, "t3-xs")
    _costed_issue(db, project, part, 8, unit_cost_ex=Decimal("40.00"))
    _receipt(db, project, part, "4", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    rows = dash.build_part_rows(
        db, _filters(db), include_amounts=True, ratio=Decimal("0.5")
    )
    row = rows[0]
    assert row["valuation"]["state"] == "ready"
    assert row["valuation"]["bad_estimated_value_inc_tax"] == "226.00"
    assert row["valuation"]["bad_estimated_value_ex_tax"] == "200.00"
    assert row["valuation"]["coverage_pct"] == 100.0
    assert row["valuation"]["domains"]["purchase"]["value_inc_tax"] == "226.00"
    assert row["valuation"]["domains"]["cost"]["value_inc_tax"] == "90.40"
    references = row["references"]
    assert references["chosen"] == "purchase"
    assert references["purchase"]["available"] is True
    assert references["purchase"]["inc_tax"] == "113.00"
    assert references["sales"]["available"] is True
    assert references["cost"]["available"] is True
    assert references["cost"]["unit_cost_ex_tax"] == "40.00"
    # 无金额权：估价与参考键整个不出现（不是置 0）。
    rows_no_amount = dash.build_part_rows(
        db, _filters(db), include_amounts=False, ratio=Decimal("0.5")
    )
    assert "valuation" not in rows_no_amount[0]
    assert "references" not in rows_no_amount[0]


def test_part_rows_without_ratio_marks_awaiting_and_keeps_references(db):
    """无比例：行级待试算（金额键不出现），参考价仍并列展示。"""
    project = _project(db, "MDD-T3N")
    part = _part(db, "MDD-T3N-PN", unit="件")
    _purchase(db, part, "t3n-cg")
    _receipt(db, project, part, "4", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    rows = dash.build_part_rows(db, _filters(db), include_amounts=True)
    valuation = rows[0]["valuation"]
    assert valuation["state"] == "awaiting_ratio"
    assert "bad_estimated_value_inc_tax" not in valuation
    assert valuation["awaiting_ratio_bad_qty"] == "4.000"
    assert valuation["coverage_pct"] is None
    references = rows[0]["references"]
    assert references["purchase"]["available"] is True
    assert references["purchase"]["inc_tax"] == "113.00"
