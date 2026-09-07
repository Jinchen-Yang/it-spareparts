"""收款单（SKD）台账导入第三轮复核契约（D-16，2026-09-07 再复核后补充，#288）。

审查 probe（test_probe_cas_math / test_probe_d16 / test_probe_cas_code /
test_probe_gc_reuse / test_probe_explain2）逐一转化：
1. 逐项 CAS 失败一律 code=stale_preview、文案以「，请重新预览」结尾；
2. 应用先取项目工作簿并发锁再读快照 / 台账，正规入口的并发改值进不了窗口；
3. 基线之后、覆盖起点之前的已作废快照整合同 fail-closed；
4. 累计经过的覆盖月（漂移 / 级联）都是依赖，方向无关；
5. record_receipts 行同样受构成月约束（提示 + 应用硬拒）；
6. 带新收款却不可核实的月份是被阻断构成月，依赖它的行随之阻断；
7. 裁决后全部收款"已入账"仍要按台账出 update 行；
8. 无台账支撑的月份推导值高于已确认值同样 cumulative_unverifiable；
9. 新建 / 登记入台账同样是经营事实写入，实名门禁；
10. fail-closed 行 match_state=invalid、同行 code 去重、冲突行结构化 canonical/before；
11. 复用归档刷新 mtime 重新进入 GC 宽限期；应用前核验原件；
12. selection_hash 查询走偏唯一索引；
13. 迁移 upgrade 带 lock_timeout；
14. 归档暂存残片可辨认且由 GC 回收；
15. 台账有行 / bulk_import 快照存在时拒绝降级。
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command as alembic_command
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.config import get_settings
from app.db import SessionLocal, engine
from app.etl import pipeline
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.models.system import SysImportBatch, SysRawFile
from app.services import maintenance_bulk_import as bulk
from app.services import maintenance_project_operations as operations
from app.services import raw_archive_gc
from tests.boss_board_helpers import client_for
from tests.test_maintenance_receipt_ledger_hardening import (
    _API,
    _apply_body,
    _by_month,
    _ledger,
    _legacy,
    _ready_keys,
    _shared_admin_client,
)
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

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/b7d3f9a1c5e2_maintenance_collection_receipt_ledger.py"
)


def _codes(row: dict) -> list[str]:
    return [issue["code"] for issue in [*row["warnings"], *row["errors"]]]


# ---------- (1) 逐项 CAS 失败一律 stale_preview ----------

@pytest.mark.parametrize(
    "scenario", ["contract_version", "create_month_taken", "selected_month_version"]
)
def test_per_item_cas_failures_are_stale_preview_with_repreview_message(db, scenario):
    """审查 probe：三处逐项 CAS 原来只抛裸 apply_conflict，前端无法识别为"重新预览即可"。"""
    project, contract = _project_with_contract(db)
    if scenario == "selected_month_version":
        first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
        _apply(db, first, _ready_keys(first))
        jan = _snapshots(db, contract)[date(2026, 1, 1)]
        preview = _preview(
            db, _receipt_xlsx([(ORDER_NO, "SK-1B", date(2026, 1, 25), 30)]), name="b.xlsx"
        )
        keys = [_by_month(preview)["2026-01-01"]["row_key"]]
        # 只改备注也递增版本：快照被他人碰过，冻结的预览不能再落。
        operations.update_collection(
            db, collection_id=jan.collection_id, version=jan.version,
            updates={"remark": "B 改了备注"}, reason="B 编辑", operated_by="user-b",
        )
        db.commit()
    elif scenario == "create_month_taken":
        preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-F", date(2026, 2, 5), 50)]))
        keys = _ready_keys(preview)
        _legacy(db, project, contract, date(2026, 2, 1), "10.00")
    else:
        preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
        keys = _ready_keys(preview)
        db.expire_all()
        relation = db.get(MaintenanceProjectContract, contract.project_contract_id)
        relation.version += 1
        db.commit()

    with pytest.raises(bulk.BulkImportConflict) as caught:
        _apply(db, preview, keys)
    db.rollback()
    assert caught.value.code == "stale_preview"
    assert str(caught.value).endswith("，请重新预览"), str(caught.value)

    real = client_for(db, username=f"skd-real-{scenario}", role="admin")
    response = real.post(f"{_API}/apply", json=_apply_body(preview, keys))
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "stale_preview" and detail["message"].endswith("，请重新预览")
    db.expire_all()
    batch = db.get(SysImportBatch, int(preview["preview_id"]))
    assert batch.status == "failed"
    assert batch.report_json["failure"]["error_code"] == "stale_preview"


# ---------- (2) 先取项目锁再读：正规入口的并发改值进不了窗口 ----------

def test_concurrent_canonical_edit_inside_apply_window_waits_for_project_lock(db, monkeypatch):
    """审查 probe（test_p5）：应用读完快照 / 台账、尚未写入时，另一会话经 update_collection
    把基线 4 月 200→250 提交——旧口径逐项 CAS 与指纹都按读到的旧值通过，5 月落 260 而
    真值是 310。新口径应用先按项目取工作簿并发锁再读，正规入口的并发写只能等锁。"""
    project, contract = _project_with_contract(db)
    apr = _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-MAY", date(2026, 5, 3), 60)]))
    assert _by_month(preview)["2026-05-01"]["after"]["cumulative_amount"] == "260.00"
    original = bulk._contract_fingerprint
    outcome: dict[str, bool] = {}

    def racing_fingerprint(**kwargs):
        # 应用期指纹复算发生在读快照 / 台账之后、第一次写入之前——正是审查 probe 的窗口。
        if not outcome:
            other = SessionLocal()
            try:
                other.execute(text("SET LOCAL lock_timeout = '1500ms'"))
                row = other.get(MaintenanceCollectionSnapshot, apr.collection_id)
                operations.update_collection(
                    other, collection_id=row.collection_id, version=row.version,
                    updates={"cumulative_amount": Decimal("250.00")},
                    reason="race", operated_by="mgr2",
                )
                other.commit()
                outcome["committed"] = True
            except OperationalError:
                other.rollback()
                outcome["committed"] = False
            finally:
                other.close()
        return original(**kwargs)

    monkeypatch.setattr(bulk, "_contract_fingerprint", racing_fingerprint)
    result = _apply(db, preview, _ready_keys(preview))
    assert result["applied"] == 1
    assert outcome == {"committed": False}, "并发改值必须在项目锁上等待，而不是进入应用窗口"
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 4, 1)].cumulative_amount == Decimal("200.00")
    assert snaps[date(2026, 5, 1)].cumulative_amount == Decimal("260.00")


# ---------- (3) 基线之后、覆盖起点之前的已作废快照 ----------

@pytest.mark.parametrize("variant", ["after_baseline", "no_baseline"])
def test_void_snapshot_between_baseline_and_coverage_fails_contract_closed(db, variant):
    """审查 probe（test_p1）：1 月 100、2 月 300 旧快照，台账从 3 月建账（基线 2 月）；
    随后 2 月作废。旧口径基线跳到 1 月、2 月不在候选月里——2 月的收款既不在台账也
    不在基线，3 月给出 350→150 的"覆盖"、4 月 160 新建，2 月的钱静默蒸发。"""
    project, contract = _project_with_contract(db)
    if variant == "after_baseline":
        _legacy(db, project, contract, date(2026, 1, 1), "100.00")
        _legacy(db, project, contract, date(2026, 2, 1), "300.00")
        _legacy(db, project, contract, date(2026, 3, 1), "350.00")
        seed = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-MAR", date(2026, 3, 5), 50)]))
        assert _by_month(seed)["2026-03-01"]["action"] == "record_receipts"
        _apply(db, seed, _ready_keys(seed))
        feb = _snapshots(db, contract)[date(2026, 2, 1)]
        operations.update_collection(
            db, collection_id=feb.collection_id, version=feb.version,
            updates={"status": "void"}, reason="void feb", operated_by="mgr",
        )
        db.commit()
        expected_months = {"2026-03-01", "2026-04-01"}
    else:
        _legacy(db, project, contract, date(2026, 1, 1), "10.00", status="void")
        expected_months = {"2026-04-01"}

    preview = _preview(
        db, _receipt_xlsx([(ORDER_NO, "SK-APR", date(2026, 4, 5), 10)]), name="apr.xlsx"
    )

    by_month = _by_month(preview)
    assert set(by_month) == expected_months
    for row in by_month.values():
        assert row["row_status"] == "blocked" and row["match_state"] == "invalid"
        assert row["after"]["cumulative_amount"] is None
        voided = next(e for e in row["errors"] if e["code"] == "snapshot_voided")
        assert "早于覆盖起点" in voided["message"]
        assert len(_codes(row)) == len(set(_codes(row)))
    assert preview["can_apply"] is False
    assert not any(row.receipt_no == "SK-APR" for row in _ledger(db))


# ---------- (4) 累计经过的覆盖月都是依赖，方向无关 ----------

def test_later_month_create_built_on_drifted_month_depends_on_that_update(db):
    """审查 probe（test_p4）：1 月台账 100 被人工抬到 200，文件带 2 月 150。旧口径 2 月
    create 250 > 1 月现值 200 不触发方向判定，可单独勾选——落库后 1 月 200 / 2 月 250
    暗示 2 月只收 50，与台账的 150 互相矛盾。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    jan = _snapshots(db, contract)[date(2026, 1, 1)]
    operations.update_collection(
        db, collection_id=jan.collection_id, version=jan.version,
        updates={"cumulative_amount": Decimal("200.00")}, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 150)]), name="feb.xlsx")
    by_month = _by_month(preview)
    jan_row, feb_row = by_month["2026-01-01"], by_month["2026-02-01"]
    assert jan_row["action"] == "update_collection_snapshot"
    assert feb_row["action"] == "upsert_collection_snapshot" and feb_row["row_status"] == "ready"
    assert feb_row["after"]["cumulative_amount"] == "250.00"
    assert feb_row["depends_on_row_keys"] == [jan_row["row_key"]]
    assert any("需同时勾选 2026-01 的覆盖行" in hint for hint in feb_row["hint_messages"])

    with pytest.raises(bulk.BulkImportInvalid, match="2026-01"):
        _apply(db, preview, [feb_row["row_key"]])
    db.rollback()
    assert date(2026, 2, 1) not in _snapshots(db, contract)
    assert db.get(SysImportBatch, int(preview["preview_id"])).status == "processing"

    result = _apply(db, preview, [jan_row["row_key"], feb_row["row_key"]])
    assert result["applied"] == 2
    snaps = _snapshots(db, contract)
    assert snaps[date(2026, 1, 1)].cumulative_amount == Decimal("100.00")
    assert snaps[date(2026, 2, 1)].cumulative_amount == Decimal("250.00")


