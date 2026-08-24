"""维保备件需求单（WBDD）专用上传（plan v1.3 M1-6/M1-7/M1-8）。

流程（单步端点，fail-closed）：
  幂等重放检查 → 零写入预检（文件类型门 + 90/91 布局门）→ 通用管线快照 upsert
  → 快照差异报告 → 提交 → 成本回填（复用 maintenance_cost.recompute，引擎零改动）
  → 回执落库（同 Idempotency-Key 重放返回原报告）。

布局规则（事实档案 §1.1）：
  91 列（当前年度版）：头段 [0..6]∪[44..90]，明细段 [7..43]；
  90 列（历史版）　：头段 [0..52]，明细段 [53..89]；
  锚定列 =「需求明细.数据ID(不可修改)」位置 D：D<44 → 91 列布局；D≥44 → 90 列布局。
  位置数学自检：91 = 头 54 + 明细 37；90 = 头 53 + 明细 37（差集恰为 91 列独有的
  「是否可以接受通用号」）。任一自检不过 → 整批拒绝，零写入。
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.etl import mapping, pipeline, reader, sheet_selection
from app.models.maintenance import FMaintenanceOrder, MaintenanceDemandTombstone
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.services import maintenance_cost

_log = logging.getLogger(__name__)

_LINE_ANCHOR = "需求明细.数据ID(不可修改)"
_SNAPSHOT_DIFF_SAMPLE = 50


class WbddImportError(Exception):
    """零写入拒绝（422 语义）。code ∈ not_wbdd_file / layout_unknown / segment_mismatch。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_wbdd_layout(columns: list[str]) -> str:
    """按列名序列做布局探测与按段防御校验；返回 "91" / "90"。

    列名须为 canonicalize 后的规范键（reader._inspect_frame 已做）。
    """
    cols = list(columns)
    if _LINE_ANCHOR not in cols:
        raise WbddImportError(
            "layout_unknown",
            "未找到「需求明细.数据ID(不可修改)」列，无法判定 90/91 列布局",
        )
    anchor = cols.index(_LINE_ANCHOR)
    total = len(cols)
    if anchor < 44:
        layout, head_idx, line_idx = "91", set(range(0, 7)) | set(range(44, total)), set(range(7, 44))
        expected = 91
    else:
        layout, head_idx, line_idx = "90", set(range(0, 53)), set(range(53, total))
        expected = 90
    if total != expected:
        raise WbddImportError(
            "layout_unknown",
            f"列数 {total} 与 {layout} 列布局不符（锚定列位置 {anchor}），"
            "请确认为氚云维保备件需求单标准导出",
        )
    # 按段防御校验：头字段必须落在头段、明细字段必须落在明细段（plan M1-1）
    positions = {name: i for i, name in enumerate(cols)}
    for name in mapping.MAINTENANCE_HEAD:
        pos = positions.get(name)
        if pos is not None and pos not in head_idx:
            raise WbddImportError(
                "segment_mismatch",
                f"头字段「{name}」出现在明细段（位置 {pos}，{layout} 列布局），整批拒绝",
            )
    for name in mapping.MAINTENANCE_LINE:
        pos = positions.get(name)
        if pos is not None and pos not in line_idx:
            raise WbddImportError(
                "segment_mismatch",
                f"明细字段「{name}」出现在头段（位置 {pos}，{layout} 列布局），整批拒绝",
            )
    return layout


def precheck_wbdd_file(file_path: str) -> str:
    """零写入预检：必须是 WBDD 文件且为完整 90/91 列布局。返回布局。"""
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


