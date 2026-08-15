"""B3 不返还规则测试：项目级默认 + 领用行级覆盖 + 品类回退。"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.master_data import ProductCategory
from app.models.maintenance_project import MaintenanceProject
from app.services.maintenance_bad_returns import (
    classify_return_obligation,
)


@pytest.fixture()
def disk_category(db):
    cat = ProductCategory(
        category_major="硬盘",
        category_minor="机械硬盘",
    )
    db.add(cat)
    db.flush()
    return cat.id


@pytest.fixture()
def memory_category(db):
    cat = ProductCategory(
        category_major="内存",
        category_minor="DDR4",
    )
    db.add(cat)
    db.flush()
    return cat.id


def test_classify_disk_exempt_by_category(disk_category):
    result = classify_return_obligation(
        category_id=disk_category,
        category_major="硬盘",
        category_minor="机械硬盘",
    )
    assert result["classification"] == "exempt"
    assert result["exemption_source"] == "category_disk"


def test_classify_other_category_required(memory_category):
    result = classify_return_obligation(
        category_id=memory_category,
        category_major="内存",
        category_minor="DDR4",
    )
    assert result["classification"] == "required"
    assert result["exemption_source"] == "none"


def test_classify_line_override_true_beats_category(disk_category):
    result = classify_return_obligation(
        category_id=disk_category,
        category_major="硬盘",
        category_minor="机械硬盘",
        no_return_line=False,
    )
    assert result["classification"] == "required"
    assert result["exemption_source"] == "none"


def test_classify_line_override_false_on_required_category(memory_category):
    result = classify_return_obligation(
        category_id=memory_category,
        category_major="内存",
        category_minor="DDR4",
        no_return_line=True,
    )
    assert result["classification"] == "exempt"
    assert result["exemption_source"] == "line_no_return"


def test_classify_project_default_no_return(memory_category):
    result = classify_return_obligation(
        category_id=memory_category,
        category_major="内存",
        category_minor="DDR4",
        project_no_return_default=True,
    )
    assert result["classification"] == "exempt"
    assert result["exemption_source"] == "project_default_no_return"


def test_classify_line_false_overrides_project_default(memory_category):
    result = classify_return_obligation(
        category_id=memory_category,
        category_major="内存",
        category_minor="DDR4",
        no_return_line=False,
        project_no_return_default=True,
    )
    assert result["classification"] == "required"
    assert result["exemption_source"] == "none"


def test_classify_without_category_stays_pending():
    result = classify_return_obligation(
        category_id=None,
        category_major=None,
        category_minor=None,
        project_no_return_default=True,
    )
    assert result["classification"] == "pending_category"
    assert result["exemption_source"] is None
