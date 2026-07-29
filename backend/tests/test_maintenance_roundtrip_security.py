"""DEV-15 roundtrip security boundaries missed by the happy-path contract tests."""
from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select

from app import permissions
from app.api import maintenance as maintenance_api
from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.etl import pipeline
from app.etl.reader import ReaderError
from app.models.maintenance import (
    FMaintenanceOrder,
    FProjectExpense,
    MaintenanceContractWorkbookState,
    MaintenanceRoundtripOperation,
)
from app.models.system import SysAuditLog, SysImportBatch, SysUser
from app.services import maintenance_roundtrip
from tests.test_maintenance_roundtrip import (
    _edit_data_row,
    _export_to_path,
    _seed_contract,
)


def _expense_count(db, contract: str) -> int:
    return db.scalar(
        select(func.count(FProjectExpense.id)).where(
            FProjectExpense.linked_sales_order_no == contract
        )
    )


def test_signed_contract_scope_rejects_mixed_cross_contract_workbook_atomically(
    db,
    tmp_path,
):
    _seed_contract(db, suffix="SCOPE-P1", contract="XSDD-RT-SCOPE-P1")
    path = _export_to_path(
        db,
        tmp_path / "scope-p1.xlsx",
        contract="XSDD-RT-SCOPE-P1",
    )
    for row, contract in (
        (2, "XSDD-RT-SCOPE-P1"),
        (3, "XSDD-RT-OTHER"),
    ):
        _edit_data_row(
            path,
            "04_报销明细",
            {
                "操作": "CREATE",
                "合同号": contract,
                "报销日期": date(2026, 7, 20),
                "未税金额": Decimal("10.00"),
                "变更原因": "合同授权边界",
            },
            row=row,
        )

    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="合同范围",
    ) as caught:
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )

    assert caught.value.status_code == 409
    assert _expense_count(db, "XSDD-RT-SCOPE-P1") == 0
    assert _expense_count(db, "XSDD-RT-OTHER") == 0


def test_signed_date_scope_is_closed_and_out_of_range_row_makes_zero_writes(
    db,
    tmp_path,
):
    _seed_contract(db, suffix="DATE-P1", contract="XSDD-RT-DATE-P1")
    path = _export_to_path(
        db,
        tmp_path / "date-p1.xlsx",
        contract="XSDD-RT-DATE-P1",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
    )
    for row, expense_date in (
        (2, date(2026, 7, 1)),
        (3, date(2026, 7, 31)),
        (4, date(2026, 8, 1)),
    ):
        _edit_data_row(
            path,
            "04_报销明细",
            {
                "操作": "CREATE",
                "合同号": "XSDD-RT-DATE-P1",
                "报销日期": expense_date,
                "未税金额": Decimal("10.00"),
                "变更原因": "日期授权边界",
            },
            row=row,
        )

    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="日期范围",
    ):
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )

    assert _expense_count(db, "XSDD-RT-DATE-P1") == 0


def test_order_update_cannot_move_record_outside_signed_date_scope(db, tmp_path):
    order_id, _ = _seed_contract(
        db,
        suffix="ORDER-DATE-P1",
        contract="XSDD-RT-ORDER-DATE-P1",
    )
    path = _export_to_path(
        db,
        tmp_path / "order-date-p1.xlsx",
        contract="XSDD-RT-ORDER-DATE-P1",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
    )
    _edit_data_row(
        path,
        "02_维保订单",
        {
            "操作": "UPDATE",
            "制单日期": date(2026, 8, 1),
            "变更原因": "越界移动",
        },
    )

    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="日期范围",
    ):
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )

    db.expire_all()
    assert db.get(FMaintenanceOrder, order_id).order_date == date(2026, 7, 15)


