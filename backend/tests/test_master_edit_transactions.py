"""D-36 P5-1: 人工入口、事务原子性及真实 PostgreSQL 并发契约。"""
from app.models.dimensions import DimPart
from app.models.master_data import Brand, ProductCategory
from app.services import master_edit


def test_manual_create_and_edit_rebind_dictionaries_and_clear(db):
    created = master_edit.create_part(
        db, pn_std="TX-DICT", brand="Brand A", category_major="硬盘",
        category_minor="SAS", force=True)
    part = db.get(DimPart, created["id"])
    assert part.brand_id is not None
    assert part.category_id is not None
    old_ids = (part.brand_id, part.category_id)

    master_edit.edit_part(db, pn_std=part.pn_std, updates={
        "brand": "Brand B", "category_major": "内存", "category_minor": "DDR4"})
    db.refresh(part)
    assert (part.brand_id, part.category_id) != old_ids
    assert db.get(Brand, part.brand_id).normalized_name == "Brand B"
    category = db.get(ProductCategory, part.category_id)
    assert (category.category_major, category.category_minor) == ("内存", "DDR4")
    master_edit.edit_part(db, pn_std=part.pn_std, updates={
        "brand": "", "category_major": None, "category_minor": None})
    db.refresh(part)
    assert (part.brand, part.brand_id, part.category_id) == (None, None, None)
    assert part.category_source == "MANUAL"
    assert {"brand", "category_major", "category_minor"} <= set(part.locked_fields)


def test_minor_only_edit_rebinds_category_and_keeps_manual_source(db):
    created = master_edit.create_part(db, pn_std="TX-MINOR", category_major="硬盘",
                                       category_minor="SAS", force=True)
    master_edit.edit_part(db, pn_std="TX-MINOR", updates={"category_minor": "SATA"})
    part = db.get(DimPart, created["id"])
    category = db.get(ProductCategory, part.category_id)
    assert (category.category_major, category.category_minor) == ("硬盘", "SATA")
    assert part.category_source == "MANUAL"
