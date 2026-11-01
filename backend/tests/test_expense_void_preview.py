"""导入前作废预演：预演与执行之间的承诺（pipeline 层）。

预演是无锁读、导入是加锁读，两者之间可能有别人的导入落地。所以预演给出的数字是一份
承诺：要么真实导入的作废集合与预演逐行一致，要么这次导入根本不发生。本文件钉住：
  - 预演的作废清单/金额/指纹与真实执行逐行一致（同库态）；
  - 预演之后相关行变了（金额、合同号、新增行、已作废）⇒ 装载期指纹复核在任何写入之前
    整批中止；
  - 强制预演只在 HTTP 入口（require_void_preview），直接调用 run_import 的既有路径不变；
  - 抑制/不适用形态不需要令牌。
"""
from decimal import Decimal
from datetime import date
from pathlib import Path
import uuid

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.etl import expense_void, pipeline
from app.etl.reader import ReaderError
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookState,
)
from app.models.system import SysAuditLog, SysImportBatch, SysRawFile
from app.services import import_void_preview
from app.services import maintenance_project_operations as ops


def _set_amount(row, value):
    """同时改三个金额列：ck_project_expense_amount_matches_basis 把 amount 与口径金额绑在一起。"""
    value = Decimal(value)
    row.amount = row.amount_ex_tax = value
    row.amount_inc_tax = (value * Decimal("1.13")).quantize(Decimal("0.01"))

_COLS = ["报销日期", "报销人员", "支出事由", "报销金额", "单号", "序号"]


def _anchored(tmp_path, name, rows, anchor="XSDD-P"):
    wb = Workbook(); ws = wb.active; ws.title = "报销明细"
    ws.append(["销售订单", anchor]); ws.append(_COLS)
    for r in rows:
        ws.append(r)
    p = tmp_path / name; wb.save(str(p)); return str(p)


def _rowwise(tmp_path, name, rows):
    wb = Workbook(); ws = wb.active; ws.title = "Sheet1"
    ws.append(_COLS + ["销售订单"])
    for r in rows:
        ws.append(r)
    p = tmp_path / name; wb.save(str(p)); return str(p)


R1 = ["2026-05-01", "甲", "备件", 5000, "BXD-1", 1]
R2 = ["2026-05-02", "乙", "差旅", 3000, "BXD-2", 1]
R3 = ["2026-05-03", "丙", "租金", 1200, "BXD-3", 1]


def _seed(db, tmp_path, rows=(R1, R2, R3), anchor="XSDD-P"):
    pipeline.run_import(db, _anchored(tmp_path, "seed.xlsx", list(rows), anchor), "seed.xlsx")
    db.commit()


def _seed_project(db, contract="XSDD-P"):
    """建一个带合同的维保项目，使报销归因与工作簿状态行真实参与装载。"""
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()),
        project_code=f"VOID-{uuid.uuid4().hex[:8]}",
        display_name="作废预演项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()),
        project_id=project.project_id,
        contract_id=f"VOID-C-{uuid.uuid4().hex[:8]}",
        contract_no=contract,
        amount_inc_tax=Decimal("10000.00"),
        included_in_total=True,
        status_mapping_state="mapped",
        status_mapping_version="test",
        effective_from=date(2026, 1, 1),
        source="ledger",
        version=1,
    ))
    db.commit()
    return project.project_id


def _rows(db):
    return {r.reason: r for r in db.scalars(select(FProjectExpense))}


# ---------- 令牌 ----------

def test_token_roundtrip_and_rejections():
    key = b"0123456789abcdef"
    tok = import_void_preview.issue(file_hash="h" * 64, mode="upsert", fingerprint="f" * 64,
                                    contract="XSDD-P", issued_at=1000, hmac_key=key)
    got = import_void_preview.verify(tok, hmac_key=key, now=1500, mode="upsert")
    assert got == {"file_hash": "h" * 64, "fingerprint": "f" * 64,
                   "contract": "XSDD-P", "issued_at": 1000}
    with pytest.raises(import_void_preview.VoidPreviewTokenError) as e:
        import_void_preview.verify(tok[:-2] + "zz", hmac_key=key, now=1500, mode="upsert")
    assert e.value.code == "void_preview_invalid"
    with pytest.raises(import_void_preview.VoidPreviewTokenError) as e:
        import_void_preview.verify(tok, hmac_key=b"another-key-xxxxxx", now=1500, mode="upsert")
    assert e.value.code == "void_preview_invalid"
    with pytest.raises(import_void_preview.VoidPreviewTokenError) as e:
        import_void_preview.verify(tok, hmac_key=key, now=1000 + 30 * 60 + 1, mode="upsert")
    assert e.value.code == "void_preview_expired"
    with pytest.raises(import_void_preview.VoidPreviewTokenError) as e:
        import_void_preview.verify(tok, hmac_key=key, now=1500, mode="skip")
    assert e.value.code == "void_preview_mode_mismatch"


