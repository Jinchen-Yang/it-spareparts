"""Locked, transaction-local edits to existing site-consumption facts.

The caller owns authorization, the state/project/issue/line lock prefix, CAS,
idempotency receipts, workbook revision and commit. Omitted lines are untouched;
creation, removal and restoration remain explicit caller operations.

Existing full-workbook/manual commands pass an explicit compatibility context
to retain their version, audit and bulk-pricing protocol at this same interface.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services import maintenance_consumption_cost as costs
from app.services import maintenance_site_return_requirements as requirements

_LINE_FIELDS = frozenset({
    "no_return", "part_id", "pn", "quantity", "serial_number",
    "demand_order_no", "remark",
})
_HEADER_FIELDS = frozenset({
    "issue_no", "issue_date", "receiver", "issued_by", "site_location",
})
_SOURCES = frozenset({"legacy", "workbook", "page_manual"})
_LegacyProfile = Literal["v1", "v2", "manual"]


class SiteIssueEditError(ValueError):
    """Stable domain error; HTTP adapters return ``detail`` after rollback."""

    _STATUS_CODES = {
        "site_issue_edit_invalid_fields": 422,
        "site_issue_edit_identity_conflict": 409,
        "site_issue_edit_inactive": 409,
        "site_issue_edit_source_blocked": 409,
        "site_issue_edit_permission_denied": 403,
        # 缺实名操作者或更正原因是请求本身不完整，不是权限问题：422 让调用方修正输入。
        "site_issue_edit_actor_required": 422,
        "site_issue_edit_demand_project_mismatch": 409,
    }

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = self._STATUS_CODES[code]

    @property
    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class SiteIssueEditResult:
    changed_line_ids: tuple[str, ...]
    issue_version: int
    line_versions: dict[str, int]
    before: dict[str, Any]
    after: dict[str, Any]
    pricing_line_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class LegacySiteIssueEditContext:
    """Only for the existing full commands, after their own validation/CAS.

    Assignment stages leave batch pricing/audit with the caller. The final
    stage supplies the original per-issue P3 snapshot, including rows the
    caller has soft-voided. This records one correction event per issue, at
    the old command's original point in the transaction.
    """

    profile: _LegacyProfile
    requirement_before: Mapping[str, dict[str, Any]] | None = None


def _header_snapshot(issue: MaintenanceSiteIssue) -> dict[str, Any]:
    return {
        **{key: getattr(issue, key) for key in sorted(_HEADER_FIELDS - {"issue_date"})},
        "issue_date": issue.issue_date.isoformat(),
        "version": issue.version,
    }


def _line_snapshot(line: MaintenanceSiteIssueLine) -> dict[str, Any]:
    # Keep the shared read representation as the audit representation too.
    from app.services.maintenance_project_operations import site_issue_line_dict

    return {**site_issue_line_dict(line), "is_active": line.is_active}


def _assign_header(issue: MaintenanceSiteIssue, changes: Mapping[str, object]) -> set[str]:
    changed = {key for key, value in changes.items() if getattr(issue, key) != value}
    for key in changed:
        setattr(issue, key, changes[key])
    return changed


def _assign_line(
    line: MaintenanceSiteIssueLine,
    changes: Mapping[str, object],
    *,
    profile: _LegacyProfile | None = None,
) -> set[str]:
    changed = {key for key, value in changes.items() if getattr(line, key) != value}
    if not changed:
        return changed
    for key in changed:
        setattr(line, key, changes[key])
    # An override belongs to the old part, never to its replacement.
    if profile != "v1" and ("part_id" in changed or "pn" in changed):
        line.manual_unit_cost = None
        line.manual_unit_cost_inc_tax = None
        line.manual_evidence = None
    if profile != "v1" and "quantity" in changed and line.unit_cost_ex_tax is not None:
        # Synchronize CHECK-constrained amounts before any correction can flush.
        rounding = {} if profile == "manual" else {"rounding": ROUND_HALF_UP}
        line.cost_amount_ex_tax = (Decimal(line.quantity) * line.unit_cost_ex_tax).quantize(
            Decimal("0.01"), **rounding)
        line.cost_amount_inc_tax = (Decimal(line.quantity) * line.unit_cost_inc_tax).quantize(
            Decimal("0.01"), **rounding)
        line.cost_amount = line.cost_amount_ex_tax
    return changed


def _validated_changes(
    db: Session, *, project_id: str, line: MaintenanceSiteIssueLine, patch: Mapping[str, object]
) -> dict[str, object]:
    if set(patch) - _LINE_FIELDS:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "包含不可编辑的领用明细字段")
    result = dict(patch)
    if "no_return" in result and result["no_return"] is not None and not isinstance(result["no_return"], bool):
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "是否免返只能为是、否或空")
    if "quantity" in result:
        try:
            quantity = Decimal(str(result["quantity"]))
            if not quantity.is_finite() or quantity <= 0 or quantity >= Decimal("1000000000000"):
                raise ValueError
            if quantity != quantity.quantize(Decimal("0.001")):
                raise ValueError
        except (ArithmeticError, ValueError, TypeError) as exc:
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用数量必须是最多三位小数的正数") from exc
        result["quantity"] = quantity
    if "pn" in result and (not isinstance(result["pn"], str) or not result["pn"].strip() or len(result["pn"]) > 128):
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用 PN 无效")
    if "part_id" in result and (isinstance(result["part_id"], bool) or not isinstance(result["part_id"], int)):
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用备件身份无效")
    part_changed = "part_id" in result and result["part_id"] != line.part_id
    pn_changed = "pn" in result and result["pn"] != line.pn
    if part_changed or pn_changed:
        part_id = result.get("part_id", line.part_id)
        if isinstance(part_id, bool) or not isinstance(part_id, int):
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用备件身份无效")
        part = db.get(DimPart, part_id)
        if part is None or part.status == "merged" or part.is_excluded:
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用备件不存在或不可选")
        if part_changed and "pn" not in result:
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "变更备件身份必须同时提供 PN")
        if result.get("pn", line.pn) != part.pn_std:
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用 PN 与备件身份不一致")
    for key in ("serial_number", "demand_order_no", "remark"):
        if key in result and result[key] is not None and not isinstance(result[key], str):
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用文本字段无效")
    if "demand_order_no" in result:
        demand_no = result["demand_order_no"]
        demand_no = (demand_no.strip() or None) if demand_no is not None else None
        if demand_no is not None:
            if len(demand_no) > 64:
                raise SiteIssueEditError("site_issue_edit_invalid_fields", "关联需求单号过长")
            # Manual commands already use this authoritative assignment check.
            # Import lazily because that adapter imports this shared primitive.
            from app.services.maintenance_manual_site_issue import _resolve_demand_project
            from app.services.maintenance_project_operations import MaintenanceOperationError

            try:
                _resolve_demand_project(db, project_id=project_id, demand_order_no=demand_no)
            except MaintenanceOperationError as exc:
                raise SiteIssueEditError("site_issue_edit_demand_project_mismatch", str(exc)) from exc
        result["demand_order_no"] = demand_no
    return result


def apply_locked_site_issue_edits(
    db: Session, *, project: MaintenanceProject, issue: MaintenanceSiteIssue,
    line_changes: Mapping[str, Mapping[str, object]],
    header_changes: Mapping[str, object] | None,
    operated_by: str, reason: str,
    legacy_context: LegacySiteIssueEditContext | None = None,
) -> SiteIssueEditResult:
    """Apply an existing-line patch under caller-held locks and checked CAS.

    The caller supplies the lock prefix; existing obligation-correction locks
    remain in its downstream service. No commit, rollback, workbook bump,
    command receipt or lifecycle transitions occur here. On failure the caller
    rolls back its whole command.
    ``before``/``after`` include full fact/cost evidence for the changed set.
    In the default patch protocol date/number edits reprice the whole issue:
    the caller must already hold all active line locks. That protocol rejects
    native delivery-backed sources. Only the existing full commands supply
    ``legacy_context`` to keep their validated source, pricing and audit rules;
    their final stage passes the original P3 snapshot once per issue.
    """
    profile = legacy_context.profile if legacy_context is not None else None
    if legacy_context is not None and profile not in {"v1", "v2", "manual"}:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "未知领用编辑协议")
    if issue.project_id != project.project_id:
        raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用单不属于当前项目")
    if profile is None and (not project.is_active or issue.normalized_status == "void"):
        raise SiteIssueEditError("site_issue_edit_inactive", "项目或领用单已作废或停用")
    if profile is None and (issue.source not in _SOURCES or issue.normalized_status not in {"confirmed", "corrected"}):
        raise SiteIssueEditError("site_issue_edit_source_blocked", "只有已确认或已更正的领用事实可编辑")
    if profile is None and (not isinstance(operated_by, str) or not operated_by.strip() or len(operated_by) > 64
            or not isinstance(reason, str) or not reason.strip()):
        raise SiteIssueEditError("site_issue_edit_actor_required", "领用更正需要实名操作者与原因")
    headers = dict(header_changes or {})
    if set(headers) - _HEADER_FIELDS:
        raise SiteIssueEditError("site_issue_edit_invalid_fields", "包含不可编辑的领用单头字段")
    for key, value in (headers.items() if profile is None else ()):
        if key == "issue_date":
            if type(value) is not date:
                raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用日期无效")
        elif value is not None and not isinstance(value, str):
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用单头文本无效")
        if key == "issue_no" and (not value or len(value) > 64):
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用单号无效")
        if key in {"receiver", "issued_by", "site_location"} and value is not None and len(value) > (256 if key == "site_location" else 128):
            raise SiteIssueEditError("site_issue_edit_invalid_fields", "领用单头文本过长")
    lines: dict[str, MaintenanceSiteIssueLine] = {}
    patches = {}
    # Validate every intent before assigning anything; no partial dirty state on
    # a simple foreign-ID/field error even when a caller catches the exception.
    with db.no_autoflush:
        for line_id, patch in sorted(line_changes.items()):
            line = db.get(MaintenanceSiteIssueLine, line_id)
            if line is None or line.issue_id != issue.issue_id:
                raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用明细不存在或不属于本单")
            if profile is None and not line.is_active:
                raise SiteIssueEditError("site_issue_edit_inactive", "领用明细已作废")
            if profile is None and line.delivery_line_id is not None:
                raise SiteIssueEditError("site_issue_edit_source_blocked", "发货关联领用须使用原有更正流程")
            lines[line_id] = line
            if profile is not None:
                if set(patch) - _LINE_FIELDS:
                    raise SiteIssueEditError("site_issue_edit_invalid_fields", "包含不可编辑的领用明细字段")
                patches[line_id] = dict(patch)
            else:
                patches[line_id] = _validated_changes(db, project_id=project.project_id, line=line, patch=patch)
        if legacy_context is not None:
            for line_id in legacy_context.requirement_before or {}:
                line = db.get(MaintenanceSiteIssueLine, line_id)
                if line is None or line.issue_id != issue.issue_id:
                    raise SiteIssueEditError("site_issue_edit_identity_conflict", "领用明细行不属于本单")
                lines[line_id] = line
        reprices_all = profile is None and any(key in headers and headers[key] != getattr(issue, key)
                           for key in ("issue_date", "issue_no"))
        if reprices_all:
            for line in db.scalars(select(MaintenanceSiteIssueLine).where(
                MaintenanceSiteIssueLine.issue_id == issue.issue_id,
                MaintenanceSiteIssueLine.is_active.is_(True),
            ).order_by(MaintenanceSiteIssueLine.line_no, MaintenanceSiteIssueLine.issue_line_id)):
                if line.delivery_line_id is not None:
                    raise SiteIssueEditError("site_issue_edit_source_blocked", "发货关联领用须使用原有更正流程")
                lines[line.issue_line_id] = line
    before = {"header": _header_snapshot(issue),
              "lines": {line_id: _line_snapshot(line) for line_id, line in lines.items()}}
    requirement_before = (
        legacy_context.requirement_before if legacy_context is not None
        else {line_id: requirements.snapshot(line) for line_id, line in lines.items()}
    )
    header_dirty = _assign_header(issue, headers)
    dirty_fields = {line_id: _assign_line(lines[line_id], patch, profile=profile)
                    for line_id, patch in patches.items()}
    if profile == "manual":
        for line_id, dirty in dirty_fields.items():
            if dirty:
                lines[line_id].version += 1
    pricing = [(issue.issue_date, line) for line_id, line in lines.items()
               if profile is None and (reprices_all or dirty_fields.get(line_id, set()) & {"quantity", "part_id", "pn", "demand_order_no"})]
    if pricing:
        # The cost resolver reads the persisted issue number. With autoflush
        # disabled it would otherwise use the old number's demand evidence.
        # All intents are validated and quantity amounts synchronized above.
        db.flush()
        costs.resolve_lines(db, lines=pricing)
    # P3 owns obligation events and the versions for its snapshot fields.
    if requirement_before is not None:
        requirements.record_corrections(
            db, project_id=project.project_id, before=requirement_before,
            operated_by=operated_by, reason=reason,
        )
    changed_ids = []
    for line_id, line in lines.items():
        previous = before["lines"][line_id]
        current = _line_snapshot(line)
        if previous != current:
            changed_ids.append(line_id)
            if profile is None and line.version == previous["version"]:
                line.version += 1
    if profile is None and (header_dirty or changed_ids) and issue.version == before["header"]["version"]:
        issue.version += 1
    after = {"header": _header_snapshot(issue),
             "lines": {line_id: _line_snapshot(line) for line_id, line in lines.items()}}
    if profile is None and (header_dirty or changed_ids):
        db.add(MaintenanceProjectOperationAudit(
            project_id=project.project_id, entity_type="site_issue", entity_id=issue.issue_id,
            action="site_issue_facts_edited", before_json=before, after_json=after,
            reason=reason, operated_by=operated_by,
        ))
    if profile is None:
        db.flush()
    return SiteIssueEditResult(
        changed_line_ids=tuple(sorted(changed_ids)), issue_version=issue.version,
        line_versions={line_id: lines[line_id].version for line_id in sorted(set(patches) | set(changed_ids))},
        before=before, after=after,
        pricing_line_ids=tuple(line_id for line_id, dirty in dirty_fields.items()
                               if dirty & {"quantity", "part_id", "pn"}),
    )
