"""Bad-part return obligations derived from confirmed maintenance consumption."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from uuid import UUID, uuid4, uuid5

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_bad_return import (
    MaintenanceBadReturn,
    MaintenanceBadReturnCommand,
    MaintenanceBadReturnLine,
    MaintenanceReturnObligation,
)
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.master_data import ProductCategory


RETURN_RULE_VERSION = "maintenance-bad-return-category-v1"
_OBLIGATION_NAMESPACE = UUID("4f8cf18a-a83f-4fb1-9425-5057ec86f45d")
_QUANTITY_MAX_EXCLUSIVE = Decimal("100000000000")
_ACTIVE_RETURN_STATUSES = ("submitted", "in_transit", "warehouse_confirmed")
_WRITE_LOCK_TIMEOUT = "5s"


class BadReturnError(Exception):
    """Invalid bad-return request or unsafe source event."""


class BadReturnConflict(Exception):
    """Concurrent, duplicate, or invalid state transition."""


class BadReturnPermissionError(Exception):
    """A project-scoped entity does not belong to the requested project."""


def classify_return_obligation(
    *,
    category_id: int | None,
    category_major: str | None,
    category_minor: str | None,
    no_return_line: bool | None = None,
    project_no_return_default: bool = False,
) -> dict:
    """Freeze the evidence used by the return rule (B3 行级默认口径).

    判定顺序（系统自动审批，无需人工逐行审批）：
    1. 行级 no_return=True → 不返还；False → 必须返还（覆盖项目默认）；
    2. 行级未填 → 项目级默认：项目默认不返还 → 不返还；否则按品类规则；
    3. 品类规则：标准大类「硬盘」不返还，其余必须返还；
    4. 无标准品类证据 → pending_category（不形成应返数量）。

    A textual category without a standard ``category_id`` is deliberately not
    evidence.
    """

    exemption_source = "none"
    if category_id is None:
        classification = "pending_category"
        major_snapshot = None
        minor_snapshot = None
        exemption_source = None
    elif no_return_line is True:
        classification = "exempt"
        major_snapshot = category_major
        minor_snapshot = category_minor
        exemption_source = "line_no_return"
    elif no_return_line is False:
        classification = "required"
        major_snapshot = category_major
        minor_snapshot = category_minor
    elif project_no_return_default:
        classification = "exempt"
        major_snapshot = category_major
        minor_snapshot = category_minor
        exemption_source = "project_default_no_return"
    elif category_major == "硬盘":
        classification = "exempt"
        major_snapshot = category_major
        minor_snapshot = category_minor
        exemption_source = "category_disk"
    else:
        classification = "required"
        major_snapshot = category_major
        minor_snapshot = category_minor
    return {
        "classification": classification,
        "category_id_snapshot": category_id,
        "category_major_snapshot": major_snapshot,
        "category_minor_snapshot": minor_snapshot,
        "exemption_source": exemption_source,
        "rule_version": RETURN_RULE_VERSION,
    }


def _no_return_flag(raw_line: dict) -> bool | None:
    value = raw_line.get("no_return")
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise BadReturnError("现场领用返还事件 no_return 标记无效")


def _rate(numerator: Decimal, denominator: Decimal) -> str:
    return str(
        (numerator / denominator * Decimal("100")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def calculate_return_rate(
    *,
    required_quantity: Decimal,
    exempt_quantity: Decimal,
    pending_quantity: Decimal,
    registered_quantity: Decimal,
    warehouse_confirmed_quantity: Decimal,
) -> dict:
    """Return operational rates while the official numerator remains undecided."""

    payload = {
        "required_quantity": required_quantity,
        "exempt_quantity": exempt_quantity,
        "pending_quantity": pending_quantity,
        "registered_quantity": registered_quantity,
        "warehouse_confirmed_quantity": warehouse_confirmed_quantity,
        "outstanding_quantity": max(
            required_quantity - warehouse_confirmed_quantity,
            Decimal("0"),
        ),
        "official_basis": None,
        "registered_rate_pct": None,
        "warehouse_confirmed_rate_pct": None,
        "official_rate_pct": None,
    }
    if pending_quantity > 0:
        return {**payload, "status": "basis_incomplete"}
    if required_quantity == 0:
        return {**payload, "status": "no_return_required"}
    registered_rate = _rate(registered_quantity, required_quantity)
    confirmed_rate = _rate(warehouse_confirmed_quantity, required_quantity)
    return {
        **payload,
        "status": "available",
        "registered_rate_pct": registered_rate,
        "warehouse_confirmed_rate_pct": confirmed_rate,
    }


def _required(value: object, label: str, max_length: int = 128) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise BadReturnError(f"{label}不能为空")
    if len(clean) > max_length:
        raise BadReturnError(f"{label}过长")
    return clean


def _quantity(value: Decimal | str) -> Decimal:
    try:
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise InvalidOperation
        normalized = parsed.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise BadReturnError("返还数量超出允许范围") from exc
    if normalized <= 0 or normalized >= _QUANTITY_MAX_EXCLUSIVE:
        raise BadReturnError("返还数量超出允许范围")
    return normalized


def _qty(value: Decimal) -> str:
    return format(value, ".3f")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _lock_idempotency_key(db: Session, key: str) -> None:
    db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
    )


def _set_write_lock_timeout(db: Session) -> None:
    """Bound PostgreSQL lock waits for this transaction; callers can retry."""

    db.execute(select(func.set_config("lock_timeout", _WRITE_LOCK_TIMEOUT, True)))


def _audit(
    db: Session,
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectOperationAudit(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=_required(reason, "操作原因", 1000),
            operated_by=_required(operated_by, "操作人", 64),
        )
    )


def _command_replay(
    db: Session,
    *,
    idempotency_key: str,
    project_id: str,
    entity_type: str,
    action: str,
    request_fingerprint: str,
    entity_id: str | None = None,
) -> dict | None:
    row = db.scalar(
        select(MaintenanceBadReturnCommand).where(
            MaintenanceBadReturnCommand.idempotency_key == idempotency_key
        )
    )
    if row is None:
        return None
    if (
        row.project_id != project_id
        or row.entity_type != entity_type
        or row.action != action
        or row.request_fingerprint != request_fingerprint
        or (entity_id is not None and row.entity_id != entity_id)
    ):
        raise BadReturnConflict("幂等键已用于不同的坏件返还操作")
    return {**row.response_json, "idempotent_replay": True}


def _record_command(
    db: Session,
    *,
    idempotency_key: str,
    project_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    request_fingerprint: str,
    response: dict,
) -> None:
    db.add(
        MaintenanceBadReturnCommand(
            command_id=str(uuid4()),
            idempotency_key=idempotency_key,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            request_fingerprint=request_fingerprint,
            response_json=response,
        )
    )


def _obligation_id(issue_id: str, delivery_line_id: str) -> str:
    return str(uuid5(_OBLIGATION_NAMESPACE, f"{issue_id}:{delivery_line_id}"))


def _obligation_dict(
    row: MaintenanceReturnObligation,
    *,
    issue_no: str | None = None,
    registered_quantity: Decimal = Decimal("0"),
    warehouse_confirmed_quantity: Decimal = Decimal("0"),
) -> dict:
    return {
        "obligation_id": row.obligation_id,
        "project_id": row.project_id,
        "issue_id": row.issue_id,
        "issue_no": issue_no,
        "issue_line_id": row.issue_line_id,
        "delivery_line_id": row.delivery_line_id,
        "part_id": row.part_id,
        "pn": row.pn,
        "source_quantity": _qty(Decimal(row.source_quantity)),
        "required_quantity": _qty(Decimal(row.required_quantity)),
        "classification": row.classification,
        "exemption_source": row.exemption_source,
        "category_id_snapshot": row.category_id_snapshot,
        "category_major_snapshot": row.category_major_snapshot,
        "category_minor_snapshot": row.category_minor_snapshot,
        "rule_version": row.rule_version,
        "source_issue_version": row.source_issue_version,
        "registered_quantity": _qty(registered_quantity),
        "warehouse_confirmed_quantity": _qty(warehouse_confirmed_quantity),
        "remaining_quantity": _qty(
            max(Decimal(row.required_quantity) - registered_quantity, Decimal("0"))
        ),
        "is_active": row.is_active,
        "version": row.version,
    }


def _return_line_dict(row: MaintenanceBadReturnLine) -> dict:
    return {
        "return_line_id": row.return_line_id,
        "line_no": row.line_no,
        "obligation_id": row.obligation_id,
        "part_id": row.part_id,
        "pn": row.pn,
        "quantity": _qty(Decimal(row.quantity)),
    }


def _bad_return_dict(
    row: MaintenanceBadReturn,
    lines: list[MaintenanceBadReturnLine],
) -> dict:
    return {
        "return_id": row.return_id,
        "return_no": row.return_no,
        "replaces_return_id": row.replaces_return_id,
        "project_id": row.project_id,
        "status": row.status,
        "logistics_reference": row.logistics_reference,
        "warehouse_reference": row.warehouse_reference,
        "inbound_reference": row.inbound_reference,
        "note": row.note,
        "created_by": row.created_by,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "in_transit_at": row.in_transit_at.isoformat() if row.in_transit_at else None,
        "warehouse_confirmed_at": (
            row.warehouse_confirmed_at.isoformat()
            if row.warehouse_confirmed_at
            else None
        ),
        "voided_at": row.voided_at.isoformat() if row.voided_at else None,
        "version": row.version,
        "lines": [_return_line_dict(line) for line in lines],
        "inventory_effect": "none",
        "cost_effect": "none",
    }


def _registered_quantities(
    db: Session,
    obligation_ids: set[str] | list[str],
) -> dict[str, tuple[Decimal, Decimal]]:
    if not obligation_ids:
        return {}
    result: dict[str, tuple[Decimal, Decimal]] = {}
    for obligation_id, registered, confirmed in db.execute(
        select(
            MaintenanceBadReturnLine.obligation_id,
            func.coalesce(
                func.sum(
                    case(
                        (
                            MaintenanceBadReturn.status.in_(_ACTIVE_RETURN_STATUSES),
                            MaintenanceBadReturnLine.quantity,
                        ),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            MaintenanceBadReturn.status == "warehouse_confirmed",
                            MaintenanceBadReturnLine.quantity,
                        ),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ),
        )
        .join(
            MaintenanceBadReturn,
            MaintenanceBadReturn.return_id == MaintenanceBadReturnLine.return_id,
        )
        .where(MaintenanceBadReturnLine.obligation_id.in_(obligation_ids))
        .group_by(MaintenanceBadReturnLine.obligation_id)
    ):
        result[obligation_id] = (Decimal(registered), Decimal(confirmed))
    return result


def _category_evidence(
    db: Session,
    part_ids: set[int],
) -> dict[int, tuple[int | None, str | None, str | None]]:
    if not part_ids:
        return {}
    return {
        part_id: (category_id, category_major, category_minor)
        for part_id, category_id, category_major, category_minor in db.execute(
            select(
                DimPart.id,
                DimPart.category_id,
                ProductCategory.category_major,
                ProductCategory.category_minor,
            )
            .outerjoin(ProductCategory, ProductCategory.id == DimPart.category_id)
            .where(DimPart.id.in_(part_ids))
        )
    }


def consume_return_event(
    db: Session,
    event: MaintenanceSiteIssueReturnEvent,
) -> list[MaintenanceReturnObligation]:
    """Project one #207 outbox event exactly once inside the caller transaction."""

    _set_write_lock_timeout(db)

    expected_prefix = "maintenance-return-obligations:"
    if event.downstream_reference is not None:
        if not event.downstream_reference.startswith(expected_prefix):
            raise BadReturnConflict("现场领用返还事件已被其他下游登记")
        return list(
            db.scalars(
                select(MaintenanceReturnObligation).where(
                    MaintenanceReturnObligation.issue_id == event.issue_id
                )
            )
        )

    payload = event.payload or {}
    if (
        payload.get("schema_version")
        != "maintenance-return-obligation-interface-v1"
        or payload.get("project_id") != event.project_id
        or payload.get("issue_id") != event.issue_id
    ):
        raise BadReturnError("现场领用返还事件契约无效")
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        raise BadReturnError("现场领用返还事件缺少明细")

    existing = list(
        db.scalars(
            select(MaintenanceReturnObligation)
            .where(MaintenanceReturnObligation.issue_id == event.issue_id)
            .order_by(MaintenanceReturnObligation.obligation_id)
            .with_for_update()
        )
    )
    latest_obligation_version = max(
        (row.source_issue_version for row in existing), default=0
    )
    latest_consumed_event_version = db.scalar(
        select(func.max(MaintenanceSiteIssueReturnEvent.issue_version)).where(
            MaintenanceSiteIssueReturnEvent.issue_id == event.issue_id,
            MaintenanceSiteIssueReturnEvent.downstream_reference.like(
                f"{expected_prefix}%"
            ),
        )
    )
    latest_projected_version = max(
        latest_obligation_version,
        int(latest_consumed_event_version or 0),
    )
    if event.issue_version < latest_projected_version:
        # Historical/out-of-order source events are still acknowledged so they
        # cannot retry forever, but they must never overwrite a newer issue
        # projection. Existing rows include inactive obligations left by voids,
        # so the version watermark also survives an empty active result set.
        event.downstream_reference = (
            f"maintenance-return-obligations:{event.issue_id}:"
            f"stale-v{event.issue_version}:current-v{latest_projected_version}"
        )
        event.consumed_at = datetime.now(UTC)
        db.flush()
        return existing
    existing_by_delivery = {row.delivery_line_id: row for row in existing}
    quantities = _registered_quantities(
        db, {row.obligation_id for row in existing}
    )
    now_lines: dict[str, dict] = {}
    for raw_line in raw_lines:
        if not isinstance(raw_line, dict):
            raise BadReturnError("现场领用返还事件明细无效")
        delivery_line_id = _required(
            raw_line.get("delivery_line_id"), "发货明细稳定编号", 64
        )
        if delivery_line_id in now_lines:
            raise BadReturnError("现场领用返还事件存在重复发货明细")
        now_lines[delivery_line_id] = raw_line

    if event.event_type == "return_obligation_voided":
        now_lines = {}
    elif event.event_type not in {
        "return_obligation_created",
        "return_obligation_corrected",
    }:
        raise BadReturnError("现场领用返还事件类型无效")

    for row in existing:
        if row.delivery_line_id in now_lines or not row.is_active:
            continue
        registered = quantities.get(row.obligation_id, (Decimal("0"), Decimal("0")))[0]
        if registered > 0:
            raise BadReturnConflict("已有返还登记的领用行不能从更正中移除")
        before = _obligation_dict(row)
        row.is_active = False
        row.source_issue_version = event.issue_version
        row.last_source_event_id = event.event_id
        row.version += 1
        _audit(
            db,
            project_id=row.project_id,
            entity_type="return_obligation",
            entity_id=row.obligation_id,
            action="deactivate",
            before=before,
            after=_obligation_dict(row),
            reason=f"消费现场领用返还事件 {event.event_id}",
            operated_by="system:return-obligation-projector",
        )

    new_part_ids = {
        int(line["part_id"])
        for delivery_id, line in now_lines.items()
        if delivery_id not in existing_by_delivery
    }
    categories = _category_evidence(db, new_part_ids)
    project_no_return_default = bool(
        db.scalar(
            select(MaintenanceProject.no_return_default).where(
                MaintenanceProject.project_id == event.project_id
            )
        )
    )
    projected: list[MaintenanceReturnObligation] = []
    for delivery_line_id, raw_line in now_lines.items():
        issue_line_id = _required(raw_line.get("issue_line_id"), "领用明细编号", 64)
        part_id = int(raw_line["part_id"])
        pn = _required(raw_line.get("pn"), "料号", 128)
        source_quantity = _quantity(raw_line["quantity"])
        row = existing_by_delivery.get(delivery_line_id)
        if row is None:
            if part_id not in categories:
                raise BadReturnError("现场领用明细引用的标准 PN 不存在")
            category_id, category_major, category_minor = categories[part_id]
            classification = classify_return_obligation(
                category_id=category_id,
                category_major=category_major,
                category_minor=category_minor,
                no_return_line=_no_return_flag(raw_line),
                project_no_return_default=project_no_return_default,
            )
            required_quantity = (
                source_quantity
                if classification["classification"] == "required"
                else Decimal("0")
            )
            row = MaintenanceReturnObligation(
                obligation_id=_obligation_id(event.issue_id, delivery_line_id),
                project_id=event.project_id,
                issue_id=event.issue_id,
                issue_line_id=issue_line_id,
                delivery_line_id=delivery_line_id,
                part_id=part_id,
                pn=pn,
                source_quantity=source_quantity,
                required_quantity=required_quantity,
                classification=classification["classification"],
                exemption_source=classification["exemption_source"],
                category_id_snapshot=classification["category_id_snapshot"],
                category_major_snapshot=classification["category_major_snapshot"],
                category_minor_snapshot=classification["category_minor_snapshot"],
                rule_version=classification["rule_version"],
                source_issue_version=event.issue_version,
                last_source_event_id=event.event_id,
                is_active=True,
                version=1,
            )
            db.add(row)
            db.flush()
            _audit(
                db,
                project_id=row.project_id,
                entity_type="return_obligation",
                entity_id=row.obligation_id,
                action="create",
                before=None,
                after=_obligation_dict(row),
                reason=f"消费现场领用返还事件 {event.event_id}",
                operated_by="system:return-obligation-projector",
            )
        else:
            if row.part_id != part_id or row.pn != pn:
                raise BadReturnConflict("同一稳定发货明细的 PN 身份不能更改")
            # 更正路径沿用行内冻结的品类证据（新分类仅用于新行；老行不因
            # 本轮 categories 未重新加载而退化到 pending）
            category_id, category_major, category_minor = categories.get(
                part_id,
                (
                    row.category_id_snapshot,
                    row.category_major_snapshot,
                    row.category_minor_snapshot,
                ),
            )
            classification = classify_return_obligation(
                category_id=category_id,
                category_major=category_major,
                category_minor=category_minor,
                no_return_line=_no_return_flag(raw_line),
                project_no_return_default=project_no_return_default,
            )
            required_quantity = (
                source_quantity
                if classification["classification"] == "required"
                else Decimal("0")
            )
            registered = quantities.get(
                row.obligation_id,
                (Decimal("0"), Decimal("0")),
            )[0]
            if registered > required_quantity:
                raise BadReturnConflict("更正后的应返数量不能低于已登记返还数量")
            before = _obligation_dict(row, registered_quantity=registered)
            row.issue_line_id = issue_line_id
            row.source_quantity = source_quantity
            row.required_quantity = required_quantity
            row.classification = classification["classification"]
            row.exemption_source = classification["exemption_source"]
            row.category_id_snapshot = classification["category_id_snapshot"]
            row.category_major_snapshot = classification["category_major_snapshot"]
            row.category_minor_snapshot = classification["category_minor_snapshot"]
            row.rule_version = classification["rule_version"]
            row.source_issue_version = event.issue_version
            row.last_source_event_id = event.event_id
            row.is_active = True
            row.version += 1
            _audit(
                db,
                project_id=row.project_id,
                entity_type="return_obligation",
                entity_id=row.obligation_id,
                action="correct",
                before=before,
                after=_obligation_dict(row, registered_quantity=registered),
                reason=f"消费现场领用返还事件 {event.event_id}",
                operated_by="system:return-obligation-projector",
            )
        projected.append(row)

    event.downstream_reference = (
        f"maintenance-return-obligations:{event.issue_id}:v{event.issue_version}"
    )
    event.consumed_at = datetime.now(UTC)
    db.flush()
    return projected