# ---------- 预演状态 ----------

def test_preview_statuses_without_db_dependency(db, tmp_path):
    armed = _anchored(tmp_path, "a.xlsx", [R1])
    assert pipeline.preview_expense_void(db, armed, mode="skip")["status"] == "not_applicable"

    dup = _anchored(tmp_path, "dup.xlsx", [R1, ["2026-05-01", "甲", "重复", 1, "BXD-1", 1]])
    r = pipeline.preview_expense_void(db, dup, mode="upsert")
    assert r["status"] == "will_be_rejected" and r["blocking_error_types"] == ["duplicate_key"]

    dropped = _rowwise(tmp_path, "d.xlsx", [R1 + ["XSDD-P"], R2 + [None]])
    assert pipeline.preview_expense_void(db, dropped, mode="upsert")["reason"] == "dropped_no_contract"
    multi = _rowwise(tmp_path, "m.xlsx", [R1 + ["XSDD-P"], R2 + ["XSDD-Q"]])
    assert pipeline.preview_expense_void(db, multi, mode="upsert")["reason"] == "multi_contract"
    unanchored = _rowwise(tmp_path, "u.xlsx", [R1 + ["XSDD-P"]])
    assert pipeline.preview_expense_void(db, unanchored, mode="upsert")["reason"] == "unanchored"

    blank = _anchored(tmp_path, "blank.xlsx", [])
    assert pipeline.preview_expense_void(db, blank, mode="upsert")["status"] == "not_applicable"


# ---------- 承诺兑现 ----------

def test_preview_matches_execution_row_for_row(db, tmp_path):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])          # R3 不在本表 ⇒ 将作废
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    assert pv["status"] == "ready" and pv["contract"] == "XSDD-P"
    assert pv["void"] == {"rows": 1, "amount": "1200.00", "already_void_rows": 0}
    (row,) = pv["void_rows"]
    assert row["reason"] == "租金" and row["amount"] == "1200.00" \
        and row["linked_sales_order_no"] == "XSDD-P"

    batch = pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                                expected_void_fingerprint=pv["fingerprint"],
                                require_void_preview=True)
    db.commit()
    assert batch.status == "success"
    assert batch.report_json["expense_rows_voided"] == 1
    assert batch.report_json["expense_void_fingerprint"] == pv["fingerprint"]
    rows = _rows(db)
    assert rows["租金"].data_status == "已作废"
    assert rows["备件"].data_status == "已结束" and rows["差旅"].data_status == "已结束"


def test_already_void_rows_are_reported_not_revoided(db, tmp_path):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                        expected_void_fingerprint=pipeline.preview_expense_void(
                            db, page, mode="upsert")["fingerprint"])
    db.commit()
    pv = pipeline.preview_expense_void(db, page, mode="upsert")     # 再预演一次
    assert pv["void"] == {"rows": 0, "amount": "0", "already_void_rows": 1}


