"""Pure business rules for maintenance bad-part return obligations."""

from decimal import Decimal

from app.services.maintenance_bad_returns import (
    RETURN_RULE_VERSION,
    calculate_return_rate,
    classify_return_obligation,
)


def test_only_standard_hard_drive_category_is_exempt() -> None:
    assert classify_return_obligation(
        category_id=17,
        category_major="硬盘",
        category_minor="SAS-HDD",
    ) == {
        "classification": "exempt",
        "category_id_snapshot": 17,
        "category_major_snapshot": "硬盘",
        "category_minor_snapshot": "SAS-HDD",
        "rule_version": RETURN_RULE_VERSION,
    }
    assert classify_return_obligation(
        category_id=18,
        category_major="服务器配件",
        category_minor="硬盘托架",
    )["classification"] == "required"
    assert classify_return_obligation(
        category_id=None,
        category_major="硬盘",
        category_minor=None,
    )["classification"] == "pending_category"


def test_return_rate_never_fabricates_a_percentage_for_incomplete_or_empty_basis() -> None:
    incomplete = calculate_return_rate(
        required_quantity=Decimal("10"),
        exempt_quantity=Decimal("2"),
        pending_quantity=Decimal("1"),
        registered_quantity=Decimal("4"),
        warehouse_confirmed_quantity=Decimal("3"),
    )
    assert incomplete["status"] == "basis_incomplete"
    assert incomplete["registered_rate_pct"] is None
    assert incomplete["warehouse_confirmed_rate_pct"] is None

    empty = calculate_return_rate(
        required_quantity=Decimal("0"),
        exempt_quantity=Decimal("2"),
        pending_quantity=Decimal("0"),
        registered_quantity=Decimal("0"),
        warehouse_confirmed_quantity=Decimal("0"),
    )
    assert empty["status"] == "no_return_required"
    assert empty["official_rate_pct"] is None

    available = calculate_return_rate(
        required_quantity=Decimal("8"),
        exempt_quantity=Decimal("2"),
        pending_quantity=Decimal("0"),
        registered_quantity=Decimal("6"),
        warehouse_confirmed_quantity=Decimal("4"),
    )
    assert available["status"] == "available"
    assert available["registered_rate_pct"] == "75.00"
    assert available["warehouse_confirmed_rate_pct"] == "50.00"
    assert available["official_basis"] == "warehouse_confirmed_v1"
    assert available["official_rate_pct"] == "50.00"
