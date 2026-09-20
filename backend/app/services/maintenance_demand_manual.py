"""维保需求单页面直改/直建（v1.36 Phase E）。

写入协议与 ETL/loader、总表 v2 对齐（缺一即静默数据腐化）：
advisory lock（DATA_CHANGE）→ workbook state 预锁（先于事实行锁）→
order/line 行锁 → 写 + override 账本 → bump workbook revision →
数量/PN 变更行进 maintenance_cost.recompute。

铁律：
- 成本列（cost_* / reference_* / anomaly_flags / cost_bucket）为 recompute
  独占，永不进白名单；
- 单头字段（order_no/salesperson/…）本版不可编辑（loader _MAINT_ORDER_UPD
  无保护，页面改了会被下次导入静默覆盖——审查 #1）；
- 手工行 edited_source='page_manual'：snapshot_diff/latest_missing 排除，
  防"手工单被报氚云已删、一键作废"陷阱（审查 #2）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)
from app.models.system import SysImportBatch
from app.services import maintenance_project_operations as _project_ops
from app.services.maintenance_cost import (
    MaintenanceCostRecomputeBusy,
    recompute as recompute_cost,
)


class DemandManualError(Exception):
    """业务校验失败（400 语义）。"""


class DemandManualConflict(Exception):
    """并发/状态冲突（409 语义）。"""


# 字段白名单（失败关闭）：只开放需求事实行上可安全页面编辑的列。
# 头字段在 f_maintenance_order、无 loader 保护 —— 显式排除（见模块 docstring）。
# 成本列 recompute 独占；异常标记是导入/重算混合所有权 —— 双双排除。
DEMAND_LINE_EDITABLE_FIELDS: frozenset[str] = frozenset({
    "qty", "return_qty", "serial_numbers", "description", "pn_raw", "pn_std",
})

_QTY_COLUMNS = frozenset({"qty", "return_qty"})
_REPRICE_COLUMNS = _QTY_COLUMNS | {"pn_raw", "pn_std"}


def _now() -> datetime:
    return datetime.now(UTC)


def _lock_data_change(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"),
               {"k": DATA_CHANGE_ADVISORY_LOCK_KEY})


def _line_for_update(db: Session, raw_line_id: str) -> FMaintenanceLine:
    line = db.execute(
        select(FMaintenanceLine)
        .where(FMaintenanceLine.raw_line_id == raw_line_id)
        .with_for_update()
    ).scalar_one_or_none()
    if line is None:
        raise DemandManualError("需求单明细行不存在")
    if not line.is_active:
        raise DemandManualConflict("已作废的明细行不能页面编辑")
    return line


def _assigned_project_ids(db: Session, source_order_ids: set[str]) -> set[str]:
    from app.models.maintenance_source_assignment import (
        MaintenanceSourceOrderAssignment,
    )
    out: set[str] = set()
    for sid in source_order_ids:
        out.update(db.scalars(
            select(MaintenanceSourceOrderAssignment.project_id).where(
                MaintenanceSourceOrderAssignment.source_order_id == sid,
                MaintenanceSourceOrderAssignment.is_active.is_(True),
            )
        ))
    return out


def _resolve_part_id(db: Session, *, part_id: int | None, pn_std: str | None) -> int | None:
    """part_id 显式传入优先；否则按 pn_std 精确匹配（含 merged 回溯）。"""
    if part_id is not None:
        part = db.get(DimPart, part_id)
        if part is None:
            raise DemandManualError("型号主数据不存在")
        return part_id
    if not pn_std:
        return None
    part = db.execute(
        select(DimPart).where(DimPart.pn_std == pn_std.strip())
    ).scalar_one_or_none()
    while part is not None and part.merged_into_id is not None:
        part = db.get(DimPart, part.merged_into_id)
    return part.id if part is not None else None


def _validate_qty(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise DemandManualError(f"{field} 必须是数值")
    try:
        dec = Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise DemandManualError(f"{field} 必须是数值") from None
    if dec < 0 or dec > Decimal("99999999999"):
        raise DemandManualError(f"{field} 超出范围（0 – 99999999999）")
    return dec.quantize(Decimal("0.001"))


def _line_snapshot(line: FMaintenanceLine) -> dict:
    return {
        "raw_line_id": line.raw_line_id,
        "qty": str(line.qty) if line.qty is not None else None,
        "return_qty": str(line.return_qty) if line.return_qty is not None else None,
        "serial_numbers": line.serial_numbers,
        "description": line.description,
        "pn_raw": line.pn_raw,
        "pn_std": line.pn_std,
        "edited_source": line.edited_source,
        "manual_override": dict(line.manual_override or {}),
    }


def _write_audit(db: Session, *, source_order_id: str, entity_id: str,
                 action: str, operated_by: str, reason: str,
                 before: dict | None, after: dict | None,
                 project_id: str | None) -> None:
    """需求单行审计。有项目挂靠 → operation_audit（String 实体键）；
    无挂靠 → 也落 operation_audit 但 project_id 用空哨兵行不可行（NOT NULL），
    故无挂靠时拒绝编辑（失败关闭，避免审计缺口——审查 #3）。"""
    if project_id is None:
        raise DemandManualError(
            "该需求单未归属任何项目，暂不支持页面编辑（审计要求项目挂靠）"
        )
    db.add(MaintenanceProjectOperationAudit(
        project_id=project_id,
        entity_type="demand_line",
        entity_id=str(entity_id),
        action=action,
        before_json=before,
        after_json=after,
        reason=reason,
        operated_by=operated_by,
    ))


