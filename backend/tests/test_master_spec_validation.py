"""人工规格输入的公开验证接口，不以自动描述抽取规则限制人工容量。"""
from decimal import Decimal

from app.services.spec_extract import validate_manual_specs


def test_manual_memory_capacity_accepts_small_gb_values():
    result = validate_manual_specs({"capacity": {
        "value": "32GB", "unit": "GB", "numeric_value": "32"}})
    assert result["capacity"]["numeric_value"] == Decimal("32")
