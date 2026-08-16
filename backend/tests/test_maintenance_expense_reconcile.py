"""报销对账只读服务/API 测试（C4，round-4 修复版）。

反例（Codex round-4 Blocker 3/10 最小集）：
- 同单号多明细聚合后再比较（400+600 vs 1000 → mismatch）；
- 三源全集并集输出（formal_only / BXD+formal 无台账）；
- pending/failed 批次与未生效正式行不参与；
- HTTP 权限矩阵：anonymous/admin/boss/普通角色/data_profit=false。
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app import permissions as _perms
from app.api import maintenance_expense_reconcile
from app.auth import hash_password
from app.models.maintenance import FProjectExpense
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
)
from app.models.maintenance_ledger import (
    MaintenanceLedgerExpenseRow,
    MaintenanceLedgerImportBatch,
)
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_expense_reconcile as reconcile

APPLIED_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _ledger_batch(db, *, batch_id: str, status: str = "applied") -> str:
    db.add(
        MaintenanceLedgerImportBatch(
            batch_id=batch_id,
            file_hash=f"h-{batch_id}",
            filename="台账.xlsx",
            idempotency_key=f"key-{batch_id}",
            source_kind="project_manager_xls_v1",
            uploaded_by="合成管理员",
            status=status,
            applied_by="合成管理员" if status == "applied" else None,
            applied_at=APPLIED_AT if status == "applied" else None,
        )
    )
    db.flush()
    return batch_id


def _bxd_batch(db, *, batch_id: str, status: str = "applied") -> str:
    db.add(
        MaintenanceDocImportBatch(
            batch_id=batch_id,
            doc_type="bxd_expense",
            file_hash=f"h-{batch_id}",
            filename="报销.xlsx",
            idempotency_key=f"key-{batch_id}",
            uploaded_by="合成管理员",
            status=status,
            applied_by="合成管理员" if status == "applied" else None,
            applied_at=APPLIED_AT if status == "applied" else None,
        )
    )
    db.flush()
    return batch_id


def _ledger_row(db, *, row_id: str, batch_id: str, bxd_no: str, amount: str) -> None:
    db.add(
        MaintenanceLedgerExpenseRow(
            row_id=row_id,
            batch_id=batch_id,
            row_no=2,
            bxd_no_raw=bxd_no,
            bxd_no=bxd_no,
            project_name_raw="某项目",
            amount=Decimal(amount),
            issues=[],
        )
    )
    db.flush()


def _bxd_head_and_lines(
    db, *, batch_id: str, head_id: str, head_no: str, amounts: list[str]
) -> None:
    db.add(
        MaintenanceDocHeadRow(
            row_id=head_id,
            batch_id=batch_id,
            row_no=3,
            raw_json={"费用单号": head_no},
            head_no=head_no,
            issues=[],
        )
    )
    db.flush()
    for index, amount in enumerate(amounts, 1):
        db.add(
            MaintenanceDocLineRow(
                row_id=f"{head_id}-line-{index}",
                batch_id=batch_id,
                head_row_id=head_id,
                row_no=3 + index,
                raw_json={"报销明细.报销金额": amount},
                line_key=f"LID-{head_no}-{index}",
                amount=Decimal(amount),
                issues=[],
            )
        )
    db.flush()


def _formal_row(
    db,
    *,
    raw_line_id: str,
    bxd_no: str,
    amount_inc_tax: str,
    data_status: str = "已结束",
) -> None:
    import_batch = db.execute(
        select(SysImportBatch).where(SysImportBatch.file_type == "expense")
    ).scalars().first()
    if import_batch is None:
        import_batch = SysImportBatch(
            filename="expense.xlsx",
            file_type="expense",
            file_hash="h-expense",
            status="success",
        )
        db.add(import_batch)
        db.flush()
    db.add(
        FProjectExpense(
            raw_line_id=raw_line_id,
            bxd_no=bxd_no,
            line_no=1,
            data_status=data_status,
            fee_category="差旅费",
            amount=Decimal(amount_inc_tax),
            amount_ex_tax=Decimal(amount_inc_tax) / Decimal("1.13"),
            amount_inc_tax=Decimal(amount_inc_tax),
            tax_basis="inc",
            import_batch_id=import_batch.id,
        )
    )
    db.flush()


def test_reconcile_aggregates_lines_before_compare(db):
    """BXD 明细 400+600 聚合后 vs 台账/正式 1000 → 证据对齐（旧实现取首行 400 会错报）。"""
    _ledger_batch(db, batch_id="rc-ledger-1")
    _bxd_batch(db, batch_id="rc-bxd-1")
    _ledger_row(db, row_id="rc-ler-1", batch_id="rc-ledger-1",
                bxd_no="BXD-20260425-0001", amount="1000.00")
    _bxd_head_and_lines(db, batch_id="rc-bxd-1", head_id="rc-head-1",
                        head_no="BXD-20260425-0001", amounts=["400.00", "600.00"])
    _formal_row(db, raw_line_id="rc-fpe-1", bxd_no="BXD-20260425-0001",
                amount_inc_tax="1000.00")
    db.commit()
    rows = reconcile.expense_reconcile_rows(db)
    row = {r["bxd_no"]: r for r in rows}["BXD-20260425-0001"]
    assert row["status"] == "matched"  # 台账含税 == 正式含税（业务确认口径）
    assert row["conclusion_basis"]
    assert row["bxd_aligned"] is True
    assert row["bxd_amount"] == 1000.0  # 400+600 聚合
    assert row["bxd_line_count"] == 2
    assert row["ledger_amount"] == 1000.0
    assert row["formal_amount"] == 1000.0


def test_reconcile_detects_over_summed_bxd(db):
    """BXD 明细 1000+500 vs 台账/正式 1000 → mismatch（round-4 反例二）。"""
    _ledger_batch(db, batch_id="rc-ledger-2")
    _bxd_batch(db, batch_id="rc-bxd-2")
    _ledger_row(db, row_id="rc-ler-2", batch_id="rc-ledger-2",
                bxd_no="BXD-20260425-0002", amount="1000.00")
    _bxd_head_and_lines(db, batch_id="rc-bxd-2", head_id="rc-head-2",
                        head_no="BXD-20260425-0002", amounts=["1000.00", "500.00"])
    _formal_row(db, raw_line_id="rc-fpe-2", bxd_no="BXD-20260425-0002",
                amount_inc_tax="1000.00")
    db.commit()
    rows = reconcile.expense_reconcile_rows(db)
    row = {r["bxd_no"]: r for r in rows}["BXD-20260425-0002"]
    assert row["status"] == "matched"  # 台账↔正式一致
    assert row["bxd_aligned"] is False  # BXD 证据 1500 ≠ 正式 1000
    assert row["bxd_amount"] == 1500.0


def test_reconcile_three_way_matched(db):
    _ledger_batch(db, batch_id="rc-ledger-3")
    _bxd_batch(db, batch_id="rc-bxd-3")
    _ledger_row(db, row_id="rc-ler-3", batch_id="rc-ledger-3",
                bxd_no="BXD-20260425-0003", amount="1068.50")
    _bxd_head_and_lines(db, batch_id="rc-bxd-3", head_id="rc-head-3",
                        head_no="BXD-20260425-0003", amounts=["1068.50"])
    _formal_row(db, raw_line_id="rc-fpe-3", bxd_no="BXD-20260425-0003",
                amount_inc_tax="1068.50")
    db.commit()
    rows = reconcile.expense_reconcile_rows(db)
    row = {r["bxd_no"]: r for r in rows}["BXD-20260425-0003"]
    assert row["status"] == "matched"
    assert row["bxd_aligned"] is True


def test_reconcile_union_includes_formal_only_and_bxd_formal(db):
    """三源并集：formal-only 输出；BXD+正式无台账 → partial_match/mismatch。"""
    _formal_row(db, raw_line_id="rc-fpe-only", bxd_no="BXD-20260425-0004",
                amount_inc_tax="800.00")
    _bxd_batch(db, batch_id="rc-bxd-4")
    _bxd_head_and_lines(db, batch_id="rc-bxd-4", head_id="rc-head-4",
                        head_no="BXD-20260425-0005", amounts=["800.00"])
    _formal_row(db, raw_line_id="rc-fpe-5", bxd_no="BXD-20260425-0005",
                amount_inc_tax="800.00")
    db.commit()
    rows = {r["bxd_no"]: r for r in reconcile.expense_reconcile_rows(db)}
    assert rows["BXD-20260425-0004"]["status"] == "formal_only"
    # 无台账：BXD 与正式只作证据对齐，不单独出结论
    assert rows["BXD-20260425-0005"]["status"] == "formal_only"
    assert rows["BXD-20260425-0005"]["bxd_aligned"] is True
    assert rows["BXD-20260425-0005"]["ledger_amount"] is None


def test_reconcile_excludes_pending_failed_and_inactive(db):
    """pending/failed 批次、未生效正式行不参与对账。"""
    _ledger_batch(db, batch_id="rc-ledger-pending", status="pending")
    _ledger_row(db, row_id="rc-ler-pending", batch_id="rc-ledger-pending",
                bxd_no="BXD-20260425-0006", amount="1000.00")
    _bxd_batch(db, batch_id="rc-bxd-failed", status="failed")
    _bxd_head_and_lines(db, batch_id="rc-bxd-failed", head_id="rc-head-failed",
                        head_no="BXD-20260425-0007", amounts=["1000.00"])
    _formal_row(db, raw_line_id="rc-fpe-inactive", bxd_no="BXD-20260425-0008",
                amount_inc_tax="1000.00", data_status="审批中")
    db.commit()
    rows = reconcile.expense_reconcile_rows(db)
    assert rows == []


def _http_client(db, *, username: str, role: str = "admin", overrides: dict | None = None) -> TestClient:
    graph = _perms.effective(role, None)
    db.add(
        SysUser(
            username=username,
            role=role,
            display_name=f"合成对账{username}",
            password_hash=hash_password("synthetic-password-123"),
            template_perms=dict(graph),
            perm_overrides=overrides or None,
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_expense_reconcile.router, prefix="/api")
    client = TestClient(app)
    return client


def test_reconcile_http_permission_matrix(db):
    admin = _http_client(db, username="reconcile_admin")
    login = admin.post(
        "/api/auth/login",
        json={"username": "reconcile_admin", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    admin.headers["Authorization"] = f"Bearer {login.json()['token']}"

    boss = _http_client(db, username="reconcile_boss", role="boss")
    login = boss.post(
        "/api/auth/login",
        json={"username": "reconcile_boss", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    boss.headers["Authorization"] = f"Bearer {login.json()['token']}"

    sales = _http_client(
        db,
        username="reconcile_sales",
        role="sales",
        overrides={"data_profit": True, "page_maintenance": True},
    )
    login = sales.post(
        "/api/auth/login",
        json={"username": "reconcile_sales", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    sales.headers["Authorization"] = f"Bearer {login.json()['token']}"

    no_profit = _http_client(
        db,
        username="reconcile_no_profit",
        role="boss",
        overrides={"data_profit": False},
    )
    login = no_profit.post(
        "/api/auth/login",
        json={"username": "reconcile_no_profit", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    no_profit.headers["Authorization"] = f"Bearer {login.json()['token']}"

    anonymous = TestClient(no_profit.app)

    assert admin.get("/api/maintenance/reconcile/expenses").status_code == 200
    assert "no-store" in admin.get("/api/maintenance/reconcile/expenses").headers["cache-control"]
    assert boss.get("/api/maintenance/reconcile/expenses").status_code == 200
    assert sales.get("/api/maintenance/reconcile/expenses").status_code == 403
    assert no_profit.get("/api/maintenance/reconcile/expenses").status_code == 403
    assert anonymous.get("/api/maintenance/reconcile/expenses").status_code == 401


def test_reconcile_limit_offset_pagination(db):
    """limit/offset 在服务端分页（round-5 Blocker 10）。"""
    _ledger_batch(db, batch_id="rc-ledger-page")
    for index in range(3):
        _ledger_row(
            db,
            row_id=f"rc-ler-page-{index}",
            batch_id="rc-ledger-page",
            bxd_no=f"BXD-20260425-1{index:03d}",
            amount="100.00",
        )
    db.commit()
    page = reconcile.expense_reconcile_rows(db, limit=2, offset=1)
    assert [row["bxd_no"] for row in page] == [
        "BXD-20260425-1001",
        "BXD-20260425-1002",
    ]


def test_reconcile_mismatch_and_retransmit_dedup(db):
    """台账 1200 ≠ 正式 1000 → mismatch；同文件重传两个 applied 批次只计最新（round-6 Blocker 8）。"""
    from datetime import timedelta

    # 同 file_hash 的两个 applied 台账批次（重传场景），较旧批次的金额不参与
    for batch_id, key in (("rc-ledger-old", "key-old"), ("rc-ledger-new", "key-new")):
        db.add(
            MaintenanceLedgerImportBatch(
                batch_id=batch_id,
                file_hash="same-file-hash-retransmit",
                filename="台账.xlsx",
                idempotency_key=key,
                source_kind="project_manager_xls_v1",
                uploaded_by="合成管理员",
                status="applied",
                applied_by="合成管理员",
                applied_at=APPLIED_AT
                + (timedelta(seconds=0) if batch_id.endswith("old") else timedelta(days=1)),
            )
        )
    db.flush()
    _ledger_row(db, row_id="rc-ler-old", batch_id="rc-ledger-old",
                bxd_no="BXD-20260425-0009", amount="999.00")
    _ledger_row(db, row_id="rc-ler-new", batch_id="rc-ledger-new",
                bxd_no="BXD-20260425-0009", amount="1200.00")
    _formal_row(db, raw_line_id="rc-fpe-9", bxd_no="BXD-20260425-0009",
                amount_inc_tax="1000.00")
    db.commit()
    rows = {r["bxd_no"]: r for r in reconcile.expense_reconcile_rows(db)}
    row = rows["BXD-20260425-0009"]
    assert row["status"] == "mismatch"
    assert row["ledger_amount"] == 1200.0  # 只计最新批次，旧批次 999 被去重
    assert row["formal_amount"] == 1000.0