def patch_demand_line(
    db: Session,
    *,
    raw_line_id: str,
    updates: dict,
    reason: str,
    operated_by: str,
) -> dict:
    """页面直改一条需求明细行。

    updates 只允许 DEMAND_LINE_EDITABLE_FIELDS 内的字段（未知键拒绝）。
    每个被改字段写入 manual_override 账本 {value, source_value, updated_by,
    updated_at}；主列同步写新值（读侧零特殊处理）。qty/pn 变更行进 recompute。
    行上有 override 的字段在 loader upsert 时保留现值（见 loader 侧配套改动）。
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise DemandManualError("必须填写修改原因（1–256 字）")
    unknown = set(updates) - DEMAND_LINE_EDITABLE_FIELDS
    if unknown:
        raise DemandManualError(f"不支持的修改字段：{sorted(unknown)}")

    _lock_data_change(db)
    # 锁序与 loader 全局一致：advisory → workbook state 预锁 → order 行锁 →
    # line 行锁。先用无锁读定位归属项目，再按序锁定。
    from app.models.maintenance import FMaintenanceOrder as _FO
    order_id = db.scalar(
        select(FMaintenanceLine.order_id).where(FMaintenanceLine.raw_line_id == raw_line_id)
    )
    if order_id is None:
        raise DemandManualError("需求单明细行不存在")
    order_raw_id = db.scalar(
        select(_FO.raw_order_id).where(_FO.id == order_id)
    )
    projects = _assigned_project_ids(db, {order_raw_id})
    prelocked = _project_ops.lock_workbook_states(db, project_ids=projects)

    order = db.execute(
        select(_FO).where(_FO.id == order_id).with_for_update()
    ).scalar_one()
    line = _line_for_update(db, raw_line_id)

    before = _line_snapshot(line)
    override: dict = dict(line.manual_override or {})
    changed_columns: dict = {}
    for field in DEMAND_LINE_EDITABLE_FIELDS & set(updates):
        value = updates[field]
        if field in _QTY_COLUMNS:
            value = _validate_qty(value, field)
            current = getattr(line, field)
            current = current.quantize(Decimal("0.001")) if current is not None else None
            if value == current:
                continue
        elif field == "serial_numbers":
            if value is not None and not isinstance(value, str):
                raise DemandManualError("SN 必须是文本")
            value = (value or "").strip() or None
            if value == (line.serial_numbers or None):
                continue
        else:
            if value is not None and not isinstance(value, str):
                raise DemandManualError(f"{field} 必须是文本")
            if field in ("pn_raw", "pn_std") and not (value or "").strip():
                raise DemandManualError(f"{field} 不能为空")
            value = (value or "").strip() or None if field == "description" else (value or "").strip()
            if value == getattr(line, field):
                continue
        # override 账本：source_value 只记首次改前的原值（后续覆盖保留最早原值，
        # clear_override 才能一路还原到氚云原始事实）
        entry = override.get(field) or {}
        override[field] = {
            "value": str(value) if isinstance(value, Decimal) else value,
            "source_value": entry.get("source_value"),
            "updated_by": operated_by,
            "updated_at": _now().isoformat(),
        }
        if entry.get("source_value") is None:
            old = getattr(line, field)
            override[field]["source_value"] = (
                str(old) if isinstance(old, Decimal) else old
            )
        changed_columns[field] = value

    if not changed_columns:
        return {"changed": False, **_line_snapshot(line)}

    # part_id 跟随 PN 变化（身份解析沿 merged 链）
    if {"pn_raw", "pn_std"} & set(changed_columns):
        resolved = _resolve_part_id(
            db, part_id=None,
            pn_std=str(changed_columns.get("pn_std") or line.pn_std or ""),
        )
        if resolved is not None:
            changed_columns["part_id"] = resolved

    for field, value in changed_columns.items():
        setattr(line, field, value)
    line.manual_override = override
    if line.edited_source == "wbdd":
        line.edited_source = "page_manual"

    after = _line_snapshot(line)
    _write_audit(
        db, source_order_id=order.raw_order_id, entity_id=line.raw_line_id,
        action="page_update", operated_by=operated_by, reason=reason.strip(),
        before=before, after=after,
        project_id=(sorted(projects)[0] if len(projects) == 1 else None),
    )

    # bump workbook revision（防旧总表 base_version 留空路径静默覆盖）。
    # 走 canonical helper：同一事务内至多 bump 一次，幂等语义与全库一致。
    for pid in sorted(projects):
        state = prelocked.get(pid)
        if state is not None:
            _project_ops.bump_locked_workbook_revision(db, state=state)

    # 数量/PN 变更 → 成本重算（同事务，全成或全败）
    if _REPRICE_COLUMNS & set(changed_columns):
        db.flush()
        try:
            recompute_cost(db, commit=False, line_ids={line.id})
        except MaintenanceCostRecomputeBusy as exc:
            raise DemandManualConflict(
                "成本重算忙（导入/重算进行中），请稍后重试"
            ) from exc

    db.flush()
    return {"changed": True, **_line_snapshot(line)}


def create_manual_demand_line(
    db: Session,
    *,
    order_date,  # date
    project_id: str,
    pn_std: str,
    qty,
    return_qty=0,
    serial_numbers: str | None = None,
    description: str | None = None,
    reason: str,
    operated_by: str,
) -> dict:
    """页面直建一条手工需求行：建头（专用前缀）+ 建行（manual-line: 前缀，
    edited_source='page_manual'）+ 合成 SysImportBatch。复用总表 v2 的先例。
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise DemandManualError("必须填写新建原因（1–256 字）")
    if not (pn_std or "").strip():
        raise DemandManualError("PN 不能为空")
    qty_v = _validate_qty(qty, "qty")
    return_qty_v = _validate_qty(return_qty, "return_qty")

    _lock_data_change(db)
    prelocked = _project_ops.lock_workbook_states(db, project_ids=[project_id])

    part_id = _resolve_part_id(db, part_id=None, pn_std=pn_std)
    if part_id is None:
        raise DemandManualError(f"PN 未匹配到型号主数据：{pn_std}（先在主数据治理建档）")

    batch = SysImportBatch(
        filename="manual-demand-line-page.xlsx",
        file_type="maintenance",
        file_hash=hashlib.sha256(
            f"page-manual-demand:{uuid4().hex}".encode("utf-8")
        ).hexdigest(),
        uploaded_by=operated_by,
        rows_total=1,
        rows_inserted=1,
        status="success",
        report_json={"source": "page_manual_create", "project_id": project_id},
    )
    db.add(batch)
    db.flush()

    suffix = uuid4().hex
    order = FMaintenanceOrder(
        raw_order_id=f"page-manual-{suffix}",
        order_no=f"PAGE-{suffix[:12].upper()}",
        order_date=order_date,
        project_raw=None,
        data_status="页面手工",
        import_batch_id=batch.id,
    )
    db.add(order)
    db.flush()

    line = FMaintenanceLine(
        raw_line_id="manual-line:" + hashlib.sha256(suffix.encode()).hexdigest()[:48],
        order_id=order.id,
        line_no=1,
        part_id=part_id,
        pn_std=pn_std.strip(),
        pn_raw=pn_std.strip(),
        description=(description or "").strip() or None,
        qty=qty_v,
        return_qty=return_qty_v,
        serial_numbers=(serial_numbers or "").strip() or None,
        edited_source="page_manual",
        is_active=True,
        import_batch_id=batch.id,
    )
    db.add(line)
    db.flush()

    _write_audit(
        db, source_order_id=order.raw_order_id, entity_id=line.raw_line_id,
        action="page_create", operated_by=operated_by, reason=reason.strip(),
        before=None, after=_line_snapshot(line), project_id=project_id,
    )
    state = prelocked.get(project_id)
    if state is not None:
        _project_ops.bump_locked_workbook_revision(db, state=state)

    db.flush()
    try:
        recompute_cost(db, commit=False, line_ids={line.id})
    except MaintenanceCostRecomputeBusy as exc:
        raise DemandManualConflict("成本重算忙，请稍后重试") from exc
    db.flush()
    return {"changed": True, "order_no": order.order_no,
            **_line_snapshot(line)}


