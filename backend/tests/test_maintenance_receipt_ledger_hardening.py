"""收款单（SKD）台账导入复核加固（D-16，2026-09-06 复核后补充，#288）。

对应审查结论：
1. 预览→应用 CAS 盖不住累计所依赖的台账/基线（stale_preview）；
2. 旧 05 表合同过渡：基线规则会把历史越算越少（seed_required / cumulative_unverifiable）；
3. 同批两个文件触及同一合同（cross_file_same_contract）；
4. 作废月合同级 fail-closed；只有台账的月份是回填不是漂移（ledger_backfill）；
5. 网关应用绕过实名门禁（覆盖行必须实名）；
6. receipt_conflict 死胡同：人工裁决端点（旧行留档、更正行生效、不动快照）；
7. 同一原件分次提交后 downgrade 撞唯一索引 → 受保护降级拒绝；
8. sys_raw_file 只在应用时落，未应用的预览原件可被 GC 回收；
10. 覆盖回执点名原操作人；
11. depends_on_row_keys / hint_messages 显式化，缺依赖在写入前硬拒。
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command as alembic_command
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import permissions
from app.auth import _make_token
from app.config import get_settings
from app.db import SessionLocal, engine
from app.main import app
from app.models.maintenance_project_operations import (
    MaintenanceCollectionReceipt,
    MaintenanceCollectionSnapshot,
    MaintenanceProjectOperationAudit,
)
from app.models.system import SysAuditLog, SysImportBatch, SysRawFile
from app.services import maintenance_bulk_import as bulk
from app.services import maintenance_project_operations as operations
from app.services import raw_archive_gc
from tests.boss_board_helpers import client_for
from tests.test_maintenance_receipt_ledger_import import (
    ORDER_NO,
    PREVIOUS,
    _apply,
    _cfg,
    _preview,
    _project_with_contract,
    _receipt_xlsx,
    _rows,
    _snapshots,
)

NORM = "20240101-0001"
_API = "/api/maintenance/project-batch-transfer"


def _legacy(db, project, contract, month: date, amount: str, *, status: str = "confirmed"):
    row = MaintenanceCollectionSnapshot(
        collection_id=str(uuid4()), project_id=project.project_id,
        project_contract_id=contract.project_contract_id,
        report_month=month, cumulative_amount=Decimal(amount),
        status=status, source="legacy", version=1,
    )
    db.add(row)
    db.commit()
    return row


def _by_month(preview: dict) -> dict[str, dict]:
    return {
        row["canonical"]["report_month"]: row
        for row in preview["rows"]
        if row.get("canonical", {}).get("report_month")
    }


def _ready_keys(preview: dict) -> list[str]:
    return [row["row_key"] for row in _rows(preview, row_status="ready")]


def _ledger(db) -> list[MaintenanceCollectionReceipt]:
    db.expire_all()
    return list(db.scalars(
        select(MaintenanceCollectionReceipt).order_by(MaintenanceCollectionReceipt.id)
    ))


# ---------- (1) 预览→应用 CAS 必须盖住台账与基线 ----------

def test_ledger_change_between_preview_and_apply_is_stale_preview(db):
    """审查 probe：预览 A（2 月）后另一次应用把 1 月抬到 130；A 若照旧落库，2 月 150 就是错的。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))

    preview_a = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 50)]))
    feb_a = _by_month(preview_a)["2026-02-01"]
    assert feb_a["after"]["cumulative_amount"] == "150.00"

    preview_b = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1B", date(2026, 1, 25), 30)]), name="b.xlsx")
    jan_b = _by_month(preview_b)["2026-01-01"]
    assert jan_b["action"] == "update_collection_snapshot"
    _apply(db, preview_b, [jan_b["row_key"]])
    assert _snapshots(db, contract)[date(2026, 1, 1)].cumulative_amount == Decimal("130.00")

    with pytest.raises(bulk.BulkImportConflict, match="预览后台账/快照已变化，请重新预览") as caught:
        _apply(db, preview_a, [feb_a["row_key"]])
    assert caught.value.code == "stale_preview"
    db.rollback()
    assert date(2026, 2, 1) not in _snapshots(db, contract)
    assert {row.receipt_no for row in _ledger(db)} == {"SK-1", "SK-1B"}

    # 重新预览即得到正确值 180，依旧可应用
    again = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 50)]), name="c.xlsx")
    feb = _by_month(again)["2026-02-01"]
    assert feb["after"]["cumulative_amount"] == "180.00"
    _apply(db, again, [feb["row_key"]])
    assert _snapshots(db, contract)[date(2026, 2, 1)].cumulative_amount == Decimal("180.00")