@pytest.mark.parametrize("mutate", [
    "amount_changed", "contract_changed", "new_row_under_contract", "row_voided_meanwhile",
])
def test_drift_after_preview_aborts_before_any_write(db, tmp_path, mutate):
    """预演 → 库里相关行变了 → 带旧指纹导入：整批中止，连本表的同键覆盖也不落地。"""
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx",
                     [["2026-05-01", "甲", "备件", 5500, "BXD-1", 1], R2])   # 改金额 + 删 R3
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    assert pv["void"]["rows"] == 1

    victim = _rows(db)["租金"]
    if mutate == "amount_changed":
        _set_amount(victim, "1300")
    elif mutate == "contract_changed":
        # 指纹必须含合同号：费用归集工作簿 apply 不取全局导入锁，能在探针→逐行锁窗口里
        # 改合同号；该行仍会被作废，若指纹只看状态与金额则一声不响。
        victim.linked_sales_order_no = "XSDD-Q"
    elif mutate == "new_row_under_contract":
        db.add(FProjectExpense(
            raw_line_id="NEW-UNDER-P", bxd_no="BXD-9", line_no=1, data_status="已结束",
            expense_date=victim.expense_date, person="丁", reason="新增的旧行",
            linked_sales_order_no="XSDD-P", amount=Decimal("7"), amount_ex_tax=Decimal("7"),
            amount_inc_tax=Decimal("7.91"), tax_basis="default_ex",
            tax_rate_used=victim.tax_rate_used, import_batch_id=victim.import_batch_id))
    elif mutate == "row_voided_meanwhile":
        victim.data_status = "已作废"
    db.commit()

    with pytest.raises(expense_void.VoidPlanDrift):
        pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                            expected_void_fingerprint=pv["fingerprint"],
                            require_void_preview=True)
    db.commit()            # 与 API 层一致：失败批次留痕提交；中止发生在任何写入之前
    db.expire_all()
    rows = _rows(db)
    assert rows["备件"].amount == Decimal("5000")                    # 同键覆盖未落地
    if mutate != "row_voided_meanwhile":
        assert rows["租金"].data_status == "已结束"                   # 未被作废
    failed = db.scalars(select(pipeline.SysImportBatch).order_by(
        pipeline.SysImportBatch.id.desc())).first()
    assert failed.status == "failed" and "重新预演" in failed.report_json["error"]


def test_drift_is_caught_at_the_unlocked_probe_first(db, tmp_path, monkeypatch):
    """第一次复核（无锁探针）就中止，不等逐行锁与归属探测跑完。"""
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    _set_amount(_rows(db)["租金"], "1300"); db.commit()

    calls = []
    real = expense_void.assert_fingerprint

    def spy(decision, inputs, existing, expected):
        calls.append(len(calls))
        return real(decision, inputs, existing, expected)

    monkeypatch.setattr(expense_void, "assert_fingerprint", spy)
    with pytest.raises(expense_void.VoidPlanDrift):
        pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                            expected_void_fingerprint=pv["fingerprint"])
    db.rollback()
    assert calls == [0]                                              # 第一次就抛了


# ---------- 写入后失败仍整批回滚 ----------

def test_reader_error_after_expense_write_rolls_back_whole_batch(db, tmp_path, monkeypatch):
    """已写入事实后的普通 ReaderError 仍按完整性失败整事务回滚（与预演失效区分）。

    预演失配由 expense_void.assert_fingerprint 在任何 FProjectExpense 变更之前抛出，
    走失败批次留痕；这里在真实 `_upsert_facts` 落地后注入普通 ReaderError，必须得到
    ImportIntegrityError，且调用方 rollback 后同键覆盖、作废与失败批次都不存在。
    """
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx",
                     [["2026-05-01", "甲", "备件", 5500, "BXD-1", 1], R2])   # 改金额 + 删 R3
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    assert pv["void"]["rows"] == 1
    real_upsert = pipeline.loader._upsert_facts

    def explode_after_write(*args, **kwargs):
        stats = real_upsert(*args, **kwargs)
        raise ReaderError("模拟写入后的解析失败", code="after_write")

    monkeypatch.setattr(pipeline.loader, "_upsert_facts", explode_after_write)
    with pytest.raises(pipeline.loader.ImportIntegrityError) as exc_info:
        pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                            expected_void_fingerprint=pv["fingerprint"],
                            require_void_preview=True)
    assert "整批已回滚" in str(exc_info.value)
    db.rollback()            # 与单文件/批量入口一致：完整性失败整事务回滚
    db.expire_all()
    rows = _rows(db)
    assert rows["备件"].amount == Decimal("5000")                    # 同键覆盖随事务回滚
    assert rows["租金"].data_status == "已结束"                      # 作废随事务回滚
    assert db.scalars(select(pipeline.SysImportBatch)
                      .where(pipeline.SysImportBatch.status == "failed")).all() == []


# ---------- 强制预演只在 HTTP 入口 ----------

