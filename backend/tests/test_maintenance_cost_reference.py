"""维保缺失成本参考瀑布与双税口径回归测试。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.etl import loader
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember
from app.models.maintenance import FMaintenanceLine
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.system import SysImportBatch
from app.services import maintenance_cost, maintenance_cost_reference
from tests import factories as f


@pytest.fixture()
def batch(db):
    item = SysImportBatch(
        filename="maintenance-reference.xlsx",
        file_type="maintenance",
        file_hash="maintenance-reference",
    )
    db.add(item)
    db.flush()
    return item


def _load_purchases(db, batch, orders, lines):
    loader.load(
        db,
        f.purchase_result(orders, lines),
        batch.id,
        date(2026, 6, 1),
    )


def _load_maintenance(db, batch, orders, lines):
    loader.load(
        db,
        f.maintenance_result(orders, lines),
        batch.id,
        date(2026, 6, 1),
    )


def _load_sales(db, batch, orders, lines):
    loader.load(
        db,
        f.sales_result(orders, lines),
        batch.id,
        date(2026, 6, 1),
    )


def _part_id(db, pn: str) -> int:
    return db.scalar(select(DimPart.id).where(DimPart.pn_std == pn))


def _line(db, raw_line_id: str) -> FMaintenanceLine:
    return db.scalar(
        select(FMaintenanceLine).where(
            FMaintenanceLine.raw_line_id == raw_line_id,
        )
    )


def _add_pool(
    db,
    group_id: int,
    version: int,
    *pns: str,
    status: str = "active",
    source: str = "manual",
):
    db.add(
        PartPool(
            group_id=group_id,
            name=f"测试池-{group_id}",
            status=status,
            source=source,
            version=version,
            member_count=len(pns),
        )
    )
    db.flush()
    db.add_all(
        [
            PartPoolMember(group_id=group_id, part_id=_part_id(db, pn))
            for pn in pns
        ]
    )


@pytest.mark.parametrize(
    "dirty_rate",
    [Decimal("1"), Decimal("1.3"), Decimal("NaN")],
)
def test_tax_rate_domain_defense_estimates_one_or_greater_and_nonfinite(
    dirty_rate,
):
    """纯计算纵深防御：100%/越界/NaN 税率一律回退 13% 并显式 estimated。"""
    normalized = maintenance_cost_reference.normalize_cost_sample(
        maintenance_cost_reference.CostSample(
            side="purchase",
            part_id=1,
            occurred_on=date(2025, 1, 1),
            qty=Decimal("1"),
            unit_price=Decimal("100"),
            tax_rate=dirty_rate,
            is_tax_inclusive=False,
        )
    )
    assert normalized.unit_cost_ex_tax == Decimal("100")
    assert normalized.unit_cost_inc_tax == Decimal("113.00")
    assert normalized.tax_rate_estimated is True


@pytest.mark.parametrize(
    ("qty", "price"),
    [
        (Decimal("-1"), Decimal("100")),
        (Decimal("NaN"), Decimal("100")),
        (Decimal("Infinity"), Decimal("100")),
        (Decimal("1"), Decimal("-1")),
        (Decimal("1"), Decimal("NaN")),
        (Decimal("1"), Decimal("Infinity")),
    ],
)
def test_python_sample_entry_fail_closes_nonpositive_or_nonfinite_values(
    qty,
    price,
):
    """Python 入口纵深防御：脏 qty/price 不得进入均价或产出非有限成本。"""
    sample = maintenance_cost_reference.CostSample(
        side="purchase",
        part_id=1,
        occurred_on=date(2025, 1, 1),
        qty=qty,
        unit_price=price,
        tax_rate=Decimal("0.13"),
        is_tax_inclusive=False,
    )
    with pytest.raises(ValueError, match="no positive sample"):
        maintenance_cost_reference.summarize_samples([sample])


def test_missing_cost_uses_active_pool_latest_purchase_month_with_dual_tax(
    db,
    batch,
):
    """旧五层为 none 后，以有效池最近非空采购月补价并保存双税及池版本。"""
    _load_purchases(
        db,
        batch,
        {"P1": f.purchase_head("P1", on=date(2025, 10, 8))},
        [f.purchase_line("P1", "PL1", "PN-POOL-SAMPLE", qty="2", price="100")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [f.maintenance_line("M1", "ML1", "PN-POOL-TARGET", qty="3")],
    )
    db.flush()
    pool = PartPool(
        group_id=901,
        name="测试互通池",
        status="active",
        source="manual",
        version=7,
        member_count=2,
    )
    db.add(pool)
    db.add_all(
        [
            PartPoolMember(group_id=901, part_id=_part_id(db, "PN-POOL-TARGET")),
            PartPoolMember(group_id=901, part_id=_part_id(db, "PN-POOL-SAMPLE")),
        ]
    )
    db.commit()

    stats = maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert stats["pool_purchase"] == 1
    assert line.cost_source == "pool_purchase"
    # 未注明税率的未税采购按统一 13% 估算另一口径；legacy 字段仍取原始未税值。
    assert line.unit_cost == Decimal("100.00")
    assert line.cost_amount == Decimal("300.00")
    assert line.unit_cost_ex_tax == Decimal("100.00")
    assert line.unit_cost_inc_tax == Decimal("113.00")
    assert line.cost_amount_ex_tax == Decimal("300.00")
    assert line.cost_amount_inc_tax == Decimal("339.00")
    assert line.reference_side == "purchase"
    assert line.reference_pool_group_id == 901
    assert line.reference_pool_version == 7
    assert line.reference_sample_count == 1
    assert line.reference_from_date == date(2025, 10, 8)
    assert line.reference_to_date == date(2025, 10, 8)
    assert line.reference_latest_date == date(2025, 10, 8)
    assert line.trace_months == 5
    assert line.confidence == "low"
    assert "tax_rate_estimated" in line.anomaly_flags
    first_snapshot = (
        line.cost_source,
        line.unit_cost,
        line.unit_cost_inc_tax,
        line.unit_cost_ex_tax,
        line.cost_amount_inc_tax,
        line.cost_amount_ex_tax,
        line.reference_pool_group_id,
        line.reference_pool_version,
        line.reference_sample_count,
        line.reference_latest_date,
        tuple(line.anomaly_flags),
    )

    second_stats = maintenance_cost.recompute(db)
    db.expire_all()
    line = _line(db, "ML1")
    assert second_stats["pool_purchase"] == 1
    assert (
        line.cost_source,
        line.unit_cost,
        line.unit_cost_inc_tax,
        line.unit_cost_ex_tax,
        line.cost_amount_inc_tax,
        line.cost_amount_ex_tax,
        line.reference_pool_group_id,
        line.reference_pool_version,
        line.reference_sample_count,
        line.reference_latest_date,
        tuple(line.anomaly_flags),
    ) == first_snapshot

    project = maintenance_cost.projects_aggregate(
        db,
        lifecycle="all",
    )["rows"][0]
    # legacy 仍按原始税口径分桶，不改旧字段含义。
    assert (project["cost_inc"], project["cost_ex"]) == (0.0, 300.0)
    assert project["parts_cost_inc_tax"] == 339.0
    assert project["parts_cost_ex_tax"] == 300.0
    assert project["parts_cost_inc_tax_complete"] is True
    assert project["parts_cost_ex_tax_complete"] is True
    assert project["parts_cost_inc_tax_quality"] == "contains_estimate"
    assert project["parts_cost_ex_tax_quality"] == "contains_estimate"

    contract = maintenance_cost.board(db, lifecycle="all")["rows"][0]
    assert contract["parts_cost_inc_tax"] == 339.0
    assert contract["parts_cost_ex_tax"] == 300.0
    assert contract["parts_cost_inc_tax_complete"] is True
    assert contract["parts_cost_ex_tax_complete"] is True


def test_existing_direct_source_keeps_legacy_values_and_adds_dual_tax(
    db,
    batch,
):
    """新增双税底座不得改变既有 direct 的 legacy 单价、金额、来源和税口径。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head(
                "P1",
                order_no="CGDD-DIRECT",
                on=date(2026, 3, 1),
                source_type="维保需求",
                linked_maintenance_order_no="WBDD-DIRECT",
                is_tax_inclusive=True,
                tax_rate=Decimal("0.13"),
            )
        },
        [f.purchase_line("P1", "PL1", "PN-DIRECT-DUAL", qty="2", price="113")],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M1": f.maintenance_head(
                "M1",
                order_no="WBDD-DIRECT",
                on=date(2026, 3, 5),
            )
        },
        [f.maintenance_line("M1", "ML1", "PN-DIRECT-DUAL", qty="3")],
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert (
        line.cost_source,
        line.cost_tax_basis,
        line.unit_cost,
        line.cost_amount,
    ) == ("direct", "inc", Decimal("113.00"), Decimal("339.00"))
    assert line.unit_cost_inc_tax == Decimal("113.00")
    assert line.unit_cost_ex_tax == Decimal("100.00")
    assert line.cost_amount_inc_tax == Decimal("339.00")
    assert line.cost_amount_ex_tax == Decimal("300.00")
    assert line.reference_side == "purchase"
    assert line.reference_sample_count == 1