def test_baseline_month_edited_between_preview_and_apply_is_stale_preview(db):
    """基线月（覆盖范围之前的已确认快照）被修正：预览时 260 = 200 + 60，真值 310。"""
    project, contract = _project_with_contract(db)
    apr = _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-MAY", date(2026, 5, 3), 60)]))
    may = _by_month(preview)["2026-05-01"]
    assert may["after"]["cumulative_amount"] == "260.00" and may["row_status"] == "ready"

    operations.update_collection(
        db, collection_id=apr.collection_id, version=apr.version,
        updates={"cumulative_amount": Decimal("250.00")}, reason="correction",
        operated_by="corrector",
    )
    db.commit()

    with pytest.raises(bulk.BulkImportConflict) as caught:
        _apply(db, preview, [may["row_key"]])
    assert caught.value.code == "stale_preview"
    db.rollback()
    snaps = _snapshots(db, contract)
    assert date(2026, 5, 1) not in snaps
    assert snaps[date(2026, 4, 1)].cumulative_amount == Decimal("250.00")
    assert _ledger(db) == []


def test_baseline_voided_between_preview_and_apply_is_stale_preview(db):
    project, contract = _project_with_contract(db)
    apr = _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-MAY", date(2026, 5, 3), 60)]))
    may = _by_month(preview)["2026-05-01"]
    operations.update_collection(
        db, collection_id=apr.collection_id, version=apr.version,
        updates={"status": "void"}, reason="void", operated_by="someone-else",
    )
    db.commit()
    with pytest.raises(bulk.BulkImportConflict) as caught:
        _apply(db, preview, [may["row_key"]])
    assert caught.value.code == "stale_preview"
    db.rollback()
    assert date(2026, 5, 1) not in _snapshots(db, contract)


def test_snapshot_inserted_between_baseline_and_coverage_is_stale_preview(db):
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-JUN", date(2026, 6, 3), 40)]))
    jun = _by_month(preview)["2026-06-01"]
    assert jun["after"]["cumulative_amount"] == "240.00"
    operations.create_collection(
        db, project_id=project.project_id, project_contract_id=contract.project_contract_id,
        report_month=date(2026, 5, 1), cumulative_amount=Decimal("230.00"), status="confirmed",
        receipt_reference=None, remark=None, reason="manual", operated_by="someone-else",
    )
    db.commit()
    with pytest.raises(bulk.BulkImportConflict) as caught:
        _apply(db, preview, [jun["row_key"]])
    assert caught.value.code == "stale_preview"
    db.rollback()
    assert date(2026, 6, 1) not in _snapshots(db, contract)


def test_two_operators_previews_second_apply_is_stale_preview(db):
    """两个操作员各自预览不同月份的增量导出：先应用的赢，后应用的必须重新预览。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100), (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ]))
    _apply(db, first, _ready_keys(first))

    preview_a = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-3", date(2026, 3, 2), 20)]), name="a.xlsx")
    preview_b = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-4", date(2026, 4, 2), 60)]), name="b.xlsx")
    assert _by_month(preview_b)["2026-04-01"]["after"]["cumulative_amount"] == "210.00"

    _apply(db, preview_a, _ready_keys(preview_a))
    with pytest.raises(bulk.BulkImportConflict) as caught:
        _apply(db, preview_b, _ready_keys(preview_b))
    assert caught.value.code == "stale_preview"
    db.rollback()
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 3, 1)].cumulative_amount == Decimal("170.00")
    assert date(2026, 4, 1) not in snaps
    # B 的批次可由入口记为 failed；重新预览得到真值 230
    again = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-4", date(2026, 4, 2), 60)]), name="b2.xlsx")
    assert _by_month(again)["2026-04-01"]["after"]["cumulative_amount"] == "230.00"


def test_ledger_grows_before_apply_even_for_earlier_month(db):
    """预览 A（5 月 60）后另一批把 4 月 40 入账：A 冻结的 60 少了 4 月的钱。"""
    project, contract = _project_with_contract(db)
    preview_a = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-MAY", date(2026, 5, 3), 60)]))
    may = _by_month(preview_a)["2026-05-01"]
    assert may["after"]["cumulative_amount"] == "60.00"
    preview_b = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-APR", date(2026, 4, 9), 40)]), name="b.xlsx")
    _apply(db, preview_b, _ready_keys(preview_b))
    with pytest.raises(bulk.BulkImportConflict) as caught:
        _apply(db, preview_a, [may["row_key"]])
    assert caught.value.code == "stale_preview"
    db.rollback()
    assert date(2026, 5, 1) not in _snapshots(db, contract)


# ---------- (2) 旧 05 表合同过渡：先建账，历史不能越算越少 ----------

def test_mid_history_file_on_legacy_contract_requires_seed(db):
    """审查 probe：4 月 200、5 月 260 是旧快照，文件从 5 月中旬开始。旧口径会给出
    5 月 260→235、6 月 300→275 的"覆盖"，7 月 305 而不是 330——唯一的导入方法是
    勾错误的降值。新口径：整合同 seed_required，一分钱都不算。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    _legacy(db, project, contract, date(2026, 5, 1), "260.00")
    _legacy(db, project, contract, date(2026, 6, 1), "300.00")

    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-M2", date(2026, 5, 25), 35),
        (ORDER_NO, "SK-J", date(2026, 6, 3), 40),
        (ORDER_NO, "SK-JL", date(2026, 7, 3), 30),
    ]))

    assert preview["can_apply"] is False and preview["summary"]["ready"] == 0
    by_month = _by_month(preview)
    assert set(by_month) == {"2026-05-01", "2026-06-01", "2026-07-01"}
    for row in by_month.values():
        assert row["row_status"] == "blocked"
        assert row["after"]["cumulative_amount"] is None
        assert any(issue["code"] == "seed_required" for issue in row["errors"])
        assert any(
            "该合同已有确认快照但尚无收款单台账，请先上传该合同的全量历史收款单导出建账" in hint
            for hint in row["hint_messages"]
        )
    assert _ledger(db) == []
    assert {m: str(s.cumulative_amount) for m, s in _snapshots(db, contract).items()} == {
        date(2026, 4, 1): "200.00", date(2026, 5, 1): "260.00", date(2026, 6, 1): "300.00",
    }