# ---------- (5) record_receipts 行同样受构成月约束 ----------

@pytest.mark.parametrize("variant", ["probe_p3", "probe_b"])
def test_record_receipts_row_requires_its_constituent_months(db, variant):
    """审查 probe（test_p3 / test_probe_b）：record_receipts 行不写快照，旧口径应用期
    跳过 requires_months、预览也不给提示——单独勾它只把本月收款登记入台账，构成月的
    收款永远进不了台账，下一次预览按台账推导整条历史都对不上。"""
    project, contract = _project_with_contract(db)
    if variant == "probe_p3":
        _legacy(db, project, contract, date(2026, 2, 1), "300.00")
        rows = [(ORDER_NO, "SK-J", date(2026, 1, 10), 100), (ORDER_NO, "SK-F", date(2026, 2, 5), 200)]
        constituent, dependent = "2026-01-01", "2026-02-01"
    else:
        _legacy(db, project, contract, date(2026, 1, 1), "100.00")
        _legacy(db, project, contract, date(2026, 3, 1), "300.00")
        rows = [(ORDER_NO, "SK-F", date(2026, 2, 5), 100), (ORDER_NO, "SK-M", date(2026, 3, 5), 100)]
        constituent, dependent = "2026-02-01", "2026-03-01"
    preview = _preview(db, _receipt_xlsx(rows))
    by_month = _by_month(preview)
    earlier, later = by_month[constituent], by_month[dependent]
    assert earlier["action"] == "upsert_collection_snapshot"
    assert later["action"] == "record_receipts" and later["row_status"] == "ready"
    assert later["depends_on_row_keys"] == [earlier["row_key"]]
    assert any(
        f"{date.fromisoformat(constituent):%Y-%m}" in hint and "需同时勾选" in hint
        for hint in later["hint_messages"]
    )

    with pytest.raises(bulk.BulkImportInvalid, match=f"{date.fromisoformat(constituent):%Y-%m}"):
        _apply(db, preview, [later["row_key"]])
    db.rollback()
    assert _ledger(db) == []
    assert date.fromisoformat(constituent) not in _snapshots(db, contract)

    result = _apply(db, preview, [earlier["row_key"], later["row_key"]])
    assert result["applied"] == 2
    assert {row.receipt_no for row in _ledger(db)} == {receipt for _o, receipt, _d, _a in rows}


