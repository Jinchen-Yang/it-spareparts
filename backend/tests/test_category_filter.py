"""按品类查询（宋总 2026-07-05）：自动分类回填（尊重人工/锁定）+ 搜索品类过滤 + 品类字典。"""
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.services import master_data, part_overview


def _p(db, pn, desc, brand=None, ma=None, mi=None, source=None, locked=None):
    p = DimPart(pn_std=pn, description=desc, brand=brand, category_major=ma,
                category_minor=mi, category_source=source, locked_fields=locked or [])
    db.add(p)
    db.flush()
    return p


def test_classify_backfill_fills_clean_categories(db):
    _p(db, "PN-HDD", "Seagate 6TB SATA HDD 6Gb 7.2K 3.5")      # 未分类 → 应打「硬盘」
    _p(db, "PN-MEM", "32GB 2666V 2Rx4 DDR4")                    # → 内存
    _p(db, "PN-GPU", "NVIDIA A100 80GB HBM2e PCIe GPU")        # → 卡/显卡GPU
    # 脏销售类目但本身是备件（非人工来源）→ 应被干净分类覆盖
    _p(db, "PN-JUNK", "华为 硬盘 4TB 3.5 SAS", ma="磁盘存储", source="IMPORT")
    db.commit()

    res = master_data.classify_backfill(db)
    assert res["parts_reclassified"] >= 4

    def cat(pn):
        r = db.scalar(select(DimPart).where(DimPart.pn_std == pn))
        return (r.category_major, r.category_minor, r.category_source)

    assert cat("PN-HDD")[0] == "硬盘" and cat("PN-HDD")[2] == "AUTO"
    assert cat("PN-MEM")[0] == "内存"
    assert cat("PN-GPU") == ("卡", "显卡GPU", "AUTO")
    # 脏的销售类目被干净分类覆盖（非人工来源）
    assert cat("PN-JUNK")[0] == "硬盘"


def test_classify_backfill_respects_manual_and_locks(db):
    _p(db, "PN-M1", "Seagate 6TB SATA HDD 6Gb 7.2K 3.5", ma="我的自定义类", source="MANUAL")
    _p(db, "PN-M2", "32GB 2666V 2Rx4 DDR4", ma="锁定类", locked=["category_major"])
    db.commit()
    master_data.classify_backfill(db)
    m1 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-M1"))
    m2 = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-M2"))
    assert m1.category_major == "我的自定义类"      # MANUAL 不动
    assert m2.category_major == "锁定类"            # 锁定字段不动


def test_classify_backfill_idempotent(db):
    _p(db, "PN-X", "NVIDIA A100 80GB HBM2e PCIe GPU")
    db.commit()
    master_data.classify_backfill(db)
    second = master_data.classify_backfill(db)
    assert second["parts_reclassified"] == 0        # 已是该值 → 二次零改动


def test_search_filter_by_category(db):
    _p(db, "PN-A", "Seagate 6TB SATA HDD", ma="硬盘", mi="SATA-HDD-3.5")
    _p(db, "PN-B", "WD 8TB SATA HDD", ma="硬盘", mi="SATA-HDD-3.5")
    _p(db, "PN-C", "NVIDIA A100 GPU", ma="卡", mi="显卡GPU")
    db.commit()
    # 只按一级品类「硬盘」
    r = part_overview.search_parts(db, None, 1, 20, category_major="硬盘")
    assert {it["pn_std"] for it in r["items"]} == {"PN-A", "PN-B"}
    # 一级「卡」+二级「显卡GPU」
    r2 = part_overview.search_parts(db, None, 1, 20, category_major="卡", category_minor="显卡GPU")
    assert {it["pn_std"] for it in r2["items"]} == {"PN-C"}
    # 品类 + 文本词联合过滤
    r3 = part_overview.search_parts(db, "8TB", 1, 20, category_major="硬盘")
    assert {it["pn_std"] for it in r3["items"]} == {"PN-B"}


def test_categories_dict_from_present_data(db):
    _p(db, "PN-A", "d", ma="硬盘", mi="SATA-HDD-3.5")
    _p(db, "PN-B", "d", ma="硬盘", mi="SAS-HDD-2.5")
    _p(db, "PN-C", "d", ma="卡", mi="显卡GPU")
    _p(db, "PN-N", "d")                               # 未分类不进字典
    db.commit()
    rows = db.execute(
        select(DimPart.category_major, DimPart.category_minor)
        .where(DimPart.status == "active", DimPart.category_major.is_not(None)).distinct()
    ).all()
    tree = {}
    for ma, mi in rows:
        tree.setdefault(ma, set()).add(mi)
    assert set(tree) == {"硬盘", "卡"}
    assert tree["硬盘"] == {"SATA-HDD-3.5", "SAS-HDD-2.5"}