def test_full_history_export_seeds_ledger_then_incremental_derives_from_it(db):
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    _legacy(db, project, contract, date(2026, 5, 1), "260.00")
    _legacy(db, project, contract, date(2026, 6, 1), "300.00")

    full = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-A", date(2026, 4, 5), 200),
        (ORDER_NO, "SK-M", date(2026, 5, 3), 60),
        (ORDER_NO, "SK-J", date(2026, 6, 3), 40),
        (ORDER_NO, "SK-JL", date(2026, 7, 3), 30),
    ]))
    by_month = _by_month(full)
    assert [(m, by_month[m]["action"], by_month[m]["after"]["cumulative_amount"]) for m in sorted(by_month)] == [
        ("2026-04-01", "record_receipts", "200.00"),
        ("2026-05-01", "record_receipts", "260.00"),
        ("2026-06-01", "record_receipts", "300.00"),
        ("2026-07-01", "upsert_collection_snapshot", "330.00"),
    ]
    assert full["summary"]["ready"] == 4
    result = _apply(db, full, _ready_keys(full))
    assert result["applied"] == 4
    assert {row.receipt_no for row in _ledger(db)} == {"SK-A", "SK-M", "SK-J", "SK-JL"}
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 7, 1)].cumulative_amount == Decimal("330.00")
    assert snaps[date(2026, 5, 1)].version == 1  # record_receipts 不动快照

    # 建账后：增量导出按台账推导，历史月份 noop、新月份 create
    incremental = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-AUG", date(2026, 8, 2), 20)]))
    by_month = _by_month(incremental)
    assert by_month["2026-08-01"]["action"] == "upsert_collection_snapshot"
    assert by_month["2026-08-01"]["after"]["cumulative_amount"] == "350.00"
    assert all(by_month[m]["row_status"] == "unchanged" for m in ("2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01"))
    _apply(db, incremental, _ready_keys(incremental))
    assert _snapshots(db, contract)[date(2026, 8, 1)].cumulative_amount == Decimal("350.00")

    # 台账有支撑的月份迟到一笔：显式覆盖（默认不勾），级联到后续月份
    late = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-M2", date(2026, 5, 20), 5)]))
    by_month = _by_month(late)
    assert by_month["2026-05-01"]["action"] == "update_collection_snapshot"
    assert by_month["2026-05-01"]["after"]["cumulative_amount"] == "265.00"
    assert by_month["2026-08-01"]["after"]["cumulative_amount"] == "355.00"