# ---------- (6) 不可核实的月份是被阻断构成月 ----------

def test_unverifiable_month_with_new_receipts_blocks_dependent_months(db):
    """审查 probe（test_p2）：2 月人工 300、无台账支撑，文件带 2 月 5 + 3 月 250。2 月
    不可核实（105 < 300）；旧口径 3 月 create 355 却可单独勾选——355 里含 2 月那 5 元，
    而它永远进不了台账。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    operations.create_collection(
        db, project_id=project.project_id, project_contract_id=contract.project_contract_id,
        report_month=date(2026, 2, 1), cumulative_amount=Decimal("300.00"), status="confirmed",
        receipt_reference=None, remark=None, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-F", date(2026, 2, 5), 5), (ORDER_NO, "SK-M", date(2026, 3, 5), 250),
    ]), name="fm.xlsx")

    by_month = _by_month(preview)
    feb, mar = by_month["2026-02-01"], by_month["2026-03-01"]
    assert any(e["code"] == "cumulative_unverifiable" for e in feb["errors"])
    assert mar["row_status"] == "blocked" and mar["match_state"] == "invalid"
    blocked = next(e for e in mar["errors"] if e["code"] == "constituent_blocked")
    assert "2026-02" in blocked["message"]
    assert mar["after"]["cumulative_amount"] is None
    assert preview["can_apply"] is False
    assert by_month["2026-01-01"]["row_status"] == "unchanged"
    with pytest.raises(bulk.BulkImportInvalid):
        _apply(db, preview, [mar["row_key"]])
    db.rollback()
    assert {row.receipt_no for row in _ledger(db)} == {"SK-1"}


# ---------- (7) 裁决后全部"已入账"仍要出 update 行 ----------

def test_ruling_only_correction_surfaces_as_update_on_repreview(db):
    """审查 probe（test_probe_a）：SK-1 100 入账后裁决为 120；再预览同一文件，SK-1
    "已入账"、无新收款——旧口径整合同跳过，1 月快照 100 与台账 120 的差永远浮不上来。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    conflicting = _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 120)])
    assert _preview(db, conflicting)["summary"]["receipt_conflicts"] == 1
    ruling = bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-1", receipt_date=date(2026, 1, 10),
        actual_amount="120.00", reason="凭证为 120", operated_by="finance-boss",
    )
    db.commit()
    assert [m["report_month"] for m in ruling["affected_months"]] == ["2026-01-01"]

    again = _preview(db, conflicting, name="again.xlsx")

    by_month = _by_month(again)
    assert list(by_month) == ["2026-01-01"]
    jan = by_month["2026-01-01"]
    assert jan["action"] == "update_collection_snapshot" and jan["row_status"] == "ready"
    assert jan["requires_confirmation"] is True
    assert jan["before"]["cumulative_amount"] == "100.00"
    assert jan["after"]["cumulative_amount"] == "120.00"
    assert jan["after"]["new_receipts"] == 0
    assert any(w["code"] == "ledger_drift" for w in jan["warnings"])
    known = next(row for row in again["rows"] if row["canonical"].get("receipt_key"))
    assert any("已在台账" in hint for hint in known["hint_messages"])
    assert again["summary"]["known"] == 1 and again["summary"]["receipt_conflicts"] == 0

    result = _apply(db, again, [jan["row_key"]])
    assert result["applied"] == 1
    assert _snapshots(db, contract)[date(2026, 1, 1)].cumulative_amount == Decimal("120.00")

    # 快照与台账一致后再预览：无新收款、无漂移 → 该合同不再出任何行。
    settled = _preview(db, conflicting, name="settled.xlsx")
    assert _by_month(settled) == {} and settled["can_apply"] is False