def snapshot_diff(db: Session, file_order_nos: set[str],
                  file_dates: list[date]) -> dict:
    """快照差异（F1 默认）：库内在本文件业务日期窗内、但本文件未出现的活跃单。

    只报告不删除不打标；已 tombstone（未恢复）的单属预期缺失，不计。
    窗口取文件自身 order_date [min, max]——历史全量库与年度快照文件天然只在窗内可比。
    """
    if not file_dates or not file_order_nos:
        return {"missing_orders": 0, "sample_order_nos": [],
                "window": None}
    lo, hi = min(file_dates), max(file_dates)
    tombstoned = select(MaintenanceDemandTombstone.source_order_id).where(
        MaintenanceDemandTombstone.restored_at.is_(None)
    )
    rows = db.execute(
        select(FMaintenanceOrder.order_no)
        .where(
            FMaintenanceOrder.order_date >= lo,
            FMaintenanceOrder.order_date <= hi,
            FMaintenanceOrder.order_no.notin_(file_order_nos),
            FMaintenanceOrder.raw_order_id.notin_(tombstoned),
        )
        .order_by(FMaintenanceOrder.order_no)
    ).scalars().all()
    return {
        "missing_orders": len(rows),
        "sample_order_nos": list(rows[:_SNAPSHOT_DIFF_SAMPLE]),
        "window": {"from": lo.isoformat(), "to": hi.isoformat()},
    }


# 差异清单明细上限（#265 契约）：与 void-fast 的 MAX_DELETE_HEADERS 对齐，
# 超出部分 truncated=true，前端提示分批作废。
_MISSING_DETAILS_LIMIT = 1_000
# 2026-08-24 用户拍板：解除「疑似不完整导出」50% 拦截（原 _MISSING_SUSPICIOUS_RATIO
# = 0.5，超阈值前端禁用批量作废并收起差异清单）——需要能 100% 全量跟随修改。
# missing_ratio 仍计算并返回，仅作展示；误传风险由前端确认弹窗与审计兜底。


