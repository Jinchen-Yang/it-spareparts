"""回收清单导入服务（板块 D，D-1）。

口径依据：docs/decisions/0002（D-18~D-24，2026-10-07 甲方确认）+ 编码前方案
（docs/备件循环_回收清单导入_编码前方案_2026-10-07.md）。

- 回收清单 = 待检测队列的来源单据（PN 级批量）；本服务只记回收事实，
  不写成本/库存主账，不产生任何上架/送修决定（D-20/D-22）；
- 文件级幂等：完整文件 SHA-256（F 清单已确认的事实基础），同文件重传返回原批次；
- 未收录 PN 不臆造主档：part_id 置空 + needs_review（铁律：数据诚实）；
- 预检分级：错误行阻断整批（零写入），警告行放行但打标（总价不一致、同件多货位聚合）。
"""
from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.circulation import CONDITIONS, RecycleBatch, RecycleLine
from app.models.dimensions import DimPart

# 回收清单固定 10 列（甲方样本原词；列名不符 = 布局不符 = 422 零写入）
REQUIRED_COLUMNS = (
    "备件号", "物料描述", "库房代码", "货位编码", "2WR",
    "中文品名", "备件状态", "公司信息", "单价", "总价",
)
# 受控库区枚举（第十批样本实测 5 个；新库区出现→行级错误，先扩这里再导入）
KNOWN_WAREHOUSES = ("IT_BJDC", "IT_HZGZDC", "HZGZDC", "GZDC", "IT_HZDC")
# 连续空行达到该数即认为数据区结束（样本尾部有 ~104 万行公式空尾巴）
_EMPTY_ROW_EOF_LIMIT = 200
_CENT = Decimal("0.01")


class RecycleImportError(Exception):
    """布局/文件级错误（422，零写入）。"""


@dataclass
class RecycleIssue:
    row: int | None  # 源行号（1-based，含表头偏移）；文件级为 None
    field: str
    message: str


@dataclass
class RecycleLineDraft:
    pn_raw: str
    description: str | None
    category_label: str | None
    warehouse_code: str
    bin_code: str
    condition: str
    qty: Decimal
    unit_price: Decimal
    total_price: Decimal
    company_entity: str | None
    part_id: int | None = None
    needs_review: bool = False
    price_mismatch: bool = False


@dataclass
class RecycleParseReport:
    file_sha256: str
    rows_total: int = 0
    errors: list[RecycleIssue] = field(default_factory=list)
    warnings: list[RecycleIssue] = field(default_factory=list)
    lines: list[RecycleLineDraft] = field(default_factory=list)
    total_amount: Decimal = Decimal("0")

    @property
    def ok(self) -> bool:
        return not self.errors


