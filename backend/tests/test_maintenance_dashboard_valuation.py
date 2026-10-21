"""维保数据看板估价：R33 系数、批量双域参考与单位校验（服务层）。

口径见 docs/decisions/0022（R33 裁定第 1/7 条）：
- 坏件/坏品/故障 20%、废品 5%、成品 100%；件况未知不估价（绝不算 0）。
- 采购参考优先、无合格采购样本回退销售；样本单位与主数据单位不一致/
  缺失/混合时该域不可用，两域都不可用标「待估」，不做箱/件换算。
- 行金额 = qty × 参考价 × 系数（HALF_UP 两位）后求和；锚点=统计结束日。
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
from app.services.maintenance_dashboard_valuation_policy import (
    POLICY_VERSION,
    factor_for,
)
from tests.test_maintenance_analytics import _project
from tests.test_maintenance_return_metrics import _receipt

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
        raw_line_id=f"{key}-l1", order_id=order.id, part_id=part.id,
        pn_std=part.pn_std, qty=qty, unit_price=price, unit=unit,
        counts_revenue=counts_revenue, import_batch_id=order.import_batch_id,
    ))
    db.flush()
    return order


# ---------- 系数配置（R33 第 1 条） ----------

def test_policy_factors_per_condition():
    assert factor_for("坏件") == Decimal("0.20")
    assert factor_for("坏品") == Decimal("0.20")
    assert factor_for("故障") == Decimal("0.20")
    assert factor_for("废品") == Decimal("0.05")
    assert factor_for("成品") == Decimal("1.00")
    # 件况未知：未填/空白/其他/任意未知文本一律不估价。
    for unknown in (None, "", "  ", "其他", "别的"):
        assert factor_for(unknown) is None
    assert POLICY_VERSION == "mdv-2026-10-21-r33"


def test_value_line_rounds_half_up_per_line():
    # 含税 3×0.05×0.20=0.030；未税 3×0.04×0.20=0.024 → HALF_UP 两位 = 0.02。
    inc, ex = value_line(Decimal(3), Decimal("0.20"), (Decimal("0.05"), Decimal("0.04")))
    assert inc == Decimal("0.03")
    assert ex == Decimal("0.02")
    # 0.005 边界：HALF_UP 入位。
    inc, _ = value_line(Decimal(1), Decimal("1.00"), (Decimal("0.005"), Decimal("0.005")))
    assert inc == Decimal("0.01")


# ---------- 批量双域参考与单位校验 ----------

def test_batch_references_purchase_preferred_sales_fallback(db):
    both = _part(db, "MDD-REF-BOTH", unit="件")
    _purchase(db, both, "ref-both-cg")           # 113.00 含税 / 100.00 未税
    _sale(db, both, "ref-both-xs")
    mismatch = _part(db, "MDD-REF-MISMATCH", unit="件")
    _purchase(db, mismatch, "ref-mm-cg", unit="箱")  # 采购单位 ≠ 主数据
    _sale(db, mismatch, "ref-mm-xs", unit="件")      # 销售同单位可用
    refs = batch_price_references(
        db, {both.id, mismatch.id}, anchor=ANCHOR, settings=get_settings()
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
        anchor=ANCHOR, settings=get_settings(),
    )
    assert refs[missing_master.id].sales.unavailable_reason == "unit_missing_master"
    assert refs[missing_sample.id].sales.unavailable_reason == "unit_missing_sample"
    assert refs[mixed.id].sales.unavailable_reason == "unit_mixed_samples"
    for part_id in (missing_master.id, missing_sample.id, mixed.id):
        assert refs[part_id].chosen is None
    assert refs[none.id].chosen is None
    assert refs[none.id].chosen_reason == "no_samples"


def test_batch_references_window_anchor_and_no_future_trades(db):
    part = _part(db, "MDD-WINDOW", unit="件")
    _purchase(db, part, "win-future", order_date=ANCHOR + timedelta(days=1))
    _purchase(db, part, "win-edge", order_date=ANCHOR - timedelta(days=29))
    _purchase(db, part, "win-stale", order_date=ANCHOR - timedelta(days=30))
    refs = batch_price_references(
        db, {part.id}, anchor=ANCHOR, settings=get_settings()
    )
    assert refs[part.id].chosen == "purchase"
    assert refs[part.id].purchase.available
    assert refs[part.id].purchase.latest_order_date == ANCHOR - timedelta(days=29)
    assert refs[part.id].purchase.oldest_order_date == ANCHOR - timedelta(days=29)


# ---------- 行级估价（build_valuation） ----------

def test_build_valuation_line_rounding_then_sum_and_factors(db):
    project = _project(db, "MDD-VAL")
    part = _part(db, "MDD-VAL-PN", unit="件")
    _purchase(db, part, "val-cg", qty=Decimal(1), price=Decimal("0.05"))
    # 3 条 坏品 各 qty 1：行 ex = 1×0.04×0.2 = 0.008 → 每行 0.01，合计 0.03；
    # 若只对合计四舍五入会得到 0.024 → 0.02，可证逐行取整口径。
    for index in range(3):
        _receipt(
            db, project, part, "1", condition="坏品",
            occurred_at=datetime(2026, 10, 15, 8 + index, tzinfo=UTC),
        )
    _receipt(db, project, part, "2", condition="废品",
             occurred_at=datetime(2026, 10, 16, 8, tzinfo=UTC))
    _receipt(db, project, part, "5", condition="成品",
             occurred_at=datetime(2026, 10, 16, 9, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db))
    for line in bundle.line_values.values():
        assert line["state"] == "ready"
    # 坏品 3×0.01=0.03（未税）；废品 2×0.04×0.05=0.004→0.00；成品 5×0.04×1=0.20。
    assert bundle.total_bad_ex == Decimal("0.03")
    assert bundle.total_finished_ex == Decimal("0.20")
    # 坏件口径含废品：已估坏件数量 = 坏品 3 + 废品 2。
    assert bundle.estimated_bad_qty == Decimal("5")
    assert bundle.project_bad_estimated_qty[project.project_id] == Decimal("5")
    # 参考选择：采购优先。
    assert bundle.references[part.id].chosen == "purchase"


def test_unknown_condition_not_valued_and_pending_never_zero(db):
    """件况未知不估价：行级标待估；绝不出现在价值合计里（不是按 0 元估价）。

    件况未知不属于坏件口径（M02 只认 坏品/坏件/故障/废品），因此不进
    已估/待估坏件数量，覆盖率在 M02=0 时无比例（None）。
    """
    project = _project(db, "MDD-UNK")
    part = _part(db, "MDD-UNK-PN", unit="件")
    _purchase(db, part, "unk-cg")
    _receipt(db, project, part, "4", condition=None,
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    _receipt(db, project, part, "3", condition="其他",
             occurred_at=datetime(2026, 10, 15, 9, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db))
    assert bundle.estimated_bad_qty == Decimal("0")
    assert bundle.pending_bad_qty == Decimal("0")
    assert bundle.total_bad_inc == Decimal("0")
    reasons = {tuple(sorted(item.items())) for item in bundle.line_values.values()}
    assert reasons == {(("reason", "unknown_condition"), ("state", "pending"))}

    rows = dash.build_part_rows(db, _filters(db), include_amounts=True)
    row = rows[0]
    # 实收含件况未知（M01），坏件实收为 0（M02）。
    assert row["receipt_qty"] == "7.000"
    assert row["bad_receipt_qty"] == "0.000"
    valuation = row["valuation"]
    assert valuation["estimated_bad_qty"] == "0.000"
    assert valuation["pending_bad_qty"] == "0.000"
    assert valuation["coverage_pct"] is None  # M02=0 → 无比例


def test_bad_condition_without_price_is_pending_not_zero(db):
    """坏件实收但无合格参考（两域单位都不合）→ 待估坏件量单独呈现，不算 0 元。"""
    project = _project(db, "MDD-NOPX")
    part = _part(db, "MDD-NOPX-PN", unit="件")
    _purchase(db, part, "nopx-cg", unit="箱")   # 单位不合 → 采购不可用
    _sale(db, part, "nopx-xs", unit="板")       # 单位不合 → 销售不可用
    _receipt(db, project, part, "4", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    _receipt(db, project, part, "2", condition="成品",
             occurred_at=datetime(2026, 10, 15, 9, tzinfo=UTC))
    db.commit()

    bundle = dash.build_valuation(db, _filters(db))
    assert bundle.estimated_bad_qty == Decimal("0")
    assert bundle.total_bad_inc == Decimal("0")
    assert bundle.pending_bad_qty == Decimal("4")
    assert bundle.project_pending_bad_qty[project.project_id] == Decimal("4")
    bad_line, finished_line = list(bundle.line_values.values())
    assert bad_line["state"] == "pending"
    assert "unit_mismatch" in bad_line["reason"]
    assert finished_line["state"] == "pending"

    rows = dash.build_part_rows(db, _filters(db), include_amounts=True)
    valuation = rows[0]["valuation"]
    assert valuation["pending_bad_qty"] == "4.000"
    assert valuation["coverage_pct"] == 0.0  # 已估 0 ÷ 坏件实收 4
    # 成品行无参考同样待估，但不进坏件桶。
    assert bundle.total_finished_inc == Decimal("0")


def test_valuation_pending_reason_pn_unresolved(db):
    project = _project(db, "MDD-PNUN")
    _part(db, "MDD-PNUN-MASTER", unit="件")
    # part_id 为空 + PN 在主数据无唯一匹配 → 行身份未解析，待估。
    _receipt(db, project, None, "2", pn="MDD-PNUN-GHOST", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    bundle = dash.build_valuation(db, _filters(db))
    assert bundle.line_values and all(
        item["reason"] == "pn_unresolved" for item in bundle.line_values.values()
    )
    assert bundle.pending_bad_qty == Decimal("2")


def test_valuation_resolved_by_unique_canonical_pn_fallback(db):
    project = _project(db, "MDD-PNFB")
    part = _part(db, "MDD-PNFB-PN", unit="件")
    _purchase(db, part, "pnfb-cg")
    # part_id 为空但 PN 唯一匹配主数据 → 按唯一规范 PN 回退估价。
    _receipt(db, project, None, "2", pn=" mdd-pnfb-pn ", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    bundle = dash.build_valuation(db, _filters(db))
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
    bundle = dash.build_valuation(db, _filters(db))
    assert bundle.estimated_bad_qty == Decimal("6")
    assert bundle.pending_bad_qty == Decimal("0")


# ---------- T3 行组装：估价与参考并列 ----------

def test_part_rows_valuation_and_references_payload(db):
    project = _project(db, "MDD-T3")
    part = _part(db, "MDD-T3-PN", unit="件")
    _purchase(db, part, "t3-cg")
    _sale(db, part, "t3-xs")
    _receipt(db, project, part, "4", condition="坏品",
             occurred_at=datetime(2026, 10, 15, 8, tzinfo=UTC))
    db.commit()
    rows = dash.build_part_rows(db, _filters(db), include_amounts=True)
    row = rows[0]
    assert row["valuation"]["bad_estimated_value_inc_tax"] == "90.40"
    assert row["valuation"]["bad_estimated_value_ex_tax"] == "80.00"
    assert row["valuation"]["coverage_pct"] == 100.0
    references = row["references"]
    assert references["chosen"] == "purchase"
    assert references["purchase"]["available"] is True
    assert references["purchase"]["inc_tax"] == "113.00"
    assert references["sales"]["available"] is True
    # 无金额权：估价与参考键整个不出现（不是置 0）。
    rows_no_amount = dash.build_part_rows(db, _filters(db), include_amounts=False)
    assert "valuation" not in rows_no_amount[0]
    assert "references" not in rows_no_amount[0]