def test_partial_export_lowering_unbacked_month_is_unverifiable_not_overwrite(db):
    """审查 probe：12 月已确认 700（手工），台账只到 11 月；文件只带一笔迟到的 12 月 50。
    旧口径给出 700→550 的覆盖；新口径：该月无台账收款支撑、推导值更低 → 不可核实，阻断。"""
    project, contract = _project_with_contract(db)
    nov = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-N", date(2025, 11, 5), 500)]))
    _apply(db, nov, _ready_keys(nov))
    operations.create_collection(
        db, project_id=project.project_id, project_contract_id=contract.project_contract_id,
        report_month=date(2025, 12, 1), cumulative_amount=Decimal("700.00"), status="confirmed",
        receipt_reference=None, remark=None, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-X", date(2025, 12, 15), 50)]))

    by_month = _by_month(preview)
    dec = by_month["2025-12-01"]
    assert dec["row_status"] == "blocked" and dec["action"] == "block"
    assert any(issue["code"] == "cumulative_unverifiable" for issue in dec["errors"])
    assert any("无法核实" in hint for hint in dec["hint_messages"])
    assert not any(w["code"] == "snapshot_overwrite" for w in dec["warnings"])
    assert by_month["2025-11-01"]["row_status"] == "unchanged"
    assert preview["can_apply"] is False
    assert _snapshots(db, contract)[date(2025, 12, 1)].cumulative_amount == Decimal("700.00")


def test_lower_value_with_ledger_backing_stays_explicit_override(db):
    """对照：1 月被人工抬到 200 但台账有 1 月的 100——覆盖行保留（默认不勾）。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    jan = _snapshots(db, contract)[date(2026, 1, 1)]
    operations.update_collection(
        db, collection_id=jan.collection_id, version=jan.version,
        updates={"cumulative_amount": Decimal("200.00")}, reason="manual", operated_by="mgr",
    )
    db.commit()
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 50)]))
    by_month = _by_month(preview)
    assert by_month["2026-01-01"]["action"] == "update_collection_snapshot"
    assert by_month["2026-01-01"]["requires_confirmation"] is True
    assert by_month["2026-01-01"]["after"]["cumulative_amount"] == "100.00"


# ---------- (3) 同批两个文件触及同一合同 ----------

def test_two_files_touching_same_contract_are_blocked_at_preview(db):
    """审查 probe：a.xlsx 5 月 100、b.xlsx 6 月 150 各自独立推导，6 月会漏掉 5 月的钱。"""
    _project, contract = _project_with_contract(db)
    preview = bulk.preview_transfer(
        db,
        [
            ("a.xlsx", _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 5, 10), 100)])),
            ("b.xlsx", _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 6, 10), 150)])),
        ],
        operated_by="skd-operator",
    )
    assert preview["can_apply"] is False and preview["summary"]["ready"] == 0
    month_rows = [row for row in preview["rows"] if row.get("canonical", {}).get("report_month")]
    assert {row["filename"] for row in month_rows} == {"a.xlsx", "b.xlsx"}
    for row in month_rows:
        assert row["row_status"] == "blocked"
        error = next(e for e in row["errors"] if e["code"] == "cross_file_same_contract")
        assert "同一批次两个文件触及同一合同" in error["message"]
        assert "a.xlsx" in error["message"] and "b.xlsx" in error["message"]
    with pytest.raises(bulk.BulkImportInvalid):
        _apply(db, preview, [row["row_key"] for row in month_rows])
    db.rollback()
    assert _snapshots(db, contract) == {}


def test_two_files_for_different_contracts_still_apply(db):
    _project, contract_a = _project_with_contract(db)
    other_no = "XSDD-20240101-0002"
    _project_b, contract_b = _project_with_contract(db, contract_no=other_no)
    preview = bulk.preview_transfer(
        db,
        [
            ("a.xlsx", _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 5, 10), 100)])),
            ("b.xlsx", _receipt_xlsx([(other_no, "SK-2", date(2026, 6, 10), 150)])),
        ],
        operated_by="skd-operator",
    )
    assert preview["summary"]["ready"] == 2
    _apply(db, preview, _ready_keys(preview))
    assert _snapshots(db, contract_a)[date(2026, 5, 1)].cumulative_amount == Decimal("100.00")
    assert _snapshots(db, contract_b)[date(2026, 6, 1)].cumulative_amount == Decimal("150.00")


# ---------- (4) 作废月合同级 fail-closed；只有台账的月份是回填 ----------

def test_void_month_blocks_every_month_of_the_contract(db):
    """审查 probe：5 月快照作废（金额 999），文件带 5 月 60 + 6 月 40。
    旧口径只挡 5 月，6 月 300 把永远进不了台账的 5 月收款算了进去。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    _legacy(db, project, contract, date(2026, 5, 1), "999.00", status="void")

    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-MAY", date(2026, 5, 3), 60),
        (ORDER_NO, "SK-JUN", date(2026, 6, 3), 40),
    ]))

    by_month = _by_month(preview)
    assert set(by_month) == {"2026-05-01", "2026-06-01"}
    for row in by_month.values():
        assert row["row_status"] == "blocked"
        assert row["after"]["cumulative_amount"] is None
        assert any(issue["code"] == "snapshot_voided" for issue in row["errors"])
    assert not any(
        issue["code"] == "collection_not_monotonic"
        for row in by_month.values() for issue in row["errors"]
    )
    assert preview["can_apply"] is False
    assert _ledger(db) == []


def test_voided_snapshot_of_ledgered_month_blocks_later_month(db):
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 5, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    snap = _snapshots(db, contract)[date(2026, 5, 1)]
    operations.update_collection(
        db, collection_id=snap.collection_id, version=snap.version,
        updates={"status": "void"}, reason="void", operated_by="mgr",
    )
    db.commit()
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 7, 10), 50)]))
    by_month = _by_month(preview)
    assert {m: row["row_status"] for m, row in by_month.items()} == {
        "2026-05-01": "blocked", "2026-07-01": "blocked",
    }
    assert all(any(i["code"] == "snapshot_voided" for i in row["errors"]) for row in by_month.values())


