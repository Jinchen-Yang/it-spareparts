"""合并服务：repoint/别名UPSERT/路径压缩/前置校验/日志（整改 P4 核心回归）。"""
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.inventory import PartSubstitute
from app.models.master_data import ProductMergeLog
from app.models.purchase import FPurchaseLine
from app.models.sales import FSalesLine
from app.models.system import SysImportBatch
from app.services import merge, substitute
from tests import factories as f


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="h2")
    db.add(b)
    db.flush()
    orders = {"O1": f.purchase_head("O1", on=date(2026, 1, 10))}
    lines = [f.purchase_line("O1", "L1", "PN-A", qty="2", price="50", description="甲"),
             f.purchase_line("O1", "L2", "PN-B", qty="3", price="60")]
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))
    so = {"S1": f.sales_head("S1", on=date(2026, 3, 1))}
    sl = [f.sales_line("S1", "SL1", "PN-A", qty="1", price="100")]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit()
    return b


def _part(db, pn):
    return db.scalar(select(DimPart).where(DimPart.pn_std == pn))


def test_merge_repoints_and_logs(db, seeded):
    a, b = _part(db, "PN-A"), _part(db, "PN-B")
    result = merge.merge_parts(db, "PN-A", "PN-B", "同款", "tester")

    assert result["affected_counts"]["f_purchase_line_ids"] == 1
    assert result["affected_counts"]["f_sales_line_ids"] == 1
    db.refresh(a), db.refresh(b)
    assert a.status == "merged" and a.merged_into_id == b.id
    assert b.status == "active"
    assert b.description == "甲", "目标缺描述时从源补"

    # 恒等别名 UPSERT：PN-A 的别名现在指向 PN-B
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "PN-A"))
    assert alias.pn_std == "PN-B" and alias.part_id == b.id and alias.status == "active"

    log = db.scalar(select(ProductMergeLog))
    assert log.source_part_id == a.id and log.target_part_id == b.id
    assert log.source_snapshot["pn_std"] == "PN-A"
    assert log.affected_records["f_sales_line_ids"], "回滚依据：受影响行 id 必须留档"


def test_merge_precondition_rejections(db, seeded):
    merge.merge_parts(db, "PN-A", "PN-B", None, "t")
    with pytest.raises(merge.MergeError):     # 源已是墓碑
        merge.merge_parts(db, "PN-A", "PN-B", None, "t")
    with pytest.raises(merge.MergeError):     # 目标是墓碑
        merge.merge_parts(db, "PN-B", "PN-A", None, "t")
    with pytest.raises(merge.MergeError):     # 自合并
        merge.merge_parts(db, "PN-B", "PN-B", None, "t")
    with pytest.raises(merge.MergeError):     # 不存在
        merge.merge_parts(db, "PN-B", "PN-NOPE", None, "t")


def test_merge_path_compression(db, seeded):
    bt = SysImportBatch(filename="t2.xlsx", file_type="purchase", file_hash="h3")
    db.add(bt)
    db.flush()
    loader.load(db, f.purchase_result({"O9": f.purchase_head("O9")},
                                      [f.purchase_line("O9", "L9", "PN-C")]),
                bt.id, date(2026, 6, 1))
    db.commit()
    merge.merge_parts(db, "PN-A", "PN-B", None, "t")   # A → B
    merge.merge_parts(db, "PN-B", "PN-C", None, "t")   # B → C，A 应被压缩指向 C
    a, c = _part(db, "PN-A"), _part(db, "PN-C")
    db.refresh(a)
    assert a.merged_into_id == c.id, "路径压缩：链长恒 ≤1"


def test_merge_substitute_self_pair_dropped(db, seeded):
    substitute.add_substitute(db, "PN-A", "PN-B", "互替", "t")
    assert db.scalar(select(PartSubstitute)) is not None
    merge.merge_parts(db, "PN-A", "PN-B", None, "t")
    assert db.scalar(select(PartSubstitute)) is None, "源↔目标互替在合并后是自配对，应删除"


def test_unmerge_round_trip(db, seeded):
    """合并→回滚：事实行 part_id、别名、墓碑状态、目标回补字段全部还原。"""
    a, b = _part(db, "PN-A"), _part(db, "PN-B")
    a_id, b_id = a.id, b.id
    # 合并前快照
    pl_before = {r[0]: r[1] for r in db.execute(
        select(FPurchaseLine.id, FPurchaseLine.part_id)).all()}
    sl_before = {r[0]: r[1] for r in db.execute(
        select(FSalesLine.id, FSalesLine.part_id)).all()}
    assert b.description is None  # 目标缺描述，合并会从源补

    result = merge.merge_parts(db, "PN-A", "PN-B", "测试", "t")
    db.expire_all()
    assert _part(db, "PN-A").status == "merged"
    assert db.get(DimPart, b_id).description == "甲"  # 已回补

    merge.unmerge(db, result["merge_log_id"], "t")
    db.expire_all()

    a2 = db.get(DimPart, a_id)
    assert a2.status == "active" and a2.merged_into_id is None, "源应复活"
    assert db.get(DimPart, b_id).description is None, "目标回补字段应还原为空"
    # 事实行 part_id 完全复原
    pl_after = {r[0]: r[1] for r in db.execute(
        select(FPurchaseLine.id, FPurchaseLine.part_id)).all()}
    sl_after = {r[0]: r[1] for r in db.execute(
        select(FSalesLine.id, FSalesLine.part_id)).all()}
    assert pl_after == pl_before, "采购行 part_id 应逐行还原"
    assert sl_after == sl_before, "销售行 part_id 应逐行还原"
    # 源恒等别名指回源，不残留指向目标（否则再导入会被错误重定向）
    alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "PN-A"))
    assert alias.part_id == a_id and alias.pn_std == "PN-A"