def clear_override(
    db: Session,
    *,
    raw_line_id: str,
    field: str,
    reason: str,
    operated_by: str,
) -> dict:
    """撤销一个字段的 override，恢复到账本里的 source_value（氚云原始值）。"""
    if field not in DEMAND_LINE_EDITABLE_FIELDS:
        raise DemandManualError(f"不支持撤销的字段：{field}")
    if not isinstance(reason, str) or not reason.strip():
        raise DemandManualError("必须填写撤销原因")

    _lock_data_change(db)
    line = _line_for_update(db, raw_line_id)
    from app.models.maintenance import FMaintenanceOrder as _FO
    order = db.execute(
        select(_FO).where(_FO.id == line.order_id).with_for_update()
    ).scalar_one()
    projects = _assigned_project_ids(db, {order.raw_order_id})

    override = dict(line.manual_override or {})
    entry = override.get(field)
    if entry is None:
        raise DemandManualError(f"该行没有 {field} 的 override 记录")

    before = _line_snapshot(line)
    source_value = entry.get("source_value")
    if field in _QTY_COLUMNS and source_value is not None:
        setattr(line, field, Decimal(str(source_value)).quantize(Decimal("0.001")))
    else:
        setattr(line, field, source_value)
    override.pop(field, None)
    line.manual_override = override
    after = _line_snapshot(line)
    _write_audit(
        db, source_order_id=order.raw_order_id, entity_id=line.raw_line_id,
        action="page_override_clear", operated_by=operated_by, reason=reason.strip(),
        before=before, after=after,
        project_id=(sorted(projects)[0] if len(projects) == 1 else None),
    )
    if _REPRICE_COLUMNS & {field}:
        db.flush()
        try:
            recompute_cost(db, commit=False, line_ids={line.id})
        except MaintenanceCostRecomputeBusy as exc:
            raise DemandManualConflict("成本重算忙，请稍后重试") from exc
    db.flush()
    return {"changed": True, **_line_snapshot(line)}
