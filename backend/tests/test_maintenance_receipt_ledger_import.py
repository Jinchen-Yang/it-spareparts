"""收款单（SKD）批量导入升级（D-16，issue #288）：台账、幂等、覆盖、归档、迁移。

覆盖：
- 迁移单 head、台账表/快照来源约束/批次索引谓词，downgrade→upgrade 无损；
- 预览即归档原件（磁盘文件 + 批次 file_hash = 原件 sha256；sys_raw_file 行到应用成功才落）；
- 应用写出台账行（带批次），快照 source=bulk_import 且指向批次；
- 同文件再导：全部已入账，无可提交行；
- 增量文件：只新建新月份；既有月份变化 = update 行，未勾选不写，勾选后走
  update_collection（版本 +1、事实审计、来源改指新批次）；
- 台账冲突预览即阻断；单调性倒退预览即阻断而不是应用时 422；
- 通用导入的同 hash 去重不被批量网关批次污染。
"""
from __future__ import annotations

import hashlib
import os
from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from openpyxl import Workbook
from sqlalchemy import inspect, select, text

from app.config import get_settings
from app.db import engine
from app.etl import pipeline
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceCollectionReceipt,
    MaintenanceCollectionSnapshot,
    MaintenanceProjectOperationAudit,
)
from app.models.system import SysImportBatch, SysRawFile
from app.services import maintenance_bulk_import as bulk

REVISION = "b7d3f9a1c5e2"
PREVIOUS = "a8e4f1c7d3b9"
ORDER_NO = "XSDD-20240101-0001"


def _cfg() -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(__file__), "..", "alembic"),
    )
    return cfg


def _project_with_contract(db, *, contract_no: str = ORDER_NO):
    project = MaintenanceProject(
        project_id=str(uuid4()),
        project_code=f"SKD-{uuid4().hex[:8]}",
        display_name="收款台账测试项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    contract = MaintenanceProjectContract(
        project_contract_id=str(uuid4()),
        project_id=project.project_id,
        contract_id="C-1",
        contract_no=contract_no,
        amount_inc_tax=Decimal("100000.00"),
        included_in_total=True,
        status_mapping_state="mapped",
        status_mapping_version="v1",
        effective_from=date(2026, 1, 1),
        source="ledger",
        version=1,
    )
    db.add(contract)
    db.commit()
    return project, contract


def _receipt_xlsx(rows: list[tuple]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "收款明细.销售订单(必填)", "收款单号(必填)", "收款日期(必填)",
        "收款明细.实收金额", "数据状态", "收款明细.备注",
    ])
    for row in rows:
        order_no, receipt_no, receipt_date, amount = row[:4]
        remark = row[4] if len(row) > 4 else ""
        sheet.append([order_no, receipt_no, receipt_date, amount, "已生效", remark])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _preview(db, data: bytes, *, name: str = "receipts.xlsx") -> dict:
    return bulk.preview_transfer(db, [(name, data)], operated_by="skd-operator")


def _apply(db, preview: dict, row_keys: list[str], *, real_operator: bool = True) -> dict:
    # 2026-09-06 复核：覆盖行（update_collection_snapshot）必须实名账号；服务层
    # 由 HTTP 入口传入实名判定，这里模拟实名（HTTP 门禁见 hardening 测试）。
    return bulk.apply_transfer(
        db,
        preview_token=preview["preview_token"],
        payload_hash=preview["payload_hash"],
        data_version=preview["data_version"],
        row_keys=row_keys,
        operated_by="skd-operator",
        real_operator=real_operator,
    )


def _rows(preview: dict, **filters) -> list[dict]:
    return [
        row for row in preview["rows"]
        if all(row.get(key) == value for key, value in filters.items())
    ]


def _snapshots(db, contract) -> dict[date, MaintenanceCollectionSnapshot]:
    db.expire_all()
    return {
        row.report_month: row
        for row in db.scalars(
            select(MaintenanceCollectionSnapshot).where(
                MaintenanceCollectionSnapshot.project_contract_id
                == contract.project_contract_id
            )
        )
    }