def latest_missing(db: Session) -> dict:
    """最近一次快照的差异清单明细（#264/#267：氚云删单 → 系统跟随作废）。

    实时重算而非读导入时快照：批次事实（import_batch_id）与窗口都在库里，
    重算永远反映当前墓碑状态——已作废的单自动从清单消失，重复点击安全。
    """
    from app.models.maintenance import FMaintenanceLine
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )

    receipt = db.execute(
        select(MaintenanceWbddImportReceipt)
        .order_by(MaintenanceWbddImportReceipt.created_at.desc(),
                  MaintenanceWbddImportReceipt.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if receipt is None:
        return {"readiness": "not_imported", "missing_orders": [],
                "truncated": False}

    file_rows = db.execute(
        select(FMaintenanceOrder.order_no, FMaintenanceOrder.order_date)
        .where(FMaintenanceOrder.import_batch_id == receipt.batch_id)
    ).all()
    file_order_nos = {r.order_no for r in file_rows}
    file_dates = [r.order_date for r in file_rows if r.order_date is not None]
    diff = snapshot_diff(db, file_order_nos, file_dates)
    base = {
        "readiness": "ready",
        "batch_id": receipt.batch_id,
        "uploaded_at": receipt.created_at.isoformat(),
        "window": diff.get("window"),
        "missing_count": diff["missing_orders"],
        "missing_orders": [],
        "truncated": False,
        "db_active_in_window": None,
        "missing_ratio": None,
        # 2026-08-24 用户拍板：不再输出 suspicious——任意缺失占比都允许
        # 全量跟随作废（含 100%），前端差异清单恒展开。
    }
    if diff["missing_orders"] == 0 or not diff.get("window"):
        return base

    lo = date.fromisoformat(diff["window"]["from"])
    hi = date.fromisoformat(diff["window"]["to"])
    tombstoned = select(MaintenanceDemandTombstone.source_order_id).where(
        MaintenanceDemandTombstone.restored_at.is_(None)
    )
    active_line_count = (
        select(func.count(FMaintenanceLine.id))
        .where(
            FMaintenanceLine.order_id == FMaintenanceOrder.id,
            FMaintenanceLine.is_active.is_(True),
        )
        .correlate(FMaintenanceOrder)
        .scalar_subquery()
    )
    rows = db.execute(
        select(
            FMaintenanceOrder.raw_order_id,
            FMaintenanceOrder.order_no,
            FMaintenanceOrder.order_date,
            MaintenanceSourceOrderAssignment.project_id,
            active_line_count.label("line_count"),
        )
        .outerjoin(
            MaintenanceSourceOrderAssignment,
            (MaintenanceSourceOrderAssignment.source_order_id
             == FMaintenanceOrder.raw_order_id)
            & MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
        .where(
            FMaintenanceOrder.order_date >= lo,
            FMaintenanceOrder.order_date <= hi,
            FMaintenanceOrder.order_no.notin_(file_order_nos),
            FMaintenanceOrder.raw_order_id.notin_(tombstoned),
        )
        .order_by(FMaintenanceOrder.order_no)
        .limit(_MISSING_DETAILS_LIMIT + 1)
    ).all()
    active_in_window = int(db.scalar(
        select(func.count(FMaintenanceOrder.id)).where(
            FMaintenanceOrder.order_date >= lo,
            FMaintenanceOrder.order_date <= hi,
            FMaintenanceOrder.raw_order_id.notin_(tombstoned),
        )
    ) or 0)
    base["db_active_in_window"] = active_in_window
    if active_in_window:
        ratio = Decimal(diff["missing_orders"]) / Decimal(active_in_window)
        base["missing_ratio"] = float(round(ratio, 4))
    base["truncated"] = len(rows) > _MISSING_DETAILS_LIMIT
    base["missing_orders"] = [
        {
            "source_order_id": r.raw_order_id,
            "order_no": r.order_no,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "line_count": int(r.line_count or 0),
            "assigned_project_id": r.project_id,
        }
        for r in rows[:_MISSING_DETAILS_LIMIT]
    ]
    return base


def import_wbdd(db: Session, *, file_path: str, original_name: str,
                operator: str, idempotency_key: str) -> tuple[dict, bool]:
    """执行导入并返回 (报告, replayed)。调用方负责 HTTP 错误映射与临时文件清理。

    提交语义：导入（含批次/事实/回执）先 commit；recompute 随后独立执行——
    与 /api/import/upload 的 _post_import_refresh 一致，重算失败不影响已完成导入；
    重算忙（另一重算进行中）由调用方映射 409 recompute_busy（导入已提交，
    upsert 幂等，客户端整体重试安全）。
    """
    existing = find_receipt(db, uploaded_by=operator, idempotency_key=idempotency_key)
    if existing is not None:
        report = dict(existing.report_json or {})
        if report.get("recompute") is None:
            # 首次调用在 recompute 处 409（重算忙）时，回执已提交但成本回填未完成。
            # 若重放只回放报告，这批单的成本会永远停在导入前的口径——报告看起来还
            # 是成功的（静默）。重放时补跑重算（导入本身仍幂等，不会重复入库）。
            report["recompute"] = maintenance_cost.recompute(db)
            existing.report_json = report
            db.commit()
        return report, True

    layout = precheck_wbdd_file(file_path)  # 零写入门：非 WBDD / 布局不符在此拒绝

    batch = pipeline.run_import(
        db, file_path, original_name, uploaded_by=operator, mode="upsert"
    )
    report_counts = dict(batch.report_json or {})

    # 快照差异：文件内 order_no 集合与业务日期窗（从已入库批次读会混入历史，须用本批文件）
    # run_import 已完成 transform+load；order_no/日期从本批次事实反查（import_batch_id 定位）。
    file_rows = db.execute(
        select(FMaintenanceOrder.order_no, FMaintenanceOrder.order_date)
        .where(FMaintenanceOrder.import_batch_id == batch.id)
    ).all()
    # upsert 更新既有单时 import_batch_id 也被白名单刷新为本批，故本批文件的全部单都能反查到
    file_order_nos = {r.order_no for r in file_rows}
    file_dates = [r.order_date for r in file_rows if r.order_date is not None]
    diff = snapshot_diff(db, file_order_nos, file_dates)

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
        report_json=report,
    )
    db.add(receipt)
    db.commit()

    # 成本回填接线（铁律 2：只接线不重写）。busy 上抛由 API 映射 409。
    recompute_stats = maintenance_cost.recompute(db)
    report["recompute"] = recompute_stats
    receipt.report_json = report
    db.commit()
    return report, False


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