def test_ledger_only_month_without_snapshot_is_backfill_not_drift(db):
    project, contract = _project_with_contract(db)
    db.add(MaintenanceCollectionReceipt(
        project_contract_id=contract.project_contract_id, contract_no=NORM,
        receipt_no="SK-OLD", receipt_date=date(2026, 5, 10), actual_amount=Decimal("100.00"),
        source_sha256="a" * 64, is_active=True, created_by="seed",
    ))
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 7, 10), 50)]))

    by_month = _by_month(preview)
    may = by_month["2026-05-01"]
    assert may["action"] == "upsert_collection_snapshot" and may["row_status"] == "ready"
    assert may["after"]["cumulative_amount"] == "100.00"
    assert any(w["code"] == "ledger_backfill" for w in may["warnings"])
    assert not any(w["code"] == "ledger_drift" for w in may["warnings"])
    assert not may["errors"]
    jul = by_month["2026-07-01"]
    assert jul["after"]["cumulative_amount"] == "150.00"
    _apply(db, preview, _ready_keys(preview))
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 5, 1)].cumulative_amount == Decimal("100.00")
    assert snaps[date(2026, 7, 1)].cumulative_amount == Decimal("150.00")


# ---------- (11) 依赖显式化：row_key 级依赖 + 写入前硬拒 ----------

def test_depends_on_update_is_explicit_and_rejected_before_any_write(db):
    """审查 probe：1 月被人工抬到 200，文件带 2 月 50。前端默认只勾 create 行，
    旧口径在守卫处 422 且批次落成 failed；新口径写入前硬拒（预览仍可用）。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    jan = _snapshots(db, contract)[date(2026, 1, 1)]
    operations.update_collection(
        db, collection_id=jan.collection_id, version=jan.version,
        updates={"cumulative_amount": Decimal("200.00")}, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 50)]))
    by_month = _by_month(preview)
    jan_row, feb_row = by_month["2026-01-01"], by_month["2026-02-01"]
    assert jan_row["action"] == "update_collection_snapshot"
    assert feb_row["action"] == "upsert_collection_snapshot" and feb_row["row_status"] == "ready"
    assert feb_row["depends_on_row_keys"] == [jan_row["row_key"]]
    assert any("需同时勾选 2026-01 的覆盖行" in hint for hint in feb_row["hint_messages"])
    assert jan_row["depends_on_row_keys"] == []

    with pytest.raises(bulk.BulkImportInvalid, match="2026-01") as caught:
        _apply(db, preview, [feb_row["row_key"]])
    assert "覆盖行" in str(caught.value)
    db.rollback()
    batch = db.get(SysImportBatch, int(preview["preview_id"]))
    assert batch.status == "processing"
    assert date(2026, 2, 1) not in _snapshots(db, contract)

    result = _apply(db, preview, [jan_row["row_key"], feb_row["row_key"]])
    assert result["applied"] == 2
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 1, 1)].cumulative_amount == Decimal("100.00")
    assert snaps[date(2026, 2, 1)].cumulative_amount == Decimal("150.00")


def test_constituent_months_are_explicit_row_key_dependencies(db):
    _project, _contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100), (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ]))
    _apply(db, first, _ready_keys(first))
    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1B", date(2026, 1, 25), 30), (ORDER_NO, "SK-3", date(2026, 3, 2), 20),
    ]))
    by_month = _by_month(preview)
    jan_key = by_month["2026-01-01"]["row_key"]
    assert by_month["2026-01-01"]["depends_on_row_keys"] == []
    assert by_month["2026-02-01"]["depends_on_row_keys"] == [jan_key]
    assert by_month["2026-03-01"]["depends_on_row_keys"] == [jan_key]
    assert any("2026-01" in hint for hint in by_month["2026-03-01"]["hint_messages"])
    with pytest.raises(bulk.BulkImportInvalid, match="2026-01"):
        _apply(db, preview, [by_month["2026-03-01"]["row_key"]])
    db.rollback()


def test_dependent_month_is_blocked_when_its_constituent_is_blocked(db):
    """1 月被单调性拦下（400 < 旧 11 月 500）而 2 月 550 本身过得了守卫：2 月的累计
    含 1 月的新收款，1 月永远勾不上 → 2 月预览即随之阻断，不留到应用时才拒。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2025, 11, 1), "500.00")
    _legacy(db, project, contract, date(2025, 12, 1), "300.00")
    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100), (ORDER_NO, "SK-2", date(2026, 2, 5), 150),
    ]))
    by_month = _by_month(preview)
    assert any(i["code"] == "collection_not_monotonic" for i in by_month["2026-01-01"]["errors"])
    feb = by_month["2026-02-01"]
    assert feb["row_status"] == "blocked"
    assert any(i["code"] == "constituent_blocked" and "2026-01" in i["message"] for i in feb["errors"])
    assert preview["can_apply"] is False


