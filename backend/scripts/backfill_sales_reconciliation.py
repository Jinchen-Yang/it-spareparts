#!/usr/bin/env python3
"""销售单已对账快照历史回填 + 含税三件套回填（D-40/D-41，PR-2）。

背景
----
D-40（已对账快照）与 D-41（销售单金额三件套）的列在 2026-06-30 之后的新型销售
导出里才有；PR-2 之前的导入不落这些列。本脚本把历史批次（sys_import_batch 中
file_type=sales、status=success/superseded）的归档原件逐个回放，补两样东西：

1. ``f_sales_reconciliation_snapshot``：source='backfill'，import_batch_id=原件
   所属批次；(raw_order_id, import_batch_id) 唯一 + ON CONFLICT DO NOTHING——
   每批次每单记一条观测，重跑新增 0 行（幂等、可续跑）；不同批次各记一条
   （即使内容相同，A→B→A 不被吞）。
2. ``f_sales_order`` 含税三件套（is_tax_inclusive/tax_amount/amount_inc_tax）：
   只取该订单 ``import_batch_id`` 对应批次原件中的值（与 amount_ex_tax 出自
   同一文件，保持口径一致）。**比对该批次拥有的全部订单**（不只三列含 NULL
   的）：仅库内为 NULL 时填；库内已有不同值 → 逐字段记冲突（订单号/字段/
   库内值/原件值）不覆盖；原件缺失/哈希不符/解析失败/无该值 → 记未解决保持
   NULL。

只写以上两处，绝不触碰任何其它既有列。解析复用导入同一代码路径
（pipeline.load_workbook / transform_workbook），列映射与导入完全同源。

安全护栏（沿用 remediate_contract_amount_inc_tax 的模式）
----------------------------------------------------------
* 默认 dry-run：只读事务，输出 JSON 报告与计划 manifest sha256；
* ``--execute`` 必须同时给 ``--operator``（在册有效 sys_user）与
  ``--confirm-manifest-sha256``（dry-run 指纹）；执行前在锁内重算计划，指纹
  不一致拒绝执行；锁内每文件再按同一口径的单文件指纹复核，变化的文件零写入
  记 ``plan_changed_since_dry_run`` 失败（其余文件继续，最终 status=partial）；
* PostgreSQL 会话级 advisory lock 防并发回填：锁绑定**专用连接**取/放
  （ORM Session 事务边界会归还连接，锁绝不能记在 Session 连接上），try 模式
  获取——另一实例在跑时立即以 ``backfill_already_running`` 拒绝，不阻塞等待；
* 每文件事务开头取**文件级 advisory xact lock**（键由会话级锁命名空间
  +batch_id 派生，兜住专用锁连接在提交前瞬间丢失的 TOCTOU 窗口，取不到即
  ``backfill_file_lock_busy`` 中止）并重新校验操作员（FOR SHARE 行锁只保
  当前事务，中途停用 → ``operator_not_active`` 中止）；
* 单文件单事务：部分失败只记入报告（files_failed>0 → status=partial、CLI
  退出码 3），不影响其他文件；坏档（archive_missing / archive_hash_mismatch /
  parse_failed / unexpected_file_type）跳过不执行（保持计划状态、files_skipped
  计数、审计 action=skip，不触发 partial）；每文件落 SysAuditLog 审计
  （operator + 实际计数 + manifest 指纹）；
* 只连接环境变量 ``DATABASE_URL`` 指定的库（不设则拒绝运行，绝不隐式连默认库）。

用法
----
::

    # 1) 小范围试跑
    DATABASE_URL=... uv run python scripts/backfill_sales_reconciliation.py --batch-id 123
    # 2) 全量 dry-run，人工核对报告（文件数/单数/金额合计/冲突/未解决）
    DATABASE_URL=... uv run python scripts/backfill_sales_reconciliation.py > plan.json
    # 3) 确认后执行（sha 来自 dry-run 输出的 manifest_sha256）
    DATABASE_URL=... uv run python scripts/backfill_sales_reconciliation.py \
        --execute --operator 张三 --confirm-manifest-sha256 <sha>

生产执行必须先备份，并得到杨金臣（金臣）明确授权。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.etl import mapping, pipeline
from app.models.sales import FSalesOrder, FSalesReconciliationSnapshot
from app.models.system import SysAuditLog, SysImportBatch, SysRawFile
from app.services.sales_reconciliation import (
    SOURCE_BACKFILL,
    snapshot_rows_from_orders,
)

TAX_COLUMNS = ("is_tax_inclusive", "tax_amount", "amount_inc_tax")
_QUANTUM = Decimal("0.01")
# 会话级 advisory lock key：hashtextextended 确定性 bigint，独立于 DATA_CHANGE_ADVISORY_LOCK_KEY
_LOCK_KEY_SEED = "backfill_sales_reconciliation"
_MAX_CONFLICTS_IN_REPORT = 200
# 专用锁连接的 libpq TCP keepalive：长回填期间锁连接一直空闲，防中间设备/NAT 掐断空闲
# TCP 导致会话级锁静默丢失（丢失仍由每文件提交前的 pg_locks 校验兜底）。
LOCK_CONNECT_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
    "application_name": "backfill_sales_reconciliation_lock",
}


class BackfillError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _SafeArgumentError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _SafeArgumentError(message)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _money_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_QUANTUM), ".2f")


def _active_operator(db: Session, operator: str) -> bool:
    """校验操作员在册且启用；FOR SHARE 锁住该行直至**当前事务**结束。

    行锁不跨事务（提交/回滚即释放）：execute 在整次执行前校验一次，并在
    **每个文件事务开头**重新校验（见 run_backfill 执行循环）——保证「校验通过
    → 该文件提交」之间账号无法被停用；前一个文件提交后到下一个文件校验之间
    的窗口由下一次校验兜住（中途停用 → operator_not_active 中止整个执行）。
    """
    return db.scalar(text(
        "SELECT 1 FROM sys_user "
        "WHERE username = :operator AND is_active IS TRUE "
        "AND deleted_at IS NULL LIMIT 1 FOR SHARE"
    ), {"operator": operator}) is not None


def _lock_key() -> Any:
    return func.hashtextextended(_LOCK_KEY_SEED, 0)


def _file_lock_key(batch_id: int) -> Any:
    """文件级 xact 锁键：会话级锁同命名空间 + ":file:" + batch_id 派生。

    hashtextextended 的输出与会话级键、其他批次的文件键互不相同（64 位哈希，
    碰撞概率可忽略）；同一命名空间派生让两类键的归属可从键名追溯。
    """
    return func.hashtextextended(f"{_LOCK_KEY_SEED}:file:{batch_id}", 0)


def _acquire_file_xact_lock(db: Session, batch_id: int) -> None:
    """文件事务级互斥（TOCTOU 兜底）：会话级锁绑定专用连接，极端情况下该连接
    可在 ``_assert_lock_held`` 与 ``commit`` 之间断开——彼时另一份回填能取到
    会话级锁、并发处理**同一个文件**。每文件事务开头取本锁（xact 版随提交/
    回滚自动释放，无需显式放锁），并发实例在同一文件上的事务便无法重叠。

    取不到 → ``backfill_file_lock_busy`` **中止整个执行**（而非跳过该文件）：
    这不是单文件的数据问题，而是互斥前提被破坏（疑似存在并发实例），继续跑
    只会扩大交错写入面；已提交文件不受影响，重跑幂等。
    """
    acquired = db.execute(
        select(func.pg_try_advisory_xact_lock(_file_lock_key(batch_id)))).scalar_one()
    if acquired is not True:
        raise BackfillError(
            "backfill_file_lock_busy",
            f"batch_id={batch_id} 的文件级 advisory xact lock 被其他会话持有"
            "（存在并发回填同一文件，或会话级互斥已被破坏）：已回滚当前文件并中止"
            "整个执行；已提交的文件不受影响，排查并发实例后重跑（幂等）")


# 执行循环里「中止整个执行」的错误码（其余单文件错误只记 execute_failed）：
# 丢锁 / 文件锁被占 / 操作员失效都不是单文件数据问题，而是执行前提被破坏。
_ABORT_ERROR_CODES = frozenset({
    "backfill_lock_lost", "backfill_file_lock_busy", "operator_not_active",
})
# 坏档计划状态：execute 时跳过——保持计划状态、零写入、files_skipped 计数、
# 审计 action=skip；dry-run 报告已如实预告，不触发 partial。
_SKIPPED_PLAN_STATUSES = frozenset({
    "archive_missing", "archive_hash_mismatch", "unexpected_file_type", "parse_failed",
})


class _BackfillLock:
    """持锁专用连接 + 其服务端 pid（pg_locks 校验用）+ 其专属 NullPool engine。"""

    def __init__(self, conn: Connection, engine: Any, pid: int):
        self.conn = conn
        self.engine = engine
        self.pid = pid


def _assert_lock_held(db: Session, lock: "_BackfillLock") -> None:
    """每文件提交前校验：持锁连接的服务端 pid 在 pg_locks 里仍持有回填锁。

    在业务 Session 上查（与锁连接无关）：锁连接若已被断开/终止，其 pid 不再持锁，
    这里立即发现。丢锁 → backfill_lock_lost，调用方回滚当前文件并中止整个执行。
    """
    try:
        held = db.execute(text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype = 'advisory'"
            " AND l.pid = :pid AND l.granted AND l.objsubid = 1"
            " AND ((l.classid::bigint << 32) | l.objid::bigint)"
            " = hashtextextended(:seed, 0))"),
            {"pid": lock.pid, "seed": _LOCK_KEY_SEED}).scalar_one()
    except Exception as exc:   # noqa: BLE001 —— 无法确认持锁 = 按丢锁处理，绝不冒险提交
        raise BackfillError(
            "backfill_lock_lost",
            f"无法校验回填 advisory lock（{type(exc).__name__}），按丢锁处理："
            "已回滚当前文件并中止") from exc
    if held is not True:
        raise BackfillError(
            "backfill_lock_lost",
            f"回填 advisory lock 已丢失（持锁连接 pid={lock.pid} 不再持有），"
            "已回滚当前文件并中止；已提交的文件不受影响，确认无并发实例后重跑即可（幂等）")


def _acquire_backfill_lock(db: Session) -> "_BackfillLock":
    """try 模式取会话级 advisory lock，返回持有锁的**专用**连接。

    锁必须与持有它的连接绑定：ORM Session 每次 commit/rollback 都把连接归还
    连接池，之后的语句可能落在另一条连接上——把锁记在 Session 的连接上，
    跨事务后的解锁会失效，锁随池内空闲连接滞留永不释放，此后任何取同一把
    锁的语句永久阻塞（曾让 CI 全量测试确定性卡死）。专用连接与 Session 事务
    互不相干，取/放永远在同一条连接上。try 模式：另一实例在跑时立即失败，
    绝不无限等待。
    """
    # 专属 NullPool engine：锁连接不进连接池（close 即断开服务端会话、必然放锁），
    # 并开 TCP keepalive（LOCK_CONNECT_ARGS）。
    engine = create_engine(db.get_bind().engine.url, poolclass=NullPool,
                           connect_args=dict(LOCK_CONNECT_ARGS))
    try:
        conn = engine.connect()
    except BaseException:
        engine.dispose()
        raise
    try:
        acquired = conn.execute(
            select(func.pg_try_advisory_lock(_lock_key()))).scalar_one()
        if acquired is not True:
            raise BackfillError(
                "backfill_already_running",
                "advisory lock 已被另一回填实例持有，拒绝并发启动"
                "（确认没有实例在跑时，请排查持有该锁的数据库会话后重试）")
        pid = conn.execute(select(func.pg_backend_pid())).scalar_one()
        conn.rollback()   # 结束取锁事务（锁是会话级，跨事务存活），避免执行期间 idle in transaction
        return _BackfillLock(conn, engine, pid)
    except BaseException:
        conn.close()
        engine.dispose()
        raise


def _release_backfill_lock(lock: "_BackfillLock") -> None:
    """在同一条连接上解锁、检查返回值并归还连接。

    解锁失败或解锁语句本身出错 → invalidate 废弃该连接：服务端会话断开必然
    释放会话级锁，而连接池归还时的 ROLLBACK 复位不会——绝不能把可能仍持锁
    的连接还回池。只告警不抛异常：本函数在 finally 里调用，抛出会顶掉正文的
    原始异常。
    """
    lock_conn = lock.conn
    try:
        unlocked = lock_conn.execute(
            select(func.pg_advisory_unlock(_lock_key()))).scalar_one()
        if unlocked is not True:
            lock_conn.invalidate()
            print("backfill_sales_reconciliation: pg_advisory_unlock 返回 false，"
                  "已废弃锁连接强制释放（断开服务端会话即放锁）", file=sys.stderr)
    except Exception as exc:   # noqa: BLE001 —— 解锁语句失败：连接已不可靠，废弃强制释放
        lock_conn.invalidate()
        print(f"backfill_sales_reconciliation: advisory lock 解锁失败，"
              f"已废弃锁连接强制释放（断开服务端会话即放锁）：{exc!r}", file=sys.stderr)
    finally:
        lock_conn.close()
        lock.engine.dispose()


def _iter_batches(db: Session, batch_ids: list[int] | None) -> list[SysImportBatch]:
    stmt = (
        select(SysImportBatch)
        .where(
            SysImportBatch.file_type == mapping.SALES,
            SysImportBatch.status.in_(("success", "superseded")),
        )
        .order_by(SysImportBatch.uploaded_at, SysImportBatch.id)
    )
    if batch_ids:
        found = db.scalars(stmt.where(SysImportBatch.id.in_(batch_ids))).all()
        missing = sorted(set(batch_ids) - {b.id for b in found})
        if missing:
            raise BackfillError(
                "invalid_batch_scope",
                f"--batch-id 不在回放范围（须为 sales 且 success/superseded 的批次）：{missing}",
            )
        return list(found)
    return list(db.scalars(stmt).all())


def _raw_file_for(db: Session, batch: SysImportBatch) -> SysRawFile | None:
    rows = db.scalars(
        select(SysRawFile).where(SysRawFile.batch_id == batch.id).order_by(SysRawFile.id)
    ).all()
    for row in rows:
        if row.file_hash == batch.file_hash and row.storage_path:
            return row
    return rows[0] if rows else None


def _orders_in_batch(db: Session, batch_id: int) -> list[Any]:
    """import_batch_id=本批次的**全部**订单（税列比对候选）。

    runbook 承诺比对而非只补 NULL：三列已填但与本批次原件不同的订单同样
    要报冲突，所以不能只选含 NULL 的订单。定序：fills / 冲突列表顺序都进
    计划指纹，无 ORDER BY 的行序在 PG 不保证稳定，dry↔execute 指纹比对
    可能误拒（方向安全，但没必要）——按 raw_order_id 定序消除。
    """
    return db.execute(
        select(
            FSalesOrder.raw_order_id,
            FSalesOrder.order_no,
            FSalesOrder.is_tax_inclusive,
            FSalesOrder.tax_amount,
            FSalesOrder.amount_inc_tax,
        ).where(
            FSalesOrder.import_batch_id == batch_id,
        ).order_by(FSalesOrder.raw_order_id)
    ).all()


def _plan_file(db: Session, batch: SysImportBatch) -> dict[str, Any]:
    """单文件回放计划（纯读，不写任何东西）。

    返回 dict：报告字段（进 JSON/manifest）+ 两个私有键 ``_snapshot_rows`` /
    ``_tax_fills``（执行用的完整行数据，dry-run 报告里剔除）。
    """
    plan: dict[str, Any] = {
        "batch_id": batch.id,
        "filename": batch.filename,
        "file_hash": batch.file_hash,
        "status": "planned",
        "orders_in_file": 0,
        "snapshots_planned": 0,
        "snapshots_existing": 0,
        "snapshots_plan_sha256": None,
        "tax_fills_planned": 0,
        "tax_fills_plan_sha256": None,
        "tax_conflicts": [],
        "tax_conflicts_sha256": _canonical_sha256([]),
        "tax_unresolved": 0,
        "transform_errors": 0,
        "_snapshot_rows": [],
        "_tax_fills": [],
    }
    batch_orders = _orders_in_batch(db, batch.id)

    def _unreadable(status: str, error: str | None = None) -> dict[str, Any]:
        # 原件读不了 → 本批次订单的 NULL 税列全部记未解决（保持 NULL）。
        # 计数单位与可读路径一致：列格数（订单×列为 NULL 的格子）。
        plan["status"] = status
        plan["tax_unresolved"] = sum(
            1 for o in batch_orders for c in TAX_COLUMNS if getattr(o, c) is None)
        if error:
            plan["error"] = error[:300]
        return plan

    raw = _raw_file_for(db, batch)
    if raw is None or not raw.storage_path or not Path(raw.storage_path).exists():
        return _unreadable("archive_missing")
    try:
        pipeline.verify_archive(raw.storage_path, batch.file_hash)
    except pipeline.ArchiveError as exc:
        return _unreadable("archive_hash_mismatch", str(exc))
    try:
        loaded = pipeline.load_workbook(raw.storage_path)
        if loaded.file_type != mapping.SALES:
            return _unreadable(
                "unexpected_file_type", f"批次标注 sales，原件识别为 {loaded.file_type}")
        pipeline.require_clean_workbook(loaded)
        transformed = pipeline.transform_workbook(loaded)
    except Exception as exc:   # noqa: BLE001 —— ReaderError 及 openpyxl 等一切解析失败
        return _unreadable("parse_failed", f"{type(exc).__name__}: {exc}")

    result = transformed.result
    plan["orders_in_file"] = len(result.orders)
    plan["transform_errors"] = len(result.errors)

    # ---- 1) 快照计划：本批次在库内还没有观测的订单才追加 ----
    if result.has_reconciliation_columns:
        raw_ids = list(result.orders)
        existing: set[str] = set()
        for start in range(0, len(raw_ids), 1000):
            chunk = raw_ids[start:start + 1000]
            existing.update(rid for (rid,) in db.execute(
                select(FSalesReconciliationSnapshot.raw_order_id).where(
                    FSalesReconciliationSnapshot.raw_order_id.in_(chunk),
                    FSalesReconciliationSnapshot.import_batch_id == batch.id,
                )
            ).all())
        oid_map = {}
        for start in range(0, len(raw_ids), 1000):
            chunk = raw_ids[start:start + 1000]
            oid_map.update(dict(db.execute(
                select(FSalesOrder.raw_order_id, FSalesOrder.id)
                .where(FSalesOrder.raw_order_id.in_(chunk))
            ).all()))
        # 行构造与导入路径同一事实源（#396 S5：sales_reconciliation.snapshot_
        # rows_from_orders）；本脚本只多一步「库内已有本批次观测则不追加」的预筛
        rows = snapshot_rows_from_orders(
            result.orders, batch_id=batch.id, source=SOURCE_BACKFILL, oid_map=oid_map)
        plan["_snapshot_rows"] = [r for r in rows if r["raw_order_id"] not in existing]
        plan["snapshots_planned"] = len(plan["_snapshot_rows"])
        plan["snapshots_existing"] = len(rows) - len(plan["_snapshot_rows"])
        # 清单指纹用 (raw_order_id, content_hash)：本批次内每单至多一条，
        # raw_order_id 已唯一刻画将写行，content_hash 同时锁定将写内容
        plan["snapshots_plan_sha256"] = _canonical_sha256(
            [[r["raw_order_id"], r["content_hash"]] for r in plan["_snapshot_rows"]])
        totals = {key: None for key in
                  ("order_amount", "reconciled_amount", "pending_amount",
                   "return_reconciled_amount")}
        for key in totals:
            vals = [r[key] for r in plan["_snapshot_rows"] if r[key] is not None]
            totals[key] = sum(vals, Decimal("0.00")) if vals else None
        plan["snapshot_amount_totals"] = {k: _money_text(v) for k, v in totals.items()}
    else:
        # 旧格式文件没有对账区列：不写快照（D-40），状态单列以便报告区分
        plan["status"] = "no_reconciliation_columns"

    # ---- 2) 税列计划：比对该批次全部订单；只补 NULL，已填且不同报冲突不覆盖 ----
    fills: list[dict[str, Any]] = []
    for order in batch_orders:
        head = result.orders.get(order.raw_order_id)
        for column in TAX_COLUMNS:
            db_value = getattr(order, column)
            file_value = head.get(column) if head is not None else None
            if db_value is None:
                if file_value is None:
                    plan["tax_unresolved"] += 1
                else:
                    fills.append({
                        "raw_order_id": order.raw_order_id,
                        "column": column,
                        "value": file_value,
                    })
            elif file_value is not None and file_value != db_value:
                plan["tax_conflicts"].append({
                    "raw_order_id": order.raw_order_id,
                    "order_no": order.order_no,
                    "column": column,
                    "db_value": _jsonable_value(db_value),
                    "file_value": _jsonable_value(file_value),
                })
    plan["_tax_fills"] = fills
    plan["tax_fills_planned"] = len(fills)
    plan["tax_fills_plan_sha256"] = _canonical_sha256(
        [[f["raw_order_id"], f["column"], _jsonable_value(f["value"])] for f in fills])
    # 冲突明细进计划指纹：库内冲突值在 dry-run 之后变化 → 指纹变 → execute 拒绝，
    # 与 fills 同一确定性口径（冲突列表同样按 raw_order_id 定序）。
    plan["tax_conflicts_sha256"] = _canonical_sha256([
        [c["raw_order_id"], c["order_no"], c["column"], c["db_value"], c["file_value"]]
        for c in plan["tax_conflicts"]])
    return plan


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.quantize(_QUANTUM), ".2f")
    return value


def _manifest_entry(plan: dict[str, Any]) -> dict[str, Any]:
    """manifest 的单文件条目：dry-run 整体指纹与锁内逐文件复核共用同一口径
    （锁内 fresh 计划与核准清单条目字段逐一比对，杜绝两套口径漂移）。"""
    return {
        "batch_id": plan["batch_id"],
        "file_hash": plan["file_hash"],
        "status": plan["status"],
        "snapshots_planned": plan["snapshots_planned"],
        "snapshots_plan_sha256": plan["snapshots_plan_sha256"],
        "tax_fills_planned": plan["tax_fills_planned"],
        "tax_fills_plan_sha256": plan["tax_fills_plan_sha256"],
        "tax_conflicts_count": len(plan["tax_conflicts"]),
        "tax_conflicts_sha256": plan["tax_conflicts_sha256"],
        "tax_unresolved": plan["tax_unresolved"],
    }


def _manifest(batches: list[SysImportBatch], plans: list[dict[str, Any]]) -> dict[str, Any]:
    """dry-run ↔ execute 之间必须不变的计划身份（不含报告性字段与时间戳）。"""
    return {
        "batch_ids": [b.id for b in batches],
        "files": [_manifest_entry(p) for p in plans],
    }


def _is_newer_observation(mod: datetime | None, bid: int,
                          prev_row: dict[str, Any]) -> bool:
    """D-40「取每单最新」比较：source_modified_at DESC NULLS LAST、批次号 DESC。

    与库内 latest_snapshots_by_raw_order_id 的排序键一致；(raw_order_id,
    import_batch_id) 唯一约束下每单每批次至多一条，无需再比写入顺序。
    """
    prev_mod, prev_bid = prev_row["source_modified_at"], prev_row["import_batch_id"]
    if mod is None:
        return prev_mod is None and bid > prev_bid
    if prev_mod is None:
        return True
    if mod != prev_mod:
        return mod > prev_mod
    return bid > prev_bid


def _totals(plans: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for p in plans:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    # 每批次每单一条观测、各自成行（不同批次即使内容相同也各写一条，如
    # superseded 批与后继批共用同一原件）——汇总不做跨文件去重，totals
    # 与执行真实写入数一致。
    planned_rows: list[dict[str, Any]] = [r for p in plans for r in p["_snapshot_rows"]]
    snapshot_totals = {key: Decimal("0.00") for key in
                       ("order_amount", "reconciled_amount", "pending_amount",
                        "return_reconciled_amount")}
    for row in planned_rows:
        for key in snapshot_totals:
            if row[key] is not None:
                snapshot_totals[key] += row[key]
    # 按单「最新快照」合计（D-40 口径，见 _is_newer_observation）——
    # 首跑干净库上可直接对 D-40 验收数核对。
    latest: dict[str, dict[str, Any]] = {}
    for row in planned_rows:
        rid, mod, bid = (row["raw_order_id"], row["source_modified_at"],
                         row["import_batch_id"])
        prev = latest.get(rid)
        if prev is None or _is_newer_observation(mod, bid, prev):
            latest[rid] = row
    latest_totals = {key: Decimal("0.00") for key in
                     ("reconciled_amount", "return_reconciled_amount")}
    for row in latest.values():
        for key in latest_totals:
            if row[key] is not None:
                latest_totals[key] += row[key]
    conflicts = [c for p in plans for c in p["tax_conflicts"]]
    return {
        "batches_considered": len(plans),
        "files_by_status": by_status,
        "orders_with_planned_snapshots": len({r["raw_order_id"] for r in planned_rows}),
        "snapshots_planned": len(planned_rows),
        # 已存在的同批次观测数（续跑时 planned 只剩剩余工作量的信号）
        "snapshots_existing": sum(p["snapshots_existing"] for p in plans),
        "snapshot_amount_totals": {
            k: _money_text(v) for k, v in snapshot_totals.items()} if planned_rows else None,
        "latest_per_order": {
            "orders": len(latest),
            "reconciled_amount": _money_text(latest_totals["reconciled_amount"]),
            "return_reconciled_amount": _money_text(latest_totals["return_reconciled_amount"]),
        },
        "tax_fills_planned": sum(p["tax_fills_planned"] for p in plans),
        "tax_fills_planned_by_column": {
            column: sum(1 for p in plans for f in p["_tax_fills"] if f["column"] == column)
            for column in TAX_COLUMNS},
        "tax_conflicts_total": len(conflicts),
        "tax_conflicts_preview": conflicts[:_MAX_CONFLICTS_IN_REPORT],
        "tax_unresolved": sum(p["tax_unresolved"] for p in plans),
    }


def _public_file_report(plan: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in plan.items() if not k.startswith("_")}
    if out.get("tax_conflicts"):
        out["tax_conflicts_preview"] = out["tax_conflicts"][:_MAX_CONFLICTS_IN_REPORT]
        out["tax_conflicts_total"] = len(out["tax_conflicts"])
        out["tax_conflicts"] = out["tax_conflicts"][:20]
    return out


# 税列回填目标列 → SQL 类型（VALUES 连表里的显式 CAST，防参数类型歧义）。
# 白名单同时防注入：列名来自计划 JSON，未来新增列必须先在这里显式登记。
_TAX_FILL_COLUMN_TYPES: dict[str, str] = {
    "amount_inc_tax": "numeric",
    "is_tax_inclusive": "boolean",
    "tax_amount": "numeric",
}

# 每批 fill 行数（与 loader._CHUNK / 本脚本快照插入同因）：单语句绑定参数必须
# 远低于 PostgreSQL 65535 上限——每行 2 个参数（rid/val），1000 行 = 2001 参数。
_TAX_FILL_CHUNK = 1000


def _apply_tax_fills(db: Session, batch: SysImportBatch,
                     fills: list[dict[str, Any]]) -> int:
    """税三件套回填：按列分块 UPDATE ... FROM (VALUES ...) 批量执行（#396 S6）。

    与逐条 UPDATE 同一守卫语义：限定本批次（计划只来自 import_batch_id=本批次的
    订单；loader 的 upsert 会把既有行迁到新批次，无此谓词时并发迁移后的行会被
    本批次原件的值误写）+ 目标列 IS NULL 只补不覆盖。返回真实补齐的行数
    （RETURNING 计数，被并发抢先补掉的行不计）。按 _TAX_FILL_CHUNK 分块，防
    超大批次（>3 万行）撑爆单语句 65535 绑定参数上限。
    """
    by_column: dict[str, list[dict[str, Any]]] = {}
    for fill in fills:
        by_column.setdefault(fill["column"], []).append(fill)
    filled = 0
    for column, column_fills in by_column.items():
        sql_type = _TAX_FILL_COLUMN_TYPES.get(column)
        if sql_type is None:
            raise ValueError(f"未登记的税列回填目标：{column}")
        for start in range(0, len(column_fills), _TAX_FILL_CHUNK):
            chunk = column_fills[start:start + _TAX_FILL_CHUNK]
            values_sql = ", ".join(
                f"(:rid{i}, CAST(:val{i} AS {sql_type}))"
                for i in range(len(chunk))
            )
            params: dict[str, Any] = {"batch_id": batch.id}
            for i, fill in enumerate(chunk):
                params[f"rid{i}"] = fill["raw_order_id"]
                params[f"val{i}"] = fill["value"]
            stmt = text(
                f"""
                UPDATE f_sales_order AS so
                SET {column} = v.val
                FROM (VALUES {values_sql}) AS v(raw_order_id, val)
                WHERE so.raw_order_id = v.raw_order_id
                  AND so.import_batch_id = :batch_id
                  AND so.{column} IS NULL
                RETURNING so.id
                """
            )
            filled += len(db.execute(stmt, params).all())
    return filled


def _apply_file(db: Session, batch: SysImportBatch, plan: dict[str, Any],
                operator: str, manifest_sha256: str) -> dict[str, Any]:
    """单文件执行（调用方负责事务边界：每文件一个事务，失败只回滚本文件）。

    计划在本事务内重算（``_plan_file``），不跨事务复用 dry-run 的行数据——
    并发变更只会让 ON CONFLICT/WHERE NULL 守卫少写，不会写错。
    """
    inserted = 0
    for start in range(0, len(plan["_snapshot_rows"]), 1000):
        chunk = plan["_snapshot_rows"][start:start + 1000]
        stmt = pg_insert(FSalesReconciliationSnapshot).values(chunk).on_conflict_do_nothing(
            index_elements=[FSalesReconciliationSnapshot.raw_order_id,
                            FSalesReconciliationSnapshot.import_batch_id])
        inserted += len(db.execute(
            stmt.returning(FSalesReconciliationSnapshot.id)).all())
    tax_filled = _apply_tax_fills(db, batch, plan["_tax_fills"])
    db.add(SysAuditLog(
        entity_type="sales_reconciliation_backfill",
        entity_id=batch.id,
        action="execute",
        before_json=None,
        after_json={
            "snapshots_planned": plan["snapshots_planned"],
            "snapshots_inserted": inserted,
            "snapshots_existing": plan["snapshots_existing"],
            "tax_fills_planned": plan["tax_fills_planned"],
            "tax_columns_filled": tax_filled,
            "tax_conflicts": len(plan["tax_conflicts"]),
            "tax_unresolved": plan["tax_unresolved"],
        },
        reason=f"D-40/D-41 销售对账快照与含税三件套历史回填（manifest_sha256={manifest_sha256}）",
        operated_by=operator,
    ))
    return {
        "snapshots_inserted": inserted,
        "tax_columns_filled": tax_filled,
        # 冲突计数与 dry-run 报告同源同口径（锁内重算的同一计划），可逐文件核对
        "tax_conflicts": len(plan["tax_conflicts"]),
        "tax_unresolved": plan["tax_unresolved"],
    }


def _record_skip_audit(db: Session, batch: SysImportBatch, plan: dict[str, Any],
                       operator: str, manifest_sha256: str) -> None:
    """坏档跳过的审计留痕（与执行的审计同表同 entity_type，action=skip）：
    零写入也记一条——报告 / 审计 / 库内三者可互相对上「这批为什么没回填」。"""
    db.add(SysAuditLog(
        entity_type="sales_reconciliation_backfill",
        entity_id=batch.id,
        action="skip",
        before_json=None,
        after_json={
            "status": plan["status"],
            "snapshots_planned": 0,
            "snapshots_inserted": 0,
            "tax_fills_planned": 0,
            "tax_columns_filled": 0,
            "tax_unresolved": plan["tax_unresolved"],
        },
        reason=f"D-40/D-41 回填跳过坏档（status={plan['status']}，"
               f"manifest_sha256={manifest_sha256}）",
        operated_by=operator,
    ))


def run_backfill(
    db: Session,
    *,
    execute: bool = False,
    operator: str | None = None,
    confirm_manifest_sha256: str | None = None,
    batch_ids: list[int] | None = None,
) -> dict[str, Any]:
    if execute:
        if not operator or not confirm_manifest_sha256:
            raise BackfillError(
                "execution_confirmation_required",
                "execute 必须同时提供 --operator 与 --confirm-manifest-sha256")
        # 会话级锁在专用连接上跨单文件事务存活，防两份回填并发（导入不受此
        # 锁影响——快照 ON CONFLICT / 税列 WHERE NULL 守卫保证与导入并发也
        # 幂等安全）。取/放必须同连接，见 _acquire_backfill_lock。
        lock = _acquire_backfill_lock(db)
        try:
            if not _active_operator(db, operator):
                raise BackfillError("operator_not_active",
                                    "execute 必须使用在册且启用的实名系统账号")
            batches = _iter_batches(db, batch_ids)
            plans = [_plan_file(db, b) for b in batches]
            manifest_sha256 = _canonical_sha256(_manifest(batches, plans))
            if manifest_sha256 != confirm_manifest_sha256:
                raise BackfillError(
                    "manifest_confirmation_mismatch",
                    "--confirm-manifest-sha256 必须精确等于当前库状态的 dry-run 指纹"
                    "（库或归档在 dry-run 之后发生了变化，请重新 dry-run 核对）")
            db.rollback()   # 计划事务结束；逐文件重开事务
            # 核准清单按文件留底：锁内每文件重算后逐文件比对（同一口径的
            # _manifest_entry），不一致 → 该文件零写入记失败、其余文件继续。
            approved = {p["batch_id"]: _manifest_entry(p) for p in plans}
            results: list[dict[str, Any]] = []
            for batch in batches:
                try:
                    # 文件事务从此条语句开始（上一文件 commit/rollback 后连接已
                    # 归还池）：先取文件级 xact 锁、重验操作员，再重算计划比对。
                    _acquire_file_xact_lock(db, batch.id)
                    if not _active_operator(db, operator):
                        raise BackfillError(
                            "operator_not_active",
                            f"操作员 {operator} 在执行过程中被停用/删除：已回滚当前"
                            f"文件（batch_id={batch.id}）并中止；已提交的文件不受影响，"
                            "换有效账号重跑即可（幂等）")
                    fresh = _plan_file(db, batch)
                    entry = _manifest_entry(fresh)
                    if entry != approved[batch.id]:
                        changed = sorted(
                            k for k in entry if entry[k] != approved[batch.id][k])
                        raise BackfillError(
                            "plan_changed_since_dry_run",
                            f"锁内重算计划与 dry-run 核准清单不一致"
                            f"（batch_id={batch.id}，变化字段：{changed}）：本文件零写入；"
                            "本次执行结束后请重新 dry-run 核对并重走确认流程（幂等）")
                    if fresh["status"] in _SKIPPED_PLAN_STATUSES:
                        # 坏档不计执行：保持计划状态、零写入，审计 action=skip
                        _record_skip_audit(db, batch, fresh, operator, manifest_sha256)
                        file_status = fresh["status"]
                        actual = {
                            "snapshots_inserted": 0,
                            "tax_columns_filled": 0,
                            "tax_conflicts": len(fresh["tax_conflicts"]),
                            "tax_unresolved": fresh["tax_unresolved"],
                        }
                    else:
                        actual = _apply_file(db, batch, fresh, operator, manifest_sha256)
                        file_status = "executed"
                    _assert_lock_held(db, lock)   # 丢锁不提交：互斥失效时绝不落盘
                    db.commit()
                    results.append({"batch_id": batch.id, "status": file_status, **actual})
                except Exception as exc:   # noqa: BLE001 —— 单文件失败不拖垮其余文件
                    db.rollback()
                    if isinstance(exc, BackfillError) and exc.code in _ABORT_ERROR_CODES:
                        raise   # 中止码：当前文件已回滚，不再继续后续文件
                    failed: dict[str, Any] = {
                        "batch_id": batch.id, "status": "execute_failed",
                        "error": str(exc)[:300]}
                    if isinstance(exc, BackfillError):
                        failed["code"] = exc.code
                    results.append(failed)
            files_failed = sum(1 for r in results if r["status"] == "execute_failed")
            report = {
                # files_failed>0 → partial（CLI 退出码 3）：有文件没落成，不能报
                # applied；坏档跳过与空范围（0 个批次）不算失败，仍为 applied。
                "status": "partial" if files_failed else "applied",
                "dry_run": False,
                "operator": operator,
                "manifest_sha256": manifest_sha256,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "files": results,
                "totals": {
                    "files_executed": sum(1 for r in results if r["status"] == "executed"),
                    "files_skipped": sum(1 for r in results
                                         if r["status"] in _SKIPPED_PLAN_STATUSES),
                    "files_failed": files_failed,
                    "snapshots_inserted": sum(r.get("snapshots_inserted", 0) for r in results),
                    "tax_columns_filled": sum(r.get("tax_columns_filled", 0) for r in results),
                    "tax_conflicts_total": sum(r.get("tax_conflicts", 0) for r in results),
                },
            }
            return report
        finally:
            # 先放锁再收尾 Session：即使 Session 收尾出错，锁也不滞留
            _release_backfill_lock(lock)
            db.rollback()
    # dry-run：只读事务，重算同一计划（先 rollback 保证从事务起点设置只读——
    # 若会话已有打开的事务，SET TRANSACTION READ ONLY 会报错）
    db.rollback()
    db.execute(text("SET TRANSACTION READ ONLY"))
    try:
        batches = _iter_batches(db, batch_ids)
        plans = [_plan_file(db, b) for b in batches]
        manifest_sha256 = _canonical_sha256(_manifest(batches, plans))
        return {
            "status": "ready",
            "dry_run": True,
            "manifest_sha256": manifest_sha256,
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "files": [_public_file_report(p) for p in plans],
            "totals": _totals(plans),
        }
    finally:
        db.rollback()


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(
        description="销售单已对账快照与含税三件套历史回填（D-40/D-41，默认 dry-run）")
    parser.add_argument("--execute", action="store_true",
                        help="真正执行（默认 dry-run 只读）")
    parser.add_argument("--operator",
                        help="execute 必填：在册有效 sys_user 用户名")
    parser.add_argument("--confirm-manifest-sha256",
                        help="execute 必填：dry-run 输出的 manifest_sha256")
    parser.add_argument("--batch-id", type=int, action="append", default=None,
                        help="只回放指定批次（可重复，用于小范围试跑）")
    return parser


def main(argv: list[str] | None = None) -> int:
    execute = False
    try:
        args = _parser().parse_args(argv)
        execute = bool(args.execute)
        if not os.environ.get("DATABASE_URL"):
            raise BackfillError(
                "database_url_required",
                "必须通过环境变量 DATABASE_URL 显式指定目标库（绝不隐式连接默认库）")
        from app.db import SessionLocal

        with SessionLocal() as db:
            result = run_backfill(
                db,
                execute=execute,
                operator=args.operator,
                confirm_manifest_sha256=args.confirm_manifest_sha256,
                batch_ids=args.batch_id,
            )
    except _SafeArgumentError:
        print(json.dumps({"status": "failed", "dry_run": execute,
                          "code": "invalid_arguments"}, ensure_ascii=False))
        return 2
    except BackfillError as exc:
        print(json.dumps({"status": "failed", "dry_run": execute,
                          "code": exc.code, "message": exc.message},
                         ensure_ascii=False))
        if exc.code in _ABORT_ERROR_CODES:
            # 中止码可能发生在部分文件已提交之后：已提交的保留（幂等可续跑）
            print(f"backfill aborted ({exc.code}): current file rolled back; earlier "
                  "committed files (if any) stay; rerun is idempotent", file=sys.stderr)
        else:
            print("backfill failed before any write; nothing committed", file=sys.stderr)
        return 2
    except Exception as exc:   # noqa: BLE001 —— 非预期错误：完整落 stderr，JSON 只给码
        print(json.dumps({"status": "failed", "dry_run": execute,
                          "code": "internal_error", "message": str(exc)[:300]},
                         ensure_ascii=False))
        print(f"backfill internal error: {exc!r}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result.get("dry_run") is False and result.get("status") == "partial":
        # 部分成功（MUST 口径）：3 与 0/2 区分——0=applied、2=拒绝/中止、3=partial
        print("backfill partially applied (exit 3): inspect files[].error / files[].code, "
              "fix the causes, then rerun the same dry-run -> confirm -> execute flow "
              "(idempotent; already-committed files add zero rows on rerun)",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
