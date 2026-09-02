"""Three- and four-digit XSDD sequence suffixes share one strict contract."""

import pytest
from sqlalchemy import func, literal, select

from app.services import maintenance_ai_fallback
from app.services import maintenance_ckd_import
from app.services import maintenance_doc_import
from app.services import maintenance_ledger
from app.services import maintenance_project_identity


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("XSDD-20260901-044", "20260901-044"),
        ("XSDD-20260901-0044", "20260901-0044"),
        ("XSDD-20260901-44", ""),
        ("XSDD-20260901-00044", ""),
    ],
)
def test_python_database_and_query_normalizers_share_suffix_width(
    db, raw_value, expected
):
    assert maintenance_project_identity.normalize_xsdd(raw_value) == expected
    assert db.scalar(select(func.maintenance_normalize_xsdd(raw_value))) == expected
    assert db.scalar(select(
        maintenance_project_identity.normalized_xsdd_sql(literal(raw_value))
    )) == expected


_EXTRACTORS = (
    maintenance_ledger._clean_order_no,
    lambda value: maintenance_doc_import._clean(
        value, maintenance_doc_import._XSDD_RE
    ),
    lambda value: maintenance_ckd_import._clean(
        value, maintenance_ckd_import._XSDD_RE
    ),
)


@pytest.mark.parametrize("extractor", _EXTRACTORS)
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("关联 XSDD-20260901-044 项目", "XSDD-20260901-044"),
        ("关联 XSDD-20260901-0044 项目", "XSDD-20260901-0044"),
        ("关联 XSDD-20260901-44 项目", None),
        ("关联 XSDD-20260901-00044 项目", None),
    ],
)
def test_embedded_xsdd_extractors_reject_other_suffix_widths(
    extractor, raw_value, expected
):
    assert extractor(raw_value) == expected


def test_ai_sample_masking_covers_both_valid_suffix_widths_only():
    assert maintenance_ai_fallback._mask_sample_value(
        "XSDD-20260901-044"
    ) == "<单号>"
    assert maintenance_ai_fallback._mask_sample_value(
        "XSDD-20260901-0044"
    ) == "<单号>"
    assert maintenance_ai_fallback._mask_sample_value(
        "XSDD-20260901-44"
    ) != "<单号>"
    assert maintenance_ai_fallback._mask_sample_value(
        "XSDD-20260901-00044"
    ) != "<单号>"