# ---------- (10) 覆盖回执点名原操作人 ----------

def test_overwrite_names_previous_operator_from_audit(db):
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    jan = _snapshots(db, contract)[date(2026, 1, 1)]
    operations.update_collection(
        db, collection_id=jan.collection_id, version=jan.version,
        updates={"cumulative_amount": Decimal("200.00")}, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1B", date(2026, 1, 25), 30)]))
    jan_row = _by_month(preview)["2026-01-01"]
    overwrite = next(w for w in jan_row["warnings"] if w["code"] == "snapshot_overwrite")
    assert "原操作人 mgr" in overwrite["message"]
    assert "200.00 → 130.00" in overwrite["message"]

    result = _apply(db, preview, [jan_row["row_key"]])
    (receipt_row,) = result["rows"]
    assert receipt_row["previous_operated_by"] == "mgr"
    assert "原操作人 mgr" in receipt_row["message"]
    audit = db.scalars(
        select(MaintenanceProjectOperationAudit)
        .where(MaintenanceProjectOperationAudit.entity_type == "collection")
        .order_by(MaintenanceProjectOperationAudit.id.desc())
    ).first()
    assert audit.operated_by == "skd-operator"
    assert "原操作人 mgr" in audit.reason and "200.00→130.00" in audit.reason


def test_overwrite_falls_back_to_batch_uploader_without_audit(db):
    project, contract = _project_with_contract(db)
    batch = SysImportBatch(
        filename="old.xlsx", file_type="maint_bulk", file_hash="d" * 64,
        uploaded_by="uploader-x", status="success",
    )
    db.add(batch)
    db.flush()
    db.add(MaintenanceCollectionSnapshot(
        collection_id=str(uuid4()), project_id=project.project_id,
        project_contract_id=contract.project_contract_id, report_month=date(2026, 1, 1),
        cumulative_amount=Decimal("200.00"), status="confirmed", source="bulk_import",
        import_batch_id=str(batch.id), version=1,
    ))
    db.add(MaintenanceCollectionReceipt(
        project_contract_id=contract.project_contract_id, contract_no=NORM,
        receipt_no="SK-1", receipt_date=date(2026, 1, 10), actual_amount=Decimal("100.00"),
        import_batch_id=batch.id, source_sha256="d" * 64, is_active=True, created_by="uploader-x",
    ))
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1B", date(2026, 1, 25), 30)]))
    jan_row = _by_month(preview)["2026-01-01"]
    assert jan_row["action"] == "update_collection_snapshot"
    overwrite = next(w for w in jan_row["warnings"] if w["code"] == "snapshot_overwrite")
    assert "原操作人 uploader-x" in overwrite["message"]


# ---------- (6) receipt_conflict 不再是死胡同：人工裁决 ----------

def test_ruling_supersedes_ledger_row_and_repreview_shows_known_and_update(db):
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    conflicting = _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 120), (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ])
    blocked = _preview(db, conflicting)
    assert blocked["summary"]["receipt_conflicts"] == 1 and blocked["can_apply"] is False

    result = bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-1", receipt_date=date(2026, 1, 10),
        actual_amount="120.00", reason="财务核对：系统录入 100 为笔误，凭证为 120",
        operated_by="finance-boss",
    )
    db.commit()

    old, new = _ledger(db)
    assert result == {
        "ruling_id": new.ruling_id,
        "superseded_receipt_id": old.id,
        "new_receipt_id": new.id,
        "affected_months": [
            {"report_month": "2026-01-01", "current_cumulative": "100.00", "derived_cumulative": "120.00"},
        ],
    }
    assert old.is_active is False and old.superseded_by == new.id
    assert old.ruled_by == "finance-boss" and old.ruled_at is not None
    assert "笔误" in old.ruling_reason
    assert old.actual_amount == Decimal("100.00")  # 旧行原样留档
    assert new.is_active is True and new.actual_amount == Decimal("120.00")
    assert new.import_batch_id is None and new.source_sha256 is None
    assert new.ruling_id and new.created_by == "finance-boss"
    audit = db.scalars(select(SysAuditLog).where(SysAuditLog.entity_type == "collection_receipt")).one()
    assert audit.action == "supersede" and audit.entity_id == old.id
    assert audit.before_json["actual_amount"] == "100.00"
    assert audit.after_json["actual_amount"] == "120.00"
    assert audit.after_json["superseded_receipt_id"] == old.id
    assert audit.operated_by == "finance-boss"
    # 裁决不动快照
    assert _snapshots(db, contract)[date(2026, 1, 1)].cumulative_amount == Decimal("100.00")

    again = _preview(db, conflicting, name="again.xlsx")
    assert again["summary"]["receipt_conflicts"] == 0
    assert again["summary"]["known"] == 1
    by_month = _by_month(again)
    jan, feb = by_month["2026-01-01"], by_month["2026-02-01"]
    assert jan["action"] == "update_collection_snapshot"
    assert jan["before"]["cumulative_amount"] == "100.00"
    assert jan["after"]["cumulative_amount"] == "120.00"
    assert any(w["code"] == "ledger_drift" for w in jan["warnings"])
    assert feb["action"] == "upsert_collection_snapshot"
    assert feb["after"]["cumulative_amount"] == "170.00"
    applied = _apply(db, again, [jan["row_key"], feb["row_key"]])
    assert applied["applied"] == 2
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 1, 1)].cumulative_amount == Decimal("120.00")
    assert snaps[date(2026, 2, 1)].cumulative_amount == Decimal("170.00")
    assert {(row.receipt_no, row.is_active) for row in _ledger(db)} == {
        ("SK-1", False), ("SK-1", True), ("SK-2", True),
    }


