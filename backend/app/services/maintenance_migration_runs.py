"""Persistent dry-run, reconciliation, and independent approval workflow."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import hmac
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.maintenance_migration import (
    MaintenanceHistoricalCostBaseline,
    MaintenanceInventoryOpeningBalance,
    MaintenanceMigrationDiscrepancy,
    MaintenanceMigrationEvent,
    MaintenanceMigrationRun,
    MaintenanceProjectCutoverPlan,
)
from app.services import maintenance_migration_controls as controls
from app.services.maintenance_migration_source import (
    MaintenanceMigrationSourceError,
    build_project_source_payload,
)
from app.services.maintenance_migration_warehouse import (
    MaintenanceMigrationWarehouseError,
    validate_cutover_inventory_movements,
)


WarehouseLoader = Callable[
    [Session, str, date], tuple[Sequence[Mapping[str, Any]], bool]
]


class MaintenanceMigrationRunError(ValueError):
    pass


class MaintenanceMigrationRunConflict(MaintenanceMigrationRunError):
    pass


class MaintenanceMigrationRunNotFound(MaintenanceMigrationRunError):
    pass


def unavailable_warehouse_loader(
    _db: Session, _project_id: str, _cutover_date: date
) -> tuple[Sequence[Mapping[str, Any]], bool]:
    return (), False


def _clean_text(value: Any, label: str, *, max_length: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > max_length:
        raise MaintenanceMigrationRunError(f"{label}无效")
    return clean


def _parse_date(value: Any, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise MaintenanceMigrationRunError(f"{label}无效") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _uuid() -> str:
    return str(uuid4())


def _operation_key(action: str, value: Any) -> str:
    clean = _clean_text(value, "操作幂等键", max_length=110)
    return f"{action}:{clean}"


def _advisory_lock(db: Session, key: str) -> None:
    raw = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    lock_id = int.from_bytes(raw, byteorder="big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def _normalize_candidate_baseline(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MaintenanceMigrationRunError("历史成本基线无效")
    return {
        "amount_ex_tax": str(value.get("amount_ex_tax", "")).strip(),
        "amount_inc_tax": str(value.get("amount_inc_tax", "")).strip(),
        "evidence_hash": str(value.get("evidence_hash", "")).strip().lower(),
    }


def _normalize_specs(projects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not projects:
        raise MaintenanceMigrationRunError("迁移项目清单不能为空")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in projects:
        project_id = _clean_text(item.get("project_id"), "项目稳定编号", max_length=36)
        if project_id in seen:
            raise MaintenanceMigrationRunError("迁移项目重复")
        seen.add(project_id)
        mode = str(item.get("historical_mode") or "")
        if mode not in {"approved_cost_baseline", "stable_site_issues"}:
            raise MaintenanceMigrationRunError("历史成本模式无效")
        baseline = _normalize_candidate_baseline(item.get("historical_baseline"))
        if mode == "approved_cost_baseline" and baseline is None:
            # The pure calculator records the missing baseline as a blocker.  Keeping
            # it absent here is intentional: dry-run remains inspectable but unsafe.
            pass
        if mode == "stable_site_issues" and baseline is not None:
            raise MaintenanceMigrationRunError("可靠历史领用与成本基线不能同时提交")
        openings: list[dict[str, Any]] = []
        opening_keys: set[str] = set()
        for row in item.get("opening_balances") or []:
            if not isinstance(row, Mapping):
                raise MaintenanceMigrationRunError("库存期初项无效")
            balance_key = _clean_text(
                row.get("balance_key"), "库存期初稳定键", max_length=256
            )
            if balance_key in opening_keys:
                raise MaintenanceMigrationRunError("库存期初稳定键重复")
            opening_keys.add(balance_key)
            openings.append(
                {
                    "balance_key": balance_key,
                    "pn": str(row.get("pn") or "").strip() or None,
                    "quantity": ""
                    if row.get("quantity") is None
                    else str(row.get("quantity")).strip(),
                    "evidence_hash": str(row.get("evidence_hash") or "")
                    .strip()
                    .lower(),
                }
            )
        output.append(
            {
                "project_id": project_id,
                "cutover_date": _parse_date(
                    item.get("cutover_date"), "切换日期"
                ).isoformat(),
                "historical_mode": mode,
                "historical_baseline": baseline,
                "opening_balances": sorted(
                    openings, key=lambda row: row["balance_key"]
                ),
            }
        )
    return sorted(output, key=lambda row: row["project_id"])


def _load_warehouse(
    loader: WarehouseLoader,
    db: Session,
    *,
    project_id: str,
    cutover_date: date,
) -> tuple[Sequence[Mapping[str, Any]], bool]:
    movements, ready = loader(db, project_id, cutover_date)
    try:
        validated = validate_cutover_inventory_movements(
            tuple(movements), cutover_date=cutover_date
        )
    except MaintenanceMigrationWarehouseError as exc:
        raise MaintenanceMigrationRunError(str(exc)) from exc
    return validated, bool(ready)


def _source_payloads_from_specs(
    db: Session,
    *,
    specs: Sequence[Mapping[str, Any]],
    loader: WarehouseLoader,
    approvals: Mapping[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for spec in specs:
        project_id = str(spec["project_id"])
        cutover_date = _parse_date(spec["cutover_date"], "切换日期")
        candidate_state = (approvals or {}).get(project_id, {})
        baseline = candidate_state.get("historical_baseline")
        if baseline is None and spec.get("historical_baseline") is not None:
            baseline = {**dict(spec["historical_baseline"]), "approved": False}
        openings = candidate_state.get("opening_balances")
        if openings is None:
            openings = [
                {**dict(row), "approved": False}
                for row in spec.get("opening_balances") or []
            ]
        movements, ready = _load_warehouse(
            loader,
            db,
            project_id=project_id,
            cutover_date=cutover_date,
        )
        payloads.append(
            build_project_source_payload(
                db,
                project_id=project_id,
                cutover_date=cutover_date,
                historical_mode=str(spec["historical_mode"]),
                historical_baseline=baseline,
                opening_balances=openings,
                inventory_movements=movements,
                warehouse_source_ready=ready,
            )
        )
    return payloads


def _wrapper(
    *, specs: Sequence[Mapping[str, Any]], payloads: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    preview = controls.build_migration_preview(
        rule_version=controls.RULE_VERSION,
        projects=payloads,
    )
    return {
        "preview": _jsonable(preview),
        "source_specs": _jsonable(specs),
        "source_payloads": _jsonable(payloads),
    }


def _project_preview_map(wrapper: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["project_id"]): dict(item) for item in wrapper["preview"]["projects"]
    }


def _persist_initial_rows(
    db: Session,
    *,
    run: MaintenanceMigrationRun,
    wrapper: Mapping[str, Any],
) -> None:
    preview_by_project = _project_preview_map(wrapper)
    for spec in wrapper["source_specs"]:
        project_id = str(spec["project_id"])
        preview = preview_by_project[project_id]
        cost = preview["cost"]
        plan = MaintenanceProjectCutoverPlan(
            plan_id=_uuid(),
            run_id=run.run_id,
            project_id=project_id,
            cutover_date=_parse_date(spec["cutover_date"], "切换日期"),
            historical_mode=str(spec["historical_mode"]),
            source_snapshot_hash=str(preview["source_snapshot_hash"]),
            input_fingerprint=str(preview["project_input_fingerprint"]),
            historical_cost_ex_tax=Decimal(cost["historical_baseline_ex_tax"]),
            historical_cost_inc_tax=Decimal(cost["historical_baseline_inc_tax"]),
            post_cutover_cost_ex_tax=Decimal(cost["post_cutover_consumption_ex_tax"]),
            post_cutover_cost_inc_tax=Decimal(cost["post_cutover_consumption_inc_tax"]),
            approved_expense_ex_tax=Decimal(cost["approved_expense_ex_tax"]),
            approved_expense_inc_tax=Decimal(cost["approved_expense_inc_tax"]),
            total_cost_ex_tax=Decimal(cost["total_ex_tax"]),
            total_cost_inc_tax=Decimal(cost["total_inc_tax"]),
            blocker_count=len(preview["approval_blockers"]),
            status="previewed",
        )
        db.add(plan)
        db.flush()
        baseline = spec.get("historical_baseline")
        if baseline is not None:
            db.add(
                MaintenanceHistoricalCostBaseline(
                    baseline_id=_uuid(),
                    plan_id=plan.plan_id,
                    project_id=project_id,
                    amount_ex_tax=Decimal(str(baseline["amount_ex_tax"])),
                    amount_inc_tax=Decimal(str(baseline["amount_inc_tax"])),
                    evidence_hash=str(baseline["evidence_hash"]),
                    approval_state="pending",
                )
            )
        for opening in spec.get("opening_balances") or []:
            db.add(
                MaintenanceInventoryOpeningBalance(
                    opening_balance_id=_uuid(),
                    plan_id=plan.plan_id,
                    project_id=project_id,
                    balance_key=str(opening["balance_key"]),
                    pn=opening.get("pn"),
                    quantity=Decimal(str(opening["quantity"])),
                    evidence_hash=str(opening["evidence_hash"]),
                    approval_state="pending",
                )
            )
        for blocker in preview["approval_blockers"]:
            stable_key = controls.canonical_hash(
                {
                    "project_id": project_id,
                    "code": blocker["code"],
                    "entity_id": blocker.get("entity_id"),
                }
            )
            db.add(
                MaintenanceMigrationDiscrepancy(
                    discrepancy_id=_uuid(),
                    run_id=run.run_id,
                    plan_id=plan.plan_id,
                    project_id=project_id,
                    stable_key=stable_key,
                    code=str(blocker["code"]),
                    entity_id=blocker.get("entity_id"),
                    severity="blocker",
                    status="open",
                    detail_json={"detail": str(blocker["detail"])},
                )
            )


def _event_replay(
    db: Session,
    *,
    run_id: str,
    action: str,
    operation_key: str,
    command_fingerprint: str,
) -> dict[str, Any] | None:
    event = db.scalar(
        select(MaintenanceMigrationEvent).where(
            MaintenanceMigrationEvent.operation_key == operation_key
        )
    )
    if event is None:
        return None
    if (
        event.run_id != run_id
        or event.action != action
        or event.payload_json.get("command_fingerprint") != command_fingerprint
    ):
        raise MaintenanceMigrationRunConflict("操作幂等键已用于不同请求")
    return get_run_detail(db, run_id=run_id)


def create_preview_run(
    db: Session,
    *,
    idempotency_key: str,
    projects: Sequence[Mapping[str, Any]],
    reason: str,
    operated_by: str,
    warehouse_loader: WarehouseLoader = unavailable_warehouse_loader,
) -> dict[str, Any]:
    clean_key = _clean_text(idempotency_key, "请求幂等键", max_length=96)
    clean_reason = _clean_text(reason, "生成 dry-run 理由", max_length=1000)
    operator = _clean_text(operated_by, "操作人", max_length=64)
    specs = _normalize_specs(projects)
    request_fingerprint = controls.canonical_hash(
        {
            "rule_version": controls.RULE_VERSION,
            "projects": specs,
            "reason": clean_reason,
            "operated_by": operator,
        }
    )
    operation_key = _operation_key("preview", clean_key)
    _advisory_lock(db, operation_key)
    existing = db.scalar(
        select(MaintenanceMigrationRun).where(
            MaintenanceMigrationRun.idempotency_key == clean_key
        )
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise MaintenanceMigrationRunConflict("请求幂等键已用于不同迁移清单")
        return get_run_detail(db, run_id=existing.run_id)

    try:
        payloads = _source_payloads_from_specs(db, specs=specs, loader=warehouse_loader)
        wrapper = _wrapper(specs=specs, payloads=payloads)
    except (controls.MigrationControlError, MaintenanceMigrationSourceError) as exc:
        raise MaintenanceMigrationRunError(str(exc)) from exc

    preview = wrapper["preview"]
    run = MaintenanceMigrationRun(
        run_id=_uuid(),
        idempotency_key=clean_key,
        request_fingerprint=request_fingerprint,
        rule_version=controls.RULE_VERSION,
        source_snapshot_hash=str(preview["source_snapshot_hash"]),
        status="previewed",
        preview_json=wrapper,
        created_by=operator,
    )
    db.add(run)
    db.flush()
    _persist_initial_rows(db, run=run, wrapper=wrapper)
    db.add(
        MaintenanceMigrationEvent(
            event_id=_uuid(),
            operation_key=operation_key,
            run_id=run.run_id,
            action="preview",
            from_status=None,
            to_status="previewed",
            payload_json={
                "command_fingerprint": controls.canonical_hash(
                    {"request_fingerprint": request_fingerprint, "reason": clean_reason}
                ),
                "request_fingerprint": request_fingerprint,
                "source_snapshot_hash": run.source_snapshot_hash,
            },
            reason=clean_reason,
            operated_by=operator,
        )
    )
    db.flush()
    return get_run_detail(db, run_id=run.run_id)


def _candidate_approvals(db: Session, *, run_id: str) -> dict[str, dict[str, Any]]:
    plans = db.scalars(
        select(MaintenanceProjectCutoverPlan)
        .where(MaintenanceProjectCutoverPlan.run_id == run_id)
        .order_by(MaintenanceProjectCutoverPlan.project_id)
    ).all()
    approvals: dict[str, dict[str, Any]] = {}
    for plan in plans:
        baseline = db.scalar(
            select(MaintenanceHistoricalCostBaseline).where(
                MaintenanceHistoricalCostBaseline.plan_id == plan.plan_id
            )
        )
        openings = db.scalars(
            select(MaintenanceInventoryOpeningBalance)
            .where(MaintenanceInventoryOpeningBalance.plan_id == plan.plan_id)
            .order_by(MaintenanceInventoryOpeningBalance.balance_key)
        ).all()
        approvals[plan.project_id] = {
            "historical_baseline": None
            if baseline is None
            else {
                "amount_ex_tax": format(baseline.amount_ex_tax, "f"),
                "amount_inc_tax": format(baseline.amount_inc_tax, "f"),
                "evidence_hash": baseline.evidence_hash,
                "approved": baseline.approval_state == "approved",
            },
            "opening_balances": [
                {
                    "balance_key": row.balance_key,
                    "pn": row.pn,
                    "quantity": format(row.quantity, "f"),
                    "evidence_hash": row.evidence_hash,
                    "approved": row.approval_state == "approved",
                }
                for row in openings
            ],
        }
    return approvals


def _rebuild(
    db: Session,
    *,
    run: MaintenanceMigrationRun,
    loader: WarehouseLoader,
    force_candidate_approval: bool,
) -> dict[str, Any]:
    specs = list(run.preview_json["source_specs"])
    approvals = _candidate_approvals(db, run_id=run.run_id)
    if force_candidate_approval:
        for item in approvals.values():
            if item["historical_baseline"] is not None:
                item["historical_baseline"]["approved"] = True
            for opening in item["opening_balances"]:
                opening["approved"] = True
    try:
        payloads = _source_payloads_from_specs(
            db,
            specs=specs,
            loader=loader,
            approvals=approvals,
        )
        wrapper = _wrapper(specs=specs, payloads=payloads)
    except (controls.MigrationControlError, MaintenanceMigrationSourceError) as exc:
        raise MaintenanceMigrationRunError(str(exc)) from exc
    if wrapper["preview"]["source_snapshot_hash"] != run.source_snapshot_hash:
        raise MaintenanceMigrationRunConflict(
            "来源事实已变化，请生成新的 dry-run 后重新对账"
        )
    return wrapper


def _update_plans(
    db: Session,
    *,
    run_id: str,
    wrapper: Mapping[str, Any],
    status: str,
) -> None:
    previews = _project_preview_map(wrapper)
    plans = db.scalars(
        select(MaintenanceProjectCutoverPlan).where(
            MaintenanceProjectCutoverPlan.run_id == run_id
        )
    ).all()
    for plan in plans:
        preview = previews[plan.project_id]
        cost = preview["cost"]
        plan.input_fingerprint = str(preview["project_input_fingerprint"])
        plan.historical_cost_ex_tax = Decimal(cost["historical_baseline_ex_tax"])
        plan.historical_cost_inc_tax = Decimal(cost["historical_baseline_inc_tax"])
        plan.post_cutover_cost_ex_tax = Decimal(cost["post_cutover_consumption_ex_tax"])
        plan.post_cutover_cost_inc_tax = Decimal(
            cost["post_cutover_consumption_inc_tax"]
        )
        plan.approved_expense_ex_tax = Decimal(cost["approved_expense_ex_tax"])
        plan.approved_expense_inc_tax = Decimal(cost["approved_expense_inc_tax"])
        plan.total_cost_ex_tax = Decimal(cost["total_ex_tax"])
        plan.total_cost_inc_tax = Decimal(cost["total_inc_tax"])
        plan.blocker_count = len(preview["approval_blockers"])
        plan.status = status
        plan.version += 1


def _resolve_removed_discrepancies(
    db: Session,
    *,
    run_id: str,
    wrapper: Mapping[str, Any],
    reason: str,
    operated_by: str,
    now: datetime,
) -> None:
    active_keys = {
        controls.canonical_hash(
            {
                "project_id": preview["project_id"],
                "code": blocker["code"],
                "entity_id": blocker.get("entity_id"),
            }
        )
        for preview in wrapper["preview"]["projects"]
        for blocker in preview["approval_blockers"]
    }
    discrepancies = db.scalars(
        select(MaintenanceMigrationDiscrepancy).where(
            MaintenanceMigrationDiscrepancy.run_id == run_id
        )
    ).all()
    known_keys = {row.stable_key for row in discrepancies}
    if not active_keys <= known_keys:
        raise MaintenanceMigrationRunConflict(
            "对账期间出现新的差异，请生成新的 dry-run"
        )
    for row in discrepancies:
        if row.status == "open" and row.stable_key not in active_keys:
            row.status = "resolved"
            row.resolved_by = operated_by
            row.resolved_at = now
            row.resolution_reason = reason
            row.version += 1


def reconcile_run(
    db: Session,
    *,
    run_id: str,
    expected_version: int,
    operation_key: str,
    reason: str,
    operated_by: str,
    warehouse_loader: WarehouseLoader = unavailable_warehouse_loader,
) -> dict[str, Any]:
    clean_run_id = _clean_text(run_id, "迁移运行编号", max_length=36)
    clean_reason = _clean_text(reason, "对账理由", max_length=1000)
    operator = _clean_text(operated_by, "操作人", max_length=64)
    event_key = _operation_key("reconcile", operation_key)
    command_fingerprint = controls.canonical_hash(
        {
            "run_id": clean_run_id,
            "expected_version": expected_version,
            "reason": clean_reason,
        }
    )
    _advisory_lock(db, event_key)
    replay = _event_replay(
        db,
        run_id=clean_run_id,
        action="reconcile",
        operation_key=event_key,
        command_fingerprint=command_fingerprint,
    )
    if replay is not None:
        return replay
    run = db.scalar(
        select(MaintenanceMigrationRun)
        .where(MaintenanceMigrationRun.run_id == clean_run_id)
        .with_for_update()
    )
    if run is None:
        raise MaintenanceMigrationRunNotFound("迁移 dry-run 不存在")
    if run.version != expected_version:
        raise MaintenanceMigrationRunConflict("迁移运行版本已变化，请刷新后重试")
    if run.status != "previewed":
        raise MaintenanceMigrationRunConflict("只有待对账 dry-run 可以执行对账")

    wrapper = _rebuild(
        db,
        run=run,
        loader=warehouse_loader,
        force_candidate_approval=True,
    )
    now = datetime.now(timezone.utc)
    plans = db.scalars(
        select(MaintenanceProjectCutoverPlan).where(
            MaintenanceProjectCutoverPlan.run_id == run.run_id
        )
    ).all()
    plan_ids = [row.plan_id for row in plans]
    baselines = db.scalars(
        select(MaintenanceHistoricalCostBaseline).where(
            MaintenanceHistoricalCostBaseline.plan_id.in_(plan_ids)
        )
    ).all()
    openings = db.scalars(
        select(MaintenanceInventoryOpeningBalance).where(
            MaintenanceInventoryOpeningBalance.plan_id.in_(plan_ids)
        )
    ).all()
    for row in [*baselines, *openings]:
        row.approval_state = "approved"
        row.approved_by = operator
        row.approved_at = now
        row.approval_reason = clean_reason
        row.version += 1

    _resolve_removed_discrepancies(
        db,
        run_id=run.run_id,
        wrapper=wrapper,
        reason=clean_reason,
        operated_by=operator,
        now=now,
    )
    _update_plans(db, run_id=run.run_id, wrapper=wrapper, status="reconciled")
    run.status = "reconciled"
    run.preview_json = wrapper
    run.reconciled_by = operator
    run.reconciled_at = now
    run.version += 1
    db.add(
        MaintenanceMigrationEvent(
            event_id=_uuid(),
            operation_key=event_key,
            run_id=run.run_id,
            action="reconcile",
            from_status="previewed",
            to_status="reconciled",
            payload_json={
                "command_fingerprint": command_fingerprint,
                "input_fingerprint": wrapper["preview"]["input_fingerprint"],
                "remaining_blocker_count": wrapper["preview"]["approval_blocker_count"],
            },
            reason=clean_reason,
            operated_by=operator,
        )
    )
    db.flush()
    return get_run_detail(db, run_id=run.run_id)


def _signed_manifest(
    *,
    run: MaintenanceMigrationRun,
    wrapper: Mapping[str, Any],
    approved_by: str,
    approved_at: datetime,
    signing_key: bytes,
) -> tuple[dict[str, Any], str]:
    if not isinstance(signing_key, bytes) or len(signing_key) < 16:
        raise MaintenanceMigrationRunError("manifest 签名密钥配置无效")
    preview = wrapper["preview"]
    unsigned = {
        "manifest_version": "maintenance-cutover-manifest-v1",
        "run_id": run.run_id,
        "rule_version": run.rule_version,
        "source_snapshot_hash": run.source_snapshot_hash,
        "input_fingerprint": preview["input_fingerprint"],
        "projects": preview["projects"],
        "approval_chain": {
            "created_by": run.created_by,
            "reconciled_by": run.reconciled_by,
            "approved_by": approved_by,
            "approved_at": approved_at.isoformat(),
        },
        "production_activation_included": False,
    }
    manifest_hash = controls.canonical_hash(unsigned)
    signature = hmac.new(
        signing_key,
        manifest_hash.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return {
        **unsigned,
        "manifest_hash": manifest_hash,
        "signature_algorithm": "HMAC-SHA256",
        "manifest_signature": signature,
    }, manifest_hash


def approve_run(
    db: Session,
    *,
    run_id: str,
    expected_version: int,
    supplied_fingerprint: str,
    operation_key: str,
    reason: str,
    operated_by: str,
    signing_key: bytes,
    warehouse_loader: WarehouseLoader = unavailable_warehouse_loader,
) -> dict[str, Any]:
    clean_run_id = _clean_text(run_id, "迁移运行编号", max_length=36)
    clean_reason = _clean_text(reason, "审批理由", max_length=1000)
    operator = _clean_text(operated_by, "操作人", max_length=64)
    fingerprint = _clean_text(supplied_fingerprint, "预览指纹", max_length=64)
    event_key = _operation_key("approve", operation_key)
    command_fingerprint = controls.canonical_hash(
        {
            "run_id": clean_run_id,
            "expected_version": expected_version,
            "supplied_fingerprint": fingerprint,
            "reason": clean_reason,
        }
    )
    _advisory_lock(db, event_key)
    replay = _event_replay(
        db,
        run_id=clean_run_id,
        action="approve",
        operation_key=event_key,
        command_fingerprint=command_fingerprint,
    )
    if replay is not None:
        return replay
    run = db.scalar(
        select(MaintenanceMigrationRun)
        .where(MaintenanceMigrationRun.run_id == clean_run_id)
        .with_for_update()
    )
    if run is None:
        raise MaintenanceMigrationRunNotFound("迁移 dry-run 不存在")
    if run.version != expected_version:
        raise MaintenanceMigrationRunConflict("迁移运行版本已变化，请刷新后重试")
    if run.status != "reconciled":
        raise MaintenanceMigrationRunConflict("只有已对账 dry-run 可以审批")
    if operator in {run.created_by, run.reconciled_by}:
        raise MaintenanceMigrationRunConflict("最终审批人必须独立于创建人与对账人")

    wrapper = _rebuild(
        db,
        run=run,
        loader=warehouse_loader,
        force_candidate_approval=False,
    )
    current_fingerprint = str(wrapper["preview"]["input_fingerprint"])
    persisted_fingerprint = str(run.preview_json["preview"]["input_fingerprint"])
    if current_fingerprint != persisted_fingerprint:
        raise MaintenanceMigrationRunConflict("对账结果已变化，请重新生成 dry-run")
    try:
        controls.validate_approval(
            wrapper["preview"],
            supplied_fingerprint=fingerprint,
            current_fingerprint=current_fingerprint,
        )
    except controls.MigrationControlError as exc:
        raise MaintenanceMigrationRunConflict(str(exc)) from exc
    open_blockers = db.scalar(
        select(func.count())
        .select_from(MaintenanceMigrationDiscrepancy)
        .where(
            MaintenanceMigrationDiscrepancy.run_id == run.run_id,
            MaintenanceMigrationDiscrepancy.status == "open",
            MaintenanceMigrationDiscrepancy.severity == "blocker",
        )
    )
    if open_blockers:
        raise MaintenanceMigrationRunConflict("仍有未解决阻塞差异，不能审批")

    now = datetime.now(timezone.utc)
    manifest, manifest_hash = _signed_manifest(
        run=run,
        wrapper=wrapper,
        approved_by=operator,
        approved_at=now,
        signing_key=signing_key,
    )
    _update_plans(db, run_id=run.run_id, wrapper=wrapper, status="approved")
    run.status = "approved"
    run.preview_json = wrapper
    run.manifest_json = manifest
    run.manifest_hash = manifest_hash
    run.approved_by = operator
    run.approved_at = now
    run.version += 1
    db.add(
        MaintenanceMigrationEvent(
            event_id=_uuid(),
            operation_key=event_key,
            run_id=run.run_id,
            action="approve",
            from_status="reconciled",
            to_status="approved",
            payload_json={
                "command_fingerprint": command_fingerprint,
                "input_fingerprint": current_fingerprint,
                "manifest_hash": manifest_hash,
            },
            reason=clean_reason,
            operated_by=operator,
        )
    )
    db.flush()
    return get_run_detail(db, run_id=run.run_id)


def get_run_detail(db: Session, *, run_id: str) -> dict[str, Any]:
    run = db.get(MaintenanceMigrationRun, run_id)
    if run is None:
        raise MaintenanceMigrationRunNotFound("迁移 dry-run 不存在")
    plans = db.scalars(
        select(MaintenanceProjectCutoverPlan)
        .where(MaintenanceProjectCutoverPlan.run_id == run.run_id)
        .order_by(MaintenanceProjectCutoverPlan.project_id)
    ).all()
    plan_rows: list[dict[str, Any]] = []
    for plan in plans:
        baseline = db.scalar(
            select(MaintenanceHistoricalCostBaseline).where(
                MaintenanceHistoricalCostBaseline.plan_id == plan.plan_id
            )
        )
        openings = db.scalars(
            select(MaintenanceInventoryOpeningBalance)
            .where(MaintenanceInventoryOpeningBalance.plan_id == plan.plan_id)
            .order_by(MaintenanceInventoryOpeningBalance.balance_key)
        ).all()
        discrepancies = db.scalars(
            select(MaintenanceMigrationDiscrepancy)
            .where(MaintenanceMigrationDiscrepancy.plan_id == plan.plan_id)
            .order_by(
                MaintenanceMigrationDiscrepancy.severity,
                MaintenanceMigrationDiscrepancy.code,
                MaintenanceMigrationDiscrepancy.entity_id,
            )
        ).all()
        plan_rows.append(
            {
                "plan_id": plan.plan_id,
                "project_id": plan.project_id,
                "cutover_date": plan.cutover_date.isoformat(),
                "historical_mode": plan.historical_mode,
                "source_snapshot_hash": plan.source_snapshot_hash,
                "input_fingerprint": plan.input_fingerprint,
                "cost": {
                    "historical_ex_tax": format(plan.historical_cost_ex_tax, "f"),
                    "historical_inc_tax": format(plan.historical_cost_inc_tax, "f"),
                    "post_cutover_ex_tax": format(plan.post_cutover_cost_ex_tax, "f"),
                    "post_cutover_inc_tax": format(plan.post_cutover_cost_inc_tax, "f"),
                    "approved_expense_ex_tax": format(
                        plan.approved_expense_ex_tax, "f"
                    ),
                    "approved_expense_inc_tax": format(
                        plan.approved_expense_inc_tax, "f"
                    ),
                    "total_ex_tax": format(plan.total_cost_ex_tax, "f"),
                    "total_inc_tax": format(plan.total_cost_inc_tax, "f"),
                },
                "blocker_count": plan.blocker_count,
                "status": plan.status,
                "version": plan.version,
                "historical_baseline": None
                if baseline is None
                else {
                    "baseline_id": baseline.baseline_id,
                    "amount_ex_tax": format(baseline.amount_ex_tax, "f"),
                    "amount_inc_tax": format(baseline.amount_inc_tax, "f"),
                    "evidence_hash": baseline.evidence_hash,
                    "approval_state": baseline.approval_state,
                    "approved_by": baseline.approved_by,
                    "approved_at": _jsonable(baseline.approved_at),
                    "version": baseline.version,
                },
                "opening_balances": [
                    {
                        "opening_balance_id": row.opening_balance_id,
                        "balance_key": row.balance_key,
                        "pn": row.pn,
                        "quantity": format(row.quantity, "f"),
                        "evidence_hash": row.evidence_hash,
                        "approval_state": row.approval_state,
                        "approved_by": row.approved_by,
                        "approved_at": _jsonable(row.approved_at),
                        "version": row.version,
                    }
                    for row in openings
                ],
                "discrepancies": [
                    {
                        "discrepancy_id": row.discrepancy_id,
                        "code": row.code,
                        "entity_id": row.entity_id,
                        "severity": row.severity,
                        "status": row.status,
                        "detail": row.detail_json,
                        "resolved_by": row.resolved_by,
                        "resolved_at": _jsonable(row.resolved_at),
                        "version": row.version,
                    }
                    for row in discrepancies
                ],
            }
        )
    events = db.scalars(
        select(MaintenanceMigrationEvent)
        .where(MaintenanceMigrationEvent.run_id == run.run_id)
        .order_by(
            MaintenanceMigrationEvent.operated_at,
            MaintenanceMigrationEvent.event_id,
        )
    ).all()
    return {
        "run_id": run.run_id,
        "status": run.status,
        "rule_version": run.rule_version,
        "request_fingerprint": run.request_fingerprint,
        "source_snapshot_hash": run.source_snapshot_hash,
        "preview": run.preview_json["preview"],
        "manifest": run.manifest_json,
        "manifest_hash": run.manifest_hash,
        "created_by": run.created_by,
        "reconciled_by": run.reconciled_by,
        "reconciled_at": _jsonable(run.reconciled_at),
        "approved_by": run.approved_by,
        "approved_at": _jsonable(run.approved_at),
        "version": run.version,
        "created_at": _jsonable(run.created_at),
        "updated_at": _jsonable(run.updated_at),
        "plans": plan_rows,
        "events": [
            {
                "event_id": row.event_id,
                "action": row.action,
                "from_status": row.from_status,
                "to_status": row.to_status,
                "reason": row.reason,
                "operated_by": row.operated_by,
                "operated_at": _jsonable(row.operated_at),
            }
            for row in events
        ],
        "production_activation_included": False,
    }


def search_runs(
    db: Session,
    *,
    statuses: Sequence[str],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    allowed = {"previewed", "reconciled", "approved"}
    normalized = sorted(set(statuses)) if statuses else []
    if any(status not in allowed for status in normalized):
        raise MaintenanceMigrationRunError("迁移状态筛选无效")
    filters = []
    if normalized:
        filters.append(MaintenanceMigrationRun.status.in_(normalized))
    total = db.scalar(
        select(func.count()).select_from(MaintenanceMigrationRun).where(*filters)
    )
    rows = db.scalars(
        select(MaintenanceMigrationRun)
        .where(*filters)
        .order_by(
            MaintenanceMigrationRun.created_at.desc(),
            MaintenanceMigrationRun.run_id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "run_id": row.run_id,
                "status": row.status,
                "rule_version": row.rule_version,
                "source_snapshot_hash": row.source_snapshot_hash,
                "blocker_count": row.preview_json["preview"]["approval_blocker_count"],
                "created_by": row.created_by,
                "reconciled_by": row.reconciled_by,
                "approved_by": row.approved_by,
                "version": row.version,
                "created_at": _jsonable(row.created_at),
            }
            for row in rows
        ],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }
