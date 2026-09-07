"""收款单（SKD）台账导入第四轮复核契约（D-16，2026-09-07 二次补充，#288）。

审查 probe（test_probe_r4b / r4c / r4d / r4_money / lens_int2）逐一转化：
B1. 跨月 / 改日期裁决：影响面从 min(原月份, 新月份) 起重新推导；覆盖起点含被裁决
    取代的导入行，bulk_import 快照永不作基线——下一次预览把原月份重新对账，
    不再把移走的收款算两次；裁决把收款移入台账建立之前的历史月份，不把旧 05 表
    历史当漂移覆盖提供，且裁回去即可恢复。
B2. 被阻断月份（不论有没有本文件新收款）之后的每一行随之阻断，不能被默认勾选。
B3. 未确认快照同样是历史：无台账即 seed_required；覆盖文案按真实状态写"未确认"。
B4. 上游作废而台账仍有生效行 ⇒ receipt_voided_upstream（给出裁决路径），裁为 0 后
    同一导出即"已知"、合同重新可导；台账没见过的作废行仍判无效。
B5. record_receipts 行带真实 code 的 info issue。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook

from app.services import maintenance_bulk_import as bulk
from app.services import maintenance_project_operations as operations
from tests.test_maintenance_receipt_ledger_hardening import (
    _by_month,
    _ledger,
    _legacy,
    _ready_keys,
)
from tests.test_maintenance_receipt_ledger_import import (
    ORDER_NO,
    _apply,
    _preview,
    _project_with_contract,
    _receipt_xlsx,
    _rows,
    _snapshots,
)


def _codes(row: dict) -> list[str]:
    return [issue["code"] for issue in [*row["warnings"], *row["errors"]]]


def _default_tickable(preview: dict) -> list[str]:
    """前端默认勾选口径：ready、不需显式确认、且无依赖行。"""

    return [
        row["canonical"]["report_month"]
        for row in _rows(preview, row_status="ready")
        if not row["requires_confirmation"] and not row["depends_on_row_keys"]
    ]


def _status_xlsx(rows: list[tuple]) -> bytes:
    """带「数据状态」列的收款导出：(订单, 单号, 日期, 金额, 状态)。"""

    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "收款明细.销售订单(必填)", "收款单号(必填)", "收款日期(必填)",
        "收款明细.实收金额", "数据状态",
    ])
    for row in rows:
        sheet.append(list(row))
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _amounts(db, contract) -> dict[str, tuple[str, str]]:
    return {
        month.isoformat(): (str(row.cumulative_amount), row.status)
        for month, row in sorted(_snapshots(db, contract).items())
    }


# ---------- B1 跨月 / 改日期裁决 ----------

def test_b1_date_ruling_rederives_old_month_and_next_preview_never_double_counts(db):
    """probe test_j2 / test_probe_a2：SK-1 1 月 100 入账后裁成 2 月收款（只改日期）。
    旧口径：裁决报"无影响"，下一次预览把 1 月快照（bulk_import）当基线，2 月回填
    200、3 月 250——同一笔算两次且全部默认勾选。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))

    ruling = bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-1", receipt_date=date(2026, 2, 1),
        actual_amount="100.00", reason="日期录错：实际 2 月到账", operated_by="boss",
    )
    db.commit()
    assert ruling["affected_months"] == [
        {"report_month": "2026-01-01", "current_cumulative": "100.00", "derived_cumulative": "0.00"},
    ]

    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 3, 3), 50)]), name="mar.xlsx")
    by_month = _by_month(preview)
    assert set(by_month) == {"2026-01-01", "2026-02-01", "2026-03-01"}
    jan, feb, mar = by_month["2026-01-01"], by_month["2026-02-01"], by_month["2026-03-01"]
    # 1 月按台账重新对账：100 → 0 的显式覆盖，默认不勾选
    assert jan["action"] == "update_collection_snapshot" and jan["requires_confirmation"] is True
    assert (jan["before"]["cumulative_amount"], jan["after"]["cumulative_amount"]) == ("100.00", "0.00")
    assert "ledger_drift" in _codes(jan)
    # 2 月回填 100（不是 200）：1 月快照不再是基线
    assert feb["action"] == "upsert_collection_snapshot"
    assert feb["after"]["cumulative_amount"] == "100.00" and feb["after"]["baseline"] is None
    assert feb["depends_on_row_keys"] == [jan["row_key"]]
    assert mar["after"]["cumulative_amount"] == "150.00" and mar["after"]["baseline"] is None
    assert not any(row["after"]["cumulative_amount"] == "200.00" for row in by_month.values())
    assert _default_tickable(preview) == []

    applied = _apply(db, preview, [jan["row_key"], feb["row_key"], mar["row_key"]])
    assert applied["applied"] == 3
    assert _amounts(db, contract) == {
        "2026-01-01": ("0.00", "confirmed"),
        "2026-02-01": ("100.00", "confirmed"),
        "2026-03-01": ("150.00", "confirmed"),
    }