def test_logical_create_replay_survives_openpyxl_resave_and_payload_conflicts_409(
    db,
    tmp_path,
):
    _seed_contract(db, suffix="LEDGER-CREATE", contract="XSDD-RT-LEDGER-CREATE")
    path = _export_to_path(
        db,
        tmp_path / "ledger-create.xlsx",
        contract="XSDD-RT-LEDGER-CREATE",
    )
    _edit_data_row(
        path,
        "04_报销明细",
        {
            "操作": "CREATE",
            "合同号": "XSDD-RT-LEDGER-CREATE",
            "报销日期": date(2026, 7, 20),
            "未税金额": Decimal("100.00"),
            "变更原因": "逻辑幂等",
        },
    )
    first = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )
    first_hash = first["file_hash"]

    # 模拟 Excel/openpyxl 重新保存，并改一个服务端不采信的展示单元格，确保文件 hash 改变。
    _edit_data_row(
        path,
        "04_报销明细",
        {"含税金额(系统计算)": Decimal("999.00")},
    )
    assert pipeline.sha256_file(str(path)) != first_hash
    replay = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )
    assert replay["no_op"] is True
    assert replay["logical_replay"] is True
    assert replay["replayed_rows"] == 1
    assert _expense_count(db, "XSDD-RT-LEDGER-CREATE") == 1
    assert db.scalar(select(func.count(MaintenanceRoundtripOperation.id))) == 1

    _edit_data_row(path, "04_报销明细", {"未税金额": Decimal("101.00")})
    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="相同行键.*不同内容",
    ) as caught:
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )
    assert caught.value.status_code == 409
    assert _expense_count(db, "XSDD-RT-LEDGER-CREATE") == 1


def test_logical_update_replay_skips_stale_entity_version(db, tmp_path):
    order_id, _ = _seed_contract(
        db,
        suffix="LEDGER-UPDATE",
        contract="XSDD-RT-LEDGER-UPDATE",
    )
    path = _export_to_path(
        db,
        tmp_path / "ledger-update.xlsx",
        contract="XSDD-RT-LEDGER-UPDATE",
    )
    _edit_data_row(
        path,
        "02_维保订单",
        {
            "操作": "UPDATE",
            "项目名称": "账本更新后名称",
            "变更原因": "逻辑幂等更新",
        },
    )
    maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )
    # 维保单号是只读展示字段，不参与 UPDATE payload；改动它只为确保 ZIP hash 改变。
    _edit_data_row(path, "02_维保订单", {"维保单号": "展示字段被重新保存"})
    replay = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )
    assert replay["logical_replay"] is True
    db.expire_all()
    assert db.get(FMaintenanceOrder, order_id).project_raw == "账本更新后名称"


def test_mixed_logical_replay_rejects_tampered_old_row_token_before_new_write(
    db,
    tmp_path,
):
    order_id, _ = _seed_contract(
        db,
        suffix="LEDGER-MIXED-TOKEN",
        contract="XSDD-RT-LEDGER-MIXED-TOKEN",
    )
    path = _export_to_path(
        db,
        tmp_path / "ledger-mixed-token.xlsx",
        contract="XSDD-RT-LEDGER-MIXED-TOKEN",
    )
    _edit_data_row(
        path,
        "02_维保订单",
        {
            "操作": "UPDATE",
            "项目名称": "已成功应用的旧操作",
            "变更原因": "首次应用",
        },
    )
    first = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )
    assert first["counts"]["update"] == 1

    # 同一本工作簿同时包含一个账本已记录的旧操作和一个待应用的新操作。
    # 旧操作 payload 未变，只有不参与 payload hash 的 HMAC 被篡改。
    _edit_data_row(
        path,
        "02_维保订单",
        {"__row_token": "0" * 64},
    )
    _edit_data_row(
        path,
        "04_报销明细",
        {
            "操作": "CREATE",
            "合同号": "XSDD-RT-LEDGER-MIXED-TOKEN",
            "报销日期": date(2026, 7, 20),
            "未税金额": Decimal("10.00"),
            "变更原因": "不得越过坏 token 写入",
        },
    )

    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="行令牌无效",
    ) as caught:
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )

    assert caught.value.status_code == 409
    db.expire_all()
    assert db.get(FMaintenanceOrder, order_id).project_raw == "已成功应用的旧操作"
    assert _expense_count(db, "XSDD-RT-LEDGER-MIXED-TOKEN") == 0
    assert db.scalar(select(func.count(MaintenanceRoundtripOperation.id))) == 1
    assert db.scalar(
        select(func.count(SysImportBatch.id)).where(
            SysImportBatch.file_type == maintenance_roundtrip.ROUNDTRIP_FILE_TYPE,
            SysImportBatch.status == "success",
        )
    ) == 1


