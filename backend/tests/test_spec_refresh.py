"""规格派生缓存与描述同步（终审 P1）：description 变更 → product_specs 同事务重抽。

背书事实：product_specs 100% 行 source='auto'，是 description 的纯派生缓存；
旧策略 on_conflict_do_nothing 导致描述重写后规格永久陈旧（生产 6797 条已中招）。
契约：删该型号 auto 行 + 按新描述重插；source='manual' 行永远优先保留。
"""
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.services import master_edit, spec_extract

DISK_A = "Toshiba 600GB 15K SAS HDD 硬盘"          # capacity=600GB rpm=15K
DISK_B = "Toshiba 1.2TB 12Gbps 10K SAS HDD 硬盘"   # capacity=1.2TB rpm=10K speed=12Gbps
GPU_DESC = "NVIDIA A100 80GB PCIe GPU"             # 非硬盘/内存 → extract 返回 []


def _mk_part(db, pn, desc):
    p = DimPart(pn_std=pn, description=desc, machine_or_part="备件", locked_fields=[])
    db.add(p)
    db.commit()
    return p


def _specs(db, part_id) -> dict:
    rows = db.execute(select(ProductSpec.spec_key, ProductSpec.spec_value)
                      .where(ProductSpec.part_id == part_id)).all()
    return dict(rows)


def test_edit_description_refreshes_specs(db):
    p = _mk_part(db, "SPEC-R1", DISK_A)
    spec_extract.backfill_specs(db)
    db.commit()
    assert _specs(db, p.id)["capacity"] == "600GB"

    master_edit.edit_part(db, pn_std="SPEC-R1",
                          updates={"description": DISK_B}, operated_by="t")
    got = _specs(db, p.id)
    assert got["capacity"] == "1.2TB"          # 旧 600GB 不残留
    assert got["rpm"] == "10K"
    assert got["speed"] == "12Gbps"


def test_edit_to_non_extractable_clears_stale_specs(db):
    """新描述抽不出规格（如 GPU）也必须清掉旧 auto 行——不清 = 静默陈旧。"""
    p = _mk_part(db, "SPEC-R2", DISK_A)
    spec_extract.backfill_specs(db)
    db.commit()
    assert _specs(db, p.id)  # 有旧规格

    master_edit.edit_part(db, pn_std="SPEC-R2",
                          updates={"description": GPU_DESC}, operated_by="t")
    assert _specs(db, p.id) == {}


def test_manual_spec_survives_refresh(db):
    p = _mk_part(db, "SPEC-R3", DISK_A)
    db.add(ProductSpec(part_id=p.id, spec_key="capacity", spec_value="人工值",
                       spec_unit=None, numeric_value=None, source="manual"))
    db.commit()

    master_edit.edit_part(db, pn_std="SPEC-R3",
                          updates={"description": DISK_B}, operated_by="t")
    got = db.execute(select(ProductSpec.spec_value, ProductSpec.source)
                     .where(ProductSpec.part_id == p.id,
                            ProductSpec.spec_key == "capacity")).one()
    assert got == ("人工值", "manual")          # manual 优先，auto 撞键让位


def test_edit_other_field_leaves_specs_alone(db):
    p = _mk_part(db, "SPEC-R4", DISK_A)
    spec_extract.backfill_specs(db)
    db.commit()
    before = _specs(db, p.id)

    master_edit.edit_part(db, pn_std="SPEC-R4",
                          updates={"brand": "Toshiba"}, operated_by="t")
    assert _specs(db, p.id) == before


def test_backfill_rebuild_fixes_stale_rows(db):
    """rebuild=True：全量删 auto 重抽——治历史陈旧（生产 6797 条）的一次性入口。"""
    p = _mk_part(db, "SPEC-R5", DISK_B)
    # 伪造陈旧 auto 行（描述已是 1.2TB，规格还留着 600GB）
    db.add(ProductSpec(part_id=p.id, spec_key="capacity", spec_value="600GB",
                       spec_unit="GB", numeric_value=600, source="auto"))
    db.commit()

    r = spec_extract.backfill_specs(db)               # 旧默认：只补缺 → 治不了
    db.commit()
    assert _specs(db, p.id)["capacity"] == "600GB"

    r = spec_extract.backfill_specs(db, rebuild=True)  # 重建 → 治好
    db.commit()
    assert _specs(db, p.id)["capacity"] == "1.2TB"
    assert r["spec_rows"] > 0