# ---------- (8) 无支撑的月份高于已确认值同样不可核实 ----------

def test_unbacked_higher_derived_value_is_unverifiable_like_lower(db):
    """REQUIREMENTS #57 对称：2 月人工 120、无台账支撑，文件带 2 月 50 → 推导 150 高于
    120。旧口径把它作显式覆盖提供（人负责）；新口径与低于同论，不提供覆盖。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    operations.create_collection(
        db, project_id=project.project_id, project_contract_id=contract.project_contract_id,
        report_month=date(2026, 2, 1), cumulative_amount=Decimal("120.00"), status="confirmed",
        receipt_reference=None, remark=None, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-F", date(2026, 2, 5), 50)]), name="feb.xlsx")

    feb = _by_month(preview)["2026-02-01"]
    assert feb["row_status"] == "blocked" and feb["action"] == "block"
    assert feb["match_state"] == "invalid" and feb["requires_confirmation"] is False
    issue = next(e for e in feb["errors"] if e["code"] == "cumulative_unverifiable")
    assert "150.00 高于已确认累计 120.00" in issue["message"] and "无法核实" in issue["message"]
    assert not any(w["code"] == "snapshot_overwrite" for w in feb["warnings"])
    assert any("无法核实" in hint for hint in feb["hint_messages"])
    assert preview["can_apply"] is False
    assert _snapshots(db, contract)[date(2026, 2, 1)].cumulative_amount == Decimal("120.00")


# ---------- (9) 登记入台账同样是经营事实写入 ----------

def test_http_apply_record_receipts_rows_require_real_name_operator(db):
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 1, 1), "100.00")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    jan = _by_month(preview)["2026-01-01"]
    assert jan["action"] == "record_receipts"

    denied = _shared_admin_client().post(f"{_API}/apply", json=_apply_body(preview, [jan["row_key"]]))
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["message"] == "经营事实写入必须使用实名系统账号"
    assert _ledger(db) == []
    db.expire_all()
    assert db.get(SysImportBatch, int(preview["preview_id"])).status == "processing"

    real = client_for(db, username="skd-real-admin", role="admin")
    ok = real.post(f"{_API}/apply", json=_apply_body(preview, [jan["row_key"]]))
    assert ok.status_code == 200, ok.text
    assert [row.receipt_no for row in _ledger(db)] == ["SK-1"]


# ---------- (10) 公开行形态 ----------

def test_conflict_and_known_rows_carry_structured_values_and_hints(db):
    project, _contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))

    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 120), (ORDER_NO, "SK-2", date(2026, 2, 5), 50),
    ]), name="conflict.xlsx")

    conflict = next(row for row in preview["rows"] if row["canonical"].get("receipt_key"))
    assert conflict["match_state"] == "invalid" and conflict["row_status"] == "blocked"
    assert conflict["canonical"] == {
        "receipt_key": f"SK-1|{ORDER_NO}", "sales_order_no": "20240101-0001",
        "receipt_no": "SK-1", "receipt_date": "2026-01-10", "actual_amount": "120.00",
    }
    assert conflict["before"] == {
        "receipt_no": "SK-1", "receipt_date": "2026-01-10", "actual_amount": "100.00",
        "import_batch_id": int(first["preview_id"]),
    }
    assert any("与台账不一致" in hint for hint in conflict["hint_messages"])
    assert len(_codes(conflict)) == len(set(_codes(conflict)))
    month_rows = [row for row in preview["rows"] if row["canonical"].get("report_month")]
    assert month_rows and all(row["match_state"] == "invalid" for row in month_rows)
    assert preview["summary"]["ambiguous"] == 0
    assert preview["summary"]["invalid"] == len(month_rows) + 1

    known = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]), name="known.xlsx")
    known_row = next(row for row in known["rows"] if row["canonical"].get("receipt_key"))
    assert known_row["match_state"] == "matched" and known_row["before"] is None
    assert known_row["canonical"]["actual_amount"] == "100.00"
    assert any("已在台账" in hint for hint in known_row["hint_messages"])


def test_contract_level_fail_closed_rows_are_invalid_with_unique_codes(db):
    """seed_required 逐月各出一条 issue、再挂到每一行：旧口径每行 code 重复、且
    match_state 误报 ambiguous；同批两个文件触及同一合同也一样。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 4, 1), "200.00")
    _legacy(db, project, contract, date(2026, 5, 1), "260.00")
    _legacy(db, project, contract, date(2026, 6, 1), "300.00")
    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-M2", date(2026, 5, 25), 35),
        (ORDER_NO, "SK-J", date(2026, 6, 3), 40),
        (ORDER_NO, "SK-JL", date(2026, 7, 3), 30),
    ]))
    by_month = _by_month(preview)
    assert set(by_month) == {"2026-05-01", "2026-06-01", "2026-07-01"}
    for row in by_month.values():
        assert row["match_state"] == "invalid" and row["row_status"] == "blocked"
        codes = _codes(row)
        assert codes.count("seed_required") == 1 and len(codes) == len(set(codes))
    assert preview["summary"]["ambiguous"] == 0 and preview["summary"]["invalid"] == 3

    two_files = bulk.preview_transfer(
        db,
        [
            ("a.xlsx", _receipt_xlsx([(ORDER_NO, "SK-A", date(2026, 8, 10), 100)])),
            ("b.xlsx", _receipt_xlsx([(ORDER_NO, "SK-B", date(2026, 9, 10), 150)])),
        ],
        operated_by="skd-operator",
    )
    month_rows = [row for row in two_files["rows"] if row["canonical"].get("report_month")]
    assert month_rows and all(row["match_state"] == "invalid" for row in month_rows)
    assert two_files["summary"]["ambiguous"] == 0


