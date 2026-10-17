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
   同一文件，保持口径一致）；仅库内为 NULL 时填；库内已有不同值 → 记冲突不覆盖；
   原件缺失/哈希不符/解析失败/无该值 → 记未解决保持 NULL。

只写以上两处，绝不触碰任何其它既有列。解析复用导入同一代码路径
（pipeline.load_workbook / transform_workbook），列映射与导入完全同源。

安全护栏（沿用 remediate_contract_amount_inc_tax 的模式）
----------------------------------------------------------
* 默认 dry-run：只读事务，输出 JSON 报告与计划 manifest sha256；
* ``--execute`` 必须同时给 ``--operator``（在册有效 sys_user）与
  ``--confirm-manifest-sha256``（dry-run 指纹）；执行前在锁内重算计划，指纹
  不一致拒绝执行；
* 会话级 PostgreSQL advisory lock 防并发回填；
* 单文件单事务：部分失败只记入报告，不影响其他文件；每文件落 SysAuditLog
  审计（operator + 实际计数 + manifest 指纹）；
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

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.etl import mapping, pipeline
from app.models.sales import FSalesOrder, FSalesReconciliationSnapshot
from app.models.system import SysAuditLog, SysImportBatch, SysRawFile
from app.services.sales_reconciliation import SOURCE_BACKFILL, snapshot_content_hash

TAX_COLUMNS = ("is_tax_inclusive", "tax_amount", "amount_inc_tax")
_QUANTUM = Decimal("0.01")
# 会话级 advisory lock key：hashtextextended 确定性 bigint，独立于 DATA_CHANGE_ADVISORY_LOCK_KEY
_LOCK_KEY_SEED = "backfill_sales_reconciliation"
_MAX_CONFLICTS_IN_REPORT = 200


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
    # FOR SHARE：授权检查与写事务之间账号不可被停用（同 remediation 脚本）。
    return db.scalar(text(
        "SELECT 1 FROM sys_user "
        "WHERE username = :operator AND is_active IS TRUE "
        "AND deleted_at IS NULL LIMIT 1 FOR SHARE"
    ), {"operator": operator}) is not None


def _lock_key() -> Any:
    return func.hashtextextended(_LOCK_KEY_SEED, 0)


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