def test_armed_import_requires_preview_only_when_http_layer_asks(db, tmp_path):
    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    with pytest.raises(ReaderError) as e:
        pipeline.run_import(db, page, "page.xlsx", mode="upsert", require_void_preview=True)
    assert e.value.code == "void_preview_required"
    db.rollback()
    assert _rows(db)["租金"].data_status == "已结束"

    batch = pipeline.run_import(db, page, "page2.xlsx", mode="upsert")   # 既有直接调用路径
    db.commit()
    assert batch.status == "success" and batch.report_json["expense_rows_voided"] == 1


@pytest.mark.parametrize("shape", ["dropped", "multi", "unanchored", "skip_mode"])
def test_non_armed_shapes_need_no_token_even_under_http_rule(db, tmp_path, shape):
    _seed(db, tmp_path)
    if shape == "dropped":
        p, mode = _rowwise(tmp_path, "x.xlsx", [R1 + ["XSDD-P"], R2 + [None]]), "upsert"
    elif shape == "multi":
        p, mode = _rowwise(tmp_path, "x.xlsx", [R1 + ["XSDD-P"], R2 + ["XSDD-Q"]]), "upsert"
    elif shape == "unanchored":
        p, mode = _rowwise(tmp_path, "x.xlsx", [R1 + ["XSDD-P"]]), "upsert"
    else:
        p, mode = _anchored(tmp_path, "x.xlsx", [R1]), "skip"
    batch = pipeline.run_import(db, p, "x.xlsx", mode=mode, require_void_preview=True)
    db.commit()
    assert batch.status == "success" and batch.report_json["expense_rows_voided"] == 0
    assert _rows(db)["租金"].data_status == "已结束"


def test_contract_change_inside_probe_to_lock_window_is_caught(db, tmp_path, monkeypatch):
    """指纹逐行元组必须含合同号（对抗核验 D3）。

    loader 的作废候选集在无锁探针处就由 affected_ids 定死；费用归集工作簿 apply 不取
    全局导入锁，能在探针→加锁重读的窗口里把某行的合同号从 C 改到 C2 并提交。该行仍在
    affected_ids 里、仍被加锁重读读到、仍会被作废——若指纹只看状态与金额，则一声不响地
    「预演说从 C 扣、实际从 C2 扣」。这里用第二个会话在第一次复核之后改合同号。
    """
    from app.db import SessionLocal

    _seed(db, tmp_path)
    page = _anchored(tmp_path, "page.xlsx", [R1, R2])
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    victim_id = _rows(db)["租金"].id

    real = expense_void.assert_fingerprint
    calls = []

    def hook(decision, inputs, existing, expected):
        calls.append(1)
        real(decision, inputs, existing, expected)          # 第 1 次（探针）通过；第 2 次抛
        if len(calls) == 1:
            other = SessionLocal()
            try:
                row = other.get(FProjectExpense, victim_id)
                row.linked_sales_order_no = "XSDD-Q"
                other.commit()
            finally:
                other.close()

    monkeypatch.setattr(expense_void, "assert_fingerprint", hook)
    with pytest.raises(expense_void.VoidPlanDrift):
        pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                            expected_void_fingerprint=pv["fingerprint"])
    db.rollback()
    assert len(calls) == 2                                   # 第二次（加锁重读后）才抓到
    db.expire_all()
    assert _rows(db)["租金"].data_status == "已结束"