# ---------- (11) 复用归档重新进入宽限期；应用前核验原件 ----------

def test_reused_archive_reenters_gc_grace_window_before_apply(db):
    """审查 probe（test_probe_gc_reuse）：8 天前只预览未应用留下的孤儿 blob，同一原件再
    预览时被复用但 mtime 不动——预览→应用的 30 分钟里 GC 把它当孤儿删掉，应用把
    sys_raw_file 钉在一条不存在的路径上。"""
    _project_with_contract(db)
    data = _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)])
    raw_dir = os.path.abspath(get_settings().raw_file_dir)
    path = os.path.join(raw_dir, f"{hashlib.sha256(data).hexdigest()}.xlsx")

    _preview(db, data)
    assert os.path.isfile(path)
    old = time.time_ns() - 8 * 24 * 60 * 60 * 1_000_000_000
    os.utime(path, ns=(old, old))

    second = _preview(db, data, name="again.xlsx")
    assert time.time_ns() - os.stat(path).st_mtime_ns < 24 * 60 * 60 * 1_000_000_000
    db.commit()
    result = raw_archive_gc.reap_orphan_archives(
        execute=True, raw_dir=raw_dir, session_factory=SessionLocal, now_ns=time.time_ns(),
    )
    assert result["errors"] == 0 and os.path.isfile(path)

    _apply(db, second, _ready_keys(second))
    raw = db.scalars(
        select(SysRawFile).where(SysRawFile.batch_id == int(second["preview_id"]))
    ).one()
    assert raw.storage_path == path and os.path.isfile(raw.storage_path)