# ---------- 迁移 ----------

def test_migration_is_single_head_and_roundtrips(db):
    script = ScriptDirectory.from_config(_cfg())
    revisions = {rev.revision: rev for rev in script.walk_revisions()}
    assert revisions[REVISION].down_revision == PREVIOUS
    assert len(set(script.get_heads())) == 1

    inspector = inspect(db.get_bind())
    assert "maintenance_collection_receipt" in inspector.get_table_names()
    # 2026-09-06 复核：唯一键改为只约束生效行的偏唯一索引——裁决把旧行留档
    # （is_active=false）并另插同 (销售订单, 收款单号) 的更正行。
    assert inspector.get_unique_constraints("maintenance_collection_receipt") == []
    columns = {
        item["name"]: item for item in inspector.get_columns("maintenance_collection_receipt")
    }
    assert columns["source_sha256"]["nullable"] is True
    assert {"superseded_by", "ruling_id", "ruling_reason", "ruled_by", "ruled_at"} <= set(columns)
    with engine.connect() as connection:
        active_index = connection.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ux_maintenance_collection_receipt_active'"
        )).scalar_one()
        assert "UNIQUE" in active_index and "WHERE is_active" in active_index
        selection_index = connection.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_batch_success_selection_hash'"
        )).scalar_one()
        assert "report_json ->> 'selection_hash'" in selection_index
        assert "WHERE" in selection_index and "'success'" in selection_index
        source_check = connection.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_maintenance_collection_source'"
        )).scalar_one()
        assert "bulk_import" in source_check
        batch_check = connection.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_maintenance_collection_import_batch'"
        )).scalar_one()
        assert "bulk_import" in batch_check
        index_def = connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_batch_success_hash'"
        )).scalar_one()
        assert "maint_bulk" in index_def

    db.close()
    engine.dispose()
    cfg = _cfg()
    alembic_command.downgrade(cfg, PREVIOUS)
    try:
        with engine.connect() as connection:
            assert "maintenance_collection_receipt" not in inspect(
                connection
            ).get_table_names()
            legacy_check = connection.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_maintenance_collection_source'"
            )).scalar_one()
            assert "bulk_import" not in legacy_check
            legacy_index = connection.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_batch_success_hash'"
            )).scalar_one()
            assert "maint_bulk" not in legacy_index
    finally:
        alembic_command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == script.get_current_head()


# ---------- 归档 ----------