def _cell_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cell_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def parse_recycle_workbook(data: bytes, db: Session) -> RecycleParseReport:
    """解析回收清单 xlsx → 聚合行草稿 + 分级问题清单。布局不符抛 RecycleImportError。"""
    from openpyxl import load_workbook

    sha = hashlib.sha256(data).hexdigest()
    report = RecycleParseReport(file_sha256=sha)
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)

    header = None
    for row in rows:
        cells = [_cell_str(c) for c in row]
        if any(cells):
            header = cells
            break
    if header is None:
        raise RecycleImportError("工作表为空：没有任何表头行")
    missing = [name for name in REQUIRED_COLUMNS if name not in header]
    if missing:
        raise RecycleImportError(f"布局不符：缺少列 {missing}（回收清单固定 10 列，见编码前方案）")
    col = {name: header.index(name) for name in REQUIRED_COLUMNS}

    # ── 逐行校验（错误不落库，聚合后仍逐行报告）──
    raw_rows: list[tuple[int, dict[str, Any]]] = []
    empty_streak = 0
    source_row_no = 1  # 表头是第 1 行
    for row in rows:
        source_row_no += 1
        cells = list(row) + [None] * (len(header) - len(row)) if len(row) < len(header) else list(row)
        if not any(_cell_str(c) for c in cells):
            empty_streak += 1
            if empty_streak >= _EMPTY_ROW_EOF_LIMIT:
                break  # 样本尾部 ~104 万行公式空尾巴，到此视为数据区结束
            continue
        empty_streak = 0
        raw_rows.append((source_row_no, {name: cells[idx] for name, idx in col.items()}))

    report.rows_total = len(raw_rows)

    # ── 聚合键：同 PN×库区×货位×状态 合并数量与金额 ──
    agg: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    pn_index: dict[str, int | None] = {}
    for source_row_no, r in raw_rows:
        pn = _cell_str(r["备件号"])
        if not pn:
            report.errors.append(RecycleIssue(source_row_no, "备件号", "备件号缺失"))
            continue
        pn_norm = pn.upper()
        condition = _cell_str(r["备件状态"])
        if condition not in CONDITIONS:
            report.errors.append(
                RecycleIssue(source_row_no, "备件状态", f"非法状态「{condition}」，只允许 {'/'.join(CONDITIONS)}")
            )
            continue
        warehouse = _cell_str(r["库房代码"])
        if not warehouse or warehouse not in KNOWN_WAREHOUSES:
            report.errors.append(
                RecycleIssue(source_row_no, "库房代码", f"未知库区「{warehouse}」，受控枚举：{'/'.join(KNOWN_WAREHOUSES)}")
            )
            continue
        qty = _cell_decimal(r["2WR"])
        if qty is None or qty <= 0:
            report.errors.append(RecycleIssue(source_row_no, "2WR", f"数量必须为正数，得到「{r['2WR']}」"))
            continue
        unit_price = _cell_decimal(r["单价"])
        if unit_price is None or unit_price < 0:
            report.errors.append(RecycleIssue(source_row_no, "单价", f"单价必须 ≥ 0，得到「{r['单价']}」"))
            continue
        total_price = _cell_decimal(r["总价"])
        if total_price is None:
            report.errors.append(RecycleIssue(source_row_no, "总价", "总价缺失或非数字"))
            continue

        bin_code = _cell_str(r["货位编码"]) or ""
        key = (pn_norm, warehouse, bin_code, condition)
        slot = agg.get(key)
        mismatch = abs(total_price - unit_price * qty) > _CENT
        if mismatch:
            report.warnings.append(
                RecycleIssue(source_row_no, "总价", f"总价 {total_price} ≠ 单价×数量 {unit_price * qty}（保留原值并打标）")
            )
        if slot is None:
            agg[key] = {
                "description": _cell_str(r["物料描述"]),
                "category_label": _cell_str(r["中文品名"]),
                "company_entity": _cell_str(r["公司信息"]),
                "qty": qty, "unit_price": unit_price, "total_price": total_price,
                "price_mismatch": mismatch,
            }
        else:
            slot["qty"] += qty
            slot["total_price"] += total_price
            slot["price_mismatch"] = slot["price_mismatch"] or mismatch

    # ── dim_part 匹配（pn_std 精确匹配；未收录 → needs_review，不臆造主档）──
    pns = sorted({key[0] for key in agg})
    if pns and db is not None:
        found = db.execute(
            select(DimPart.id, DimPart.pn_std).where(DimPart.pn_std.in_(pns))
        ).all()
        pn_index = {pn_std: part_id for part_id, pn_std in found}

    for (pn_norm, warehouse, bin_code, condition), slot in agg.items():
        part_id = pn_index.get(pn_norm)
        qty = slot["qty"]
        total = slot["total_price"]
        unit = (total / qty).quantize(_CENT, rounding=ROUND_HALF_UP)
        report.lines.append(RecycleLineDraft(
            pn_raw=pn_norm,
            description=slot["description"],
            category_label=slot["category_label"],
            warehouse_code=warehouse,
            bin_code=bin_code,
            condition=condition,
            qty=qty,
            unit_price=unit,
            total_price=total.quantize(_CENT, rounding=ROUND_HALF_UP),
            company_entity=slot["company_entity"],
            part_id=part_id,
            needs_review=part_id is None,
            price_mismatch=slot["price_mismatch"],
        ))
    report.total_amount = sum(
        (line.total_price for line in report.lines), Decimal("0")
    ).quantize(_CENT)
    return report


def apply_recycle_import(
    db: Session,
    report: RecycleParseReport,
    *,
    source_filename: str,
    imported_by: str,
    batch_label: str | None = None,
    source_doc_no: str | None = None,
) -> tuple[RecycleBatch, bool]:
    """落库一个回收批次。同 SHA-256 重传 → 返回既有批次（duplicate=True），零重复写入。

    调用方须保证 report.ok（错误行在 API 层 422 挡下）。
    """
    existing = db.execute(
        select(RecycleBatch).where(RecycleBatch.file_sha256 == report.file_sha256)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True
    if not report.ok:
        raise RecycleImportError("存在错误行，拒绝落库（先清零错误再导入）")

    batch = RecycleBatch(
        batch_id=str(uuid.uuid4()),
        source_filename=source_filename,
        file_sha256=report.file_sha256,
        batch_label=batch_label,
        source_doc_no=source_doc_no,
        imported_by=imported_by,
        imported_at=datetime.now(),
        row_count=report.rows_total,
        line_count=len(report.lines),
        warning_count=len(report.warnings),
        total_amount=report.total_amount,
    )
    db.add(batch)
    for line in report.lines:
        db.add(RecycleLine(
            line_id=str(uuid.uuid4()),
            batch_id=batch.batch_id,
            part_id=line.part_id,
            pn_raw=line.pn_raw,
            description=line.description,
            category_label=line.category_label,
            warehouse_code=line.warehouse_code,
            bin_code=line.bin_code,
            condition=line.condition,
            qty=line.qty,
            unit_price=line.unit_price,
            total_price=line.total_price,
            company_entity=line.company_entity,
            needs_review=line.needs_review,
            price_mismatch=line.price_mismatch,
        ))
    db.commit()
    db.refresh(batch)
    return batch, False