def test_apply_refuses_to_pin_a_missing_or_altered_archive(db):
    _project_with_contract(db)
    data = _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)])
    raw_dir = os.path.abspath(get_settings().raw_file_dir)
    path = os.path.join(raw_dir, f"{hashlib.sha256(data).hexdigest()}.xlsx")
    preview = _preview(db, data)
    keys = _ready_keys(preview)
    os.remove(path)

    with pytest.raises(pipeline.ArchiveError):
        _apply(db, preview, keys)
    db.rollback()
    assert _ledger(db) == [] and db.scalars(select(SysRawFile)).all() == []
    assert db.get(SysImportBatch, int(preview["preview_id"])).status == "processing"

    # 换成内容不符的普通文件同样拒绝；HTTP 层 500 archive_failed、批次保持 processing。
    with open(path, "wb") as handle:
        handle.write(b"tampered")
    real = client_for(db, username="skd-real-admin", role="admin")
    response = real.post(f"{_API}/apply", json=_apply_body(preview, keys))
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "archive_failed"
    db.expire_all()
    assert db.get(SysImportBatch, int(preview["preview_id"])).status == "processing"
    assert _ledger(db) == []

    # 原件复原后同一预览照常应用。
    pipeline.archive_bytes(data, hashlib.sha256(data).hexdigest())
    ok = real.post(f"{_API}/apply", json=_apply_body(preview, keys))
    assert ok.status_code == 200, ok.text


