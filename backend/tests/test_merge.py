"""合并服务：repoint/别名UPSERT/路径压缩/前置校验/日志（整改 P4 核心回归）。"""
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.inventory import PartSubstitute
from app.models.master_data import ProductMergeLog
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
