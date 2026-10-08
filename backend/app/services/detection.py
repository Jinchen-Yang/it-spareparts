"""检测单服务（板块 D，D-2/D-23）。

口径依据：docs/decisions/0002 D-23（检测环节为 SN 采集点、SN 挂 PN 下
一 SN 一物、实物 PN 以实物为准修正留痕）+ 甲方检测清单字段
（类别/PN/描述/数量/好坏/实收数量/SN/实物PN/处理方式/检测人）。

- 检测单是循环入库的权威事实：检测生成 SN 台账记录（好件 in_stock、坏件 bad_stock）；
- 分批检测合法：同一回收行可开多张检测单，累计实收不得超过该行清单数量；
- SN 数据库级唯一（一 SN 一物）；本次请求内重复 SN、与台账已存在 SN 冲突 → 422；
- 实物 PN 与标称不符时以实物为准解析身份，标称值保留在明细行（修正留痕）；
- 处理方式枚举待 J11 确认，当前自由文本（≤32 字符），确认后收紧为受控枚举。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.circulation import (
    CONDITIONS,
    CirculationSnItem,
    DetectionItem,
    DetectionSheet,
    RecycleBatch,
    RecycleLine,
)
from app.models.dimensions import DimPart

MAX_SN_LENGTH = 128


class DetectionValidationError(Exception):
    """检测单校验失败（422，零写入）。message 为甲方语言。"""


@dataclass
class DetectionItemDraft:
    line_id: str
    received_qty: Decimal
    actual_condition: str
    handling: str
    sns: list[str] = field(default_factory=list)
    actual_pn_raw: str | None = None


@dataclass
class DetectionSheetDraft:
    batch_id: str
    inspector: str
    note: str | None
    items: list[DetectionItemDraft]


def _resolve_part(db: Session, pn_std: str) -> int | None:
    return db.execute(
        select(DimPart.id).where(DimPart.pn_std == pn_std)
    ).scalar_one_or_none()


def validate_detection_sheet(db: Session, draft: DetectionSheetDraft) -> dict:
    """全量校验（不写库），返回解析上下文；任何违规抛 DetectionValidationError。

    校验项：批次存在、行存在且属于批次、实收为正、实测状态枚举、处理方式非空、
    SN 非空唯一且数量=实收、累计实收不超清单数量、SN 与台账不冲突。
    """
    issues: list[str] = []
    batch = db.get(RecycleBatch, draft.batch_id)
    if batch is None:
        raise DetectionValidationError(f"回收批次 {draft.batch_id} 不存在")
    if not draft.inspector or len(draft.inspector) > 64:
        raise DetectionValidationError("检测人必须为 1–64 字符实名")
    if not draft.items:
        raise DetectionValidationError("检测单至少包含一条检测明细")
    if any(not item.handling or len(item.handling) > 32 for item in draft.items):
        raise DetectionValidationError("处理方式必须为 1–32 字符（枚举待 J11 确认后收紧）")

    # 请求内 SN 全局查重
    all_sns: list[str] = []
    for item in draft.items:
        for sn in item.sns:
            sn = sn.strip()
            if not sn or len(sn) > MAX_SN_LENGTH:
                raise DetectionValidationError(f"SN「{sn}」为空或超过 {MAX_SN_LENGTH} 字符")
            if sn in all_sns:
                raise DetectionValidationError(f"SN「{sn}」在本单内重复（一个 SN 对应一个物品）")
            all_sns.append(sn)
    # 与既有台账冲突
    if all_sns:
        existing = db.execute(
            select(CirculationSnItem.sn).where(CirculationSnItem.sn.in_(all_sns))
        ).scalars().all()
        if existing:
            raise DetectionValidationError(
                f"以下 SN 已存在于台账：{'、'.join(sorted(existing)[:5])}（一个 SN 对应一个物品）"
            )

    # 行校验与累计实收
    resolved: list[dict] = []
    per_line_seen: dict[str, Decimal] = {}
    for item in draft.items:
        line = db.get(RecycleLine, item.line_id)
        if line is None:
            raise DetectionValidationError(f"回收明细 {item.line_id} 不存在")
        if line.batch_id != draft.batch_id:
            raise DetectionValidationError(f"回收明细 {line.pn_raw} 不属于批次 {draft.batch_id}")
        if item.received_qty <= 0:
            raise DetectionValidationError(f"行 {line.pn_raw}：实收数量必须为正数")
        if item.actual_condition not in CONDITIONS:
            raise DetectionValidationError(
                f"行 {line.pn_raw}：实测状态只允许 {'/'.join(CONDITIONS)}"
            )
        if item.sns and len(item.sns) != int(item.received_qty):
            raise DetectionValidationError(
                f"行 {line.pn_raw}：提供 SN 时个数必须等于实收数量"
                f"（SN {len(item.sns)} 个 ≠ 实收 {item.received_qty}）"
            )
        per_line_seen[item.line_id] = per_line_seen.get(item.line_id, Decimal("0")) + item.received_qty
        resolved.append({"item": item, "line": line})

    for line_id, seen_qty in per_line_seen.items():
        line = db.get(RecycleLine, line_id)
        detected = db.execute(
            select(func.coalesce(func.sum(DetectionItem.received_qty), 0)).where(
                DetectionItem.line_id == line_id)
        ).scalar_one()
        if seen_qty + Decimal(str(detected)) > line.qty:
            raise DetectionValidationError(
                f"行 {line.pn_raw}：累计实收 {seen_qty + Decimal(str(detected))} "
                f"超过清单数量 {line.qty}（分批检测总量不得超过清单数量）"
            )

    return {"batch": batch, "resolved": resolved}


def apply_detection_sheet(
    db: Session, draft: DetectionSheetDraft, *, operator: str
) -> DetectionSheet:
    """校验 + 原子落库：检测单、检测明细、SN 台账记录。任何校验失败零写入。"""
    ctx = validate_detection_sheet(db, draft)

    sheet = DetectionSheet(
        sheet_id=str(uuid.uuid4()),
        batch_id=draft.batch_id,
        inspector=draft.inspector,
        note=draft.note,
        created_at=datetime.now(),
    )
    db.add(sheet)
    # 显式 flush：外键链 sheet → item → SN 逐级先落父表
    db.flush()

    for entry in ctx["resolved"]:
        item: DetectionItemDraft = entry["item"]
        line: RecycleLine = entry["line"]
        # 实物 PN 以实物为准（D-23）：实际>标称 优先；均按 strip+upper 归一
        nominal = line.pn_raw
        actual = (item.actual_pn_raw or "").strip().upper() or None
        pn_for_sn = actual or nominal
        part_id = _resolve_part(db, pn_for_sn)

        det_item = DetectionItem(
            item_id=str(uuid.uuid4()),
            sheet_id=sheet.sheet_id,
            line_id=line.line_id,
            nominal_pn_raw=nominal,
            actual_pn_raw=actual,
            part_id=part_id,
            received_qty=item.received_qty,
            actual_condition=item.actual_condition,
            handling=item.handling,
            sn_count=len(item.sns),
        )
        db.add(det_item)
        # 显式 flush：SN 台账对 detection_item 有 DB 级外键，先落明细再落 SN
        db.flush()
        # 逐件 SN 入台账（一个 SN 对应一个物品；无 SN 低值件只记明细）
        lifecycle = "in_stock" if item.actual_condition == "好件" else "bad_stock"
        for sn in item.sns:
            db.add(CirculationSnItem(
                sn=sn.strip(),
                part_id=part_id,
                pn_std=pn_for_sn,
                lifecycle_status=lifecycle,
                source_batch_id=sheet.batch_id,
                detection_item_id=det_item.item_id,
            ))
    db.commit()
    db.refresh(sheet)
    return sheet
