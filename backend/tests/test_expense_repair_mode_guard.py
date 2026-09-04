"""修复模式（以本表为准）遇 missing_link 的门禁与作废豁免（2026-09-04 客户事故）。

客户上传 3,803 行全公司支付单、选修复模式，其中 1,873 行是公司日常开销（办公/
财务/仓储/售前），业务上本就不挂任何销售订单 → 全判 missing_link → 整批拒绝、
0 行入库，另外约 1,900 行有效费用被连坐。

放行 missing_link 本身不足以安全：重建范围是**合同粒度**的，被丢弃的行会落进
别人贡献的合同范围里，走 loader 的 missing_ids 被软作废——文件里它还在，系统当
它消失了，钱从项目卡片上消失而批次报 success。故安全性由 loader 的作废豁免
（_expense_void_exemptions）承担，门禁只负责不再连坐。本文件同时钉住两侧。
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.etl import mapping, pipeline
from app.etl.reader import ReaderError
from app.etl.transform import transform
from app.models.maintenance import FProjectExpense

_ANCHORED = ["报销日期", "报销人员", "报销类别", "费用分类", "支出事由",
             "报销金额", "流程状态", "单号", "序号"]
_ROWWISE = _ANCHORED + ["销售订单"]


def _anchored_sheet(tmp_path, name, rows, anchor="XSDD-SILENT-001"):
    """T1：项目追踪工作簿报销页——页级锚把 XSDD 无差别盖在每一行上。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "报销明细"
    ws.append(["销售订单", anchor])
    ws.append(_ANCHORED)
    for r in rows:
        ws.append([r.get(c) for c in _ANCHORED])
    p = tmp_path / name
    wb.save(str(p))
    return str(p)


