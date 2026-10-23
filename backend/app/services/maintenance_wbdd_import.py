"""维保备件需求单（WBDD）专用上传（plan v1.3 M1-6/M1-7/M1-8）。

流程（单步端点，fail-closed）：
  幂等重放检查 → 零写入预检（文件类型门 + 命名字段门）→ 通用管线快照 upsert
  → 快照差异报告 → 回执落库 → 成本回填（复用 maintenance_cost.recompute(commit=False)，
  引擎零改动）→ 单次提交（同 Idempotency-Key 重放返回原报告）。
  2026-08-26 起导入事实、回执、成本重算与项目工作簿 revision bump 在同一事务：
  任一步失败整体回滚，不留「新事实 + 旧成本」的可见窗口。

D-26：核心身份、日期、PN和数量按表头名称校验。保留旧 90/91 反馈标签，
扩展/缺少可选列的文件标记为 name；地址位置不决定头/明细身份。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.etl import mapping, pipeline, reader, sheet_selection
from app.models.maintenance import (
    FMaintenanceOrder,
)
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.models.system import SysImportBatch
from app.services import maintenance_wbdd_reconciliation
from app.services import maintenance_cost

_log = logging.getLogger(__name__)

_LINE_ANCHOR = "需求明细.数据ID(不可修改)"


def _import_lock_identity(operator: str, idempotency_key: str) -> str:
    """Return a PostgreSQL-text-safe, unambiguous identity for the xact lock."""

    composite = json.dumps(
        [operator, idempotency_key],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"maintenance-wbdd-import:{composite}"


class WbddImportError(Exception):
    """零写入拒绝（422 语义）：错类型、缺核心字段或重复表头。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_wbdd_layout(columns: list[str]) -> str:
    """Named schema gate; optional columns may be absent or at any position."""
    cols = list(columns)
    required = {"数据ID(不可修改)", "需求单号", "制单日期", _LINE_ANCHOR,
                "需求明细.需供货产品", "需求明细.需求数量"}
    missing = sorted(required - set(cols))
    if missing:
        raise WbddImportError(
            "layout_unknown",
            "缺少需求单核心字段：" + "、".join(missing),
        )
    duplicates = sorted({name for name in cols if name and cols.count(name) > 1})
    if duplicates:
        raise WbddImportError(
            "layout_unknown",
            "重复表头无法确定字段来源：" + "、".join(duplicates),
        )
    return str(len(cols)) if len(cols) in (90, 91) else "name"


def precheck_wbdd_file(file_path: str) -> str:
    """零写入预检：必须是 WBDD 文件且包含核心命名字段。返回布局标签。"""
    reader.reject_roundtrip_workbook(file_path)
    inspected = reader.inspect_workbook(file_path, load_data=False)
    selection = sheet_selection.select_workbook_sheets(inspected)
    if not selection.selected or selection.file_type != mapping.MAINTENANCE:
        raise WbddImportError(
            "not_wbdd_file",
            "不是维保备件需求单导出文件（本端点只接受 WBDD；"
            "采购/销售/库存/报销文件请走各自入口）",
        )
    primary = selection.selected[0]
    if not primary.columns:
        raise WbddImportError("layout_unknown", "无法读取工作表表头")
    return validate_wbdd_layout(list(primary.columns))


