"""Three- and four-digit XSDD sequence suffixes share one strict contract."""

from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import func, literal, select, text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.services import maintenance_ai_fallback
from app.services import maintenance_ckd_import
from app.services import maintenance_doc_import
from app.services import maintenance_ledger
from app.services import maintenance_project_identity


_ROOT = Path(__file__).resolve().parents[1]
_PREVIOUS = "a7c2e9f4b1d6"


def _alembic_cfg() -> AlembicConfig:
    config = AlembicConfig(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    return config


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


def test_three_digit_migration_backfills_only_active_contract_backed_owner(db):
    """b8 repairs safe legacy owners without guessing conflicting evidence."""

    db.close()
    config = _alembic_cfg()
    alembic_command.downgrade(config, _PREVIOUS)
    try:
        with engine.begin() as connection:
            projects = [
                ("xsdd-3-mig-contract", "XSDD-3-MIG-CONTRACT", True),
                ("xsdd-3-mig-aligned", "XSDD-3-MIG-ALIGNED", True),
                ("xsdd-3-mig-wbdd-only", "XSDD-3-MIG-WBDD-ONLY", True),
                ("xsdd-3-mig-split-contract", "XSDD-3-MIG-SPLIT-C", True),
                ("xsdd-3-mig-split-wbdd", "XSDD-3-MIG-SPLIT-W", True),
                ("xsdd-3-mig-double-a", "XSDD-3-MIG-DOUBLE-A", True),
                ("xsdd-3-mig-double-b", "XSDD-3-MIG-DOUBLE-B", True),
                ("xsdd-3-mig-inactive", "XSDD-3-MIG-INACTIVE", False),
            ]
            connection.execute(
                text(
                    "INSERT INTO maintenance_project "
                    "(project_id, project_code, display_name, lifecycle_status, is_active) "
                    "VALUES (:project_id, :project_code, :project_code, 'ongoing', :active)"
                ),
                [
                    {
                        "project_id": project_id,
                        "project_code": project_code,
                        "active": active,
                    }
                    for project_id, project_code, active in projects
                ],
            )
            contracts = [
                ("contract-only", "xsdd-3-mig-contract", "20260902-101"),
                ("aligned", "xsdd-3-mig-aligned", "20260902-102"),
                ("split", "xsdd-3-mig-split-contract", "20260902-104"),
                ("double-a", "xsdd-3-mig-double-a", "20260902-105"),
                ("double-b", "xsdd-3-mig-double-b", "20260902-105"),
                ("inactive", "xsdd-3-mig-inactive", "20260902-106"),
            ]
            connection.execute(
                text(
                    "INSERT INTO maintenance_project_contract "
                    "(project_contract_id, project_id, contract_id, contract_no, "
                    "status_mapping_state, status_mapping_version, included_in_total, "
                    "effective_from, source) VALUES "
                    "(:contract_row, :project_id, :contract_id, :contract_no, "
                    "'mapped', 'migration-test-v1', true, DATE '2026-01-01', "
                    "'migration-test')"
                ),
                [
                    {
                        "contract_row": f"xsdd-3-mig-{suffix}",
                        "project_id": project_id,
                        "contract_id": f"XSDD-3-MIG-{suffix.upper()}",
                        "contract_no": f"XSDD-{xsdd}",
                    }
                    for suffix, project_id, xsdd in contracts
                ],
            )
            batch_id = connection.scalar(
                text(
                    "INSERT INTO sys_import_batch "
                    "(filename, file_type, file_hash, status) VALUES "
                    "('xsdd-three-digit-migration.xlsx', 'maintenance', "
                    "'xsdd-three-digit-migration', 'success') RETURNING id"
                )
            )
            wbdd_rows = [
                ("aligned", "xsdd-3-mig-aligned", "20260902-102"),
                ("only", "xsdd-3-mig-wbdd-only", "20260902-103"),
                ("split", "xsdd-3-mig-split-wbdd", "20260902-104"),
            ]
            connection.execute(
                text(
                    "INSERT INTO f_maintenance_order "
                    "(raw_order_id, order_no, linked_sales_order_no, project_raw, "
                    "project_std, data_status, import_batch_id) VALUES "
                    "(:source_id, :source_id, :xsdd, '迁移测试', '迁移测试', "
                    "'已生效', :batch_id)"
                ),
                [
                    {
                        "source_id": f"WBDD-3-MIG-{suffix.upper()}",
                        "xsdd": f"XSDD-{xsdd}",
                        "batch_id": batch_id,
                    }
                    for suffix, _project_id, xsdd in wbdd_rows
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO maintenance_source_order_assignment "
                    "(assignment_id, source_order_id, project_id, created_by) VALUES "
                    "(:assignment_id, :source_id, :project_id, 'migration-test')"
                ),
                [
                    {
                        "assignment_id": f"xsdd-3-mig-assignment-{suffix}",
                        "source_id": f"WBDD-3-MIG-{suffix.upper()}",
                        "project_id": project_id,
                    }
                    for suffix, project_id, _xsdd in wbdd_rows
                ],
            )

            assert connection.scalar(text(
                "SELECT maintenance_normalize_xsdd('XSDD-20260902-101')"
            )) == ""
            assert connection.scalar(text(
                "SELECT count(*) FROM maintenance_project_xsdd "
                "WHERE xsdd_norm LIKE '20260902-%'"
            )) == 0

        alembic_command.upgrade(config, "head")
        with engine.connect() as connection:
            mappings = dict(connection.execute(text(
                "SELECT xsdd_norm, project_id FROM maintenance_project_xsdd "
                "WHERE xsdd_norm LIKE '20260902-%' ORDER BY xsdd_norm"
            )).all())
            assert mappings == {
                "20260902-101": "xsdd-3-mig-contract",
                "20260902-102": "xsdd-3-mig-aligned",
            }
            connection.execute(text(
                "SELECT maintenance_require_contract_xsdd_owner("
                "'XSDD-20260902-102', 'xsdd-3-mig-aligned')"
            ))

        with engine.connect() as connection:
            with pytest.raises(DBAPIError, match="no unique matching sales contract owner"):
                connection.execute(text(
                    "SELECT maintenance_require_contract_xsdd_owner("
                    "'XSDD-20260902-103', 'xsdd-3-mig-wbdd-only')"
                ))

        alembic_command.downgrade(config, _PREVIOUS)
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT maintenance_normalize_xsdd('XSDD-20260902-101')"
            )) == ""
            assert connection.scalar(text(
                "SELECT count(*) FROM maintenance_project_xsdd "
                "WHERE source = 'migration_three_digit_contract_evidence'"
            )) == 0

        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO maintenance_project_xsdd "
                "(xsdd_norm, project_id, source) VALUES "
                "('20260902-101', 'xsdd-3-mig-double-a', 'direct-test')"
            ))
        with pytest.raises(DBAPIError, match="map conflicting with contract evidence"):
            alembic_command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT maintenance_normalize_xsdd('XSDD-20260902-101')"
            )) == ""
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE maintenance_project_xsdd DISABLE TRIGGER "
                "trg_maintenance_xsdd_map_preserve_evidence"
            ))
            connection.execute(text(
                "DELETE FROM maintenance_project_xsdd "
                "WHERE xsdd_norm = '20260902-101'"
            ))
            connection.execute(text(
                "ALTER TABLE maintenance_project_xsdd ENABLE TRIGGER "
                "trg_maintenance_xsdd_map_preserve_evidence"
            ))
    finally:
        alembic_command.upgrade(config, "head")