def test_direct_purchase_without_date_keeps_dual_tax_and_marks_missing_date(
    db,
    batch,
):
    """直配关系可审计；缺采购日期不丢税务证据，但日期字段和异常标记必须诚实。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head(
                "P1",
                on=date(2026, 3, 1),
                linked_maintenance_order_no="WBDD-DIRECT-NO-DATE",
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            )
        },
        [
            f.purchase_line(
                "P1",
                "PL1",
                "PN-DIRECT-NO-DATE",
                qty="2",
                price="100",
            )
        ],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M1": f.maintenance_head(
                "M1",
                order_no="WBDD-DIRECT-NO-DATE",
                on=date(2026, 3, 15),
            )
        },
        [
            f.maintenance_line(
                "M1",
                "ML-DIRECT-NO-DATE",
                "PN-DIRECT-NO-DATE",
                qty="3",
            )
        ],
    )
    purchase = db.scalar(
        select(FPurchaseOrder).where(FPurchaseOrder.raw_order_id == "P1")
    )
    purchase.order_date = None
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML-DIRECT-NO-DATE")
    assert line.cost_source == "direct"
    assert line.unit_cost == Decimal("100.00")
    assert line.unit_cost_ex_tax == Decimal("100.00")
    assert line.unit_cost_inc_tax == Decimal("113.00")
    assert line.cost_amount_ex_tax == Decimal("300.00")
    assert line.cost_amount_inc_tax == Decimal("339.00")
    assert line.reference_side == "purchase"
    assert line.reference_sample_count == 1
    assert line.reference_from_date is None
    assert line.reference_to_date is None
    assert line.reference_latest_date is None
    assert line.price_month is None
    assert line.trace_months is None
    assert line.price_distance_days is None
    assert "reference_date_missing" in line.anomaly_flags


def test_direct_mixed_known_and_missing_dates_keeps_date_window_empty(
    db,
    batch,
):
    """样本数覆盖全部直配记录时，日期窗口不能只伪装成有日期的子集。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head(
                "P1",
                on=date(2026, 3, 1),
                linked_maintenance_order_no="WBDD-DIRECT-MIXED-DATE",
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
            "P2": f.purchase_head(
                "P2",
                on=date(2026, 3, 2),
                linked_maintenance_order_no="WBDD-DIRECT-MIXED-DATE",
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
        },
        [
            f.purchase_line("P1", "PL1", "PN-DIRECT-MIXED-DATE", qty="1", price="100"),
            f.purchase_line("P2", "PL2", "PN-DIRECT-MIXED-DATE", qty="3", price="200"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M1": f.maintenance_head(
                "M1",
                order_no="WBDD-DIRECT-MIXED-DATE",
                on=date(2026, 3, 15),
            )
        },
        [
            f.maintenance_line(
                "M1",
                "ML-DIRECT-MIXED-DATE",
                "PN-DIRECT-MIXED-DATE",
                qty="2",
            )
        ],
    )
    purchase_without_date = db.scalar(
        select(FPurchaseOrder).where(FPurchaseOrder.raw_order_id == "P2")
    )
    purchase_without_date.order_date = None
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML-DIRECT-MIXED-DATE")
    assert line.cost_source == "direct"
    assert line.unit_cost_ex_tax == Decimal("175.00")
    assert line.unit_cost_inc_tax == Decimal("197.75")
    assert line.cost_amount_ex_tax == Decimal("350.00")
    assert line.cost_amount_inc_tax == Decimal("395.50")
    assert line.reference_sample_count == 2
    assert line.reference_from_date is None
    assert line.reference_to_date is None
    assert line.reference_latest_date is None
    assert line.price_month is None
    assert line.trace_months is None
    assert line.price_distance_days is None
    assert "reference_date_missing" in line.anomaly_flags