def consume_pending_return_events(
    db: Session,
    *,
    project_id: str | None = None,
    project_ids: list[str] | set[str] | None = None,
) -> int:
    _set_write_lock_timeout(db)
    if project_id is not None and project_ids is not None:
        raise ValueError("project_id and project_ids are mutually exclusive")
    filters = [MaintenanceSiteIssueReturnEvent.downstream_reference.is_(None)]
    if project_id is not None:
        filters.append(MaintenanceSiteIssueReturnEvent.project_id == project_id)
    elif project_ids is not None:
        ids = list(dict.fromkeys(project_ids))
        if not ids:
            return 0
        filters.append(MaintenanceSiteIssueReturnEvent.project_id.in_(ids))
    events = list(
        db.scalars(
            select(MaintenanceSiteIssueReturnEvent)
            .where(*filters)
            .order_by(
                MaintenanceSiteIssueReturnEvent.project_id,
                MaintenanceSiteIssueReturnEvent.issue_id,
                MaintenanceSiteIssueReturnEvent.issue_version,
                MaintenanceSiteIssueReturnEvent.created_at,
                MaintenanceSiteIssueReturnEvent.event_id,
            )
            .with_for_update()
        )
    )
    for event in events:
        consume_return_event(db, event)
    return len(events)


def return_rates_for_projects(
    db: Session,
    *,
    project_ids: list[str] | set[str],
) -> dict[str, dict]:
    ids = list(dict.fromkeys(project_ids))
    if not ids:
        return {}
    # This projection is used by workspaces, cards, exports, and rate endpoints.
    # Keep it strictly read-only: source-event projection belongs to the
    # site-issue/bad-return write transaction, never to a dashboard read.
    obligation_facts: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "required_quantity": Decimal("0"),
            "exempt_quantity": Decimal("0"),
            "pending_quantity": Decimal("0"),
            "required_count": 0,
            "exempt_count": 0,
            "pending_count": 0,
        }
    )
    for (
        project_id,
        required_quantity,
        exempt_quantity,
        pending_quantity,
        required_count,
        exempt_count,
        pending_count,
    ) in db.execute(
        select(
            MaintenanceReturnObligation.project_id,
            func.coalesce(
                func.sum(MaintenanceReturnObligation.required_quantity).filter(
                    MaintenanceReturnObligation.classification == "required"
                ),
                Decimal("0"),
            ),
            func.coalesce(
                func.sum(MaintenanceReturnObligation.source_quantity).filter(
                    MaintenanceReturnObligation.classification == "exempt"
                ),
                Decimal("0"),
            ),
            func.coalesce(
                func.sum(MaintenanceReturnObligation.source_quantity).filter(
                    MaintenanceReturnObligation.classification == "pending_category"
                ),
                Decimal("0"),
            ),
            func.count().filter(
                MaintenanceReturnObligation.classification == "required"
            ),
            func.count().filter(
                MaintenanceReturnObligation.classification == "exempt"
            ),
            func.count().filter(
                MaintenanceReturnObligation.classification == "pending_category"
            ),
        )
        .where(
            MaintenanceReturnObligation.project_id.in_(ids),
            MaintenanceReturnObligation.is_active.is_(True),
        )
        .group_by(MaintenanceReturnObligation.project_id)
    ):
        obligation_facts[project_id] = {
            "required_quantity": Decimal(required_quantity),
            "exempt_quantity": Decimal(exempt_quantity),
            "pending_quantity": Decimal(pending_quantity),
            "required_count": int(required_count),
            "exempt_count": int(exempt_count),
            "pending_count": int(pending_count),
        }

    return_facts: dict[str, tuple[Decimal, Decimal]] = defaultdict(
        lambda: (Decimal("0"), Decimal("0"))
    )
    for project_id, registered, confirmed in db.execute(
        select(
            MaintenanceReturnObligation.project_id,
            func.coalesce(
                func.sum(
                    case(
                        (
                            MaintenanceBadReturn.status.in_(_ACTIVE_RETURN_STATUSES),
                            MaintenanceBadReturnLine.quantity,
                        ),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            MaintenanceBadReturn.status == "warehouse_confirmed",
                            MaintenanceBadReturnLine.quantity,
                        ),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ),
        )
        .select_from(MaintenanceReturnObligation)
        .join(
            MaintenanceBadReturnLine,
            MaintenanceBadReturnLine.obligation_id
            == MaintenanceReturnObligation.obligation_id,
        )
        .join(
            MaintenanceBadReturn,
            MaintenanceBadReturn.return_id == MaintenanceBadReturnLine.return_id,
        )
        .where(
            MaintenanceReturnObligation.project_id.in_(ids),
            MaintenanceReturnObligation.is_active.is_(True),
        )
        .group_by(MaintenanceReturnObligation.project_id)
    ):
        return_facts[project_id] = (Decimal(registered), Decimal(confirmed))

    result: dict[str, dict] = {}
    for project_id in ids:
        facts = obligation_facts[project_id]
        registered, confirmed = return_facts[project_id]
        calculated = calculate_return_rate(
            required_quantity=Decimal(facts["required_quantity"]),
            exempt_quantity=Decimal(facts["exempt_quantity"]),
            pending_quantity=Decimal(facts["pending_quantity"]),
            registered_quantity=registered,
            warehouse_confirmed_quantity=confirmed,
        )
        result[project_id] = {
            "project_id": project_id,
            **{
                key: _qty(value) if isinstance(value, Decimal) else value
                for key, value in calculated.items()
            },
            "required_count": int(facts["required_count"]),
            "exempt_count": int(facts["exempt_count"]),
            "pending_count": int(facts["pending_count"]),
            "business_assumption": (
                "仓库确认量仅作试算；官方返还率分子待业务确认。"
                "返库登记仅表示已登记或在途。"
            ),
        }
    return result


def project_return_rate(db: Session, *, project_id: str) -> dict | None:
    if db.get(MaintenanceProject, project_id) is None:
        return None
    return return_rates_for_projects(db, project_ids=[project_id])[project_id]


def search_return_obligations(
    db: Session,
    *,
    project_id: str,
    q: str | None,
    classifications: list[str],
    active_only: bool,
    page: int,
    page_size: int,
) -> dict | None:
    if db.get(MaintenanceProject, project_id) is None:
        return None
    filters = [MaintenanceReturnObligation.project_id == project_id]
    if active_only:
        filters.append(MaintenanceReturnObligation.is_active.is_(True))
    if classifications:
        filters.append(MaintenanceReturnObligation.classification.in_(classifications))
    if q and (search := q.strip()):
        filters.append(
            or_(
                MaintenanceReturnObligation.pn.icontains(search, autoescape=True),
                MaintenanceSiteIssue.issue_no.icontains(search, autoescape=True),
            )
        )
    total = int(
        db.scalar(
            select(func.count())
            .select_from(MaintenanceReturnObligation)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceReturnObligation.issue_id,
            )
            .where(*filters)
        )
        or 0
    )
    rows = list(
        db.execute(
            select(MaintenanceReturnObligation, MaintenanceSiteIssue.issue_no)
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceReturnObligation.issue_id,
            )
            .where(*filters)
            .order_by(
                MaintenanceReturnObligation.created_at.desc(),
                MaintenanceReturnObligation.obligation_id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    quantities = _registered_quantities(
        db, {row.obligation_id for row, _issue_no in rows}
    )
    return {
        "project_id": project_id,
        "rows": [
            _obligation_dict(
                row,
                issue_no=issue_no,
                registered_quantity=quantities.get(
                    row.obligation_id,
                    (Decimal("0"), Decimal("0")),
                )[0],
                warehouse_confirmed_quantity=quantities.get(
                    row.obligation_id,
                    (Decimal("0"), Decimal("0")),
                )[1],
            )
            for row, issue_no in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "return_rate": return_rates_for_projects(
            db, project_ids=[project_id]
        )[project_id],
    }


def create_bad_return(
    db: Session,
    *,
    project_id: str,
    idempotency_key: str,
    replaces_return_id: str | None,
    lines: list[dict],
    note: str | None,
    reason: str,
    operated_by: str,
) -> dict | None:
    _set_write_lock_timeout(db)
    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise BadReturnError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    clean_replaces_return_id = (
        _required(replaces_return_id, "被替代返还单编号", 36)
        if replaces_return_id is not None
        else None
    )
    if not lines or len(lines) > 200:
        raise BadReturnError("坏件返还单需要 1 至 200 条明细")
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in lines:
        obligation_id = _required(raw.get("obligation_id"), "返还义务编号", 36)
        if obligation_id in seen:
            raise BadReturnError("同一返还义务不能在一张返还单中重复")
        seen.add(obligation_id)
        normalized.append(
            {
                "obligation_id": obligation_id,
                "quantity": _quantity(raw["quantity"]),
            }
        )
    request_fingerprint = _fingerprint(
        {
            "action": "create",
            "project_id": project_id,
            "replaces_return_id": clean_replaces_return_id,
            "lines": [
                {
                    "obligation_id": row["obligation_id"],
                    "quantity": _qty(row["quantity"]),
                }
                for row in normalized
            ],
            "note": note.strip() if note and note.strip() else None,
            "reason": clean_reason,
        }
    )
    _lock_idempotency_key(db, clean_key)
    replay = _command_replay(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        entity_type="bad_return",
        action="create",
        request_fingerprint=request_fingerprint,
    )
    if replay is not None:
        return replay
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        return None
    if not project.is_active:
        raise BadReturnError("项目主档已归档")
    consume_pending_return_events(db, project_id=project_id)
    if clean_replaces_return_id is not None:
        replaced = db.scalar(
            select(MaintenanceBadReturn)
            .where(MaintenanceBadReturn.return_id == clean_replaces_return_id)
            .with_for_update()
        )
        if replaced is None:
            raise BadReturnError("被替代的坏件返还单不存在")
        if replaced.project_id != project_id:
            raise BadReturnPermissionError("被替代返还单不属于当前稳定项目")
        if replaced.status != "void":
            raise BadReturnConflict("只有已作废坏件返还单可以建立替代单")
    obligations = list(
        db.scalars(
            select(MaintenanceReturnObligation)
            .where(MaintenanceReturnObligation.obligation_id.in_(seen))
            .order_by(MaintenanceReturnObligation.obligation_id)
            .with_for_update()
        )
    )
    if len(obligations) != len(normalized):
        raise BadReturnError("返还义务不存在")
    by_id = {row.obligation_id: row for row in obligations}
    registered = _registered_quantities(db, seen)
    for requested in normalized:
        obligation = by_id[requested["obligation_id"]]
        if obligation.project_id != project_id:
            raise BadReturnPermissionError("返还义务不属于当前稳定项目")
        if not obligation.is_active or obligation.classification != "required":
            raise BadReturnError("仅明确应返的有效义务可以登记返还")
        already = registered.get(obligation.obligation_id, (Decimal("0"), Decimal("0")))[0]
        if requested["quantity"] > Decimal(obligation.required_quantity) - already:
            raise BadReturnConflict("返还数量超过当前剩余应返数量")

    return_id = str(uuid4())
    row = MaintenanceBadReturn(
        return_id=return_id,
        return_no=f"BHR-{uuid4().hex[:16].upper()}",
        replaces_return_id=clean_replaces_return_id,
        project_id=project_id,
        status="draft",
        note=note.strip() if note and note.strip() else None,
        created_by=operated_by,
        version=1,
    )
    saved_lines = [
        MaintenanceBadReturnLine(
            return_line_id=str(uuid4()),
            return_id=return_id,
            line_no=line_no,
            obligation_id=requested["obligation_id"],
            part_id=by_id[requested["obligation_id"]].part_id,
            pn=by_id[requested["obligation_id"]].pn,
            quantity=requested["quantity"],
        )
        for line_no, requested in enumerate(normalized, start=1)
    ]
    db.add(row)
    db.add_all(saved_lines)
    db.flush()
    response = {
        **_bad_return_dict(row, saved_lines),
        "idempotent_replay": False,
    }
    _audit(
        db,
        project_id=project_id,
        entity_type="bad_return",
        entity_id=return_id,
        action="create",
        before=None,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_command(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        entity_type="bad_return",
        entity_id=return_id,
        action="create",
        request_fingerprint=request_fingerprint,
        response=response,
    )
    db.flush()
    return response


def _bad_return_lines(
    db: Session,
    *,
    return_id: str,
) -> list[MaintenanceBadReturnLine]:
    return list(
        db.scalars(
            select(MaintenanceBadReturnLine)
            .where(MaintenanceBadReturnLine.return_id == return_id)
            .order_by(MaintenanceBadReturnLine.line_no)
        )
    )


def transition_bad_return(
    db: Session,
    *,
    return_id: str,
    project_id: str,
    version: int,
    idempotency_key: str,
    action: str,
    reason: str,
    operated_by: str,
    logistics_reference: str | None = None,
    warehouse_reference: str | None = None,
    inbound_reference: str | None = None,
) -> dict | None:
    _set_write_lock_timeout(db)
    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise BadReturnError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    if action not in {"submit", "in_transit", "warehouse_confirm", "void"}:
        raise BadReturnError("坏件返还状态操作无效")
    clean_logistics = (
        _required(logistics_reference, "物流引用", 128)
        if action == "in_transit"
        else None
    )
    clean_warehouse = (
        _required(warehouse_reference, "仓库确认引用", 128)
        if action == "warehouse_confirm"
        else None
    )
    clean_inbound = (
        _required(inbound_reference, "正式入库引用", 128)
        if inbound_reference is not None
        else None
    )
    request_fingerprint = _fingerprint(
        {
            "action": action,
            "return_id": return_id,
            "project_id": project_id,
            "version": version,
            "logistics_reference": clean_logistics,
            "warehouse_reference": clean_warehouse,
            "inbound_reference": clean_inbound,
            "reason": clean_reason,
        }
    )
    _lock_idempotency_key(db, clean_key)
    replay = _command_replay(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        entity_type="bad_return",
        entity_id=return_id,
        action=action,
        request_fingerprint=request_fingerprint,
    )
    if replay is not None:
        return replay
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        return None
    if not project.is_active:
        raise BadReturnError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceBadReturn)
        .where(MaintenanceBadReturn.return_id == return_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.project_id != project_id:
        raise BadReturnPermissionError("坏件返还单不属于当前稳定项目")
    if row.version != version:
        raise BadReturnConflict("坏件返还单版本已变化，请刷新后重试")
    expected = {
        "submit": {"draft"},
        "in_transit": {"submitted"},
        "warehouse_confirm": {"submitted", "in_transit"},
        "void": {"draft", "submitted", "in_transit", "warehouse_confirmed"},
    }[action]
    if row.status not in expected:
        raise BadReturnConflict("当前坏件返还状态不允许此操作")
    if action == "void" and row.status == "warehouse_confirmed" and row.inbound_reference:
        raise BadReturnConflict("已关联正式入库的坏件返还单不能作废")
    lines = _bad_return_lines(db, return_id=return_id)
    if action == "submit":
        obligation_ids = {line.obligation_id for line in lines}
        obligations = list(
            db.scalars(
                select(MaintenanceReturnObligation)
                .where(MaintenanceReturnObligation.obligation_id.in_(obligation_ids))
                .order_by(MaintenanceReturnObligation.obligation_id)
                .with_for_update()
            )
        )
        if len(obligations) != len(obligation_ids):
            raise BadReturnConflict("返还义务已变化")
        by_id = {obligation.obligation_id: obligation for obligation in obligations}
        registered = _registered_quantities(db, obligation_ids)
        for line in lines:
            obligation = by_id[line.obligation_id]
            if (
                obligation.project_id != project_id
                or not obligation.is_active
                or obligation.classification != "required"
            ):
                raise BadReturnConflict("返还义务已变化，草稿不能提交")
            already = registered.get(
                obligation.obligation_id,
                (Decimal("0"), Decimal("0")),
            )[0]
            if already + Decimal(line.quantity) > Decimal(obligation.required_quantity):
                raise BadReturnConflict("返还数量超过当前剩余应返数量")

    before = _bad_return_dict(row, lines)
    now = datetime.now(UTC)
    if action == "submit":
        row.status = "submitted"
        row.submitted_at = now
    elif action == "in_transit":
        row.status = "in_transit"
        row.logistics_reference = clean_logistics
        row.in_transit_at = now
    elif action == "warehouse_confirm":
        row.status = "warehouse_confirmed"
        row.warehouse_reference = clean_warehouse
        row.inbound_reference = clean_inbound
        row.warehouse_confirmed_at = now
    else:
        row.status = "void"
        row.voided_at = now
    row.version += 1
    db.flush()
    response = {
        **_bad_return_dict(row, lines),
        "idempotent_replay": False,
    }
    _audit(
        db,
        project_id=project_id,
        entity_type="bad_return",
        entity_id=return_id,
        action=action,
        before=before,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_command(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        entity_type="bad_return",
        entity_id=return_id,
        action=action,
        request_fingerprint=request_fingerprint,
        response=response,
    )
    db.flush()
    return response


def search_bad_returns(
    db: Session,
    *,
    project_id: str,
    statuses: list[str],
    page: int,
    page_size: int,
) -> dict | None:
    if db.get(MaintenanceProject, project_id) is None:
        return None
    filters = [MaintenanceBadReturn.project_id == project_id]
    if statuses:
        filters.append(MaintenanceBadReturn.status.in_(statuses))
    total = int(
        db.scalar(
            select(func.count()).select_from(MaintenanceBadReturn).where(*filters)
        )
        or 0
    )
    documents = list(
        db.scalars(
            select(MaintenanceBadReturn)
            .where(*filters)
            .order_by(
                MaintenanceBadReturn.created_at.desc(),
                MaintenanceBadReturn.return_id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return_ids = [row.return_id for row in documents]
    lines_by_return: dict[str, list[MaintenanceBadReturnLine]] = defaultdict(list)
    if return_ids:
        for line in db.scalars(
            select(MaintenanceBadReturnLine)
            .where(MaintenanceBadReturnLine.return_id.in_(return_ids))
            .order_by(
                MaintenanceBadReturnLine.return_id,
                MaintenanceBadReturnLine.line_no,
            )
        ):
            lines_by_return[line.return_id].append(line)
    return {
        "project_id": project_id,
        "rows": [
            _bad_return_dict(row, lines_by_return[row.return_id])
            for row in documents
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def resolve_obligation_category(
    db: Session,
    *,
    obligation_id: str,
    project_id: str,
    version: int,
    category_id: int,
    idempotency_key: str,
    reason: str,
    operated_by: str,
) -> dict | None:
    _set_write_lock_timeout(db)
    clean_key = _required(idempotency_key, "幂等键", 128)
    if len(clean_key) < 8:
        raise BadReturnError("幂等键至少需要 8 个字符")
    clean_reason = _required(reason, "操作原因", 1000)
    request_fingerprint = _fingerprint(
        {
            "action": "resolve_category",
            "obligation_id": obligation_id,
            "project_id": project_id,
            "version": version,
            "category_id": category_id,
            "reason": clean_reason,
        }
    )
    _lock_idempotency_key(db, clean_key)
    replay = _command_replay(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        entity_type="return_obligation",
        entity_id=obligation_id,
        action="resolve_category",
        request_fingerprint=request_fingerprint,
    )
    if replay is not None:
        return replay
    project = db.scalar(
        select(MaintenanceProject)
        .where(MaintenanceProject.project_id == project_id)
        .with_for_update()
    )
    if project is None:
        return None
    if not project.is_active:
        raise BadReturnError("项目主档已归档")
    row = db.scalar(
        select(MaintenanceReturnObligation)
        .where(MaintenanceReturnObligation.obligation_id == obligation_id)
        .with_for_update()
    )
    if row is None:
        return None
    if row.project_id != project_id:
        raise BadReturnPermissionError("返还义务不属于当前稳定项目")
    if row.version != version:
        raise BadReturnConflict("返还义务版本已变化，请刷新后重试")
    if not row.is_active or row.classification != "pending_category":
        raise BadReturnConflict("仅品类待判定义务可以关联标准品类")
    category = db.get(ProductCategory, category_id)
    if category is None:
        raise BadReturnError("标准品类不存在")
    classification = classify_return_obligation(
        category_id=category.id,
        category_major=category.category_major,
        category_minor=category.category_minor,
    )
    before = _obligation_dict(row)
    row.category_id_snapshot = classification["category_id_snapshot"]
    row.category_major_snapshot = classification["category_major_snapshot"]
    row.category_minor_snapshot = classification["category_minor_snapshot"]
    row.classification = classification["classification"]
    row.rule_version = classification["rule_version"]
    row.required_quantity = (
        Decimal(row.source_quantity)
        if row.classification == "required"
        else Decimal("0")
    )
    row.version += 1
    db.flush()
    response = {
        **_obligation_dict(row),
        "idempotent_replay": False,
    }
    _audit(
        db,
        project_id=project_id,
        entity_type="return_obligation",
        entity_id=obligation_id,
        action="resolve_category",
        before=before,
        after=response,
        reason=clean_reason,
        operated_by=operated_by,
    )
    _record_command(
        db,
        idempotency_key=clean_key,
        project_id=project_id,
        entity_type="return_obligation",
        entity_id=obligation_id,
        action="resolve_category",
        request_fingerprint=request_fingerprint,
        response=response,
    )
    db.flush()
    return response