def find_receipt(db: Session, *, uploaded_by: str,
                 idempotency_key: str) -> MaintenanceWbddImportReceipt | None:
    return db.execute(
        select(MaintenanceWbddImportReceipt).where(
            MaintenanceWbddImportReceipt.uploaded_by == uploaded_by,
            MaintenanceWbddImportReceipt.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()


def public_report(report: dict) -> dict:
    """Private source membership stays server-side, including replay responses."""
    return {key: value for key, value in report.items() if not key.startswith("_")}


def latest_source_batch(db: Session) -> SysImportBatch | None:
    return db.scalar(select(SysImportBatch).where(
        SysImportBatch.file_type == mapping.MAINTENANCE,
        SysImportBatch.status == "success",
        SysImportBatch.report_json.op("?")("_wbdd_source_snapshot"),
    ).order_by(SysImportBatch.id.desc()).limit(1))


def latest_missing(db: Session, *, allowed_project_ids: set[str] | None = None) -> dict:
    """D-27: compare the immutable source, never mutable last-import ownership."""
    batch = latest_source_batch(db)
    if batch is None:
        return {"readiness": "snapshot_required", "missing_orders": [],
                "missing_lines": [], "missing_count": 0, "missing_line_count": 0,
                "total_candidate_count": 0, "truncated": False,
                "batch_id": None, "uploaded_at": None, "candidate_digest": None,
                "coverage": {"daily_windows": [], "source_order_count": 0, "blocked_order_count": 0},
                "warnings": ["历史上传没有保存完整来源集合，请重新上传需要核对的需求单原件。"]}
    return maintenance_wbdd_reconciliation.build_candidates(
        db, batch, allowed_project_ids=allowed_project_ids,
    )


def import_wbdd(db: Session, *, file_path: str, original_name: str,
                operator: str, idempotency_key: str,
                actor_user_id: int | None = None) -> tuple[dict, bool]:
    """执行导入并返回 (报告, replayed)。调用方负责 HTTP 错误映射与临时文件清理。

    提交语义（2026-08-26 单事务化）：导入批次/事实、快照差异、回执与成本重算
    （recompute(commit=False)，含项目工作簿 revision bump）在同一事务一次提交；
    任一步失败整体回滚（fail closed），不留「新事实 + 旧成本」可见窗口。
    upsert 幂等，客户端整体重试安全。重算失败的回滚在本函数内完成，调用方
    拿到异常时会话已是干净的。
    """
    # 幂等判定必须位于同键事务锁内；否则两个并发请求都可先看到 none，后者
    # 最终只会撞 unique/500，而不是稳定 replay 首个结果。
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
        _import_lock_identity(operator, idempotency_key), 0,
    ))))
    existing = find_receipt(db, uploaded_by=operator, idempotency_key=idempotency_key)
    if existing is not None:
        if existing.file_hash != pipeline.sha256_file(file_path):
            raise WbddImportError("idempotency_file_mismatch", "同一上传请求不能更换文件，请重新选择文件上传")
        report = dict(existing.report_json or {})
        if report.get("recompute") is None:
            # 只可能命中单事务化之前两半提交留下的历史回执（事实已提交、重算缺失）。
            # 若重放只回放报告，这批单的成本会永远停在导入前的口径——报告看起来还
            # 是成功的（静默）。重放时补跑重算（导入本身仍幂等，不会重复入库）。
            try:
                report["recompute"] = maintenance_cost.recompute(db, commit=False)
                existing.report_json = dict(report)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return public_report(report), True

    layout = precheck_wbdd_file(file_path)  # 零写入门：非 WBDD / 布局不符在此拒绝

    batch = pipeline.run_import(
        db,
        file_path,
        original_name,
        uploaded_by=operator,
        actor_user_id=actor_user_id,
        mode="upsert",
        auto_assign_maintenance_projects=True,
    )
    report_counts = dict(batch.report_json or {})

    # The raw source identities were captured before business validation by the
    # shared pipeline. They remain immutable after later imports and edits.
    candidates = maintenance_wbdd_reconciliation.build_candidates(db, batch)
    diff = {
        "missing_orders": candidates.get("missing_count", 0),
        "missing_lines": candidates.get("missing_line_count", 0),
        "coverage": candidates.get("coverage", []),
        "window": None,
    }

    report = {
        "batch_id": batch.id,
        "file_hash": batch.file_hash,
        "layout": layout,
        "snapshot_diff": diff,
        **report_counts,
    }

    receipt = MaintenanceWbddImportReceipt(
        batch_id=batch.id,
        idempotency_key=idempotency_key,
        uploaded_by=operator,
        file_hash=batch.file_hash,
        layout=layout,
        # 与函数返回的可变 report 隔离。否则下方追加 recompute 会先原地改掉
        # ORM 当前值，随后赋一份相等的新 dict 仍会被 flush 判为净变化 0。
        report_json=dict(report),
    )
    db.add(receipt)
    db.flush()

    # 成本回填接线（铁律 2：只接线不重写）。与导入同事务（commit=False），
    # 重算异常 → 整体回滚，绝不提交半截状态。run_import 已持有数据变更 advisory
    # 锁（同会话 xact 锁可重入），此处 pg_try_advisory_xact_lock 必成功，busy 仅
    # 可能来自测试注入等异常路径——同样整体回滚后上抛，由 API 映射 409。
    try:
        recompute_stats = maintenance_cost.recompute(db, commit=False)
    except Exception:
        db.rollback()
        raise
    report["recompute"] = recompute_stats
    # 赋独立新对象，使 JSONB 变化可被 ORM 稳定检测并写入 receipt。
    receipt.report_json = dict(report)
    db.commit()
    return public_report(report), False


def latest_health(db: Session) -> dict:
    """WBDD 源健康（M1 版；M4 扩展为四源 /health）。"""
    row = db.execute(
        select(
            func.max(FMaintenanceOrder.order_date),
            func.count(FMaintenanceOrder.id),
        )
    ).one()
    as_of, total_orders = row
    last_receipt = db.execute(
        select(MaintenanceWbddImportReceipt)
        .order_by(MaintenanceWbddImportReceipt.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if total_orders == 0:
        readiness = "not_imported"
    else:
        readiness = "ready"
    return {
        "readiness": readiness,
        "as_of": as_of.isoformat() if as_of else None,
        "orders_total": total_orders if total_orders else None,
        "batch_id": last_receipt.batch_id if last_receipt else None,
        "uploaded_at": (
            last_receipt.created_at.isoformat() if last_receipt else None
        ),
        "layout": last_receipt.layout if last_receipt else None,
    }
