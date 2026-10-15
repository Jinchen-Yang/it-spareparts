"""真实 PostgreSQL 双连接：等待父锁后比较现值，不能用旧 ORM 缓存覆盖。"""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.services import batch_normalize, master_edit


@pytest.mark.parametrize("second_updates,expected", [
    ({"description": "第二人旧草稿", "brand": "不得部分写"}, "field_conflict"),
    ({"brand": "第二人品牌"}, "applied"),
])
def test_waiting_writer_rechecks_fields_after_parent_lock(db, second_updates, expected):
    created = master_edit.create_part(db, pn_std="PG-CONCURRENT", description="基线", force=True)
    baseline = master_edit.get_part_edit_state(db, created["id"])
    db.commit()  # 释放基线读取锁。
    master_edit.edit_part_in_transaction(db, part_id=created["id"], pn_std="PG-CONCURRENT",
                                         updates={"description": "第一人描述"}, base_snapshot=baseline)
    started = Queue()

    def second_writer():
        with SessionLocal() as other:
            started.put(other.scalar(text("SELECT pg_backend_pid()")))
            try:
                master_edit.edit_part_in_transaction(
                    other, part_id=created["id"], pn_std="PG-CONCURRENT",
                    updates=second_updates, base_snapshot=baseline)
                other.commit()
                return "applied"
            except master_edit.MasterEditConflict as exc:
                other.rollback()
                return exc.detail["code"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(second_writer)
        try:
            pid = started.get(timeout=10)
            deadline = monotonic() + 10
            with SessionLocal() as monitor:
                while not monitor.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}):
                    assert monotonic() < deadline, "第二写者未实际等待第一写者的父锁"
                    sleep(0.01)
            db.commit()
            assert future.result(timeout=15) == expected
        finally:
            db.rollback()  # 任一断言失败也释放父锁，不遗留工作线程。
    final = master_edit.get_part_edit_state(db, created["id"])
    assert final["fields"]["description"] == "第一人描述"
    assert final["fields"]["brand"] == ("第二人品牌" if expected == "applied" else None)


def test_cached_part_is_refreshed_before_baseline_and_locks_are_merged(db):
    created = master_edit.create_part(db, pn_std="PG-CACHED", description="旧描述", force=True)
    baseline = master_edit.get_part_edit_state(db, created["id"])
    db.commit()
    # 提交后的普通读取重新缓存实体；随后的其他连接提交不会自动失效此缓存。
    cached = db.get(DimPart, created["id"])
    with SessionLocal() as other:
        master_edit.edit_part(other, pn_std="PG-CACHED", updates={"description": "其他连接已改"})
    assert cached.description == "旧描述"
    with pytest.raises(master_edit.MasterEditConflict):
        master_edit.edit_part_in_transaction(db, part_id=created["id"], pn_std="PG-CACHED",
                                             updates={"description": "不得覆盖"}, base_snapshot=baseline)
    db.rollback()
    master_edit.edit_part(db, part_id=created["id"], pn_std="PG-CACHED",
                          updates={"brand": "仅改品牌"}, base_snapshot=baseline)
    final = db.get(DimPart, created["id"])
    assert final.description == "其他连接已改"
    assert {"description", "brand"} <= set(final.locked_fields)


def test_batch_recomputes_from_locked_current_part(db):
    part = DimPart(pn_std="PG-BATCH", description="8TB 12Gbps 7200转 256MB 3.5inch SAS HDD",
                   machine_or_part="备件", locked_fields=[])
    db.add(part)
    db.commit()
    assert part.description.startswith("8TB")  # 在另一连接写前，加载提交后已过期的实体。
    with SessionLocal() as other:
        master_edit.edit_part(other, pn_std="PG-BATCH", updates={"description": "人工自定义"})
    assert part.description.startswith("8TB")
    batch_normalize.apply_batch(db, [part.id], fields=["description"], operated_by="batch-user")
    db.expire_all()
    assert db.get(DimPart, part.id).description == "人工自定义"


def test_batch_category_preserves_manual_minor_lock(db):
    part = DimPart(pn_std="PG-BATCH-MINOR", description="8TB 12Gbps 7200转 256MB 3.5inch SAS HDD",
                   machine_or_part="备件", category_major="硬盘", locked_fields=[])
    db.add(part)
    db.commit()
    master_edit.edit_part(db, pn_std=part.pn_std, updates={"category_minor": "人工小类"})
    result = batch_normalize.apply_batch(db, [part.id], fields=["category"], operated_by="batch-user")
    assert result == {"applied": 0, "skipped": 1}
    db.refresh(part)
    assert part.category_minor == "人工小类"