def test_tampered_keep_row_token_rejects_before_snapshot_or_recompute(
    db,
    tmp_path,
    monkeypatch,
):
    order_id, _ = _seed_contract(
        db,
        suffix="KEEP-TOKEN",
        contract="XSDD-RT-KEEP-TOKEN",
    )
    path = _export_to_path(
        db,
        tmp_path / "keep-token.xlsx",
        contract="XSDD-RT-KEEP-TOKEN",
    )
    _edit_data_row(
        path,
        "02_维保订单",
        {"__row_token": "f" * 64},
    )
    recomputed = False

    def fail_if_recomputed(_db):
        nonlocal recomputed
        recomputed = True
        raise AssertionError("tampered KEEP row reached recompute")

    monkeypatch.setattr(
        maintenance_roundtrip,
        "_recompute_in_transaction",
        fail_if_recomputed,
    )
    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="行令牌无效",
    ) as caught:
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )

    assert caught.value.status_code == 409
    assert recomputed is False
    assert db.get(MaintenanceContractWorkbookState, "XSDD-RT-KEEP-TOKEN") is None
    assert db.scalar(
        select(func.count(SysImportBatch.id)).where(
            SysImportBatch.file_type == maintenance_roundtrip.ROUNDTRIP_FILE_TYPE,
        )
    ) == 0
    assert db.scalar(select(func.count(SysAuditLog.id))) == 0
    db.expire_all()
    assert db.get(FMaintenanceOrder, order_id).project_raw == "回填项目-KEEP-TOKEN"


def test_contract_revisions_use_bounded_hidden_table_instead_of_one_json_cell(
    db,
    tmp_path,
):
    _seed_contract(db, suffix="REVISIONS", contract="XSDD-RT-REVISIONS")
    path = _export_to_path(
        db,
        tmp_path / "revisions.xlsx",
        contract="XSDD-RT-REVISIONS",
    )
    workbook = load_workbook(path, data_only=False)
    try:
        assert workbook["99_合同版本"].sheet_state == "veryHidden"
        assert set(workbook["99_合同版本"].tables) == {
            "tbl_contract_revisions_v1"
        }
        metadata = {
            workbook["99_元数据"].cell(row=row, column=1).value:
            workbook["99_元数据"].cell(row=row, column=2).value
            for row in range(2, workbook["99_元数据"].max_row + 1)
        }
        assert "contract_revisions" not in metadata
        assert metadata["contract_revision_count"] == "1"
        assert len(metadata["contract_revisions_sha256"]) == 64
    finally:
        workbook.close()


def test_generic_import_rejects_roundtrip_protocol_before_hash_or_business_write(
    db,
    tmp_path,
):
    _seed_contract(db, suffix="WRONG-ENDPOINT", contract="XSDD-RT-WRONG-ENDPOINT")
    path = _export_to_path(
        db,
        tmp_path / "wrong-endpoint.xlsx",
        contract="XSDD-RT-WRONG-ENDPOINT",
    )
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    generic = SysImportBatch(
        filename="legacy-wrong-success.xlsx",
        file_type="maintenance",
        file_hash=file_hash,
        status="success",
    )
    db.add(generic)
    db.commit()

    with pytest.raises(ReaderError, match="维保项目回填入口") as caught:
        pipeline.run_import(
            db,
            str(path),
            path.name,
            uploaded_by="tester",
        )
    assert caught.value.code == "roundtrip_wrong_endpoint"
    db.rollback()

    result = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )
    assert result["no_op"] is False
    assert result["batch_id"] != generic.id


def test_exact_hash_replay_short_circuits_before_openpyxl(db, tmp_path, monkeypatch):
    _seed_contract(db, suffix="HASH-PREFLIGHT", contract="XSDD-RT-HASH-PREFLIGHT")
    path = _export_to_path(
        db,
        tmp_path / "hash-preflight.xlsx",
        contract="XSDD-RT-HASH-PREFLIGHT",
    )
    first = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )

    def fail_if_openpyxl_is_reached(_path):
        raise AssertionError("exact hash replay reached openpyxl")

    monkeypatch.setattr(
        maintenance_roundtrip,
        "_load_and_parse",
        fail_if_openpyxl_is_reached,
    )
    replay = maintenance_roundtrip.import_roundtrip_workbook(
        db,
        str(path),
        filename=path.name,
        operated_by="tester",
    )

    assert replay["no_op"] is True
    assert replay["logical_replay"] is False
    assert replay["batch_id"] == first["batch_id"]


def test_unknown_invalid_file_rolls_back_preflight_transaction(db, tmp_path):
    path = tmp_path / "invalid-roundtrip.xlsx"
    path.write_bytes(b"not-an-xlsx")

    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="有效的 .xlsx|无法打开维保回填|文件无法按 .xlsx 解析",
    ):
        maintenance_roundtrip.import_roundtrip_workbook(
            db,
            str(path),
            filename=path.name,
            operated_by="tester",
        )

    assert db.in_transaction() is False