def test_b1_receipt_ruled_into_pre_ledger_history_is_not_a_drift_overwrite(db):
    """probe test_b（Variant B）：旧 05 表 1 月 500，台账从 2 月建（2 月 600、3 月 650），
    SK-2 被裁成 1 月收款。旧口径：基线消失，1 月 500→50、2 月 600→150 作为漂移覆盖
    提供。新口径：1 月是台账建立之前的历史、该月没有导入行支撑 ⇒ 不可核实阻断，
    其后月份随之阻断；裁回 3 月即恢复（基线 1 月 500 重新生效）。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 1, 1), "500.00")
    p1 = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 2, 10), 100)]))
    _apply(db, p1, _ready_keys(p1))
    p2 = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 3, 10), 50)]), name="b.xlsx")
    _apply(db, p2, _ready_keys(p2))
    assert _amounts(db, contract) == {
        "2026-01-01": ("500.00", "confirmed"),
        "2026-02-01": ("600.00", "confirmed"),
        "2026-03-01": ("650.00", "confirmed"),
    }

    bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-2", receipt_date=date(2026, 1, 20),
        actual_amount="50.00", reason="实际 1 月到账", operated_by="boss",
    )
    db.commit()
    incremental = _receipt_xlsx([(ORDER_NO, "SK-9", date(2026, 4, 1), 1)])
    preview = _preview(db, incremental, name="c.xlsx")
    by_month = _by_month(preview)
    jan = by_month["2026-01-01"]
    assert jan["row_status"] == "blocked" and "cumulative_unverifiable" in _codes(jan)
    assert jan["action"] == "block" and "snapshot_overwrite" not in _codes(jan)
    assert not _rows(preview, action="update_collection_snapshot")
    assert all(by_month[m]["row_status"] == "blocked" for m in ("2026-02-01", "2026-03-01", "2026-04-01"))
    assert preview["can_apply"] is False and preview["summary"]["ready"] == 0

    # 裁回 3 月：覆盖起点回到 2 月，1 月 500 重新成为基线，4 月按 651 新建
    bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-2", receipt_date=date(2026, 3, 10),
        actual_amount="50.00", reason="复核：仍是 3 月到账", operated_by="boss",
    )
    db.commit()
    again = _preview(db, incremental, name="d.xlsx")
    by_month = _by_month(again)
    assert "2026-01-01" not in by_month
    assert by_month["2026-02-01"]["row_status"] == "unchanged"
    assert by_month["2026-03-01"]["row_status"] == "unchanged"
    apr = by_month["2026-04-01"]
    assert apr["action"] == "upsert_collection_snapshot" and apr["after"]["cumulative_amount"] == "651.00"
    assert apr["after"]["baseline"]["report_month"] == "2026-01-01"
    assert again["can_apply"] is True


def test_b1_bulk_import_snapshot_is_never_the_baseline_after_date_ruling_later(db):
    """probe test_a：2 月 SK-1 100、3 月 SK-2 50 入账后，SK-1 裁成 3 月 5 日收款。
    2 月快照（bulk_import）不作基线：2 月 100→0 覆盖、3 月 150 不变、4 月 170。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-1", date(2026, 2, 10), 100), (ORDER_NO, "SK-2", date(2026, 3, 10), 50),
    ]))
    _apply(db, first, _ready_keys(first))
    ruling = bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-1", receipt_date=date(2026, 3, 5),
        actual_amount="100.00", reason="到账日期录错", operated_by="boss",
    )
    db.commit()
    assert [(m["report_month"], m["derived_cumulative"]) for m in ruling["affected_months"]] == [
        ("2026-02-01", "0.00"),
    ]
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-3", date(2026, 4, 2), 20)]), name="apr.xlsx")
    by_month = _by_month(preview)
    assert by_month["2026-02-01"]["action"] == "update_collection_snapshot"
    assert by_month["2026-02-01"]["after"]["cumulative_amount"] == "0.00"
    assert by_month["2026-03-01"]["row_status"] == "unchanged"
    assert by_month["2026-04-01"]["after"]["cumulative_amount"] == "170.00"
    _apply(db, preview, _ready_keys(preview))
    assert _amounts(db, contract)["2026-03-01"] == ("150.00", "confirmed")
    assert _amounts(db, contract)["2026-04-01"] == ("170.00", "confirmed")