def test_ruling_rejects_unknown_receipt_and_unchanged_values(db):
    _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    with pytest.raises(bulk.BulkImportNotFound):
        bulk.rule_receipt(
            db, contract_no=ORDER_NO, receipt_no="SK-9", receipt_date=date(2026, 1, 10),
            actual_amount="1.00", reason="x", operated_by="boss",
        )
    db.rollback()
    with pytest.raises(bulk.BulkImportInvalid, match="无需裁决"):
        bulk.rule_receipt(
            db, contract_no=ORDER_NO, receipt_no="SK-1", receipt_date=date(2026, 1, 10),
            actual_amount="100.00", reason="x", operated_by="boss",
        )
    db.rollback()
    assert [row.is_active for row in _ledger(db)] == [True]


# ---------- (5) HTTP：覆盖行与裁决必须实名 ----------

def _shared_admin_client() -> TestClient:
    """共享口令（ADMIN_PASSWORD）登录的 admin：authn=shared、无 sys_user 行。"""
    perms = permissions.runtime_safe(permissions.effective("admin", None))
    token, _exp = _make_token("admin", "admin", None, perms=perms, authn="shared")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _update_preview(db):
    """1 月已入账 100，文件带一笔迟到的 1 月 30 → 1 月覆盖行（update_collection_snapshot）。"""
    _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1B", date(2026, 1, 25), 30)]))
    jan = _by_month(preview)["2026-01-01"]
    assert jan["action"] == "update_collection_snapshot"
    return preview, jan


def _apply_body(preview: dict, keys: list[str]) -> dict:
    return {
        "preview_token": preview["preview_token"],
        "payload_hash": preview["payload_hash"],
        "data_version": preview["data_version"],
        "row_keys": keys,
    }


def test_http_apply_with_update_rows_requires_real_name_operator(db):
    preview, jan = _update_preview(db)

    shared = _shared_admin_client()
    denied = shared.post(f"{_API}/apply", json=_apply_body(preview, [jan["row_key"]]))
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["message"] == "经营事实写入必须使用实名系统账号"
    db.expire_all()
    assert db.get(SysImportBatch, int(preview["preview_id"])).status == "processing"

    real = client_for(db, username="skd-real-admin", role="admin")
    ok = real.post(f"{_API}/apply", json=_apply_body(preview, [jan["row_key"]]))
    assert ok.status_code == 200, ok.text
    assert ok.json()["applied"] == 1
    db.expire_all()
    assert db.get(SysImportBatch, int(preview["preview_id"])).status == "success"


def test_http_apply_without_update_rows_does_not_require_real_name(db):
    _project_with_contract(db)
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    shared = _shared_admin_client()
    ok = shared.post(f"{_API}/apply", json=_apply_body(preview, _ready_keys(preview)))
    assert ok.status_code == 200, ok.text


def test_http_stale_preview_returns_409_with_code_and_marks_batch_failed(db):
    _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    preview_a = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 50)]))
    preview_b = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-3", date(2026, 3, 5), 5)]), name="b.xlsx")
    _apply(db, preview_b, _ready_keys(preview_b))

    real = client_for(db, username="skd-real-admin", role="admin")
    stale = real.post(f"{_API}/apply", json=_apply_body(preview_a, _ready_keys(preview_a)))
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == {
        "code": "stale_preview", "message": "预览后台账/快照已变化，请重新预览",
    }
    db.expire_all()
    batch = db.get(SysImportBatch, int(preview_a["preview_id"]))
    assert batch.status == "failed"
    assert batch.report_json["failure"]["error_code"] == "stale_preview"


