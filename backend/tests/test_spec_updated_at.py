"""D-36 #392 审查：只写 product_specs 的路径必须推进 dim_part.updated_at。

specs 是 /list 的输出字段，增量游标只认 updated_at；规格单独变化若不推进，
商城只能等 24 h 完整遍历才发现。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.services import spec_extract

DISK_A = "Toshiba 600GB 15K SAS HDD 硬盘"
DISK_B = "Toshiba 1.2TB 12Gbps 10K SAS HDD 硬盘"
GPU_DESC = "NVIDIA A100 80GB PCIe GPU"
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _mk(db, pn, desc):
    part = DimPart(pn_std=pn, description=desc, machine_or_part="备件", locked_fields=[],
                   updated_at=OLD)
    db.add(part)
    db.flush()
    return part


def _ts(db, part):
    return db.scalar(select(DimPart.updated_at).where(DimPart.id == part.id))


def test_backfill_specs_bumps_only_parts_that_gained_rows(db):
    disk, gpu = _mk(db, "SPEC-U1", DISK_A), _mk(db, "SPEC-U2", GPU_DESC)
    db.commit()
    spec_extract.backfill_specs(db)
    db.commit()
    assert _ts(db, disk) > OLD
    assert _ts(db, gpu) == OLD          # 抽不出规格 → 无写入 → 不推进

    before = _ts(db, disk)
    spec_extract.backfill_specs(db)     # 幂等重跑：无新行，不推进
    db.commit()
    assert _ts(db, disk) == before


def test_backfill_specs_rebuild_bumps_rebuilt_parts(db):
    disk = _mk(db, "SPEC-U3", DISK_A)
    spec_extract.backfill_specs(db)
    db.commit()
    before = _ts(db, disk)
    spec_extract.backfill_specs(db, rebuild=True)
    db.commit()
    assert _ts(db, disk) > before


def test_refresh_part_specs_bumps_on_change_only(db):
    disk = _mk(db, "SPEC-U4", DISK_A)
    spec_extract.refresh_part_specs(db, disk.id, DISK_A)
    db.commit()
    before = _ts(db, disk)
    spec_extract.refresh_part_specs(db, disk.id, DISK_A)   # 同描述重抽：内容不变
    db.commit()
    assert _ts(db, disk) == before
    spec_extract.refresh_part_specs(db, disk.id, DISK_B)   # 规格变了
    db.commit()
    assert _ts(db, disk) > before
    before = _ts(db, disk)
    spec_extract.refresh_part_specs(db, disk.id, GPU_DESC)  # 清空旧 auto 行也是变化
    db.commit()
    assert _ts(db, disk) > before


def test_apply_manual_specs_bumps(db):
    gpu = _mk(db, "SPEC-U5", GPU_DESC)
    db.commit()
    spec_extract.apply_manual_specs(db, gpu.id, {})
    db.commit()
    assert _ts(db, gpu) == OLD
    spec_extract.apply_manual_specs(db, gpu.id, spec_extract.validate_manual_specs(
        {"capacity": {"value": "80GB", "unit": "GB", "numeric_value": "80"}}))
    db.commit()
    assert _ts(db, gpu) > OLD


def test_spec_bump_uses_current_timestamp(db):
    disk = _mk(db, "SPEC-U6", DISK_A)
    db.commit()
    before = datetime.now(timezone.utc) - timedelta(minutes=1)
    spec_extract.backfill_specs(db)
    db.commit()
    assert _ts(db, disk) > before


def test_merge_path_compression_bumps_descendant_updated_at(db):
    """合并路径压缩更新子孙行的身份指向，必须推进该行 updated_at。"""
    from app.services import merge
    a, b, c = _mk(db, "CHAIN-A", None), _mk(db, "CHAIN-B", None), _mk(db, "CHAIN-C", None)
    db.commit()
    merge.merge_parts(db, "CHAIN-A", "CHAIN-B", "测试", "tester")
    db.commit()
    before = _ts(db, a)
    merge.merge_parts(db, "CHAIN-B", "CHAIN-C", "测试", "tester")   # A 被路径压缩到 C
    db.commit()
    assert db.scalar(select(DimPart.merged_into_id).where(DimPart.id == a.id)) == c.id
    assert _ts(db, a) > before


def test_unmerge_moving_specs_back_bumps_target(db):
    """unmerge 把迁到目标的规格改回源：目标 specs 变化，必须推进目标 updated_at。"""
    from app.services import merge
    src, tgt = _mk(db, "UNM-SRC", DISK_A), _mk(db, "UNM-TGT", GPU_DESC)
    spec_extract.refresh_part_specs(db, src.id, DISK_A)
    db.commit()
    result = merge.merge_parts(db, "UNM-SRC", "UNM-TGT", "测试", "tester")
    db.commit()
    moved_keys = set(db.scalars(select(ProductSpec.spec_key).where(ProductSpec.part_id == tgt.id)))
    assert moved_keys   # 前提：源规格已迁到目标
    before = _ts(db, tgt)
    merge.unmerge(db, result["merge_log_id"], "tester")
    db.commit()
    assert not set(db.scalars(select(ProductSpec.spec_key).where(ProductSpec.part_id == tgt.id)))
    assert _ts(db, tgt) > before
