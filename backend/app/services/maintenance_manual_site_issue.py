"""页面人工登记领用（page_manual source）——受控领域命令。

业务协议先例 = 项目总表 06 人工领用（workbook-manual-v1）：用户录入的是明确
的人工业务事实（日期/接收人/PN/SN/数量/是否应返还），可独立于仓库发货引用
维护。本模块把同一协议搬进页面通道：

- source='page_manual'（诚实来源：页面人工登记，非 Excel 上传、非仓库发货）
- 状态映射 page-manual-v1，落库即 confirmed（与 06 人工新增同语义）
- 领用行不带 delivery_line_id → 天然不参与发货余额聚合，不伪造 delivery source
- 成本 = maintenance_consumption_cost 完整瀑布（需求单→采购→销售→缺价留空，
  绝不编造 0 价/库存）；PN 换绑清空行级手工价（证据属于旧备件，06 同款规则）
- 返还义务修正审计复用 maintenance_site_return_requirements（workbook 同款）
- create/patch 按 idempotency_key + 请求指纹比较回放；patch 带 version CAS；
  作废复用 operations.void_site_issue（2026-09-06 起对来源诚实开放）
- prod 不挡：这是明确的人工业务事实通道；被挡的是 direct_api 冒充与
  合成 adapter 确认（operations 内部闸门保持原样）
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.services import (
    maintenance_consumption_cost,
    maintenance_site_return_requirements as requirements,
)
from app.services.maintenance_project_operations import (
    MaintenanceOperationConflict,
    MaintenanceOperationError,
    MaintenanceOperationPermissionError,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
    _fact_audit,
    _lock_idempotency_key,
    _lock_project_for_fact_write,
    _qty,
    _required,
    _site_issue_request_fingerprint,
    site_issue_dict,
    bump_workbook_revision,
)

SOURCE = "page_manual"
STATUS_MAPPING_VERSION = "page-manual-v1"
LINE_LIMIT = 200
_DEMAND_NO_PATTERN_MAX = 64
# 与模型 Qty（Numeric(18,3)）/CHECK quantity < 1e12 对齐：quantize 后必须仍为
# 正数且落在值域内。0.0001 quantize 到 0.001 会变 0.000→非正；1e100 会让
# Decimal quantize 抛 InvalidOperation——都在这里统一拒绝，不冒 500。
_QUANTITY_QUANTUM = Decimal("0.001")
_QUANTITY_MAX_EXCLUSIVE = Decimal("1000000000000")


def _resolve_part(db: Session, part_id: int) -> DimPart:
    """页面登记只接受可选主档：merged 墓碑（应沿 merged_into_id 走新档）与
    治理排除档不得以旧身份落入领用与成本事实——与统一搜索可选规则一致。"""

    part = db.get(DimPart, part_id)
    if part is None:
        raise MaintenanceOperationError("备件不存在，请重新选择型号")
    if part.status == "merged":
        raise MaintenanceOperationError(
            "该型号已并入其他主档，请搜索并选择合并后的新型号"
        )
    if part.is_excluded:
        raise MaintenanceOperationError(
            "该型号已被治理排除，不能登记领用"
        )
    if not str(part.pn_std or "").strip():
        raise MaintenanceOperationError("备件缺少标准 PN，不能登记领用")
    return part


def _resolve_demand_project(
    db: Session,
    *,
    project_id: str,
    demand_order_no: str,
) -> None:
    """关联需求单必须是本项目的活跃需求——按归属表判定，绝不按项目名猜。"""

    row = db.execute(
        select(FMaintenanceOrder.id)
        .join(
            MaintenanceSourceOrderAssignment,
            (MaintenanceSourceOrderAssignment.source_order_id
             == FMaintenanceOrder.raw_order_id)
            & (MaintenanceSourceOrderAssignment.is_active.is_(True)),
        )
        .where(
            FMaintenanceOrder.order_no == demand_order_no,
            MaintenanceSourceOrderAssignment.project_id == project_id,
        )
        .limit(1)
    ).first()
    if row is None:
        raise MaintenanceOperationError(
            "关联需求单不属于当前项目，请核对单号或留空"
        )


def _clean_demand_no(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > _DEMAND_NO_PATTERN_MAX:
        raise MaintenanceOperationError("关联需求单号过长")
    return cleaned


def _normalize_manual_lines(
    db: Session,
    *,
    project_id: str,
    lines: list[dict],
) -> list[dict]:
    """Normalize page-entered lines: PN identity by part_id, honest demand ref."""

    if not lines:
        raise MaintenanceOperationError("领用单至少需要一条明细")
    if len(lines) > LINE_LIMIT:
        raise MaintenanceOperationError(f"领用单最多允许 {LINE_LIMIT} 条明细")
    normalized: list[dict] = []
    for raw_line in lines:
        part_id = raw_line.get("part_id")
        if part_id is None:
            raise MaintenanceOperationError("每行都必须选择备件型号")
        part = _resolve_part(db, int(part_id))
        no_return = raw_line.get("no_return")
        if no_return is not None and not isinstance(no_return, bool):
            raise MaintenanceOperationError("是否应返还只能填是/否或留空")
        demand_no = _clean_demand_no(raw_line.get("demand_order_no"))
        if demand_no is not None:
            _resolve_demand_project(
                db, project_id=project_id, demand_order_no=demand_no
            )
        quantity = raw_line.get("quantity")
        try:
            parsed = Decimal(str(quantity))
            if not parsed.is_finite():
                raise ArithmeticError
            quantized = parsed.quantize(_QUANTITY_QUANTUM)
        except (InvalidOperation, ArithmeticError, ValueError, TypeError) as exc:
            # 非有限（NaN/Inf）、超出 precision 的极大值：统一 400，不冒 500。
            raise MaintenanceOperationError(
                "领用数量必须是有效的正数（最多 3 位小数）"
            ) from exc
        if quantized <= 0 or quantized >= _QUANTITY_MAX_EXCLUSIVE:
            # 0.0001 → 0.000：quantize 后非正同样拒绝。
            raise MaintenanceOperationError(
                "领用数量必须是有效的正数（最多 3 位小数）"
            )
        normalized.append(
            {
                # PATCH 时前端显式携带既有行 id（无 id = 新建行）。绝不用
                # (part_id, SN) 猜对齐：同 PN 且无 SN 的多行会互相覆盖身份。
                "issue_line_id": (
                    str(raw_line["issue_line_id"]).strip()
                    if str(raw_line.get("issue_line_id") or "").strip()
                    else None
                ),
                "part_id": part.id,
                "pn": part.pn_std,
                "quantity": quantized,
                "serial_number": (
                    str(raw_line["serial_number"]).strip()
                    if str(raw_line.get("serial_number") or "").strip()
                    else None
                ),
                "no_return": no_return,
                "demand_order_no": demand_no,
                "remark": (
                    str(raw_line["remark"]).strip()
                    if str(raw_line.get("remark") or "").strip()
                    else None
                ),
            }
        )
    return normalized


def _manual_lines_fingerprint_payload(
    *,
    issue_date: date | None,
    receiver: str | None,
    issued_by: str | None,
    site_location: str | None,
    lines: list[dict] | None,
    reason: str,
    issue_no: str | None = None,
) -> dict:
    return {
        "issue_date": issue_date.isoformat() if issue_date else None,
        "receiver": receiver,
        "issued_by": issued_by,
        "site_location": site_location,
        "issue_no": issue_no,
        "lines": (
            [
                {
                    "issue_line_id": line.get("issue_line_id"),
                    "part_id": line["part_id"],
                    "pn": line["pn"],
                    "quantity": _qty(line["quantity"]),
                    "serial_number": line.get("serial_number"),
                    "no_return": line.get("no_return"),
                    "demand_order_no": line.get("demand_order_no"),
                    "remark": line.get("remark"),
                }
                for line in lines
            ]
            if lines is not None
            else None
        ),
        "reason": reason,
    }


def _replay_or_conflict(
    db: Session,
    *,
    idempotency_key: str,
    project_id: str,
    request_fingerprint: str,
    actions: tuple[str, ...],
) -> dict | None:
    """Return the stored receipt when the same command already succeeded.

    A key reused with a different payload must fail closed (network-lost ACK
    must not duplicate; an edited payload must not ride the old key).
    """

    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssueCommand,
    )

    row = db.scalar(
        select(MaintenanceSiteIssueCommand).where(
            MaintenanceSiteIssueCommand.idempotency_key == idempotency_key
        )
    )
    if row is None:
        return None
    if (
        row.action not in actions
        or row.project_id != project_id
        or row.request_fingerprint != request_fingerprint
    ):
        raise MaintenanceOperationConflict("幂等键已用于不同的人工领用操作")
    return {**row.response_json, "idempotent_replay": True}


def _record_command(
    db: Session,
    *,
    idempotency_key: str,
    action: str,
    issue_id: str,
    project_id: str,
    request_fingerprint: str,
    response: dict,
) -> None:
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssueCommand,
    )

    db.add(
        MaintenanceSiteIssueCommand(
            command_id=str(uuid4()),
            idempotency_key=idempotency_key,
            project_id=project_id,
            issue_id=issue_id,
            action=action,
            request_fingerprint=request_fingerprint,
            response_json=response,
        )
    )


def _record_requirement_corrections(
    db: Session,
    *,
    project_id: str,
    before: dict,
    operated_by: str,
    reason: str,
) -> None:
    """Workbook 同款：行事实变化 → 行级返还义务修正审计。

    ``record_corrections`` 内部逐行比对 before/after、无变化即跳过；新建行不在
    before 里，天然不产生伪审计。软作废行 is_active 已翻 false，会被记为
    quantity 口径变化——与 06 删行的义务撤回语义一致。
    """

    requirements.record_corrections(
        db,
        project_id=project_id,
        before=before,
        operated_by=operated_by,
        reason=reason,
    )


def _sync_amounts_before_flush(line: MaintenanceSiteIssueLine) -> None:
    """数量变化先行同步金额，避免 autoflush 撞 dual-tax CHECK（06 同款规则）。"""

    if line.unit_cost_ex_tax is not None:
        line.cost_amount_ex_tax = (
            Decimal(line.quantity) * line.unit_cost_ex_tax
        ).quantize(Decimal("0.01"))
        line.cost_amount_inc_tax = (
            Decimal(line.quantity) * line.unit_cost_inc_tax
        ).quantize(Decimal("0.01"))
        line.cost_amount = line.cost_amount_ex_tax


def preview_manual_site_issue(
    db: Session,
    *,
    project_id: str,
    issue_date: date,
    receiver: str,
    issued_by: str,
    site_location: str,
    lines: list[dict],
) -> dict:
    """预览实际消耗与成本（clone 解析，不落库、不锁行、不记审计）。"""

    clean_receiver = _required(receiver, "接收人", 128)
    clean_issued_by = _required(issued_by, "发出人", 128)
    clean_location = _required(site_location, "现场位置", 256)
    normalized = _normalize_manual_lines(db, project_id=project_id, lines=lines)
    from app.models.maintenance_project import MaintenanceProject

    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise MaintenanceOperationError("项目不存在")

    preview_lines: list[MaintenanceSiteIssueLine] = []
    for line_no, requested in enumerate(normalized, start=1):
        preview_lines.append(
            MaintenanceSiteIssueLine(
                issue_line_id=f"preview:{uuid4()}",
                issue_id="preview",
                line_no=line_no,
                part_id=requested["part_id"],
                pn=requested["pn"],
                quantity=requested["quantity"],
                serial_number=requested.get("serial_number"),
                demand_order_no=requested.get("demand_order_no"),
                remark=requested.get("remark"),
                no_return=requested.get("no_return"),
                manual_unit_cost=None,
                reference_sample_ids=[],
                reference_sample_count=0,
                reference_samples=[],
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
                version=1,
            )
        )
    # 预览行不在库中，resolve_lines 找不到 issue 头 → 跳过需求单精确行层，
    # 仍走 direct/窗口瀑布。为让需求单层生效，先用临时单头身份构造再清理。
    # 简化且诚实的做法：直接调 resolve_lines，demand 层按 issue_no 兜底
    # （preview 行无 issue_no，exact/same-order 需求层自然跳过）。
    try:
        maintenance_consumption_cost.resolve_lines(
            db, lines=[(issue_date, line) for line in preview_lines]
        )
    except maintenance_consumption_cost.CostResolutionError as exc:
        raise MaintenanceOperationError(str(exc)) from exc
    from app.services.maintenance_project_operations import site_issue_line_dict

    return {
        "project_id": project_id,
        "issue_date": issue_date.isoformat(),
        "receiver": clean_receiver,
        "issued_by": clean_issued_by,
        "site_location": clean_location,
        "lines": [
            {**site_issue_line_dict(line), "cost_gap": line.cost_source is None}
            for line in preview_lines
        ],
        "inventory_effect": "none",
        "total_cost_ex_tax": _preview_total(preview_lines, "cost_amount_ex_tax"),
        "total_cost_inc_tax": _preview_total(preview_lines, "cost_amount_inc_tax"),
    }


def _preview_total(lines: list[MaintenanceSiteIssueLine], field: str) -> str | None:
    amounts = [getattr(line, field) for line in lines]
    if any(amount is None for amount in amounts):
        return None
    total = sum((Decimal(amount) for amount in amounts), Decimal("0.00"))
    return format(total, ".2f")


def create_manual_site_issue(
    db: Session,
    *,
    project_id: str,
    idempotency_key: str,
    issue_date: date,
    receiver: str,
    issued_by: str,
    site_location: str,
    lines: list[dict],
    reason: str,
    operated_by: str,
    issue_no: str | None = None,
) -> dict | None:
    """页面人工登记一张已确认领用单（06 人工领用同语义，source=page_manual）。"""

    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise MaintenanceOperationError("幂等键至少需要 8 个字符")
    clean_receiver = _required(receiver, "接收人", 128)
    clean_issued_by = _required(issued_by, "发出人", 128)
    clean_location = _required(site_location, "现场位置", 256)
    clean_reason = _required(reason, "操作原因", 1000)
    clean_issue_no = (
        _required(issue_no, "领用单号", 64) if issue_no is not None else None
    )
    normalized = _normalize_manual_lines(db, project_id=project_id, lines=lines)

    fingerprint_payload = _manual_lines_fingerprint_payload(
        issue_date=issue_date,
        receiver=clean_receiver,
        issued_by=clean_issued_by,
        site_location=clean_location,
        lines=normalized,
        reason=clean_reason,
        issue_no=clean_issue_no,
    )
    fingerprint = _site_issue_request_fingerprint(
        {"action": "create", "project_id": project_id, **fingerprint_payload}
    )
    _lock_idempotency_key(db, clean_key)
    replay = _replay_or_conflict(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        request_fingerprint=fingerprint,
        actions=("create",),
    )
    if replay is not None:
        return replay

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")

    issue_id = str(uuid4())
    final_issue_no = clean_issue_no or (
        f"LYR-{issue_date:%Y%m%d}-{uuid4().hex[:10].upper()}"
    )
    duplicate = db.scalar(
        select(MaintenanceSiteIssue.issue_id).where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssue.issue_no == final_issue_no,
        )
    )
    if duplicate is not None:
        raise MaintenanceOperationConflict(
            "领用单号已被本项目使用，请换单号或留空自动生成"
        )

    saved_lines: list[MaintenanceSiteIssueLine] = []
    for line_no, requested in enumerate(normalized, start=1):
        saved_lines.append(
            MaintenanceSiteIssueLine(
                issue_line_id=str(uuid4()),
                issue_id=issue_id,
                line_no=line_no,
                part_id=requested["part_id"],
                pn=requested["pn"],
                quantity=requested["quantity"],
                serial_number=requested.get("serial_number"),
                demand_order_no=requested.get("demand_order_no"),
                remark=requested.get("remark"),
                no_return=requested.get("no_return"),
                is_active=True,
                manual_unit_cost=None,
                reference_sample_ids=[],
                reference_sample_count=0,
                reference_samples=[],
                algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
                version=1,
            )
        )
    row = MaintenanceSiteIssue(
        issue_id=issue_id,
        project_id=project_id,
        issue_no=final_issue_no,
        issue_date=issue_date,
        raw_status="已确认",
        status_mapping_state="mapped",
        normalized_status="confirmed",
        status_mapping_version=STATUS_MAPPING_VERSION,
        source=SOURCE,
        import_batch_id=None,
        idempotency_key=clean_key,
        request_fingerprint=fingerprint,
        receiver=clean_receiver,
        issued_by=clean_issued_by,
        site_location=clean_location,
        created_by=_required(operated_by, "操作人"),
        confirmed_at=datetime.now(UTC),
        version=1,
    )
    db.add(row)
    db.add_all(saved_lines)
    db.flush()
    try:
        maintenance_consumption_cost.resolve_lines(
            db, lines=[(issue_date, line) for line in saved_lines]
        )
    except maintenance_consumption_cost.CostResolutionError as exc:
        raise MaintenanceOperationError(str(exc)) from exc
    payload = site_issue_dict(row, saved_lines)
    payload["inventory_effect"] = "none"
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=issue_id,
        action="create",
        before=None,
        after=payload,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_command(
        db,
        idempotency_key=clean_key,
        action="create",
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
        response=payload,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return payload


def _require_page_manual_issue(
    db: Session,
    *,
    issue: MaintenanceSiteIssue,
    project_id: str,
) -> None:
    if issue.project_id != project_id:
        raise MaintenanceOperationPermissionError("领用单不属于当前项目")
    if issue.source != SOURCE:
        raise MaintenanceOperationError(
            "该领用单不是页面人工登记单，请在原通道修改"
        )


def patch_manual_site_issue(
    db: Session,
    *,
    issue_id: str,
    project_id: str,
    version: int,
    idempotency_key: str,
    issue_date: date | None = None,
    issue_no: str | None = None,
    receiver: str | None = None,
    issued_by: str | None = None,
    site_location: str | None = None,
    lines: list[dict] | None = None,
    reason: str,
    operated_by: str,
) -> dict | None:
    """人工领用单更正：CAS + 幂等回放 + 行级重定价 + 返还义务修正审计。"""

    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise MaintenanceOperationError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    normalized_lines = (
        _normalize_manual_lines(db, project_id=project_id, lines=lines)
        if lines is not None
        else None
    )
    fingerprint_payload = _manual_lines_fingerprint_payload(
        issue_date=issue_date,
        receiver=receiver,
        issued_by=issued_by,
        site_location=site_location,
        lines=normalized_lines,
        reason=clean_reason,
        issue_no=issue_no,
    )
    fingerprint = _site_issue_request_fingerprint(
        {
            "action": "patch",
            "issue_id": issue_id,
            "project_id": project_id,
            "version": version,
            **fingerprint_payload,
        }
    )
    _lock_idempotency_key(db, clean_key)
    replay = _replay_or_conflict(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        request_fingerprint=fingerprint,
        actions=("update", "correct"),
    )
    if replay is not None:
        return replay

    project = _lock_project_for_fact_write(db, project_id)
    if project is None:
        return None
    if not project.is_active:
        raise MaintenanceOperationError("项目主档已归档")
    issue = db.scalar(
        select(MaintenanceSiteIssue)
        .where(MaintenanceSiteIssue.issue_id == issue_id)
        .with_for_update()
    )
    if issue is None:
        return None
    _require_page_manual_issue(db, issue=issue, project_id=project_id)
    if issue.version != version:
        raise MaintenanceOperationConflict("领用单版本已变化，请刷新后重试")
    if issue.normalized_status not in {"confirmed", "corrected"}:
        raise MaintenanceOperationConflict(
            "只有已确认/已更正的人工领用单可以继续更正"
        )

    old_lines = list(
        db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(
                MaintenanceSiteIssueLine.issue_id == issue_id,
                MaintenanceSiteIssueLine.is_active.is_(True),
            )
            .order_by(MaintenanceSiteIssueLine.line_no)
            .with_for_update()
        )
    )
    requirement_before = {
        line.issue_line_id: requirements.snapshot(line) for line in old_lines
    }
    before = site_issue_dict(issue, old_lines)

    candidate_date = issue_date or issue.issue_date
    candidate_no = issue_no or issue.issue_no
    candidate_receiver = (
        _required(receiver, "接收人", 128) if receiver is not None else issue.receiver
    )
    candidate_issued_by = (
        _required(issued_by, "发出人", 128)
        if issued_by is not None
        else issue.issued_by
    )
    candidate_location = (
        _required(site_location, "现场位置", 256)
        if site_location is not None
        else issue.site_location
    )

    lines_changed = False
    pricing_entries: dict[str, tuple[date, MaintenanceSiteIssueLine]] = {}
    if normalized_lines is not None:
        # 行对齐只认显式 issue_line_id（前端保留已有行 id、新行无 id）。
        # 绝不用 (part_id, SN) 猜：同 PN 无 SN 的多行会互相顶替身份、
        # 把手工价证据转移/丢失。id 不属于本单 → 拒绝，不静默新建。
        old_by_id = {line.issue_line_id: line for line in old_lines}
        requested_ids = [
            line["issue_line_id"]
            for line in normalized_lines
            if line.get("issue_line_id")
        ]
        duplicate_ids = {
            line_id
            for line_id in requested_ids
            if requested_ids.count(line_id) > 1
        }
        if duplicate_ids:
            raise MaintenanceOperationError("领用明细行不能在同一单中重复出现")
        foreign = [line_id for line_id in requested_ids if line_id not in old_by_id]
        if foreign:
            raise MaintenanceOperationError(
                "领用明细行不属于本单，请刷新后重试"
            )
        used_line_ids: set[str] = set()
        # 行号唯一约束覆盖本单全部历史行，软作废行的编号也不能复用。
        # 单头已加写锁，所有更正串行分配下一编号；身份校验仍仅接受有效行。
        next_line_no = (
            db.scalar(
                select(func.max(MaintenanceSiteIssueLine.line_no)).where(
                    MaintenanceSiteIssueLine.issue_id == issue_id,
                )
            ) or 0
        ) + 1
        surviving: list[MaintenanceSiteIssueLine] = []
        for requested in normalized_lines:
            line_id = requested.get("issue_line_id")
            existing = old_by_id.get(line_id) if line_id else None
            if existing is not None:
                used_line_ids.add(existing.issue_line_id)
                surviving.append(existing)
                line_dirty = False
                if existing.quantity != requested["quantity"]:
                    existing.quantity = requested["quantity"]
                    _sync_amounts_before_flush(existing)
                    line_dirty = True
                if existing.no_return != requested.get("no_return"):
                    existing.no_return = requested.get("no_return")
                    line_dirty = True
                if existing.demand_order_no != requested.get("demand_order_no"):
                    existing.demand_order_no = requested.get("demand_order_no")
                    line_dirty = True
                if existing.remark != requested.get("remark"):
                    existing.remark = requested.get("remark")
                    line_dirty = True
                if existing.part_id != requested["part_id"]:
                    # 同一行换型号 = 行身份内换绑：清旧件手工价证据（06 同款）。
                    existing.part_id = requested["part_id"]
                    existing.pn = requested["pn"]
                    existing.manual_unit_cost = None
                    existing.manual_unit_cost_inc_tax = None
                    existing.manual_evidence = None
                    line_dirty = True
                if existing.serial_number != requested.get("serial_number"):
                    existing.serial_number = requested.get("serial_number")
                    line_dirty = True
                if line_dirty:
                    # 行事实变化必须 bump line.version：fill_manual_cost 等
                    # 补价入口按 line.version 做 CAS，不 bump 会接受更正前
                    # 的过期补价凭据（Codex 复审 P1，2026-09-20）。
                    existing.version += 1
                    lines_changed = True
                pricing_entries[existing.issue_line_id] = (candidate_date, existing)
            else:
                new_line = MaintenanceSiteIssueLine(
                    issue_line_id=str(uuid4()),
                    issue_id=issue_id,
                    line_no=next_line_no,
                    part_id=requested["part_id"],
                    pn=requested["pn"],
                    quantity=requested["quantity"],
                    serial_number=requested.get("serial_number"),
                    demand_order_no=requested.get("demand_order_no"),
                    remark=requested.get("remark"),
                    no_return=requested.get("no_return"),
                    is_active=True,
                    manual_unit_cost=None,
                    reference_sample_ids=[],
                    reference_sample_count=0,
                    reference_samples=[],
                    algorithm_version=maintenance_consumption_cost.ALGORITHM_VERSION,
                    version=1,
                )
                next_line_no += 1
                db.add(new_line)
                surviving.append(new_line)
                lines_changed = True
                pricing_entries[new_line.issue_line_id] = (candidate_date, new_line)
        for line in old_lines:
            if line.issue_line_id not in used_line_ids:
                line.is_active = False
                # 软作废同样是行事实变化：bump version 让过期补价/编辑凭据失效。
                line.version += 1
                lines_changed = True
    # 日期变化影响取价窗口：所有行重算（无 lines 提交时也要做）。
    metadata_changed = (
        candidate_date != issue.issue_date
        or candidate_no != issue.issue_no
        or candidate_receiver != issue.receiver
        or candidate_issued_by != issue.issued_by
        or candidate_location != issue.site_location
    )
    if not (lines_changed or metadata_changed):
        raise MaintenanceOperationError("领用单业务内容没有变化")
    if candidate_no != issue.issue_no:
        duplicate = db.scalar(
            select(MaintenanceSiteIssue.issue_id).where(
                MaintenanceSiteIssue.project_id == project_id,
                MaintenanceSiteIssue.issue_no == candidate_no,
                MaintenanceSiteIssue.issue_id != issue_id,
            )
        )
        if duplicate is not None:
            raise MaintenanceOperationConflict(
                "领用单号已被本项目另一张领用单使用"
            )
    if normalized_lines is None:
        pricing_entries = {
            line.issue_line_id: (candidate_date, line) for line in old_lines
        } if candidate_date != issue.issue_date else {}

    if pricing_entries:
        try:
            cost_before = {
                line.issue_line_id: (
                    line.cost_source,
                    line.unit_cost_ex_tax,
                )
                for _d, line in pricing_entries.values()
            }
            maintenance_consumption_cost.resolve_lines(
                db, lines=list(pricing_entries.values())
            )
            # 取价结果变化的行同样 bump line.version：成本证据变了，按
            # line.version 做 CAS 的下游（fill_manual_cost）必须看到新版本。
            for line in pricing_entries.values():
                target = line[1]
                if cost_before.get(target.issue_line_id) != (
                    target.cost_source,
                    target.unit_cost_ex_tax,
                ):
                    target.version += 1
        except maintenance_consumption_cost.CostResolutionError as exc:
            raise MaintenanceOperationError(str(exc)) from exc

    _record_requirement_corrections(
        db,
        project_id=project_id,
        before=requirement_before,
        operated_by=operated_by,
        reason=clean_reason,
    )

    issue.issue_date = candidate_date
    issue.issue_no = candidate_no
    issue.receiver = candidate_receiver
    issue.issued_by = candidate_issued_by
    issue.site_location = candidate_location
    issue.raw_status = "corrected"
    issue.normalized_status = "corrected"
    issue.status_mapping_version = STATUS_MAPPING_VERSION
    issue.corrected_at = datetime.now(UTC)
    issue.version += 1
    db.flush()
    surviving = list(
        db.scalars(
            select(MaintenanceSiteIssueLine)
            .where(
                MaintenanceSiteIssueLine.issue_id == issue_id,
                MaintenanceSiteIssueLine.is_active.is_(True),
            )
            .order_by(MaintenanceSiteIssueLine.line_no)
        )
    )
    response = {
        **site_issue_dict(issue, surviving),
        "inventory_effect": "none",
        "idempotent_replay": False,
    }
    _fact_audit(
        db,
        project_id=project_id,
        entity_type="site_issue",
        entity_id=issue_id,
        action="correct",
        before=before,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_command(
        db,
        idempotency_key=clean_key,
        action="correct",
        issue_id=issue_id,
        project_id=project_id,
        request_fingerprint=fingerprint,
        response=response,
    )
    bump_workbook_revision(db, project_id=project_id)
    db.flush()
    return response