# ---------- B2 被阻断月份之后的行随之阻断 ----------

def test_b2_blocked_month_without_new_receipts_blocks_every_later_row(db):
    """probe test_l / test_probe_d：1 月台账 100，人在 05 表 / API 把 2 月确认成 110
    （台账不知道的 10），文件带 3 月 20、4 月 5。旧口径：2 月不可核实，但 3 月 120
    / 4 月 125 照常 ready 且默认勾选——把 2 月的 10 静默丢掉。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    operations.create_collection(
        db, project_id=project.project_id, project_contract_id=contract.project_contract_id,
        report_month=date(2026, 2, 1), cumulative_amount=Decimal("110.00"), status="confirmed",
        receipt_reference="手工", remark=None, reason="manual", operated_by="mgr",
    )
    db.commit()

    preview = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-M", date(2026, 3, 10), 20), (ORDER_NO, "SK-A", date(2026, 4, 10), 5),
    ]), name="mar.xlsx")
    by_month = _by_month(preview)
    feb, mar, apr = by_month["2026-02-01"], by_month["2026-03-01"], by_month["2026-04-01"]
    assert "cumulative_unverifiable" in _codes(feb)
    for row in (mar, apr):
        assert row["row_status"] == "blocked" and row["match_state"] == "invalid"
        assert row["after"]["cumulative_amount"] is None
        blocked = next(e for e in row["errors"] if e["code"] == "constituent_blocked")
        assert "2026-02" in blocked["message"]
    assert _default_tickable(preview) == []
    assert preview["can_apply"] is False and preview["summary"]["ready"] == 0
    assert {row.receipt_no for row in _ledger(db)} == {"SK-1"}

    # 2 月完整导出到了：2 月只登记入台账、3 月才成立
    later = _preview(db, _receipt_xlsx([
        (ORDER_NO, "SK-F", date(2026, 2, 5), 10), (ORDER_NO, "SK-M", date(2026, 3, 10), 20),
    ]), name="feb.xlsx")
    by_month = _by_month(later)
    assert by_month["2026-02-01"]["action"] == "record_receipts"
    assert by_month["2026-03-01"]["after"]["cumulative_amount"] == "130.00"


# ---------- B3 未确认快照是历史 ----------

def test_b3_unconfirmed_history_requires_seed(db):
    """probe test_k：未确认的 1 月 500 / 2 月 600 与文件 3 月 100。旧口径：未确认既不是
    基线也不触发建账，3 月按 100 新建（历史消失）；混合场景按 600 当基线。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 1, 1), "500.00", status="unconfirmed")
    _legacy(db, project, contract, date(2026, 2, 1), "600.00", status="unconfirmed")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-M", date(2026, 3, 10), 100)]))
    assert preview["can_apply"] is False and preview["summary"]["ready"] == 0
    for row in _by_month(preview).values():
        assert row["row_status"] == "blocked" and "seed_required" in _codes(row)
        assert row["after"]["cumulative_amount"] is None
    assert any("未确认快照" in hint for hint in _by_month(preview)["2026-01-01"]["hint_messages"])
    assert _ledger(db) == []
    assert _amounts(db, contract) == {
        "2026-01-01": ("500.00", "unconfirmed"), "2026-02-01": ("600.00", "unconfirmed"),
    }

    project2, contract2 = _project_with_contract(db, contract_no="XSDD-20240101-0002")
    _legacy(db, project2, contract2, date(2026, 1, 1), "500.00")
    _legacy(db, project2, contract2, date(2026, 2, 1), "600.00", status="unconfirmed")
    mixed = _preview(db, _receipt_xlsx([("XSDD-20240101-0002", "SK-M", date(2026, 3, 10), 100)]), name="k2.xlsx")
    assert mixed["can_apply"] is False
    by_month = _by_month(mixed)
    assert "2026-01-01" not in by_month  # 已确认的 1 月是基线，不动
    assert "seed_required" in _codes(by_month["2026-02-01"])
    assert "seed_required" in _codes(by_month["2026-03-01"])
    assert date(2026, 3, 1) not in _snapshots(db, contract2)


