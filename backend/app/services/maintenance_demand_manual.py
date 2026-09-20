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
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject as _Project
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
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


def _digest(snapshot: dict) -> str:
    """P1#5 OCC：行快照的稳定摘要（sha256 canonical json）。"""
    import hashlib as _hashlib
    payload = json.dumps(
        _line_snapshot_sortable(snapshot),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _line_snapshot_sortable(snapshot: dict) -> dict:
    """OCC 摘要输入：覆盖事实字段 + override 账本 + 身份（part_id 由
    pn 快照代理即可稳定；manual_override 的任何变化都改变 digest）。"""
    out = dict(snapshot)
    out.pop("digest", None)
    return out


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


def _check_scope(projects: set[str], allowed: set[str] | None, *, action: str) -> None:
    """P1#1：scope 受限账号只能动自己可见项目内的行；full scope（None）放行。"""
    if allowed is None:
        return
    if not projects or not projects <= set(allowed):
        raise DemandManualError(f"该需求单不在你的项目可见范围内，不能{action}")


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
    """part_id 显式传入优先；否则按 canonical 解析规范（与总表
    _exact_part_for_pn 同语义）：pn_std 归一化（strip+upper）精确匹配 →
    active 别名回退；merged 墓碑不直接返回，沿 merged_into 回溯到目标。"""
    if part_id is not None:
        part = db.get(DimPart, part_id)
        if part is None:
            raise DemandManualError("型号主数据不存在")
        return part_id
    if not pn_std:
        return None
    normalized = pn_std.strip().upper()
    # 先精确读身份（不过滤 merged——墓碑行正是回溯入口），命中后沿
    # merged_into 跳到 canonical 目标，最终须是 active 身份。
    part = db.scalar(select(DimPart).where(DimPart.pn_std == normalized))
    if part is None:
        from app.models.dimensions import PartAlias
        part = db.scalar(
            select(DimPart)
            .join(PartAlias, PartAlias.part_id == DimPart.id)
            .where(
                PartAlias.pn_raw == pn_std.strip(),
                PartAlias.status == "active",
            )
        )
    while part is not None and part.merged_into_id is not None:
        part = db.get(DimPart, part.merged_into_id)
    if part is not None and part.status == "merged":
        return None
    return part.id if part is not None else None


def _retire_manual_cost_override(db: Session, line_id: int, *, operated_by: str) -> None:
    """PN 换绑时停用行上 active 的人工成本证据（与总表 refill 同语义，
    maintenance_project_master_workbook._apply_line_refills）：旧 PN 的人工价
    不能跟着 line_id 偷渡到新身份——recompute 否则仍会拿旧价。"""
    from app.models.maintenance import MaintenanceManualCostOverride

    stale = db.scalar(
        select(MaintenanceManualCostOverride).where(
            MaintenanceManualCostOverride.line_id == line_id,
            MaintenanceManualCostOverride.active.is_(True),
        )
    )
    if stale is not None:
        stale.active = False
        stale.version += 1
        stale.updated_by = operated_by


def _validate_qty(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise DemandManualError(f"{field} 必须是数值")
    try:
        dec = Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise DemandManualError(f"{field} 必须是数值") from None
    # NaN / Inf：isinstance 放行 float，但 Decimal 转换产出非有限数——显式拒绝
    if not dec.is_finite():
        raise DemandManualError(f"{field} 必须是有限数值")
    if dec < 0 or dec > Decimal("99999999999"):
        raise DemandManualError(f"{field} 超出范围（0 – 99999999999）")
    return dec.quantize(Decimal("0.001"))


def _line_snapshot(line: FMaintenanceLine) -> dict:
    return {
        "raw_line_id": line.raw_line_id,
        "part_id": line.part_id,
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
    allowed_project_ids: set[str] | None = None,
    expected_digest: str | None = None,
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
    _check_scope(projects, allowed_project_ids, action="修改")
    prelocked = _project_ops.lock_workbook_states(db, project_ids=projects)

    order = db.execute(
        select(_FO).where(_FO.id == order_id).with_for_update()
    ).scalar_one()
    line = _line_for_update(db, raw_line_id)

    before = _line_snapshot(line)
    # P1#5 OCC：调用方持有读时的 digest；不匹配 = 行已被他人改过 → 409 重载。
    if expected_digest is not None and expected_digest != _digest(before):
        raise DemandManualConflict(
            "该明细行已被他人修改（版本不一致），请刷新行数据后重试"
        )
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
        # clear_override 才能一路还原到氚云原始事实）。判"首次"用 key 存在性
        # 而非值是否 None——原值本来就是 null 时，第二次编辑不能把 null 误当
        # 首次快照覆盖成第一次的手工值（review #9）。
        entry = override.get(field) or {}
        # P1#3：**先**从旧 entry/组内取首次身份快照，再重建 entry——顺序反了
        # 会把组快照丢进刚被覆盖的新 dict（A→B→C 只改同一字段时第二次快照
        # 就从 A 变成 B，clear 回不到最初身份）。
        group_pid = None
        if field in ("pn_std", "pn_raw"):
            for k in ("pn_std", "pn_raw"):
                e = override.get(k) or {}
                if "source_value_part_id" in e:
                    group_pid = e["source_value_part_id"]
                    break
        override[field] = {
            "value": str(value) if isinstance(value, Decimal) else value,
            "source_value": entry.get("source_value"),
            "updated_by": operated_by,
            "updated_at": _now().isoformat(),
        }
        if "source_value" not in entry:
            old = getattr(line, field)
            override[field]["source_value"] = (
                str(old) if isinstance(old, Decimal) else old
            )
        if field in ("pn_std", "pn_raw"):
            override[field]["source_value_part_id"] = (
                group_pid if group_pid is not None else line.part_id)
        changed_columns[field] = value

    if not changed_columns:
        # no-op 也要回 canonical digest：客户端据此刷新 OCC token
        return {"changed": False, "digest": _digest(_line_snapshot(line)),
                **_line_snapshot(line)}

    # P1#3 part_id 跟随 PN 变化：解析失败直接拒绝（不得保留旧身份造成
    # "显示 B 但成本按 A 取"的错位），成功则原子换新身份。
    # PN 是身份组：有效身份 = 新 pn_std（若有）否则现 pn_std 的 canonical 解析。
    # 单独提交的 pn_raw 若独立解析到**不同** part_id（不是 pn_std 身份的
    # alias）→ 拒绝：raw 是展示/追溯痕迹，不得静默换掉成本身份。
    if {"pn_raw", "pn_std"} & set(changed_columns):
        effective_std = str(
            changed_columns.get("pn_std") or line.pn_std or ""
        ).strip()
        resolved = _resolve_part_id(db, part_id=None, pn_std=effective_std)
        if resolved is None:
            raise DemandManualError(
                f"新 PN 未匹配到型号主数据：{effective_std}（拒绝修改，先在主数据治理建档）"
            )
        new_raw = changed_columns.get("pn_raw")
        if new_raw is not None:
            raw_resolved = _resolve_part_id(db, part_id=None, pn_std=str(new_raw))
            if raw_resolved is not None and raw_resolved != resolved:
                raise DemandManualError(
                    f"pn_raw {new_raw} 与 pn_std {effective_std} 指向不同型号身份，"
                    "请先统一（raw 只能是 std 身份的别名写法）"
                )
        if resolved != line.part_id:
            # 真实身份变化：旧 PN 的人工成本证据必须停用（同总表 refill 语义），
            # 否则 recompute 仍按旧人工价给新 PN 记账。
            _retire_manual_cost_override(db, line.id, operated_by=operated_by)
        changed_columns["part_id"] = resolved

    for field, value in changed_columns.items():
        setattr(line, field, value)
    line.manual_override = override
    # P1#4：真实 WBDD 行的字段覆盖不升格 edited_source——行来源仍是 wbdd，
    # snapshot_diff 的删单对账照常工作；保护完全由 manual_override 字段级承担。
    # 纯手工行（create 建的 page_manual）不变。

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
    return {"changed": True, "digest": _digest(_line_snapshot(line)),
            **_line_snapshot(line)}


def create_manual_demand_line(
    db: Session,
    *,
    order_date,  # date
    project_id: str,
    allowed_project_ids: set[str] | None = None,
    idempotency_key: str | None = None,
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

    _check_scope({project_id}, allowed_project_ids, action="新建需求行")
    _lock_data_change(db)

    # 幂等重放（防丢响应重复建）：同 key + 同完整归一化 payload 的重放返回原行；
    # 任何实质字段变化的同 key 重放拒绝（与 receipt 登记同一语义）。指纹落在
    # 合成 batch 的 report_json（不新增表），重放时按 raw_order_id 反查比对。
    from hashlib import sha1 as _sha1

    def _create_fingerprint() -> str:
        payload = json.dumps({
            "project_id": project_id,
            "order_date": str(order_date),
            "pn_std": pn_std.strip(),
            "qty": str(qty_v),
            "return_qty": str(return_qty_v),
            "serial_numbers": (serial_numbers or "").strip() or None,
            "description": (description or "").strip() or None,
            "reason": reason.strip(),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return _sha1(payload.encode("utf-8")).hexdigest()

    if idempotency_key is not None and not idempotency_key.strip():
        raise DemandManualError("幂等键不能为空字符串")
    digest_key = None
    if idempotency_key:
        digest_key = _sha1(
            f"demand-create:{operated_by}:{idempotency_key}".encode("utf-8")
        ).hexdigest()
        existing = db.execute(
            select(FMaintenanceOrder).where(
                FMaintenanceOrder.raw_order_id == f"page-manual-{digest_key}"
            )
        ).scalar_one_or_none()
        if existing is not None:
            line = db.execute(
                select(FMaintenanceLine).where(
                    FMaintenanceLine.order_id == existing.id,
                ).order_by(FMaintenanceLine.line_no).limit(1)
            ).scalar_one_or_none()
            if line is None or not line.is_active:
                # 原行已作废/不存在：重放不能"复活"已作废的业务事实——换 key 重建。
                raise DemandManualConflict(
                    "幂等键对应的原需求行已作废或不存在，请更换幂等键后重新创建"
                )
            assn = db.scalar(select(MaintenanceSourceOrderAssignment.project_id).where(
                MaintenanceSourceOrderAssignment.source_order_id == existing.raw_order_id,
                MaintenanceSourceOrderAssignment.is_active.is_(True)))
            if assn != project_id:
                raise DemandManualConflict("同一幂等键的 project_id 已变化，请核对后重试")
            # 完整 payload 指纹比对：只允许"完全相同的原始请求"重放命中。
            # 行此后被人工编辑不影响判定（指纹存 batch 元数据，不看行现值）。
            existing_batch = db.get(SysImportBatch, existing.import_batch_id)
            recorded = ((existing_batch.report_json or {}) if existing_batch else {}) \
                .get("request_fingerprint")
            if recorded is None or recorded != _create_fingerprint():
                raise DemandManualConflict(
                    "同一幂等键的请求内容已变化（qty/PN/日期/SN/描述/原因），"
                    "请核对后更换幂等键重试"
                )
            return {"changed": True, "replayed": True,
                    "order_no": existing.order_no,
                    "digest": _digest(_line_snapshot(line)),
                    **_line_snapshot(line)}
    prelocked = _project_ops.lock_workbook_states(db, project_ids=[project_id])

    project = db.scalar(
        select(_Project).where(_Project.project_id == project_id)
    )
    if project is None or not project.is_active:
        raise DemandManualError("维保项目不存在或已停用，不能新建需求行")

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
        report_json={
            "source": "page_manual_create",
            "project_id": project_id,
            # 幂等重放的完整请求指纹（同 key 同 payload 才命中；见上文比对）
            "request_fingerprint": _create_fingerprint(),
        },
    )
    db.add(batch)
    db.flush()

    suffix = digest_key if idempotency_key else uuid4().hex
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

    # P1#2：挂靠协议——没有 assignment，总表读侧（inner join）看不到这条行，
    # 后续也无法再编辑。手工单挂靠与 WBDD 挂靠同构：active + 实名创建者。
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()),
        project_id=project_id,
        source_order_id=order.raw_order_id,
        is_active=True,
        created_by=operated_by,
    ))
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
            "digest": _digest(_line_snapshot(line)),
            **_line_snapshot(line)}


def clear_override(
    db: Session,
    *,
    raw_line_id: str,
    field: str,
    reason: str,
    operated_by: str,
    allowed_project_ids: set[str] | None = None,
    expected_digest: str | None = None,
) -> dict:
    """撤销一个字段的 override，恢复到账本里的 source_value（氚云原始值）。"""
    if field not in DEMAND_LINE_EDITABLE_FIELDS:
        raise DemandManualError(f"不支持撤销的字段：{field}")
    if not isinstance(reason, str) or not reason.strip():
        raise DemandManualError("必须填写撤销原因")

    _lock_data_change(db)
    # P1#5：clear 同样走 state 预锁 → 行锁的统一锁序，且撤销后必须 bump revision
    from app.models.maintenance import FMaintenanceOrder as _FO
    order_id_pre = db.scalar(
        select(FMaintenanceLine.order_id).where(FMaintenanceLine.raw_line_id == raw_line_id)
    )
    if order_id_pre is None:
        raise DemandManualError("需求单明细行不存在")
    order_raw_pre = db.scalar(
        select(_FO.raw_order_id).where(_FO.id == order_id_pre)
    )
    projects = _assigned_project_ids(db, {order_raw_pre})
    _check_scope(projects, allowed_project_ids, action="撤销")
    prelocked = _project_ops.lock_workbook_states(db, project_ids=projects)
    order = db.execute(
        select(_FO).where(_FO.id == order_id_pre).with_for_update()
    ).scalar_one()
    line = _line_for_update(db, raw_line_id)

    before = _line_snapshot(line)
    if expected_digest is not None and expected_digest != _digest(before):
        raise DemandManualConflict(
            "该明细行已被他人修改（版本不一致），请刷新行数据后重试"
        )

    override = dict(line.manual_override or {})
    entry = override.get(field)
    # PN 是身份组：clear 组内任一控件都合法（哪怕只 override 了另一个字符串）
    if entry is None and not (
        field in ("pn_std", "pn_raw") and ("pn_std" in override or "pn_raw" in override)
    ):
        raise DemandManualError(f"该行没有 {field} 的 override 记录")

    before = _line_snapshot(line)
    restored_fields: set[str] = {field}
    # PN 组 clear 时 field 自身可能无独立 override（只 override 了另一字符串）
    source_value = (entry or {}).get("source_value")
    if field in _QTY_COLUMNS and source_value is not None:
        setattr(line, field, Decimal(str(source_value)).quantize(Decimal("0.001")))
    elif field in ("pn_std", "pn_raw"):
        # PN 是一组身份（pn_std/pn_raw/part_id 三元组 + 两项 override 账本）。
        # 页面上 clear 任一 PN 控件 = 整组还原：**被 override 过的**字符串各自
        # 回 source_value（未覆盖的那个保持现值，不得置 None），两项 PN
        # override 一起撤销，part_id 回到首次覆盖前快照。半还原会留下
        # "pn_std=B 但 part_id=A"的错位身份（review 批 3 #1）。
        std_entry = override.get("pn_std") or {}
        raw_entry = override.get("pn_raw") or {}
        if "pn_std" in override:
            line.pn_std = std_entry.get("source_value")
        if "pn_raw" in override:
            line.pn_raw = raw_entry.get("source_value")
        restored_fields |= {"pn_std", "pn_raw"}
        # part_id 非空（NOT NULL 约束）：快照 key 存在即恢复。
        part_id_snapshot = None
        has_pid_snapshot = False
        for e in (std_entry, raw_entry):
            if "source_value_part_id" in e:
                part_id_snapshot = e["source_value_part_id"]
                has_pid_snapshot = True
                break
        if has_pid_snapshot and part_id_snapshot != line.part_id:
            # clear 回到原身份也是换绑：B 期间的人工价不得留给 A
            _retire_manual_cost_override(db, line.id, operated_by=operated_by)
            line.part_id = part_id_snapshot
        # 两项 PN override 只在 PN 分支撤销——clear qty/description/SN 时
        # 无条件 pop 会误删 PN 人工保护，让下次 WBDD 重导覆盖手工 PN。
        override.pop("pn_std", None)
        override.pop("pn_raw", None)
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
    # P1#5：撤销是操作事实变更——bump workbook revision（防旧总表覆盖）
    for pid in sorted(projects):
        state = prelocked.get(pid)
        if state is not None:
            _project_ops.bump_locked_workbook_revision(db, state=state)
    if _REPRICE_COLUMNS & restored_fields:
        db.flush()
        try:
            recompute_cost(db, commit=False, line_ids={line.id})
        except MaintenanceCostRecomputeBusy as exc:
            raise DemandManualConflict("成本重算忙，请稍后重试") from exc
    db.flush()
    return {"changed": True, "digest": _digest(_line_snapshot(line)),
            **_line_snapshot(line)}