def test_preview_archives_original_and_keeps_file_sha256_on_batch(db):
    """预览即归档原件；sys_raw_file 行到应用成功才落（2026-09-06 复核：只预览不
    应用的批次不该把原件钉死在磁盘上，过了宽限期由 GC 当孤儿回收）。"""
    _project_with_contract(db)
    data = _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)])
    sha256 = hashlib.sha256(data).hexdigest()

    preview = _preview(db, data, name="收款单.xlsx")

    batch = db.get(SysImportBatch, int(preview["preview_id"]))
    assert batch.file_type == "maint_bulk"
    assert batch.file_hash == sha256
    assert batch.report_json["payload_hash"] == preview["payload_hash"]
    (archive,) = batch.report_json["archives"]
    assert archive["file_hash"] == sha256 and archive["filename"] == "收款单.xlsx"
    assert "archives" not in batch.report_json["public"]
    assert os.path.dirname(os.path.abspath(archive["storage_path"])) == os.path.abspath(
        get_settings().raw_file_dir
    )
    with open(archive["storage_path"], "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == sha256
    assert db.scalars(select(SysRawFile).where(SysRawFile.batch_id == batch.id)).all() == []

    _apply(db, preview, [row["row_key"] for row in _rows(preview, row_status="ready")])

    raw = db.scalars(select(SysRawFile).where(SysRawFile.batch_id == batch.id)).one()
    assert raw.file_hash == sha256
    assert raw.filename == "收款单.xlsx"
    assert raw.storage_path == archive["storage_path"]


def test_general_import_duplicate_lookup_ignores_bulk_gateway_batches(db):
    file_hash = "c" * 64
    db.add(SysImportBatch(
        filename="sales.xlsx", file_type="maint_bulk", file_hash=file_hash,
        status="success",
    ))
    db.commit()

    assert pipeline.successful_batch_ids_by_hash(db, {file_hash}) == {}
    assert file_hash in pipeline.successful_batch_ids_by_hash(
        db, {file_hash}, file_type="maint_bulk"
    )


# ---------- 应用：台账 + 快照来源 ----------

def test_apply_writes_ledger_rows_and_snapshots_reference_batch(db):
    project, contract = _project_with_contract(db)
    data = _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100, "首期"),
        (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ])
    preview = _preview(db, data)
    ready = _rows(preview, row_status="ready")
    assert [row["action"] for row in ready] == [
        "upsert_collection_snapshot", "upsert_collection_snapshot",
    ]

    result = _apply(db, preview, [row["row_key"] for row in ready])

    assert result["applied"] == 2
    batch_id = int(result["batch_id"])
    batch = db.get(SysImportBatch, batch_id)
    assert batch.status == "success"
    assert batch.file_hash == hashlib.sha256(data).hexdigest()
    assert batch.report_json["selection_hash"]
    ledger = db.scalars(
        select(MaintenanceCollectionReceipt).order_by(MaintenanceCollectionReceipt.receipt_no)
    ).all()
    assert [(row.receipt_no, row.actual_amount, row.remark) for row in ledger] == [
        ("SK-1", Decimal("100.00"), "首期"),
        ("SK-2", Decimal("50.00"), None),
    ]
    assert {row.import_batch_id for row in ledger} == {batch_id}
    assert {row.source_sha256 for row in ledger} == {batch.file_hash}
    assert {row.contract_no for row in ledger} == {"20240101-0001"}
    assert {row.project_contract_id for row in ledger} == {contract.project_contract_id}
    snapshots = _snapshots(db, contract)
    assert snapshots[date(2026, 1, 1)].cumulative_amount == Decimal("100.00")
    assert snapshots[date(2026, 2, 1)].cumulative_amount == Decimal("150.00")
    for snapshot in snapshots.values():
        assert snapshot.source == "bulk_import"
        assert snapshot.import_batch_id == str(batch_id)
        assert snapshot.status == "confirmed"


def test_reimporting_the_same_file_finds_every_receipt_known(db):
    _project_with_contract(db)
    data = _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100),
        (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ])
    first = _preview(db, data)
    _apply(db, first, [row["row_key"] for row in _rows(first, row_status="ready")])

    second = _preview(db, data)

    assert second["summary"]["ready"] == 0
    assert second["summary"]["known"] == 2
    assert second["can_apply"] is False
    known = _rows(second, action="skip")
    assert len(known) == 2
    assert all(
        any(issue["code"] == "receipt_known" for issue in row["warnings"])
        for row in known
    )
    assert db.scalar(select(text("count(*)")).select_from(MaintenanceCollectionReceipt)) == 2