def _rowwise_sheet(tmp_path, name, rows):
    """T2：全公司支付单导出——逐行「销售订单」列，公司开销行该列留空。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(_ROWWISE)
    for r in rows:
        ws.append([r.get(c) for c in _ROWWISE])
    p = tmp_path / name
    wb.save(str(p))
    return str(p)


def _row(d="2026-05-01", person="张三", amount=100, reason="外援", bxd=None,
         seq=None, fee="外援劳务", xsdd=None):
    return {"报销日期": d, "报销人员": person, "报销类别": "维保费用",
            "费用分类": fee, "支出事由": reason, "报销金额": amount,
            "流程状态": "已结束", "单号": bxd, "序号": seq, "销售订单": xsdd}


# ---------- 门禁：只放行 missing_link ----------

def test_upsert_no_longer_blocked_by_missing_link_alone(db, tmp_path):
    """客户那一批的形状：项目行有 XSDD、公司开销行留空 → 不再连坐整批拒绝。"""
    p = _rowwise_sheet(tmp_path, "pay1.xlsx", [
        _row(amount=1000, reason="项目备件", bxd="B1", seq=1, xsdd="XSDD-A"),
        _row(amount=578, reason="办公用品", bxd="B2", seq=1, fee="办公费用"),
        _row(amount=357, reason="手续费", bxd="B3", seq=1, fee="财务费用"),
    ])
    batch = pipeline.run_import(db, p, "pay1.xlsx", mode="upsert")
    db.commit()

    assert batch.status == "success"
    assert batch.rows_inserted == 1
    assert batch.rows_error == 2
    report = batch.report_json
    assert report["expense_rows_dropped_no_contract"] == 2
    assert report["expense_rows_voided"] == 0
    # 有效行照常入库，公司开销行不入库也不牵连
    assert db.scalars(select(FProjectExpense.amount)).all() == [Decimal("1000")]


def test_upsert_still_blocked_by_other_error_types(db, tmp_path):
    """bad_number 等 9 类错误在 xsdd 求值之前就 continue，可能带着完整 XSDD，
    error_type 对「属不属于某个被重建的合同」零信息量 → 一律仍拦。"""
    ok = _rowwise_sheet(tmp_path, "cl0.xlsx",
                        [_row(amount=100, bxd="B1", seq=1, xsdd="XSDD-A")])
    pipeline.run_import(db, ok, "cl0.xlsx")
    db.commit()

    bad = _rowwise_sheet(tmp_path, "cl1.xlsx", [
        _row(amount=150, bxd="B1", seq=1, xsdd="XSDD-A"),
        _row(amount="非数字", reason="坏金额", bxd="B9", seq=1, xsdd="XSDD-A"),
    ])
    with pytest.raises(ReaderError, match="修复模式"):
        pipeline.run_import(db, bad, "cl1.xlsx", mode="upsert")
    db.commit()
    assert db.scalars(select(FProjectExpense.amount)).all() == [Decimal("100")]


def test_mixed_errors_still_blocked_and_count_excludes_missing_link(db, tmp_path):
    """混合错误仍整批拒绝，且提示的行数只数真正拦批的那些（不含 missing_link）。"""
    bad = _rowwise_sheet(tmp_path, "mix.xlsx", [
        _row(amount=150, bxd="B1", seq=1, xsdd="XSDD-A"),
        _row(amount=99, reason="办公用品"),                       # missing_link
        _row(amount="非数字", reason="坏金额", bxd="B9", seq=1, xsdd="XSDD-A"),
    ])
    with pytest.raises(ReaderError, match="发现 1 行错误"):
        pipeline.run_import(db, bad, "mix.xlsx", mode="upsert")
    db.commit()


def test_duplicate_key_still_blocks_upsert(db, tmp_path):
    """撞键行比漏入库更危险：第一行已进 lines 并会 UPDATE 掉库里的记录。"""
    bad = _rowwise_sheet(tmp_path, "dup.xlsx", [
        _row(amount=100, bxd="B1", seq=1, xsdd="XSDD-A"),
        _row(amount=200, reason="重复", bxd="B1", seq=1, xsdd="XSDD-A"),
    ])
    with pytest.raises(ReaderError, match="修复模式"):
        pipeline.run_import(db, bad, "dup.xlsx", mode="upsert")
    db.commit()


# ---------- 作废豁免：静默丢账的完整时序 ----------

def test_anchored_overhead_row_is_not_silently_voided_by_rowwise_reimport(db, tmp_path):
    """T1 锚导入（开销行被无差别打上 XSDD）→ T2 逐行导出（该行 XSDD 留空）+ 修复模式。

    没有豁免时：开销行本次被丢弃 → 它的旧行落进 missing_ids → 软作废 → 归因
    void → 卡片成本少一笔，批次却报 success。这是 refute 阶段实跑出来的时序。
    """
    t1 = _anchored_sheet(tmp_path, "t1.xlsx", [
        _row(d="2026-05-01", amount=5000, reason="项目备件", bxd="BXD-1", seq=1),
        _row(d="2026-05-02", person="王五", amount=3000, reason="办公用品打印纸",
             bxd="BXD-9", seq=1, fee="办公费用"),
    ])
    pipeline.run_import(db, t1, "t1.xlsx")
    db.commit()
    before = {r.raw_line_id: (r.data_status, r.amount)
              for r in db.scalars(select(FProjectExpense))}
    assert len(before) == 2 and all(s == "已结束" for s, _ in before.values())

    t2 = _rowwise_sheet(tmp_path, "t2.xlsx", [
        _row(d="2026-05-01", amount=5000, reason="项目备件", bxd="BXD-1", seq=1,
             xsdd="XSDD-SILENT-001"),
        # 同一笔开销，这次按业务口径不挂合同 → missing_link 被丢弃
        _row(d="2026-05-02", person="王五", amount=3000, reason="办公用品打印纸",
             bxd="BXD-9", seq=1, fee="办公费用"),
    ])
    batch = pipeline.run_import(db, t2, "t2.xlsx", mode="upsert")
    db.commit()

    assert batch.status == "success"
    report = batch.report_json
    assert report["expense_rows_voided"] == 0, "旧行被静默作废＝丢账"
    assert report["expense_rows_void_protected"] == 1
    after = {r.raw_line_id: (r.data_status, r.amount)
             for r in db.scalars(select(FProjectExpense))}
    assert all(s == "已结束" for s, _ in after.values())
    assert sum(a for _, a in after.values()) == Decimal("8000")


def test_genuinely_deleted_row_is_still_voided(db, tmp_path):
    """负面对照：真的从文件里消失的行照常作废——豁免没有把修复模式废掉。"""
    t1 = _rowwise_sheet(tmp_path, "d1.xlsx", [
        _row(amount=5000, reason="保留", bxd="BXD-1", seq=1, xsdd="XSDD-D"),
        _row(amount=3000, reason="要删掉的", bxd="BXD-2", seq=1, xsdd="XSDD-D"),
    ])
    pipeline.run_import(db, t1, "d1.xlsx")
    db.commit()

    t2 = _rowwise_sheet(tmp_path, "d2.xlsx", [
        _row(amount=5000, reason="保留", bxd="BXD-1", seq=1, xsdd="XSDD-D"),
    ])
    batch = pipeline.run_import(db, t2, "d2.xlsx", mode="upsert")
    db.commit()

    assert batch.report_json["expense_rows_voided"] == 1
    assert batch.report_json["expense_rows_void_protected"] == 0
    voided = db.scalars(
        select(FProjectExpense).where(FProjectExpense.reason == "要删掉的")).one()
    assert voided.data_status == "已作废"


def test_void_suppressed_even_when_dropped_row_matches_nothing(db, tmp_path):
    """Codex P1（2026-09-04）：曾用三族身份比对逐行豁免，弱签名不匹配时假阴性。

    源系统里改一次金额，签名就对不上 → 不豁免、也不计「身份不可得」→ 旧行照样
    被作废。这不止于弱签名：旧行可能是在源表还没有「数据ID」列的年代按内容键入
    库的，连最强一族也匹配不上。**任何一次不匹配都不构成「旧行真的消失了」的
    证明**，故改为「本表有行被排除 ⇒ 整批不作废」。本例的被排除行与库里任何旧行
    都对不上（金额被改过），旧行仍必须活着。
    """
    t1 = _anchored_sheet(tmp_path, "p1.xlsx", [
        _row(d="2026-06-01", amount=700, reason="项目差旅"),
        _row(d="2026-06-02", person="赵六", amount=420, reason="仓库租金",
             fee="仓储费用"),
    ], anchor="XSDD-P1")
    pipeline.run_import(db, t1, "p1.xlsx")
    db.commit()

    t2 = _rowwise_sheet(tmp_path, "p2.xlsx", [
        _row(d="2026-06-01", amount=700, reason="项目差旅", xsdd="XSDD-P1"),
        # 同一笔仓库租金，金额在源系统里被更正过 → 与旧行签名对不上
        _row(d="2026-06-02", person="赵六", amount=999, reason="仓库租金",
             fee="仓储费用"),
    ])
    batch = pipeline.run_import(db, t2, "p2.xlsx", mode="upsert")
    db.commit()

    assert batch.report_json["expense_rows_voided"] == 0
    assert batch.report_json["expense_rows_void_protected"] == 1
    assert all(r.data_status == "已结束"
               for r in db.scalars(select(FProjectExpense)))
    assert sum(r.amount for r in db.scalars(select(FProjectExpense))) == Decimal("1120")


def test_void_suppressed_when_dropped_row_has_no_bxd(db, tmp_path):
    """无单号/序号的形态同样抑制作废（此前靠内容签名比对，现在不依赖比对）。"""
    t1 = _anchored_sheet(tmp_path, "c1.xlsx", [
        _row(d="2026-06-01", amount=700, reason="项目差旅"),
        _row(d="2026-06-02", person="赵六", amount=420, reason="仓库租金",
             fee="仓储费用"),
    ], anchor="XSDD-C")
    pipeline.run_import(db, t1, "c1.xlsx")
    db.commit()

    t2 = _rowwise_sheet(tmp_path, "c2.xlsx", [
        _row(d="2026-06-01", amount=700, reason="项目差旅", xsdd="XSDD-C"),
        _row(d="2026-06-02", person="赵六", amount=420, reason="仓库租金",
             fee="仓储费用"),
    ])
    batch = pipeline.run_import(db, t2, "c2.xlsx", mode="upsert")
    db.commit()

    assert batch.report_json["expense_rows_voided"] == 0
    assert batch.report_json["expense_rows_void_protected"] == 1
    assert all(r.data_status == "已结束"
               for r in db.scalars(select(FProjectExpense)))


# ---------- 身份留痕（豁免的前提） ----------

def test_missing_link_error_carries_normalized_identity():
    """身份提取早于 missing_link 判定：日期/金额/人员/事由都已归一，且金额恒非空
    （无金额行走 rows_skipped_no_data、无日期行走 missing_date）。"""
    df = pd.DataFrame([
        {"报销日期": "2026-05-02", "报销人员": "王五", "支出事由": "办公用品打印纸",
         "报销金额": 3000, "单号": "BXD-9", "序号": 1},
    ])
    res = transform(df, mapping.EXPENSE)
    assert [e.error_type for e in res.errors] == ["missing_link"]
    ident = res.errors[0].identity
    assert ident["expense_date"] == date(2026, 5, 2)
    assert ident["amount"] == Decimal("3000")
    assert ident["person"] == "王五" and ident["reason"] == "办公用品打印纸"
    assert ident["bxd_no"] == "BXD-9" and ident["line_no"] == 1
    # 金额必须出现在 detail 里：batch_detail 与 errors.csv 都只回 detail
    assert "¥3000.00" in res.errors[0].error_detail


def test_continuation_row_identity_inherits_head_fields():
    """延续行的 raw_row 只剩金额（_row_dict 读本行原始单元格），identity 走 gvh
    继承头行——否则豁免在一单多行形态下退化为「永远零命中」＝假安全。"""
    df = pd.DataFrame([
        {"报销日期": "2026-05-29", "费用单号": "BXD-0030", "报销人员": "李呈辉",
         "支出事由": "办公用品", "流程状态": "已结束", "报销金额": 770},
        {"报销金额": 2448},                              # 延续行：头级全空
    ])
    res = transform(df, mapping.EXPENSE)
    assert [e.error_type for e in res.errors] == ["missing_link", "missing_link"]
    cont = res.errors[1]
    assert cont.raw_row.get("person") is None          # raw_row 确实读空
    assert cont.identity["person"] == "李呈辉"
    assert cont.identity["expense_date"] == date(2026, 5, 29)
    assert cont.identity["amount"] == Decimal("2448")


def test_identity_is_persisted_into_import_error_rows(db, tmp_path):
    """留痕落库：raw_row._identity 带归一后的金额，宽松列形态下也不再是 null。"""
    from app.models.system import SysImportError

    p = _rowwise_sheet(tmp_path, "e1.xlsx",
                       [_row(amount=578, reason="办公用品", fee="办公费用")])
    batch = pipeline.run_import(db, p, "e1.xlsx")
    db.commit()
    err = db.scalars(
        select(SysImportError).where(SysImportError.batch_id == batch.id)).one()
    assert err.error_type == "missing_link"
    assert err.raw_row["_identity"]["amount"] == "578.00"
    assert err.raw_row["_identity"]["reason"] == "办公用品"


def test_real_deletion_is_held_back_when_the_sheet_also_drops_rows(db, tmp_path):
    """代价说明：本表既有真删除、又有被排除行时，删除侧整批不生效并计数上报。

    这是刻意的——本表不完整就不能代表「以本表为准」的删除侧。补齐销售订单后
    重导即可让删除生效；同键覆盖（改金额）不受影响。"""
    t1 = _rowwise_sheet(tmp_path, "u1.xlsx", [
        _row(amount=5000, reason="保留", bxd="BXD-1", seq=1, xsdd="XSDD-U"),
        _row(amount=3000, reason="真的删掉了", bxd="BXD-2", seq=1, xsdd="XSDD-U"),
    ])
    pipeline.run_import(db, t1, "u1.xlsx")
    db.commit()

    t2 = _rowwise_sheet(tmp_path, "u2.xlsx", [
        _row(amount=5500, reason="保留", bxd="BXD-1", seq=1, xsdd="XSDD-U"),
        _row(amount=99, reason="办公用品"),                       # missing_link
    ])
    batch = pipeline.run_import(db, t2, "u2.xlsx", mode="upsert")
    db.commit()

    assert batch.report_json["expense_rows_voided"] == 0
    assert batch.report_json["expense_rows_void_protected"] == 1
    assert batch.report_json["expense_rows_dropped_no_contract"] == 1
    rows = {r.reason: (r.data_status, r.amount)
            for r in db.scalars(select(FProjectExpense))}
    assert rows["真的删掉了"][0] == "已结束"        # 删除侧未生效
    assert rows["保留"] == ("已结束", Decimal("5500"))   # 同键覆盖照常生效