# ---------- (12) selection_hash 查询走偏唯一索引 ----------

def test_selection_hash_lookup_uses_the_partial_unique_index(db):
    """审查 probe（test_probe_explain2）：ORDER BY id LIMIT 1 让规划器沿主键扫描过滤，
    表达式索引形同虚设；改为不排序取全部（≤1 行）+ 偏唯一索引。"""
    db.execute(text(
        "INSERT INTO sys_import_batch (filename, file_type, file_hash, status, report_json) "
        "SELECT 'f'||g, 'maint_bulk', md5(g::text)||md5(g::text), 'success', "
        "jsonb_build_object('selection_hash', md5('s'||g)||md5('s'||g), 'pad', repeat('x', 400)) "
        "FROM generate_series(1, 3000) g"
    ))
    db.execute(text("ANALYZE sys_import_batch"))
    statement = bulk._applied_selection_statement(batch_id=5, selection_hash="a" * 64)
    sql = str(statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))
    assert "ORDER BY" not in sql and "LIMIT" not in sql
    plan = "\n".join(row[0] for row in db.execute(text("EXPLAIN " + sql)).all())
    assert "ix_batch_success_selection_hash" in plan, plan

    indexdef = db.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_batch_success_selection_hash'"
    )).scalar_one()
    assert indexdef.startswith("CREATE UNIQUE INDEX")
    assert "file_type" in indexdef and "'maint_bulk'" in indexdef and "'success'" in indexdef
    db.execute(text("SAVEPOINT dup"))
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO sys_import_batch (filename, file_type, file_hash, status, report_json) "
            "VALUES ('dup', 'maint_bulk', repeat('d', 64), 'success', "
            "jsonb_build_object('selection_hash', md5('s1')||md5('s1')))"
        ))
    db.execute(text("ROLLBACK TO SAVEPOINT dup"))
    db.rollback()


# ---------- (13) 迁移 upgrade 带 lock_timeout ----------

def test_migration_upgrade_and_downgrade_both_set_lock_timeout():
    migration = _MIGRATION.read_text(encoding="utf-8")
    upgrade_body, _sep, downgrade_body = migration.partition("def downgrade()")
    assert "SET LOCAL lock_timeout = '5s'" in upgrade_body.partition("def upgrade()")[2]
    assert "SET LOCAL lock_timeout = '5s'" in downgrade_body
    assert "IN ACCESS EXCLUSIVE MODE" in downgrade_body


