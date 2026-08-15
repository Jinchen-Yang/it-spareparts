"""氚云发货单（CKD）导入服务（C1a/F1）。

- parse：识别 166 列双表头（字段码行 + 字段名行），主表+明细分组展开；
- store_preview：落 raw 行，零业务写入；
- apply：仅「维保供货」行按 WBDD→稳定项目关联，把明细数量入前置库账本
  （front_stock kind=shipment_in，source_type=f_maintenance_line，幂等）；
  销售出库/采购退货不计入前置库；无法稳定关联的行进异常清单，不按名称猜。
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services.date_loose import parse_amount_loose, parse_date_loose
from app.services import maintenance_front_stock as front_stock

MAX_PREVIEW_BYTES = 256 * 1024 * 1024  # 真实文件含内嵌图片，允许大文件
_CKD_RE = re.compile(r"(CKD-\d{8}-\d{4})")
_WBDD_RE = re.compile(r"(WBDD-\d{8}-\d{4})")
_XSDD_RE = re.compile(r"(XSDD-\d{8}-\d{4})")

_HEAD_COLUMNS = [
    "出库单号", "出库日期", "出库类别", "出库备件/整机", "出库仓库", "仓储中心",
    "维保需求单(备件)", "维保需求单", "销售订单(备件)", "销售订单", "销售人员",
    "项目经理", "维保需求人", "备注", "数据状态",
]
_LINE_COLUMNS = [
    "备件明细.数据ID(不可修改)", "备件明细.序号", "备件明细.数据标题", "备件明细.产品名称", "备件明细.备件自贴码",
    "备件明细.备件PN", "备件明细.备件SN号", "备件明细.备件描述", "备件明细.所在仓库",
    "备件明细.所在库位", "备件明细.产品大类", "备件明细.产品小类", "备件明细.品牌",
    "备件明细.单位", "备件明细.出库数量", "备件明细.成本单价", "备件明细.成本金额",
    "备件明细.备件测试合格",
]


class CkdParseError(RuntimeError):
    """发货单文件不可解析。"""


class CkdBatchError(RuntimeError):
    """发货单批次状态错误。"""


@dataclass
class CkdHeadData:
    row_no: int
    values: dict[str, str]
    lines: list["CkdLineData"] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class CkdLineData:
    row_no: int
    values: dict[str, str]
    issues: list[str] = field(default_factory=list)


def _header_index(headers: list[str], name: str) -> int | None:
    for idx, value in enumerate(headers, 1):
        if value == name:
            return idx
    return None


def _cell(row: tuple, index: int | None) -> str | None:
    if index is None or index > len(row):
        return None
    value = row[index - 1]
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return None
    text = str(value).strip()
    if len(text) > 4096:
        return None  # 内嵌图片 base64：不落库
    return text or None


def _non_empty(row: tuple) -> bool:
    return bool(row) and any(
        v is not None and not isinstance(v, (bytes, bytearray)) and str(v).strip()
        for v in row
    )


def parse_ckd_workbook(data: bytes, filename: str) -> dict:
    """解析发货单工作簿。返回 {source_kind, file_hash, heads: [CkdHeadData], line_count}。"""
    if len(data) > MAX_PREVIEW_BYTES:
        raise CkdParseError("发货单文件超过大小上限")
    try:
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise CkdParseError(f"无法读取 Excel 文件：{type(exc).__name__}") from exc
    try:
        sheet = workbook["Sheet1"] if "Sheet1" in workbook.sheetnames else workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        header_rows: list[tuple] = []
        for row in rows:
            if header_rows or any(
                v is not None and str(v).startswith("F00") for v in row
            ):
                header_rows.append(row)
                if len(header_rows) >= 2:
                    break
        if len(header_rows) < 2:
            raise CkdParseError("发货单缺少字段码/字段名双表头")
        headers = [
            str(v).strip() if v is not None else "" for v in header_rows[-1]
        ]
        head_indexes = {name: _header_index(headers, name) for name in _HEAD_COLUMNS}
        line_indexes = {name: _header_index(headers, name) for name in _LINE_COLUMNS}
        if head_indexes["出库单号"] is None:
            raise CkdParseError("发货单缺少「出库单号」列")
        if line_indexes["备件明细.备件PN"] is None:
            raise CkdParseError("发货单缺少「备件明细.备件PN」列")

        heads: list[CkdHeadData] = []
        current: CkdHeadData | None = None
        row_no = 3
        for row in rows:
            if not _non_empty(row):
                continue
            head_anchor = _cell(row, head_indexes["出库单号"])
            if head_anchor:
                current = CkdHeadData(
                    row_no=row_no,
                    values={
                        name: _cell(row, head_indexes[name]) for name in _HEAD_COLUMNS
                    },
                )
                heads.append(current)
            line_values = {
                name: _cell(row, line_indexes[name]) for name in _LINE_COLUMNS
            }
            if line_values["备件明细.备件PN"] or line_values["备件明细.出库数量"]:
                line = CkdLineData(row_no=row_no, values=line_values)
                if current is None:
                    raise CkdParseError("明细行出现在主表行之前")
                current.lines.append(line)
            row_no += 1
    finally:
        workbook.close()
    return {
        "source_kind": "ckd_shipment_v1",
        "file_hash": hashlib.sha256(data).hexdigest(),
        "filename": filename,
        "heads": heads,
        "line_count": sum(len(h.lines) for h in heads),
    }


def store_preview(db: Session, parsed: dict, operated_by: str) -> str:
    """落 raw 行并返回 batch_id。零业务写入。"""
    batch = MaintenanceCkdImportBatch(
        batch_id=str(uuid4()),
        file_hash=parsed["file_hash"],
        filename=parsed["filename"][:255],
        uploaded_by=operated_by,
        head_rows=len(parsed["heads"]),
        line_rows=parsed["line_count"],
        status="pending",
    )
    db.add(batch)
    db.flush()
    issue_rows = 0
    for head in parsed["heads"]:
        values = head.values
        order_no = _clean(values["出库单号"], _CKD_RE)
        if not order_no:
            head.issues.append("出库单号缺失或格式异常")
        order_date, _ = parse_date_loose(values["出库日期"])
        wbdd = _clean(values["维保需求单(备件)"] or values["维保需求单"], _WBDD_RE)
        if values["出库类别"] == "维保供货" and not wbdd:
            head.issues.append("维保供货缺少维保需求单关联")
        if head.issues:
            issue_rows += 1
        head_row = MaintenanceCkdHeadRow(
            row_id=str(uuid4()),
            batch_id=batch.batch_id,
            row_no=head.row_no,
            order_no_raw=values["出库单号"],
            order_date_raw=values["出库日期"],
            category_raw=values["出库类别"],
            machine_or_part_raw=values["出库备件/整机"],
            warehouse_raw=values["出库仓库"],
            wh_center_raw=values["仓储中心"],
            wbdd_raw=values["维保需求单(备件)"] or values["维保需求单"],
            sales_order_raw=values["销售订单(备件)"] or values["销售订单"],
            salesperson_raw=values["销售人员"],
            project_manager_raw=values["项目经理"],
            maintainer_raw=values["维保需求人"],
            data_status_raw=values["数据状态"],
            remark_raw=values["备注"],
            order_no=order_no,
            order_date=order_date,
            category=values["出库类别"] or None,
            wbdd_no=wbdd,
            sales_order_no=_clean(
                values["销售订单(备件)"] or values["销售订单"], _XSDD_RE
            ),
            issues=head.issues,
        )
        db.add(head_row)
        db.flush()
        for line in head.lines:
            line_values = line.values
            if line.issues:
                issue_rows += 1
            pn = (line_values["备件明细.备件PN"] or "").strip() or None
            qty = parse_amount_loose(line_values["备件明细.出库数量"])
            if pn and qty is None:
                line.issues.append("出库数量无法解析")
            db.add(
                MaintenanceCkdLineRow(
                    row_id=str(uuid4()),
                    batch_id=batch.batch_id,
                    head_row_id=head_row.row_id,
                    row_no=line.row_no,
                    data_id_raw=line_values["备件明细.数据ID(不可修改)"],
                    seq_raw=line_values["备件明细.序号"],
                    title_raw=line_values["备件明细.数据标题"],
                    part_name_raw=line_values["备件明细.产品名称"],
                    self_code_raw=line_values["备件明细.备件自贴码"],
                    pn_raw=line_values["备件明细.备件PN"],
                    sn_raw=line_values["备件明细.备件SN号"],
                    desc_raw=line_values["备件明细.备件描述"],
                    warehouse_raw=line_values["备件明细.所在仓库"],
                    location_raw=line_values["备件明细.所在库位"],
                    brand_raw=line_values["备件明细.品牌"],
                    category_major_raw=line_values["备件明细.产品大类"],
                    category_minor_raw=line_values["备件明细.产品小类"],
                    unit_raw=line_values["备件明细.单位"],
                    out_qty_raw=line_values["备件明细.出库数量"],
                    unit_cost_raw=line_values["备件明细.成本单价"],
                    cost_amount_raw=line_values["备件明细.成本金额"],
                    test_result_raw=line_values["备件明细.备件测试合格"],
                    pn=pn,
                    out_qty=qty,
                    unit_cost=parse_amount_loose(line_values["备件明细.成本单价"]),
                    cost_amount=parse_amount_loose(line_values["备件明细.成本金额"]),
                    issues=line.issues,
                )
            )
    batch.issue_rows = issue_rows
    batch.report_json = {
        "head_rows": batch.head_rows,
        "line_rows": batch.line_rows,
        "issue_rows": issue_rows,
    }
    db.commit()
    return batch.batch_id


def _clean(raw: str | None, pattern: re.Pattern) -> str | None:
    if not raw:
        return None
    match = pattern.search(raw)
    return match.group(1) if match else None


def _resolve_project_id(db: Session, wbdd_no: str) -> str | None:
    order = db.execute(
        select(FMaintenanceOrder).where(FMaintenanceOrder.order_no == wbdd_no)
    ).scalar_one_or_none()
    if order is None:
        return None
    assignment = db.execute(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ).scalar_one_or_none()
    return assignment.project_id if assignment is not None else None


def _resolve_part_id(db: Session, pn: str) -> int | None:
    part = db.execute(
        select(DimPart).where(DimPart.pn_std == pn)
    ).scalar_one_or_none()
    return part.id if part is not None else None


def apply_batch(db: Session, batch_id: str, operated_by: str) -> dict:
    """把「维保供货」明细入前置库账本；幂等。"""
    batch = db.get(MaintenanceCkdImportBatch, batch_id)
    if batch is None:
        raise CkdBatchError("发货单批次不存在")
    if batch.status == "applied":
        raise CkdBatchError("发货单批次已应用，不能重复应用")
    summary = {
        "maintenance_heads": 0,
        "applied_lines": 0,
        "skipped_lines": 0,
        "ignored_heads": 0,
    }
    head_rows = (
        db.execute(
            select(MaintenanceCkdHeadRow)
            .where(MaintenanceCkdHeadRow.batch_id == batch_id)
            .order_by(MaintenanceCkdHeadRow.row_no)
        )
        .scalars()
        .all()
    )
    for head in head_rows:
        if head.category != "维保供货":
            summary["ignored_heads"] += 1
            continue
        summary["maintenance_heads"] += 1
        if head.wbdd_no is None:
            summary["skipped_lines"] += 1
            continue
        project_id = _resolve_project_id(db, head.wbdd_no)
        if project_id is None:
            summary["skipped_lines"] += 1
            continue
        lines = (
            db.execute(
                select(MaintenanceCkdLineRow)
                .where(MaintenanceCkdLineRow.head_row_id == head.row_id)
                .order_by(MaintenanceCkdLineRow.row_no)
            )
            .scalars()
            .all()
        )
        for line in lines:
            if line.pn is None or line.out_qty is None or line.out_qty <= 0:
                summary["skipped_lines"] += 1
                continue
            part_id = _resolve_part_id(db, line.pn)
            if part_id is None:
                summary["skipped_lines"] += 1
                continue
            try:
                front_stock.apply_movement(
                    db,
                    project_id=project_id,
                    part_id=part_id,
                    kind="shipment_in",
                    source_type="f_maintenance_line",
                    source_ref=f"ckd:{head.order_no}:"
                    f"{line.data_id_raw or line.seq_raw or line.row_no}",
                    qty=line.out_qty,
                    warehouse_name="",
                    reason=f"发货单 {head.order_no} 维保供货入前置库",
                    operated_by=operated_by,
                )
                summary["applied_lines"] += 1
            except front_stock.FrontStockError:
                summary["skipped_lines"] += 1
    batch.status = "applied"
    batch.applied_by = operated_by
    batch.applied_at = datetime.now(timezone.utc)
    db.commit()
    return summary
