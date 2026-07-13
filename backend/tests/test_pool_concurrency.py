"""PoolCatalog 真实写链的并发回归。

每个竞争方都在独立线程内创建真实 Session，通过公开写接口完成整条事务；
测试不预占私有锁，也不把 advisory-lock 原语当作行为断言。
"""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.dimensions import DimPart
from app.services import pool_catalog as svc


_RACE_TIMEOUT_SECONDS = 10

Write = Callable[[Session], dict | None]
Outcome = tuple[str, dict | str | None]


def _part(db: Session, pn: str) -> int:
    part = DimPart(pn_std=pn)
    db.add(part)
    db.flush()
    return part.id


def _pool(db: Session, name: str) -> dict:
    member_ids = [_part(db, f"{name}-A"), _part(db, f"{name}-B")]
    return svc.create_pool(
        db,
        name=name,
        member_part_ids=member_ids,
        operated_by="seed",
    )


def _race(left: Write, right: Write) -> list[Outcome]:
    """令两条真实写链同时越过起跑线，并为锁等待设硬超时。"""
    start = Barrier(2, timeout=5)

    def run(write: Write) -> Outcome:
        session = SessionLocal()
        try:
            # 即使锁策略回归成死锁，用例也会有界失败而不会卡死套件。
            session.execute(text("SET LOCAL statement_timeout = 5000"))
            start.wait()
            try:
                return "ok", write(session)
            except svc.PoolConflictError as exc:
                return "conflict", str(exc)
        finally:
            session.rollback()
            session.close()

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pool-write")
    futures = [executor.submit(run, left), executor.submit(run, right)]
    try:
        return [future.result(timeout=_RACE_TIMEOUT_SECONDS) for future in futures]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_same_pn_concurrently_added_to_two_active_pools_has_one_winner(db):
    """同一 PN 同时加入两个有效池：最终只能有一条写链提交。"""
    candidate_id = _part(db, "CONCURRENT-SHARED-PN")
    left_pool = _pool(db, "并发甲池")
    right_pool = _pool(db, "并发乙池")
    db.commit()

    outcomes = _race(
        lambda session: svc.update_members(
            session,
            group_id=left_pool["group_id"],
            version=1,
            add_part_ids=[candidate_id],
            operated_by="left-writer",
        ),
        lambda session: svc.update_members(
            session,
            group_id=right_pool["group_id"],
            version=1,
            add_part_ids=[candidate_id],
            operated_by="right-writer",
        ),
    )

    assert sorted(kind for kind, _ in outcomes) == ["conflict", "ok"]
    conflict = next(value for kind, value in outcomes if kind == "conflict")
    assert "CONCURRENT-SHARED-PN" in str(conflict)

    details = [
        svc.get_pool(db, left_pool["group_id"]),
        svc.get_pool(db, right_pool["group_id"]),
    ]
    owners = [
        detail["group_id"]
        for detail in details
        if candidate_id in {member["part_id"] for member in detail["members"]}
    ]
    assert len(owners) == 1
    assert sorted(detail["version"] for detail in details) == [1, 2]
    assert sorted(detail["member_count"] for detail in details) == [2, 3]


def test_same_pool_concurrent_writes_with_same_version_have_one_winner(db):
    """同池两条写链都携 v1：一条提交，另一条收敛为版本冲突。"""
    pool = _pool(db, "同池并发")
    db.commit()

    outcomes = _race(
        lambda session: svc.update_pool(
            session,
            group_id=pool["group_id"],
            version=1,
            updates={"name": "甲写链获胜"},
            operated_by="left-writer",
        ),
        lambda session: svc.update_pool(
            session,
            group_id=pool["group_id"],
            version=1,
            updates={"name": "乙写链获胜"},
            operated_by="right-writer",
        ),
    )

    assert sorted(kind for kind, _ in outcomes) == ["conflict", "ok"]
    conflict = next(value for kind, value in outcomes if kind == "conflict")
    assert "已被他人修改" in str(conflict)

    successful = next(value for kind, value in outcomes if kind == "ok")
    detail = svc.get_pool(db, pool["group_id"])
    assert detail["version"] == 2
    assert detail["name"] == successful["name"]