# ---------- (14) 归档暂存残片可辨认、由 GC 回收 ----------

def test_archive_staging_files_are_recognisable_and_reaped_after_a_day(db, tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    monkeypatch.setattr(get_settings(), "raw_file_dir", str(raw_dir))
    staged: list[str] = []
    original_archive = pipeline._archive

    def spy_archive(src_path, file_hash):
        staged.append(os.path.basename(src_path))
        return original_archive(src_path, file_hash)

    original_replace = pipeline._replace_archive_temp

    def spy_replace(temp_path, dest_path):
        staged.append(os.path.basename(temp_path))
        return original_replace(temp_path, dest_path)

    monkeypatch.setattr(pipeline, "_archive", spy_archive)
    monkeypatch.setattr(pipeline, "_replace_archive_temp", spy_replace)
    payload = b"staged-bytes"
    pipeline.archive_bytes(payload, hashlib.sha256(payload).hexdigest())
    assert len(staged) == 2
    assert all(raw_archive_gc._STAGING_NAME_RE.fullmatch(name) for name in staged), staged
    assert sorted(p.name for p in raw_dir.iterdir()) == [f"{hashlib.sha256(payload).hexdigest()}.xlsx"]

    now_ns = time.time_ns()
    stale = raw_dir / "staging-deadbeef.part"
    stale.write_bytes(b"left behind")
    old = now_ns - 2 * 24 * 60 * 60 * 1_000_000_000
    os.utime(stale, ns=(old, old))
    fresh = raw_dir / "staging-inflight.part"
    fresh.write_bytes(b"in flight")
    recent = now_ns - 60 * 60 * 1_000_000_000
    os.utime(fresh, ns=(recent, recent))
    (raw_dir / "staging-link.part").symlink_to(stale)
    os.utime(raw_dir / "staging-link.part", ns=(old, old), follow_symlinks=False)

    dry = raw_archive_gc.reap_orphan_archives(
        raw_dir=str(raw_dir), session_factory=SessionLocal, now_ns=now_ns,
    )
    assert dry["errors"] == 0 and dry["deleted"] == 0 and stale.exists()
    assert dry["candidates"] == 1

    result = raw_archive_gc.reap_orphan_archives(
        execute=True, raw_dir=str(raw_dir), session_factory=SessionLocal, now_ns=now_ns,
    )
    assert result["errors"] == 0
    assert result["deleted"] == 1 and result["deleted_bytes"] == len(b"left behind")
    assert not stale.exists()
    assert fresh.exists() and (raw_dir / "staging-link.part").is_symlink()
    assert (raw_dir / f"{hashlib.sha256(payload).hexdigest()}.xlsx").exists()


# ---------- (15) 台账有行 / bulk_import 快照存在时拒绝降级 ----------

def test_downgrade_refused_while_ledger_rows_or_bulk_import_snapshots_exist(db):
    project, contract = _project_with_contract(db)
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, preview, _ready_keys(preview))
    assert _snapshots(db, contract)[date(2026, 1, 1)].source == "bulk_import"
    db.close()
    engine.dispose()
    cfg = _cfg()

    def _version() -> str:
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    original = _version()
    try:
        with pytest.raises(DBAPIError, match="export the receipt ledger.*downgrade refused"):
            alembic_command.downgrade(cfg, PREVIOUS)
        assert _version() == original
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM maintenance_collection_receipt")
            ).scalar_one() == 1
            connection.execute(text("DELETE FROM maintenance_collection_receipt"))

        with pytest.raises(DBAPIError, match="export snapshot provenance.*downgrade refused"):
            alembic_command.downgrade(cfg, PREVIOUS)
        assert _version() == original
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE maintenance_collection_snapshot "
                "SET source = 'direct_api', import_batch_id = NULL"
            ))

        # 运维导出并清空事实后才允许降级。
        alembic_command.downgrade(cfg, PREVIOUS)
        assert _version() == PREVIOUS
    finally:
        alembic_command.upgrade(cfg, "head")
    assert _version() == original