def test_incremental_file_creates_new_month_and_updates_changed_month_only_when_ticked(db):
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100),
        (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ]))
    _apply(db, first, [row["row_key"] for row in _rows(first, row_status="ready")])
    before = _snapshots(db, contract)
    jan_version = before[date(2026, 1, 1)].version
    feb_version = before[date(2026, 2, 1)].version

    # 增量导出：一笔迟到的 1 月收款 + 3 月新收款；2 月不在文件里
    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1B", date(2026, 1, 25), 30),
        (ORDER_NO, "SK-3", date(2026, 3, 2), 20),
    ]))
    by_month = {
        row["canonical"]["report_month"]: row
        for row in preview["rows"] if row.get("canonical", {}).get("report_month")
    }
    assert by_month["2026-01-01"]["action"] == "update_collection_snapshot"
    assert by_month["2026-01-01"]["requires_confirmation"] is True
    assert by_month["2026-01-01"]["before"]["cumulative_amount"] == "100.00"
    assert by_month["2026-01-01"]["after"]["cumulative_amount"] == "130.00"
    assert by_month["2026-02-01"]["action"] == "update_collection_snapshot"
    assert by_month["2026-02-01"]["after"]["cumulative_amount"] == "180.00"
    assert by_month["2026-03-01"]["action"] == "upsert_collection_snapshot"
    assert by_month["2026-03-01"]["after"]["cumulative_amount"] == "200.00"
    assert preview["summary"]["updates"] == 2

    # 3 月的 200 包含 1 月迟到的 30：只勾 3 月（update 默认不勾）应用即拒，
    # 否则快照会算进台账没有的收款。
    assert any(
        w["code"] == "requires_earlier_months" for w in by_month["2026-03-01"]["warnings"]
    )
    try:
        _apply(db, preview, [by_month["2026-03-01"]["row_key"]])
    except bulk.BulkImportInvalid as exc:
        db.rollback()
        assert "必须同时勾选" in str(exc) and "2026-01" in str(exc)
    else:
        raise AssertionError("未勾选构成累计的更早月份时必须拒绝")
    assert date(2026, 3, 1) not in _snapshots(db, contract)

    # 只勾 1 月 update：走 update_collection —— 版本 +1、事实审计、来源改指新批次；
    # 未勾选的 2 月原样不动，3 月不新建。
    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1B", date(2026, 1, 25), 30),
        (ORDER_NO, "SK-3", date(2026, 3, 2), 20),
    ]))
    jan_row = next(
        row for row in preview["rows"]
        if row.get("canonical", {}).get("report_month") == "2026-01-01"
    )
    result = _apply(db, preview, [jan_row["row_key"]])
    assert result["applied"] == 1
    batch_id = int(result["batch_id"])
    after = _snapshots(db, contract)
    assert after[date(2026, 1, 1)].cumulative_amount == Decimal("130.00")
    assert after[date(2026, 1, 1)].version == jan_version + 1
    assert after[date(2026, 1, 1)].source == "bulk_import"
    assert after[date(2026, 1, 1)].import_batch_id == str(batch_id)
    assert after[date(2026, 2, 1)].cumulative_amount == Decimal("150.00")
    assert after[date(2026, 2, 1)].version == feb_version
    assert date(2026, 3, 1) not in after
    audit = db.scalars(
        select(MaintenanceProjectOperationAudit).where(
            MaintenanceProjectOperationAudit.entity_type == "collection",
            MaintenanceProjectOperationAudit.action == "update",
        )
    ).one()
    assert audit.entity_id == after[date(2026, 1, 1)].collection_id
    assert audit.before_json["cumulative_amount"] == "100.00"
    assert audit.after_json["cumulative_amount"] == "130.00"
    assert audit.after_json["import_batch_id"] == str(batch_id)
    assert f"batch={batch_id}" in audit.reason
    assert "100.00→130.00" in audit.reason
    assert "skd-operator" in audit.reason
    assert audit.operated_by == "skd-operator"
    (receipt_row,) = result["rows"]
    assert receipt_row["report_month"] == "2026-01-01"
    assert receipt_row["before_amount"] == "100.00"
    assert receipt_row["after_amount"] == "130.00"
    assert receipt_row["previous_source"] == "bulk_import"
    assert receipt_row["before_version"] == jan_version
    assert receipt_row["after_version"] == jan_version + 1
    assert "原值 100.00 → 新值 130.00" in receipt_row["message"]
    ledger = db.scalars(select(MaintenanceCollectionReceipt)).all()
    assert {row.receipt_no for row in ledger} == {"SK-1", "SK-2", "SK-1B"}
    assert next(row for row in ledger if row.receipt_no == "SK-1B").import_batch_id == batch_id

    # 再预览同一文件：SK-1B 已入账、1 月 noop；2 月漂移为 update（150→180）；3 月 create
    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1B", date(2026, 1, 25), 30),
        (ORDER_NO, "SK-3", date(2026, 3, 2), 20),
    ]))
    by_month = {
        row["canonical"]["report_month"]: row
        for row in preview["rows"] if row.get("canonical", {}).get("report_month")
    }
    assert by_month["2026-01-01"]["row_status"] == "unchanged"
    assert by_month["2026-02-01"]["action"] == "update_collection_snapshot"
    assert any(w["code"] == "ledger_drift" for w in by_month["2026-02-01"]["warnings"])
    assert by_month["2026-03-01"]["action"] == "upsert_collection_snapshot"
    result = _apply(db, preview, [row["row_key"] for row in _rows(preview, row_status="ready")])
    assert result["applied"] == 2
    after = _snapshots(db, contract)
    assert after[date(2026, 2, 1)].cumulative_amount == Decimal("180.00")
    assert after[date(2026, 2, 1)].version == feb_version + 1
    assert after[date(2026, 3, 1)].cumulative_amount == Decimal("200.00")
    assert after[date(2026, 3, 1)].source == "bulk_import"
    assert after[date(2026, 3, 1)].import_batch_id == result["batch_id"]
    assert {row.receipt_no for row in db.scalars(select(MaintenanceCollectionReceipt))} == {
        "SK-1", "SK-2", "SK-1B", "SK-3",
    }


