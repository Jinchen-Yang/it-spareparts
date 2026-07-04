"""批量规范化（WP3）：按销售额排序 + 改动检测 + 批量应用并锁定 + 幂等 + 尊重锁定。"""
from datetime import date

import pytest
from sqlalchemy import select, update

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import batch_normalize as B
from app.services import master_edit
from tests import factories as f


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="sales", file_hash="h-bn")
    db.add(b)
    db.flush()
    so = {"S1": f.sales_head("S1", on=date.today())}
    sl = [
        f.sales_line("S1", "L1", "ST8000NM001A", qty="1", price="10000",
                     description="8TB 12Gbps 7200转 256MB 3.5inch SAS HDD"),   # 高价值
        f.sales_line("S1", "L2", "AL15SEB18EQ", qty="1", price="100",
                     description="1.8TB 12Gbps 10K 128MB Cache 2.5inch SAS HDD"),  # 低价值
    ]
    loader.load(db, f.sales_result(so, sl), b.id, date.today())
    db.execute(update(DimPart).where(DimPart.machine_or_part.is_(None)).values(machine_or_part="备件"))
    db.commit()
    return db


def test_preview_orders_by_value_and_flags_changes(seeded):
    res = B.preview(seeded, page=1, page_size=20)
    items = res["items"]
    assert len(items) == 2
    assert items[0]["pn_std"] == "ST8000NM001A"          # 高销售额排前
    assert items[0]["recent_sales_amount"] == 10000.0
    assert "description" in items[0]["changes"]           # 描述会被规范化
    assert items[0]["suggestion"]["canonical_description"] == "8TB SAS HDD 12Gb 7.2K 3.5"
    assert items[0]["suggestion"]["category_l2"] == "SAS-HDD-3.5"


def test_apply_batch_normalizes_and_locks(seeded):
    ids = [p.id for p in seeded.scalars(select(DimPart)).all()]
    res = B.apply_batch(seeded, ids, fields=None, operated_by="cui")
    assert res["applied"] == 2
    seeded.expire_all()
    st = seeded.scalar(select(DimPart).where(DimPart.pn_std == "ST8000NM001A"))
    assert st.description == "8TB SAS HDD 12Gb 7.2K 3.5"
    assert st.category_major == "硬盘" and st.category_minor == "SAS-HDD-3.5"
    assert {"description", "category_major"} <= set(st.locked_fields)   # 锁定防重导覆盖


def test_apply_is_idempotent(seeded):
    ids = [p.id for p in seeded.scalars(select(DimPart)).all()]
    B.apply_batch(seeded, ids, fields=None, operated_by="cui")
    seeded.expire_all()
    res2 = B.apply_batch(seeded, ids, fields=None, operated_by="cui")
    assert res2["applied"] == 0 and res2["skipped"] == 2   # 已规范 → 无改动


def test_preview_respects_locked_fields(seeded):
    # 人工把 ST 的描述锁定成自定义值 → 预览不得再建议改它的描述
    master_edit.edit_part(seeded, pn_std="ST8000NM001A",
                          updates={"description": "采购自定义描述"}, operated_by="cui")
    seeded.expire_all()
    res = B.preview(seeded, page=1, page_size=20)
    st = next(i for i in res["items"] if i["pn_std"] == "ST8000NM001A") if any(
        i["pn_std"] == "ST8000NM001A" for i in res["items"]) else None
    if st is not None:
        assert "description" not in st["changes"]          # 锁定字段不在建议改动里


def test_apply_batch_skips_review_required(db):
    """§17：REVIEW_REQUIRED 项（缺关键字段，如硬盘无接口）即便有品牌建议也不批量写回，交单条人工。"""
    master_edit.create_part(db, pn_std="REV-HDD-1", description="某硬盘 1TB",
                            brand="希捷（Seagate）", machine_or_part="备件",
                            force=True, operated_by="cui")
    db.commit()
    pid = db.scalar(select(DimPart.id).where(DimPart.pn_std == "REV-HDD-1"))
    res = B.apply_batch(db, [pid], fields=None, operated_by="cui")
    assert res["applied"] == 0 and res["skipped"] == 1   # REVIEW → 不写回