def test_http_ruling_requires_admin_or_boss_real_name_and_profit(db):
    _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    body = {
        "contract_no": ORDER_NO, "receipt_no": "SK-1", "receipt_date": "2026-01-10",
        "actual_amount": "120.00", "reason": "凭证为 120",
    }

    shared = _shared_admin_client()
    denied = shared.post(f"{_API}/receipt-rulings", json=body)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == "经营事实写入必须使用实名系统账号"

    sales = client_for(db, username="skd-sales", role="sales", overrides={
        "page_maintenance": True, "data_profit": True, "action_maintenance_ledger_import": True,
    })
    forbidden = sales.post(f"{_API}/receipt-rulings", json=body)
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["detail"]["code"] == "permission_denied"

    boss_without_profit = client_for(db, username="skd-boss-np", role="boss", overrides={
        "page_maintenance": True, "data_profit": False,
    })
    assert boss_without_profit.post(f"{_API}/receipt-rulings", json=body).status_code == 403

    boss = client_for(db, username="skd-boss", role="boss", overrides={
        "page_maintenance": True, "data_profit": True,
    })
    ok = boss.post(f"{_API}/receipt-rulings", json=body)
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert set(payload) == {"ruling_id", "superseded_receipt_id", "new_receipt_id", "affected_months"}
    assert payload["affected_months"] == [
        {"report_month": "2026-01-01", "current_cumulative": "100.00", "derived_cumulative": "120.00"},
    ]
    old, new = _ledger(db)
    assert old.is_active is False and new.is_active is True and new.ruled_by is None
    assert old.ruled_by == "skd-boss" and new.created_by == "skd-boss"

    again = boss.post(f"{_API}/receipt-rulings", json=body)
    assert again.status_code == 422 and "无需裁决" in again.json()["detail"]["message"]
    missing = boss.post(f"{_API}/receipt-rulings", json={**body, "receipt_no": "SK-9"})
    assert missing.status_code == 404


# ---------- (8) 只预览不应用的原件不再被钉住 ----------

def test_abandoned_preview_blob_is_reclaimable_and_applied_one_is_pinned(db):
    _project_with_contract(db)
    raw_dir = os.path.abspath(get_settings().raw_file_dir)
    abandoned = _receipt_xlsx([(ORDER_NO, "SK-A", date(2026, 1, 10), 100)])
    applied = _receipt_xlsx([(ORDER_NO, "SK-B", date(2026, 2, 10), 100)])
    preview_abandoned = _preview(db, abandoned, name="abandoned.xlsx")
    preview_applied = _preview(db, applied, name="applied.xlsx")
    _apply(db, preview_applied, _ready_keys(preview_applied))

    paths = {
        hashlib.sha256(data).hexdigest(): os.path.join(
            raw_dir, f"{hashlib.sha256(data).hexdigest()}.xlsx"
        )
        for data in (abandoned, applied)
    }
    for path in paths.values():
        assert os.path.isfile(path)
        stamp = time.time_ns() - 30 * 24 * 60 * 60 * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
    assert db.scalars(select(SysRawFile.batch_id)).all() == [int(preview_applied["preview_id"])]
    db.close()

    result = raw_archive_gc.reap_orphan_archives(
        execute=True, raw_dir=raw_dir, session_factory=SessionLocal, now_ns=time.time_ns(),
    )
    assert result["errors"] == 0
    assert result["deleted"] >= 1
    assert not os.path.exists(paths[hashlib.sha256(abandoned).hexdigest()])
    assert os.path.isfile(paths[hashlib.sha256(applied).hexdigest()])
    assert preview_abandoned["preview_id"] != preview_applied["preview_id"]


# ---------- (7) 受保护降级 ----------

def test_downgrade_refused_when_two_success_batches_share_file_hash(db):
    """同一原件分次勾选提交 → 两条 success maint_bulk 批次同 file_hash；旧唯一索引
    建不回来。审查 probe 里 downgrade 以 UniqueViolation 半途崩掉；现在明确拒绝。"""
    _project_with_contract(db)
    data = _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100), (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ])
    first = _preview(db, data)
    jan = _by_month(first)["2026-01-01"]
    _apply(db, first, [jan["row_key"]])
    second = _preview(db, data)
    assert second["summary"]["ready"] == 1
    _apply(db, second, _ready_keys(second))
    groups = db.execute(text(
        "SELECT file_type, count(*) FROM sys_import_batch WHERE status = 'success' "
        "GROUP BY file_type, file_hash HAVING count(*) > 1"
    )).all()
    assert groups == [("maint_bulk", 2)]

    db.close()
    engine.dispose()
    cfg = _cfg()
    with engine.connect() as connection:
        original = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    try:
        with pytest.raises(DBAPIError, match="downgrade refused"):
            alembic_command.downgrade(cfg, PREVIOUS)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == original
            assert connection.execute(
                text("SELECT count(*) FROM maintenance_collection_receipt")
            ).scalar_one() == 2
            assert "maint_bulk" in connection.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_batch_success_hash'"
            )).scalar_one()
    finally:
        alembic_command.upgrade(cfg, "head")