def test_b3_unconfirmed_month_overwrite_wording_says_unconfirmed(db):
    """probe test_e：未确认的 1 月 100 与文件 1 月 100 相等 ⇒ update 行把它确认掉，
    覆盖文案写"未确认累计"而不是"已确认累计"；有台账支撑的未确认月不等同样是
    显式覆盖，不误判为不可核实。"""
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 1, 1), "100.00", status="unconfirmed")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    jan = _by_month(preview)["2026-01-01"]
    assert jan["action"] == "update_collection_snapshot" and jan["requires_confirmation"] is True
    assert jan["before"]["status"] == "unconfirmed"
    overwrite = next(w for w in jan["warnings"] if w["code"] == "snapshot_overwrite")
    assert overwrite["message"].startswith("将覆盖 2026-01 未确认累计 100.00 → 100.00")
    assert "已确认累计" not in overwrite["message"]
    _apply(db, preview, [jan["row_key"]])
    assert _amounts(db, contract)["2026-01-01"] == ("100.00", "confirmed")
    assert {row.receipt_no for row in _ledger(db)} == {"SK-1"}

    # 人把 1 月改成未确认 90：台账有 1 月导入行支撑 ⇒ 覆盖行，文案"未确认累计 90.00 → 100.00"
    snapshot = _snapshots(db, contract)[date(2026, 1, 1)]
    operations.update_collection(
        db, collection_id=snapshot.collection_id, version=snapshot.version,
        updates={"status": "unconfirmed", "cumulative_amount": Decimal("90.00")},
        reason="复核中", operated_by="mgr",
    )
    db.commit()
    again = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-2", date(2026, 2, 5), 50)]), name="feb.xlsx")
    jan = _by_month(again)["2026-01-01"]
    assert jan["action"] == "update_collection_snapshot"
    assert "cumulative_unverifiable" not in _codes(jan)
    overwrite = next(w for w in jan["warnings"] if w["code"] == "snapshot_overwrite")
    assert overwrite["message"].startswith("将覆盖 2026-01 未确认累计 90.00 → 100.00")


# ---------- B4 上游作废 ----------