def test_second_guard_drift_commit_keeps_only_failed_batch(db, tmp_path, monkeypatch):
    """第二道复核失配 + API 式 commit：保存点内副作用全部回滚，只留失败批次。

    loader 在第二道复核之前会经 lock_workbook_states 为缺失项目补建 revision=0
    的工作簿状态行。这里故意删掉状态行，用真实的 assert_fingerprint 在第二次
    指纹比对时判定失配，再按 API 契约 commit：失败批次留痕，原件行、补建状态行、
    事实、归因、revision 与审计一项都不能留下。
    """
    project_id = _seed_project(db)
    _seed(db, tmp_path)
    # 另一个同合同项目：已有 revision=5 的状态行，用于证明失败批次不会 bump
    # 既有 revision；本项目状态行则被删掉，用于证明补建的 revision=0 不落库。
    other_project_id = _seed_project(db)
    ops.get_or_create_workbook_state(db, project_id=other_project_id)
    db.get(MaintenanceProjectWorkbookState, other_project_id).revision = 5
    db.commit()

    db.expire_all()
    state = db.get(MaintenanceProjectWorkbookState, project_id)
    assert state is not None and state.revision == 1
    db.delete(state)
    db.commit()
    assert db.get(MaintenanceProjectWorkbookState, project_id) is None

    def fact_snapshot():
        return {
            row.raw_line_id: (row.amount, row.amount_ex_tax, row.amount_inc_tax,
                              row.data_status, row.import_batch_id)
            for row in db.scalars(select(FProjectExpense))
        }

    def attribution_snapshot():
        return {
            row.expense_id: (row.project_id, row.normalized_status,
                             row.amount_ex_tax, row.amount_inc_tax, row.version)
            for row in db.scalars(select(MaintenanceProjectExpenseAttribution))
        }

    facts_before = fact_snapshot()
    attributions_before = attribution_snapshot()
    audit_counts_before = (
        db.scalar(select(func.count()).select_from(SysAuditLog)),
        db.scalar(select(func.count()).select_from(MaintenanceProjectOperationAudit)),
    )
    raw_rows_before = db.scalar(select(func.count()).select_from(SysRawFile))
    archive_dir = Path(pipeline.get_settings().raw_file_dir)
    archived_before = sum(1 for path in archive_dir.rglob("*") if path.is_file())

    page = _anchored(tmp_path, "page.xlsx", [R1, R2])              # R3 将作废
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    assert pv["status"] == "ready" and pv["void"]["rows"] == 1

    real_fingerprint = expense_void.fingerprint
    calls = []

    def drifting_fingerprint(decision, inputs, existing):
        actual = real_fingerprint(decision, inputs, existing)
        calls.append(actual)
        if len(calls) == 2:
            # 第二道复核之前 lock_workbook_states 已补建 revision=0 状态行
            assert db.get(MaintenanceProjectWorkbookState, project_id) is not None
            return actual[:-1] + ("0" if actual[-1] != "0" else "1")
        return actual

    monkeypatch.setattr(expense_void, "fingerprint", drifting_fingerprint)
    with pytest.raises(expense_void.VoidPlanDrift):
        pipeline.run_import(db, page, "page.xlsx", mode="upsert",
                            expected_void_fingerprint=pv["fingerprint"],
                            require_void_preview=True)
    assert len(calls) == 2
    db.commit()             # 与 API 层一致：409 只提交失败批次留痕
    db.expire_all()

    failed = db.scalars(select(SysImportBatch).order_by(SysImportBatch.id.desc())).first()
    assert failed.status == "failed" and "重新预演" in failed.report_json["error"]
    assert fact_snapshot() == facts_before                            # 事实与作废未落地
    assert attribution_snapshot() == attributions_before              # 归因未改
    assert db.get(MaintenanceProjectWorkbookState, project_id) is None  # 补建状态行已回滚
    assert db.get(MaintenanceProjectWorkbookState, other_project_id).revision == 5
    assert db.scalar(select(func.count()).select_from(SysRawFile)
                     .where(SysRawFile.batch_id == failed.id)) == 0
    assert db.scalar(select(func.count()).select_from(SysRawFile)) == raw_rows_before
    assert (db.scalar(select(func.count()).select_from(SysAuditLog)),
            db.scalar(select(func.count()).select_from(
                MaintenanceProjectOperationAudit))) == audit_counts_before
    archived_after = sum(1 for path in archive_dir.rglob("*") if path.is_file())
    assert archived_after == archived_before                          # 预演失效不归档


def test_preview_over_row_cap_withholds_token(db, tmp_path, monkeypatch):
    """清单超过上限就不签令牌（Codex P1）：确认框承诺的是每一行，不完整的清单不能被确认。"""
    _seed(db, tmp_path)                                          # R1 R2 R3 在库
    page = _anchored(tmp_path, "page.xlsx", [R1])                # R2 R3 将作废
    monkeypatch.setattr(pipeline, "VOID_PREVIEW_ROW_CAP", 1)
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    assert pv["status"] == "too_large" and pv["void"]["rows"] == 2
    assert "preview_token" not in pv and "fingerprint" not in pv and "void_rows" not in pv

    monkeypatch.setattr(pipeline, "VOID_PREVIEW_ROW_CAP", 2)
    pv = pipeline.preview_expense_void(db, page, mode="upsert")
    assert pv["status"] == "ready" and len(pv["void_rows"]) == 2   # 完整清单，不截断