def test_pool_sales_is_used_when_active_pool_has_no_purchase_sample(
    db,
    batch,
):
    """池内采购无样本时再用池内销售；销售单价按含税原值归一双口径。"""
    _load_sales(
        db,
        batch,
        {
            "S1": f.sales_head(
                "S1",
                on=date(2025, 11, 20),
                tax_rate=Decimal("0.06"),
            )
        },
        [f.sales_line("S1", "SL1", "PN-POOL-SALE-SAMPLE", qty="4", price="106")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [f.maintenance_line("M1", "ML1", "PN-POOL-SALE-TARGET", qty="2")],
    )
    db.flush()
    _add_pool(
        db,
        902,
        3,
        "PN-POOL-SALE-TARGET",
        "PN-POOL-SALE-SAMPLE",
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "pool_sales"
    assert line.unit_cost == Decimal("106.00")
    assert line.unit_cost_inc_tax == Decimal("106.00")
    assert line.unit_cost_ex_tax == Decimal("100.00")
    assert line.cost_amount_inc_tax == Decimal("212.00")
    assert line.cost_amount_ex_tax == Decimal("200.00")
    assert line.reference_side == "sales"
    assert line.reference_pool_group_id == 902
    assert line.trace_months == 4


def test_purchase_history_uses_latest_eligible_month_quantity_weighted_average(
    db,
    batch,
):
    """无有效池时，本 PN 采购历史取出库日前最近非空自然月并按数量加权。"""
    _load_purchases(
        db,
        batch,
        {
            "P0": f.purchase_head("P0", on=date(2025, 8, 1), tax_rate=Decimal("0.13")),
            "P1": f.purchase_head("P1", on=date(2025, 10, 2), tax_rate=Decimal("0.13")),
            "P2": f.purchase_head("P2", on=date(2025, 10, 20), tax_rate=Decimal("0.13")),
            "PF": f.purchase_head("PF", on=date(2026, 4, 1), tax_rate=Decimal("0.13")),
        },
        [
            f.purchase_line("P0", "PL0", "PN-HISTORY-P", qty="1", price="999"),
            f.purchase_line("P1", "PL1", "PN-HISTORY-P", qty="1", price="100"),
            f.purchase_line("P2", "PL2", "PN-HISTORY-P", qty="3", price="200"),
            f.purchase_line("PF", "PLF", "PN-HISTORY-P", qty="1", price="1"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [f.maintenance_line("M1", "ML1", "PN-HISTORY-P", qty="2")],
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "purchase_history"
    assert line.unit_cost_ex_tax == Decimal("175.00")
    assert line.unit_cost_inc_tax == Decimal("197.75")
    assert line.reference_sample_count == 2
    assert line.reference_from_date == date(2025, 10, 2)
    assert line.reference_to_date == date(2025, 10, 20)
    assert line.reference_latest_date == date(2025, 10, 20)
    assert line.price_month == "2025-10"
    assert line.trace_months == 5


def test_sales_history_is_last_reference_and_zero_tax_keeps_both_bases_equal(
    db,
    batch,
):
    """无池/采购历史时才用本 PN 销售历史；0% 税率不得错误套用 13%。"""
    _load_sales(
        db,
        batch,
        {
            "S1": f.sales_head(
                "S1",
                on=date(2025, 9, 9),
                tax_rate=Decimal("0"),
            )
        },
        [f.sales_line("S1", "SL1", "PN-HISTORY-S", qty="2", price="88")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [
            f.maintenance_line(
                "M1",
                "ML1",
                "PN-HISTORY-S",
                qty="4",
                return_qty="1",
            )
        ],
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "sales_history"
    assert line.unit_cost_inc_tax == Decimal("88.00")
    assert line.unit_cost_ex_tax == Decimal("88.00")
    assert line.cost_amount_inc_tax == Decimal("264.00")
    assert line.cost_amount_ex_tax == Decimal("264.00")
    assert line.reference_side == "sales"
    assert "tax_rate_estimated" not in line.anomaly_flags
    assert "has_return" in line.anomaly_flags


def test_only_active_pool_is_considered_and_pool_purchase_beats_pool_sales(
    db,
    batch,
):
    """同 PN 的归档池不参与；有效池内采购优先于即使更晚的池销售。"""
    _load_purchases(
        db,
        batch,
        {"P1": f.purchase_head("P1", on=date(2025, 9, 1), tax_rate=Decimal("0"))},
        [
            f.purchase_line("P1", "PL1", "PN-ACTIVE-PURCHASE", qty="1", price="70"),
            f.purchase_line("P1", "PL2", "PN-ARCHIVED-PURCHASE", qty="1", price="10"),
        ],
    )
    _load_sales(
        db,
        batch,
        {"S1": f.sales_head("S1", on=date(2026, 2, 1), tax_rate=Decimal("0"))},
        [f.sales_line("S1", "SL1", "PN-ACTIVE-SALE", qty="1", price="90")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [f.maintenance_line("M1", "ML1", "PN-MULTI-POOL", qty="1")],
    )
    db.flush()
    _add_pool(
        db,
        903,
        2,
        "PN-MULTI-POOL",
        "PN-ARCHIVED-PURCHASE",
        status="archived",
    )
    _add_pool(
        db,
        904,
        4,
        "PN-MULTI-POOL",
        "PN-ACTIVE-PURCHASE",
        "PN-ACTIVE-SALE",
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "pool_purchase"
    assert line.unit_cost == Decimal("70.00")
    assert line.reference_pool_group_id == 904
    assert line.reference_pool_version == 4


def test_active_pool_average_includes_target_part_history(
    db,
    batch,
):
    """冻结口径：目标 PN 也是有效池成员，单成员有历史时仍归 pool_purchase。"""
    _load_purchases(
        db,
        batch,
        {"P1": f.purchase_head("P1", on=date(2025, 8, 8), tax_rate=Decimal("0"))},
        [f.purchase_line("P1", "PL1", "PN-OWN-HISTORY", qty="1", price="66")],
    )
    # 先让空样本同伴建入 dim_part。
    _load_maintenance(
        db,
        batch,
        {
            "M0": f.maintenance_head("M0", on=date(2023, 1, 1)),
            "M1": f.maintenance_head("M1", on=date(2026, 3, 15)),
        },
        [
            f.maintenance_line("M0", "ML0", "PN-EMPTY-PEER", qty="1"),
            f.maintenance_line("M1", "ML1", "PN-OWN-HISTORY", qty="1"),
        ],
    )
    db.flush()
    _add_pool(db, 905, 1, "PN-OWN-HISTORY", "PN-EMPTY-PEER")
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "pool_purchase"
    assert line.unit_cost == Decimal("66.00")
    assert line.reference_pool_group_id == 905


def test_pool_weighted_average_combines_target_and_other_members(
    db,
    batch,
):
    """多成员池同月均价包含目标 PN，并按所有有效样本数量加权。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head("P1", on=date(2025, 8, 8), tax_rate=Decimal("0")),
            "P2": f.purchase_head("P2", on=date(2025, 8, 9), tax_rate=Decimal("0")),
        },
        [
            f.purchase_line("P1", "PL1", "PN-POOL-WEIGHT-TARGET", qty="1", price="100"),
            f.purchase_line("P2", "PL2", "PN-POOL-WEIGHT-PEER", qty="3", price="200"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M0": f.maintenance_head("M0", on=date(2023, 1, 1)),
            "M1": f.maintenance_head("M1", on=date(2026, 3, 15)),
        },
        [
            f.maintenance_line("M0", "ML0", "PN-POOL-WEIGHT-PEER", qty="1"),
            f.maintenance_line("M1", "ML1", "PN-POOL-WEIGHT-TARGET", qty="1"),
        ],
    )
    db.flush()
    _add_pool(
        db,
        908,
        1,
        "PN-POOL-WEIGHT-TARGET",
        "PN-POOL-WEIGHT-PEER",
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "pool_purchase"
    assert line.unit_cost == Decimal("175.00")
    assert line.reference_sample_count == 2


def test_confirmed_source_error_is_excluded_before_latest_month_selection(
    db,
    batch,
):
    """已确认源错误不能参与参考；剔除后再选择最近仍有有效样本的月份。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head("P1", on=date(2025, 10, 8), tax_rate=Decimal("0")),
            "P2": f.purchase_head("P2", on=date(2025, 12, 8), tax_rate=Decimal("0")),
        },
        [
            f.purchase_line("P1", "PL-VALID", "PN-DQ-PEER", qty="1", price="80"),
            f.purchase_line("P2", "PL-ERROR", "PN-DQ-PEER", qty="1", price="1"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M1": f.maintenance_head("M1", on=date(2026, 3, 15)),
        },
        [f.maintenance_line("M1", "ML1", "PN-DQ-TARGET", qty="1")],
    )
    db.flush()
    _add_pool(db, 906, 1, "PN-DQ-TARGET", "PN-DQ-PEER")
    error_line = db.scalar(
        select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PL-ERROR")
    )
    db.add(
        FactDataQualityIssue(
            side="purchase",
            line_id=error_line.id,
            part_id=error_line.part_id,
            import_batch_id=error_line.import_batch_id,
            rule_code="test_price_error",
            rule_version="1",
            evidence={},
            source_fingerprint="test-fingerprint",
            status="confirmed_source_error",
            detected_by="pytest",
        )
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "pool_purchase"
    assert line.unit_cost == Decimal("80.00")
    assert line.price_month == "2025-10"
    assert line.reference_sample_count == 1


def test_mixed_tax_samples_are_normalized_per_order_before_weighted_average(
    db,
    batch,
):
    """含/未税混样本必须逐单归一再汇总，不能先混原值再套单一税率。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head(
                "P1",
                on=date(2025, 10, 2),
                is_tax_inclusive=True,
                tax_rate=Decimal("0.06"),
            ),
            "P2": f.purchase_head(
                "P2",
                on=date(2025, 10, 20),
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
        },
        [
            f.purchase_line("P1", "PL1", "PN-MIXED-TAX", qty="1", price="106"),
            f.purchase_line("P2", "PL2", "PN-MIXED-TAX", qty="1", price="100"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [f.maintenance_line("M1", "ML1", "PN-MIXED-TAX", qty="1")],
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "purchase_history"
    # (106 含税 + 113 含税) / 2；未税两笔均为 100。
    assert line.unit_cost_inc_tax == Decimal("109.50")
    assert line.unit_cost_ex_tax == Decimal("100.00")
    # legacy 继续执行 inc_first，只取原含税样本，不改既有字段语义。
    assert (line.unit_cost, line.cost_tax_basis) == (Decimal("106.00"), "inc")
    assert "tax_rate_estimated" not in line.anomaly_flags


def test_pool_reference_never_looks_ahead_for_each_maintenance_line(
    db,
    batch,
):
    """同批不同出库日分别截断参考事实，不能用批内较晚日期放开未来样本。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head("P1", on=date(2026, 2, 20), tax_rate=Decimal("0")),
            "P2": f.purchase_head("P2", on=date(2026, 3, 20), tax_rate=Decimal("0")),
        },
        [
            f.purchase_line("P1", "PL1", "PN-NO-FUTURE-PEER", qty="1", price="80"),
            f.purchase_line("P2", "PL2", "PN-NO-FUTURE-PEER", qty="1", price="90"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M1": f.maintenance_head("M1", on=date(2026, 3, 10)),
            "M2": f.maintenance_head("M2", on=date(2026, 3, 25)),
        },
        [
            f.maintenance_line("M1", "ML-EARLY", "PN-NO-FUTURE-TARGET", qty="1"),
            f.maintenance_line("M2", "ML-LATE", "PN-NO-FUTURE-TARGET", qty="1"),
        ],
    )
    db.flush()
    _add_pool(
        db,
        907,
        1,
        "PN-NO-FUTURE-TARGET",
        "PN-NO-FUTURE-PEER",
    )
    db.commit()

    maintenance_cost.recompute(db)

    early = _line(db, "ML-EARLY")
    late = _line(db, "ML-LATE")
    assert (early.unit_cost, early.price_month) == (Decimal("80.00"), "2026-02")
    assert (late.unit_cost, late.price_month) == (Decimal("90.00"), "2026-03")


def test_reference_older_than_twelve_months_is_explicitly_marked_stale(
    db,
    batch,
):
    """历史兜底不限 12 个月，但超过 12 个月必须显式低置信陈旧标记。"""
    _load_purchases(
        db,
        batch,
        {"P1": f.purchase_head("P1", on=date(2024, 1, 5), tax_rate=Decimal("0"))},
        [f.purchase_line("P1", "PL1", "PN-STALE", qty="1", price="55")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 15))},
        [f.maintenance_line("M1", "ML1", "PN-STALE", qty="1")],
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "purchase_history"
    assert line.trace_months == 26
    assert line.confidence == "low"
    assert "stale_cost_reference" in line.anomaly_flags


def test_existing_window_month_trace_and_sales_sources_get_dual_costs_without_drift(
    db,
    batch,
):
    """其余既有四层保持原来源/legacy 值，同时全部补齐双税成本。"""
    _load_purchases(
        db,
        batch,
        {
            "PW": f.purchase_head(
                "PW",
                on=date(2026, 3, 8),
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
            "PM1": f.purchase_head(
                "PM1",
                on=date(2026, 3, 1),
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
            "PM2": f.purchase_head(
                "PM2",
                on=date(2026, 3, 30),
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
            "PT": f.purchase_head(
                "PT",
                on=date(2026, 1, 5),
                is_tax_inclusive=False,
                tax_rate=Decimal("0.13"),
            ),
        },
        [
            f.purchase_line("PW", "PLW", "PN-OLD-WINDOW", qty="1", price="100"),
            f.purchase_line("PM1", "PLM1", "PN-OLD-MONTH", qty="1", price="90"),
            f.purchase_line("PM2", "PLM2", "PN-OLD-MONTH", qty="1", price="110"),
            f.purchase_line("PT", "PLT", "PN-OLD-TRACE", qty="1", price="80"),
        ],
    )
    _load_sales(
        db,
        batch,
        {
            "PS": f.sales_head(
                "PS",
                on=date(2026, 3, 5),
                tax_rate=Decimal("0.13"),
            )
        },
        [f.sales_line("PS", "PLS", "PN-OLD-SALES", qty="1", price="113")],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M1": f.maintenance_head("M1", on=date(2026, 3, 10)),
            "M2": f.maintenance_head("M2", on=date(2026, 3, 15)),
        },
        [
            f.maintenance_line("M1", "MLW", "PN-OLD-WINDOW", qty="1"),
            f.maintenance_line("M2", "MLM", "PN-OLD-MONTH", qty="1"),
            f.maintenance_line("M2", "MLT", "PN-OLD-TRACE", qty="1"),
            f.maintenance_line("M2", "MLS", "PN-OLD-SALES", qty="1"),
        ],
    )
    db.commit()

    maintenance_cost.recompute(db)

    expected = {
        "MLW": ("window", Decimal("100.00"), Decimal("113.00"), Decimal("100.00")),
        "MLM": ("month_avg", Decimal("100.00"), Decimal("113.00"), Decimal("100.00")),
        "MLT": ("trace_avg", Decimal("80.00"), Decimal("90.40"), Decimal("80.00")),
        "MLS": ("sales_ref", Decimal("113.00"), Decimal("113.00"), Decimal("100.00")),
    }
    for raw_line_id, values in expected.items():
        line = _line(db, raw_line_id)
        assert (
            line.cost_source,
            line.unit_cost,
            line.unit_cost_inc_tax,
            line.unit_cost_ex_tax,
        ) == values


def test_reference_index_uses_fixed_three_queries_and_resolve_is_sql_free(
    db,
    batch,
):
    """目标 PN 数量增长不能把历史参考读取退化为逐行 N+1。"""
    purchase_orders = {}
    purchase_lines = []
    maintenance_orders = {}
    maintenance_lines = []
    target_parts = []
    for index in range(12):
        pn = f"PN-QUERY-{index:02d}"
        purchase_id = f"P{index}"
        maintenance_id = f"M{index}"
        purchase_orders[purchase_id] = f.purchase_head(
            purchase_id,
            on=date(2025, 8, 1),
            tax_rate=Decimal("0"),
        )
        purchase_lines.append(
            f.purchase_line(
                purchase_id,
                f"PL{index}",
                pn,
                qty="1",
                price=str(50 + index),
            )
        )
        maintenance_orders[maintenance_id] = f.maintenance_head(
            maintenance_id,
            on=date(2026, 3, 1),
        )
        maintenance_lines.append(
            f.maintenance_line(
                maintenance_id,
                f"ML{index}",
                pn,
                qty="1",
            )
        )
    _load_purchases(db, batch, purchase_orders, purchase_lines)
    _load_maintenance(db, batch, maintenance_orders, maintenance_lines)
    db.commit()
    target_parts = [
        _part_id(db, f"PN-QUERY-{index:02d}")
        for index in range(12)
    ]

    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        index = maintenance_cost_reference.build_reference_index(
            db,
            target_part_ids=target_parts,
            max_as_of=date(2026, 3, 1),
        )
        assert all(
            index.resolve(part_id, date(2026, 3, 1)) is not None
            for part_id in target_parts
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) == 3


def test_nonpositive_or_inactive_history_never_becomes_a_cost_reference(
    db,
    batch,
):
    """非已生效、非正价或非正量样本全部剔除；无证据时继续明确 none。"""
    _load_purchases(
        db,
        batch,
        {
            "P0": f.purchase_head("P0", on=date(2025, 8, 1)),
            "PI": f.purchase_head(
                "PI",
                on=date(2025, 9, 1),
                data_status="草稿",
            ),
        },
        [
            f.purchase_line("P0", "PL0", "PN-INVALID", qty="1", price="0"),
            f.purchase_line("PI", "PLI", "PN-INVALID", qty="1", price="77"),
        ],
    )
    _load_sales(
        db,
        batch,
        {
            "S0": f.sales_head("S0", on=date(2025, 10, 1)),
            "SI": f.sales_head(
                "SI",
                on=date(2025, 11, 1),
                data_status="草稿",
            ),
        },
        [
            f.sales_line("S0", "SL0", "PN-INVALID", qty="0", price="99"),
            f.sales_line("SI", "SLI", "PN-INVALID", qty="1", price="88"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {
            "M0": f.maintenance_head("M0", on=date(2023, 1, 1)),
            "M1": f.maintenance_head("M1", on=date(2026, 3, 1)),
        },
        [
            f.maintenance_line("M0", "ML0", "PN-INVALID-PEER", qty="1"),
            f.maintenance_line("M1", "ML1", "PN-INVALID", qty="1"),
        ],
    )
    db.flush()
    _add_pool(db, 909, 1, "PN-INVALID", "PN-INVALID-PEER")
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "none"
    assert line.unit_cost is None
    assert line.unit_cost_inc_tax is None
    assert line.unit_cost_ex_tax is None
    assert line.reference_side is None
    assert "no_cost" in line.anomaly_flags


def test_invalid_tax_rate_is_fail_closed_before_reference_month_selection(
    db,
    batch,
):
    """税率合法域为 0..1；越界事实不参与参考，不能污染最新月份。"""
    _load_purchases(
        db,
        batch,
        {
            "PV": f.purchase_head(
                "PV",
                on=date(2025, 9, 1),
                tax_rate=Decimal("0.13"),
            ),
            "PI": f.purchase_head(
                "PI",
                on=date(2025, 11, 1),
                tax_rate=Decimal("1.30"),
            ),
            "P100": f.purchase_head(
                "P100",
                on=date(2025, 12, 1),
                tax_rate=Decimal("1"),
            ),
        },
        [
            f.purchase_line("PV", "PLV", "PN-BAD-TAX", qty="1", price="50"),
            f.purchase_line("PI", "PLI", "PN-BAD-TAX", qty="1", price="1"),
            f.purchase_line("P100", "PL100", "PN-BAD-TAX", qty="1", price="2"),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 5, 1))},
        [f.maintenance_line("M1", "ML1", "PN-BAD-TAX", qty="1")],
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == "purchase_history"
    assert line.price_month == "2025-09"
    assert line.unit_cost == Decimal("50.00")


def test_recompute_skips_reference_queries_when_old_five_layers_resolve_all(
    db,
    batch,
):
    """旧五层全部命中时，不应再读取人工池或历史参考事实。"""
    _load_purchases(
        db,
        batch,
        {
            "P1": f.purchase_head(
                "P1",
                on=date(2026, 3, 8),
                tax_rate=Decimal("0.13"),
            ),
        },
        [f.purchase_line("P1", "PL1", "PN-FAST-LEGACY", qty="1", price="100")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 10))},
        [f.maintenance_line("M1", "ML1", "PN-FAST-LEGACY", qty="1")],
    )
    db.commit()

    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        maintenance_cost.recompute(db)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert _line(db, "ML1").cost_source == "window"
    assert "pg_try_advisory_xact_lock" in statements[0]
    assert not any("part_pool" in statement for statement in statements)
    assert not any(
        "fact_data_quality_issue" in statement
        for statement in statements
    )


def test_recompute_builds_history_scope_from_only_old_five_layer_misses(
    db,
    batch,
    monkeypatch,
):
    """历史参考的目标集合只含旧五层最终未命中的 PN。"""
    _load_purchases(
        db,
        batch,
        {
            "P-RECENT": f.purchase_head(
                "P-RECENT",
                on=date(2026, 3, 8),
                tax_rate=Decimal("0"),
            ),
            "P-OLD": f.purchase_head(
                "P-OLD",
                on=date(2025, 1, 8),
                tax_rate=Decimal("0"),
            ),
        },
        [
            f.purchase_line(
                "P-RECENT",
                "PL-RECENT",
                "PN-SCOPE-RESOLVED",
                qty="1",
                price="100",
            ),
            f.purchase_line(
                "P-OLD",
                "PL-OLD",
                "PN-SCOPE-UNRESOLVED",
                qty="1",
                price="70",
            ),
        ],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 10))},
        [
            f.maintenance_line(
                "M1",
                "ML-RESOLVED",
                "PN-SCOPE-RESOLVED",
                qty="1",
            ),
            f.maintenance_line(
                "M1",
                "ML-UNRESOLVED",
                "PN-SCOPE-UNRESOLVED",
                qty="1",
            ),
        ],
    )
    db.commit()
    expected_part_id = _part_id(db, "PN-SCOPE-UNRESOLVED")
    resolved_part_id = _part_id(db, "PN-SCOPE-RESOLVED")
    captured_targets = []
    original = maintenance_cost_reference.build_reference_index

    def capture_scope(session, *, target_part_ids, max_as_of):
        targets = tuple(target_part_ids)
        captured_targets.append(frozenset(targets))
        return original(
            session,
            target_part_ids=targets,
            max_as_of=max_as_of,
        )

    monkeypatch.setattr(
        maintenance_cost_reference,
        "build_reference_index",
        capture_scope,
    )

    maintenance_cost.recompute(db)

    assert captured_targets == [frozenset({expected_part_id})]
    assert resolved_part_id not in captured_targets[0]
    assert _line(db, "ML-RESOLVED").cost_source == "window"
    assert _line(db, "ML-UNRESOLVED").cost_source == "purchase_history"


def test_same_pool_and_as_of_share_reference_aggregation(monkeypatch):
    """同一人工池同一截止日只汇总一次，不按目标 PN 重建整池月份。"""
    sample = maintenance_cost_reference.CostSample(
        side="purchase",
        part_id=3,
        occurred_on=date(2025, 10, 1),
        qty=Decimal("2"),
        unit_price=Decimal("80"),
        tax_rate=Decimal("0"),
        is_tax_inclusive=False,
    )
    index = maintenance_cost_reference.CostReferenceIndex(
        target_pool={1: (99, 4), 2: (99, 4)},
        pool_members={99: frozenset({1, 2, 3})},
        purchases={3: {"2025-10": (sample,)}},
        sales={},
    )
    summary_calls = 0
    original = maintenance_cost_reference.summarize_samples

    def count_summary(samples):
        nonlocal summary_calls
        summary_calls += 1
        return original(samples)

    monkeypatch.setattr(
        maintenance_cost_reference,
        "summarize_samples",
        count_summary,
    )

    first = index.resolve(1, date(2026, 3, 1))
    second = index.resolve(2, date(2026, 3, 1))

    assert first == second
    assert first is not None
    assert first.source == "pool_purchase"
    assert summary_calls == 1


@pytest.mark.parametrize(
    ("target_has_own_history", "expected_source", "expected_cost"),
    [
        (True, "purchase_history", Decimal("66.00")),
        (False, "none", None),
    ],
)
def test_active_legacy_generated_pool_is_not_a_cost_reference(
    db,
    batch,
    target_has_own_history,
    expected_source,
    expected_cost,
):
    """未人工确认的 active legacy_generated 池不得用于池均价。"""
    target_pn = (
        "PN-LEGACY-POOL-OWN"
        if target_has_own_history
        else "PN-LEGACY-POOL-EMPTY"
    )
    purchase_lines = [
        f.purchase_line(
            "P1",
            "PL-PEER",
            "PN-LEGACY-POOL-PEER",
            qty="1",
            price="200",
        ),
    ]
    if target_has_own_history:
        purchase_lines.append(
            f.purchase_line(
                "P1",
                "PL-OWN",
                target_pn,
                qty="1",
                price="66",
            )
        )
    _load_purchases(
        db,
        batch,
        {"P1": f.purchase_head("P1", on=date(2025, 1, 8), tax_rate=Decimal("0"))},
        purchase_lines,
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 10))},
        [f.maintenance_line("M1", "ML1", target_pn, qty="1")],
    )
    db.flush()
    _add_pool(
        db,
        910,
        1,
        target_pn,
        "PN-LEGACY-POOL-PEER",
        source="legacy_generated",
    )
    db.commit()

    maintenance_cost.recompute(db)

    line = _line(db, "ML1")
    assert line.cost_source == expected_source
    assert line.unit_cost == expected_cost
    assert line.reference_pool_group_id is None


def test_reference_failure_rolls_back_reset_and_keeps_previous_cost(
    db,
    batch,
    monkeypatch,
):
    """历史索引异常不得提交已清零的半成品，整次重算可原子回滚。"""
    _load_purchases(
        db,
        batch,
        {"P1": f.purchase_head("P1", on=date(2026, 3, 8), tax_rate=Decimal("0"))},
        [f.purchase_line("P1", "PL1", "PN-ATOMIC", qty="1", price="88")],
    )
    _load_maintenance(
        db,
        batch,
        {"M1": f.maintenance_head("M1", on=date(2026, 3, 10))},
        [f.maintenance_line("M1", "ML1", "PN-ATOMIC", qty="2")],
    )
    db.commit()
    maintenance_cost.recompute(db)
    original = _line(db, "ML1")
    snapshot = (
        original.cost_source,
        original.unit_cost,
        original.cost_amount,
        original.unit_cost_inc_tax,
        original.unit_cost_ex_tax,
    )

    def fail_reference(*_args, **_kwargs):
        raise RuntimeError("synthetic reference index failure")

    monkeypatch.setattr(
        maintenance_cost_reference,
        "build_reference_index",
        fail_reference,
    )
    with pytest.raises(RuntimeError, match="synthetic reference"):
        maintenance_cost.recompute(db)
    db.rollback()
    db.expire_all()

    restored = _line(db, "ML1")
    assert (
        restored.cost_source,
        restored.unit_cost,
        restored.cost_amount,
        restored.unit_cost_inc_tax,
        restored.unit_cost_ex_tax,
    ) == snapshot