def test_conflicting_receipt_is_blocked_for_manual_ruling(db):
    _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, [row["row_key"] for row in _rows(first, row_status="ready")])

    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 120),
        (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ]))

    assert preview["summary"]["ready"] == 0
    assert preview["summary"]["receipt_conflicts"] == 1
    # 2026-09-07 再复核：fail-closed 的月度行也是 invalid（不再是 ambiguous）。
    invalid = _rows(preview, match_state="invalid")
    (conflict,) = [row for row in invalid if row["canonical"].get("receipt_key")]
    assert conflict["source_row"] == 2 and conflict["row_status"] == "blocked"
    assert any(issue["code"] == "receipt_conflict" for issue in conflict["errors"])
    snapshot_rows = [row for row in invalid if row["canonical"].get("report_month")]
    assert snapshot_rows and all(row["action"] == "block" for row in snapshot_rows)
    assert all(row["after"]["cumulative_amount"] is None for row in snapshot_rows)
    assert not _rows(preview, match_state="ambiguous")
    db.expire_all()
    assert db.scalar(select(MaintenanceCollectionSnapshot.cumulative_amount)) == Decimal("100.00")


def test_monotonic_regression_is_blocked_in_preview_instead_of_apply_422(db):
    """旧 05 表无守卫留下的不单调历史（11 月 500 > 12 月 300）：台账为空时
    基线取 12 月 300，2026-01 推导 400 仍低于 11 月 → 预览即 blocked。"""
    project, contract = _project_with_contract(db)
    for month, amount in ((date(2025, 11, 1), "500.00"), (date(2025, 12, 1), "300.00")):
        db.add(MaintenanceCollectionSnapshot(
            collection_id=str(uuid4()), project_id=project.project_id,
            project_contract_id=contract.project_contract_id,
            report_month=month, cumulative_amount=Decimal(amount),
            status="confirmed", source="legacy", version=1,
        ))
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))

    assert preview["summary"]["ready"] == 0
    assert preview["can_apply"] is False
    jan = next(row for row in preview["rows"] if row["canonical"].get("report_month") == "2026-01-01")
    assert jan["row_status"] == "blocked"
    assert jan["after"]["cumulative_amount"] == "400.00"
    assert any(
        issue["code"] == "collection_not_monotonic"
        and "不得低于更早月份" in issue["message"]
        for issue in jan["errors"]
    )
    assert _snapshots(db, contract).keys() == {date(2025, 11, 1), date(2025, 12, 1)}