def test_unmerge_restores_dropped_specs_and_substitutes(db, seeded):
    """合并删掉的源规格(含人工/numeric)与替代关系，回滚后无损还原。"""
    from app.models.master_data import ProductSpec
    from app.services import substitute
    from decimal import Decimal

    a, b = _part(db, "PN-A"), _part(db, "PN-B")
    a_id = a.id
    # 源、目标都有 capacity 规格(键冲突→合并时删源)；源还有人工 numeric 规格
    db.add(ProductSpec(part_id=a.id, spec_key="capacity", spec_value="1TB",
                       spec_unit="GB", numeric_value=Decimal("1000"), source="manual"))
    db.add(ProductSpec(part_id=b.id, spec_key="capacity", spec_value="2TB",
                       spec_unit="GB", numeric_value=Decimal("2000"), source="auto"))
    # 源↔目标互替(合并时作自配对删除)，带 type
    substitute.add_substitute(db, "PN-A", "PN-B", "原厂替代", "t",
                              substitute_type="original")
    db.commit()

    r = merge.merge_parts(db, "PN-A", "PN-B", None, "t")
    # 合并后源无规格、无该替代对
    assert db.scalar(select(ProductSpec).where(ProductSpec.part_id == a_id)) is None

    merge.unmerge(db, r["merge_log_id"], "t")
    db.expire_all()
    spec = db.scalar(select(ProductSpec).where(
        ProductSpec.part_id == a_id, ProductSpec.spec_key == "capacity"))
    assert spec is not None and spec.spec_value == "1TB"
    assert spec.numeric_value == Decimal("1000") and spec.source == "manual", "人工/numeric 规格应无损还原"
    sub = db.scalar(select(PartSubstitute))
    assert sub is not None and sub.substitute_type == "original", "替代类型应还原(不丢成 NULL)"


def test_merge_does_not_hijack_third_party_alias(db, seeded):
    """pn_raw 恰等于源 pn_std 但归属第三方 part 的别名，合并不得劫持它。"""
    # 造一个第三方型号 X，并让 'PN-A' 这个写法作为别名指向 X（人工 reassign 可造成）
    bt = SysImportBatch(filename="tx.xlsx", file_type="purchase", file_hash="hX")
    db.add(bt)
    db.flush()
    loader.load(db, f.purchase_result({"OX": f.purchase_head("OX")},
                                      [f.purchase_line("OX", "LX", "PN-X")]),
                bt.id, date(2026, 6, 1))
    db.commit()
    a, x = _part(db, "PN-A"), _part(db, "PN-X")
    # 删掉 A 的恒等别名，腾出 pn_raw='PN-A' 唯一槽，再让它指向 X
    db.execute(PartAlias.__table__.delete().where(PartAlias.pn_raw == "PN-A"))
    db.add(PartAlias(pn_raw="PN-A", pn_std="PN-X", part_id=x.id,
                     source="manual", status="active"))
    db.commit()

    merge.merge_parts(db, "PN-A", "PN-B", None, "t")
    hijacked = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "PN-A"))
    assert hijacked.part_id == x.id and hijacked.pn_std == "PN-X", "第三方别名不得被合并劫持"


def test_unmerge_guards(db, seeded):
    r = merge.merge_parts(db, "PN-A", "PN-B", None, "t")
    merge.unmerge(db, r["merge_log_id"], "t")
    with pytest.raises(merge.MergeError):       # 重复回滚被拦（源已 active）
        merge.unmerge(db, r["merge_log_id"], "t")
    with pytest.raises(merge.MergeError):       # 日志不存在
        merge.unmerge(db, 999999, "t")


def test_unmerge_restores_path_compression(db, seeded):
    """A→B 后 B→C，回滚 B→C：A 应改回指向 B（不是停在 C）。"""
    bt = SysImportBatch(filename="t3.xlsx", file_type="purchase", file_hash="hC")
    db.add(bt)
    db.flush()
    loader.load(db, f.purchase_result({"O9": f.purchase_head("O9")},
                                      [f.purchase_line("O9", "L9", "PN-C")]),
                bt.id, date(2026, 6, 1))
    db.commit()
    a_id = _part(db, "PN-A").id
    merge.merge_parts(db, "PN-A", "PN-B", None, "t")           # A → B
    r2 = merge.merge_parts(db, "PN-B", "PN-C", None, "t")      # B → C，A 压缩到 C
    assert db.get(DimPart, a_id).merged_into_id == _part(db, "PN-C").id
    merge.unmerge(db, r2["merge_log_id"], "t")                 # 回滚 B→C
    db.expire_all()
    b = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-B"))
    assert db.get(DimPart, a_id).merged_into_id == b.id, "A 应改回指向复活的 B"