def test_b4_upstream_voided_receipt_offers_ruling_and_reimports_after_ruling_to_zero(db):
    """probe test_m：SK-1 入账后源系统把它作废，导出带「作废」行。旧口径：invalid_receipt
    整单 fail-closed，裁决后仍然无效——合同永远导不进去。"""
    project, contract = _project_with_contract(db)
    first = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    _apply(db, first, _ready_keys(first))
    export = _status_xlsx([
        (ORDER_NO, "SK-1", date(2026, 1, 10), 100, "作废"),
        (ORDER_NO, "SK-2", date(2026, 2, 5), 50, "已生效"),
    ])

    blocked = _preview(db, export, name="v.xlsx")
    assert blocked["can_apply"] is False and blocked["summary"]["receipt_conflicts"] == 1
    voided = next(row for row in blocked["rows"] if row["canonical"].get("receipt_no") == "SK-1")
    assert voided["row_status"] == "blocked" and voided["match_state"] == "invalid"
    assert "receipt_voided_upstream" in _codes(voided) and "invalid_receipt" not in _codes(voided)
    assert voided["canonical"]["actual_amount"] == "100.00"
    assert voided["before"] == {
        "receipt_no": "SK-1", "receipt_date": "2026-01-10", "actual_amount": "100.00",
        "import_batch_id": int(first["preview_id"]),
    }
    assert any("receipt-rulings" in hint and "裁为 0" in hint for hint in voided["hint_messages"])
    assert "2026-01-01" not in _by_month(blocked)  # 作废行不冻结一个 1 月行
    assert all(
        "order_level_fail_closed" in _codes(row) for row in blocked["rows"]
    )

    ruling = bulk.rule_receipt(
        db, contract_no=ORDER_NO, receipt_no="SK-1", receipt_date=date(2026, 1, 10),
        actual_amount="0", reason="上游作废", operated_by="boss",
    )
    db.commit()
    assert ruling["affected_months"] == [
        {"report_month": "2026-01-01", "current_cumulative": "100.00", "derived_cumulative": "0.00"},
    ]

    again = _preview(db, export, name="v2.xlsx")
    assert again["can_apply"] is True and again["summary"]["known"] == 1
    known = next(row for row in again["rows"] if row["canonical"].get("receipt_no") == "SK-1")
    assert known["row_status"] == "unchanged" and "receipt_known" in _codes(known)
    by_month = _by_month(again)
    jan, feb = by_month["2026-01-01"], by_month["2026-02-01"]
    assert jan["action"] == "update_collection_snapshot" and jan["requires_confirmation"] is True
    assert (jan["before"]["cumulative_amount"], jan["after"]["cumulative_amount"]) == ("100.00", "0.00")
    assert feb["action"] == "upsert_collection_snapshot" and feb["after"]["cumulative_amount"] == "50.00"
    assert feb["depends_on_row_keys"] == [jan["row_key"]]
    _apply(db, again, [jan["row_key"], feb["row_key"]])
    assert _amounts(db, contract) == {
        "2026-01-01": ("0.00", "confirmed"), "2026-02-01": ("50.00", "confirmed"),
    }
    assert [(r.receipt_no, str(r.actual_amount), r.is_active) for r in _ledger(db)] == [
        ("SK-1", "100.00", False), ("SK-1", "0.00", True), ("SK-2", "50.00", True),
    ]


def test_b4_voided_receipt_unknown_to_ledger_stays_invalid(db):
    """守卫（改动前后同样成立）：台账从未见过的作废 / 空状态行仍判无效，fail-closed 不放松。"""
    _project_with_contract(db)
    preview = _preview(db, _status_xlsx([
        (ORDER_NO, "SK-9", date(2026, 1, 10), 100, "作废"),
        (ORDER_NO, "SK-2", date(2026, 2, 5), 50, "已生效"),
    ]))
    invalid = next(row for row in preview["rows"] if row["source_row"] == 2 and row["errors"])
    assert "invalid_receipt" in _codes(invalid) and "receipt_voided_upstream" not in _codes(invalid)
    assert preview["can_apply"] is False and _ledger(db) == []


# ---------- B5 record_receipts 带真实 code ----------

def test_b5_record_receipts_row_carries_info_code(db):
    project, contract = _project_with_contract(db)
    _legacy(db, project, contract, date(2026, 1, 1), "100.00")
    preview = _preview(db, _receipt_xlsx([(ORDER_NO, "SK-1", date(2026, 1, 10), 100)]))
    jan = _by_month(preview)["2026-01-01"]
    assert jan["action"] == "record_receipts" and jan["row_status"] == "ready"
    info = next(w for w in jan["warnings"] if w["code"] == "record_receipts")
    assert info["message"] == "累计不变，只把 1 笔新收款登记入台账"
    assert not jan["errors"]