def _orders_with_null_tax(db: Session, batch_id: int) -> list[Any]:
    """import_batch_id=本批次 且三件套至少一列为 NULL 的订单（税列回填候选）。

    定序：fills 列表顺序进 tax_fills_plan_sha256，无 ORDER BY 的行序在
    PG 不保证稳定，dry↔execute 指纹比对可能误拒（方向安全，但没必
    要）——按 raw_order_id 定序消除。
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
            or_(
                FSalesOrder.is_tax_inclusive.is_(None),
                FSalesOrder.tax_amount.is_(None),
                FSalesOrder.amount_inc_tax.is_(None),
            ),
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
        "tax_unresolved": 0,
        "transform_errors": 0,
        "_snapshot_rows": [],
        "_tax_fills": [],
    }
    null_tax_orders = _orders_with_null_tax(db, batch.id)

    def _unreadable(status: str, error: str | None = None) -> dict[str, Any]:
        # 原件读不了 → 本批次订单的 NULL 税列全部记未解决（保持 NULL）。
        # 计数单位与可读路径一致：列格数（订单×列为 NULL 的格子）。
        plan["status"] = status
        plan["tax_unresolved"] = sum(
            1 for o in null_tax_orders for c in TAX_COLUMNS if getattr(o, c) is None)
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
        rows = []
        for raw_id, head in result.orders.items():
            amounts = {
                "order_amount": head.get("amount_inc_tax"),
                "reconciled_amount": head.get("reconciled_amount"),
                "pending_amount": head.get("pending_amount"),
                "return_reconciled_amount": head.get("return_reconciled_amount"),
            }
            rows.append({
                "raw_order_id": raw_id,
                "order_no": head.get("order_no"),
                "sales_order_id": oid_map.get(raw_id),
                "import_batch_id": batch.id,
                "source": SOURCE_BACKFILL,
                "source_modified_at": head.get("source_modified_at"),
                **amounts,
                "content_hash": snapshot_content_hash(
                    raw_order_id=raw_id,
                    source_modified_at=head.get("source_modified_at"),
                    **amounts),
            })
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

    # ---- 2) 税列计划：只取本批次订单 + 只填 NULL，冲突不覆盖 ----
    fills: list[dict[str, Any]] = []
    for order in null_tax_orders:
        head = result.orders.get(order.raw_order_id)
        for column in TAX_COLUMNS:
            db_value = getattr(order, column)
            file_value = head.get(column) if head is not None else None
            if db_value is not None:
                if file_value is not None and file_value != db_value:
                    plan["tax_conflicts"].append({
                        "raw_order_id": order.raw_order_id,
                        "order_no": order.order_no,
                        "column": column,
                        "db_value": _jsonable_value(db_value),
                        "file_value": _jsonable_value(file_value),
                    })
                continue
            if file_value is None:
                plan["tax_unresolved"] += 1
            else:
                fills.append({
                    "raw_order_id": order.raw_order_id,
                    "column": column,
                    "value": file_value,
                })
    plan["_tax_fills"] = fills
    plan["tax_fills_planned"] = len(fills)
    plan["tax_fills_plan_sha256"] = _canonical_sha256(
        [[f["raw_order_id"], f["column"], _jsonable_value(f["value"])] for f in fills])
    return plan


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.quantize(_QUANTUM), ".2f")
    return value


def _manifest(batches: list[SysImportBatch], plans: list[dict[str, Any]]) -> dict[str, Any]:
    """dry-run ↔ execute 之间必须不变的计划身份（不含报告性字段与时间戳）。"""
    return {
        "batch_ids": [b.id for b in batches],
        "files": [{
            "batch_id": p["batch_id"],
            "file_hash": p["file_hash"],
            "status": p["status"],
            "snapshots_planned": p["snapshots_planned"],
            "snapshots_plan_sha256": p["snapshots_plan_sha256"],
            "tax_fills_planned": p["tax_fills_planned"],
            "tax_fills_plan_sha256": p["tax_fills_plan_sha256"],
            "tax_conflicts_count": len(p["tax_conflicts"]),
            "tax_unresolved": p["tax_unresolved"],
        } for p in plans],
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
    tax_filled = 0
    for fill in plan["_tax_fills"]:
        column = fill["column"]
        stmt = (
            update(FSalesOrder)
            .where(
                FSalesOrder.raw_order_id == fill["raw_order_id"],
                getattr(FSalesOrder, column).is_(None),
            )
            .values({column: fill["value"]})
            .returning(FSalesOrder.id)
        )
        tax_filled += len(db.execute(stmt).all())
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
    return {"snapshots_inserted": inserted, "tax_columns_filled": tax_filled}


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
        # 会话级锁：跨单文件事务存活，防两份回填并发（导入不受此锁影响——
        # 快照 ON CONFLICT / 税列 WHERE NULL 守卫保证与导入并发也幂等安全）。
        db.execute(select(func.pg_advisory_lock(_lock_key())))
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
            results: list[dict[str, Any]] = []
            for batch, plan in zip(batches, plans, strict=True):
                try:
                    # 锁内每文件重算计划：与 manifest 校验共用同一确定性口径
                    fresh = _plan_file(db, batch)
                    actual = _apply_file(db, batch, fresh, operator, manifest_sha256)
                    db.commit()
                    results.append({"batch_id": batch.id, "status": "executed", **actual})
                except Exception as exc:   # noqa: BLE001 —— 单文件失败不拖垮其余文件
                    db.rollback()
                    results.append({
                        "batch_id": batch.id, "status": "execute_failed",
                        "error": str(exc)[:300]})
            report = {
                "status": "applied",
                "dry_run": False,
                "operator": operator,
                "manifest_sha256": manifest_sha256,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "files": results,
                "totals": {
                    "files_executed": sum(1 for r in results if r["status"] == "executed"),
                    "files_failed": sum(1 for r in results if r["status"] == "execute_failed"),
                    "snapshots_inserted": sum(r.get("snapshots_inserted", 0) for r in results),
                    "tax_columns_filled": sum(r.get("tax_columns_filled", 0) for r in results),
                },
            }
            return report
        finally:
            db.execute(select(func.pg_advisory_unlock(_lock_key())))
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
        print("backfill failed before any write; nothing committed", file=sys.stderr)
        return 2
    except Exception as exc:   # noqa: BLE001 —— 非预期错误：完整落 stderr，JSON 只给码
        print(json.dumps({"status": "failed", "dry_run": execute,
                          "code": "internal_error", "message": str(exc)[:300]},
                         ensure_ascii=False))
        print(f"backfill internal error: {exc!r}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
