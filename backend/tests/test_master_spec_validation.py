"""人工规格输入的公开验证接口，不以自动描述抽取规则限制人工容量。"""
from decimal import Decimal

import pytest

from app.services.spec_extract import spec_label, validate_manual_specs


def test_manual_memory_capacity_accepts_small_gb_values():
    result = validate_manual_specs({"capacity": {
        "value": "32GB", "unit": "GB", "numeric_value": "32"}})
    assert result["capacity"]["numeric_value"] == Decimal("32")


def test_custom_specs_are_bounded_manual_text_with_readable_labels():
    result = validate_manual_specs({"custom:GPU显存": {
        "value": " 80GB HBM3 ", "unit": None, "numeric_value": None}})
    assert result == {"custom:GPU显存": {
        "spec_value": "80GB HBM3", "spec_unit": None,
        "numeric_value": None, "source": "manual"}}
    assert spec_label("custom:GPU显存") == "GPU显存"
    assert spec_label("capacity") == "容量"
    assert validate_manual_specs({"custom:GPU显存": None}) == {"custom:GPU显存": None}


@pytest.mark.parametrize("key,spec", [
    ("unknown", {"value": "x", "unit": None, "numeric_value": None}),
    ("custom:", None),
    ("custom: 空格", None),
    ("custom:空格 ", None),
    ("custom:坏\n名字", None),
    ("custom:" + "名" * 26, None),
    ("custom:显存", {"value": "80", "unit": "GB", "numeric_value": None}),
    ("custom:核心数", {"value": "80", "unit": None, "numeric_value": "80"}),
    ("custom:显存", {"value": " ", "unit": None, "numeric_value": None}),
    ("custom:显存", {"value": "x" * 129, "unit": None, "numeric_value": None}),
    ("capacity", {"value": "2TB", "unit": "GB", "numeric_value": "2"}),
])
def test_custom_namespace_cannot_bypass_spec_or_numeric_validation(key, spec):
    with pytest.raises(ValueError):
        validate_manual_specs({key: spec})