@pytest.mark.parametrize(
    ("counts", "expected_sheet"),
    [
        ([maintenance_roundtrip.MAX_ROWS_PER_TABLE + 1], "02_维保订单"),
        ([0, maintenance_roundtrip.MAX_ROWS_PER_TABLE + 1], "03_订单明细"),
        (
            [
                0,
                0,
                maintenance_roundtrip.MAX_ROWS_PER_TABLE
                - maintenance_roundtrip.BLANK_CREATE_ROWS
                + 1,
            ],
            "04_报销明细",
        ),
    ],
)
def test_export_row_caps_fail_before_full_orm_materialization(
    counts,
    expected_sheet,
):
    class CountOnlySession:
        def __init__(self):
            self._counts = iter(counts)
            self.execute_called = False

        def scalar(self, _statement):
            return next(self._counts)

        def scalars(self, _statement):
            # DISTINCT contract discovery is a bounded preflight query, not ORM rows.
            return SimpleNamespace(all=lambda: [])

        def execute(self, _statement):
            self.execute_called = True
            raise AssertionError("full ORM materialization happened before cap rejection")

    session = CountOnlySession()
    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match=expected_sheet,
    ) as caught:
        maintenance_roundtrip._selected_data(
            session,
            contract=None,
            date_from=None,
            date_to=None,
            blank=False,
        )

    assert caught.value.status_code == 413
    assert session.execute_called is False


def test_roundtrip_uncompressed_package_cap_is_64_mib(tmp_path, monkeypatch):
    path = tmp_path / "oversized-expanded.xlsx"
    path.write_bytes(b"zip-placeholder")

    class FakeArchive:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def infolist(self):
            return [
                SimpleNamespace(
                    file_size=maintenance_roundtrip.MAX_ROUNDTRIP_UNCOMPRESSED_BYTES
                    + 1
                )
            ]

        def namelist(self):
            return []

    monkeypatch.setattr(
        maintenance_roundtrip.reader,
        "_check_xlsx_archive_safety",
        lambda _path: None,
    )
    monkeypatch.setattr(
        maintenance_roundtrip.reader,
        "_check_workbook_size",
        lambda _path: None,
    )
    monkeypatch.setattr(maintenance_roundtrip.zipfile, "ZipFile", FakeArchive)

    with pytest.raises(
        maintenance_roundtrip.RoundtripWorkbookError,
        match="解压后超过 64 MiB",
    ) as caught:
        maintenance_roundtrip._assert_safe_workbook_package(str(path))

    assert caught.value.status_code == 413


def test_template_build_concurrency_is_one(db):
    first = maintenance_roundtrip.build_roundtrip_template(
        db,
        exported_by="tester",
        blank=True,
    )
    concurrent = SessionLocal()
    try:
        with pytest.raises(
            maintenance_roundtrip.RoundtripWorkbookError,
            match="正在生成",
        ) as caught:
            maintenance_roundtrip.build_roundtrip_template(
                concurrent,
                exported_by="tester",
                blank=True,
            )
        assert caught.value.status_code == 429
    finally:
        concurrent.rollback()
        concurrent.close()
        first.close()
        db.rollback()


def test_roundtrip_apply_action_defaults_fail_closed_for_non_operators():
    assert permissions.template_for("admin")[
        "action_maintenance_roundtrip_apply"
    ] is True
    assert permissions.template_for("boss")[
        "action_maintenance_roundtrip_apply"
    ] is True
    assert permissions.template_for("sales")[
        "action_maintenance_roundtrip_apply"
    ] is False
    assert permissions.template_for("purchaser")[
        "action_maintenance_roundtrip_apply"
    ] is False
    assert permissions.template_for("readonly")[
        "action_maintenance_roundtrip_apply"
    ] is False


def test_roundtrip_apply_action_403_happens_before_application_reads_upload(
    db,
    monkeypatch,
):
    user = SysUser(
        username="roundtrip-no-action",
        role="boss",
        display_name="无回填动作权限",
        password_hash=hash_password("roundtrip-password"),
        permissions={"action_maintenance_roundtrip_apply": False},
    )
    db.add(user)
    db.commit()
    called = False

    def fail_if_upload_is_read(_file):
        nonlocal called
        called = True
        raise AssertionError("upload body reached application save path")

    monkeypatch.setattr(
        maintenance_api,
        "_save_roundtrip_upload",
        fail_if_upload_is_read,
    )
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={
            "username": "roundtrip-no-action",
            "password": "roundtrip-password",
        },
    )
    assert login.status_code == 200
    response = client.post(
        "/api/maintenance/roundtrip-import",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
        files={
            "file": (
                "maintenance_roundtrip.xlsx",
                b"must-not-reach-application-reader",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "无此操作权限"
    assert called is False
