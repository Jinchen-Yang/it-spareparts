"""Persistent dry-run, reconciliation, and independent approval workflow."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.business_time import business_today
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
    lock_project_source_snapshots,
)
from app.services.maintenance_migration_warehouse import (
    MaintenanceMigrationWarehouseError,
    validate_cutover_inventory_movements,
)


WarehouseLoaderResult = (
    tuple[Sequence[Mapping[str, Any]], bool]
    | tuple[
        Sequence[Mapping[str, Any]],
        bool,
        Sequence[Mapping[str, Any]],
    ]
)
WarehouseLoader = Callable[[Session, str, date, date | None], WarehouseLoaderResult]
LegacyTruthLoader = Callable[[Session, str, date], Mapping[str, Any]]

_EVIDENCE_SECTIONS = {
    "historical_site_issues",
    "post_cutover_site_issues",
    "expenses",
    "opening_balances",
    "inventory_movements",
    "warehouse_ambiguities",
    "legacy_cost_lines",
    "legacy_expenses",
    "truth_quantity_differences",
}
MAX_MIGRATION_PROJECTS = 50
MAX_OPENINGS_PER_PROJECT = 500
MAX_TOTAL_OPENINGS = 5000
MAX_MIGRATION_FACT_ROWS = 300_000
MAX_MIGRATION_REFERENCE_SAMPLES = 500_000
MAX_MIGRATION_SNAPSHOT_BYTES = 64 * 1024 * 1024


class MaintenanceMigrationRunError(ValueError):
    pass


class MaintenanceMigrationRunConflict(MaintenanceMigrationRunError):
    pass


class MaintenanceMigrationRunNotFound(MaintenanceMigrationRunError):
    pass


def unavailable_warehouse_loader(
    _db: Session,
    _project_id: str,
    _cutover_date: date,
    _warehouse_ready_through: date | None,
) -> tuple[Sequence[Mapping[str, Any]], bool]:
    return (), False


def unavailable_legacy_truth_loader(
    _db: Session, _project_id: str, as_of: date
) -> Mapping[str, Any]:
    evidence = {
        "cost_lines": [],
        "expenses": [],
        "source_coverage": {
            "business_as_of": as_of.isoformat(),
            "legacy_truth_version": "unavailable",
        },
    }
    return {
        **evidence,
        "source_hash": controls.canonical_hash(evidence),
        "source_ready": False,
        "blockers": [
            {
                "code": "legacy_truth_source_not_ready",
                "detail": "旧 WBDD/BXD 双真值读取器未接入",
            }
        ],
    }


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
    acquired = db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )
    if acquired is not True:
        raise MaintenanceMigrationRunConflict("同一迁移操作正在处理中，请稍后重试")


def _normalize_candidate_baseline(
    value: Any, *, cutover_date: date
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MaintenanceMigrationRunError("历史成本基线无效")
    normalized = {
        "amount_ex_tax": str(value.get("amount_ex_tax", "")).strip(),
        "amount_inc_tax": str(value.get("amount_inc_tax", "")).strip(),
        "evidence_hash": str(value.get("evidence_hash", "")).strip().lower(),
        "coverage_from": _parse_date(
            value.get("coverage_from"), "历史基线覆盖起点"
        ).isoformat(),
        "coverage_through": _parse_date(
            value.get("coverage_through"), "历史基线覆盖截止日"
        ).isoformat(),
        "scope": str(value.get("scope") or "").strip(),
        "excludes_expenses": value.get("excludes_expenses") is True,
        "source_artifact_locator": str(
            value.get("source_artifact_locator") or ""
        ).strip(),
        "source_row_count": value.get("source_row_count"),
        "aggregation_fingerprint": str(value.get("aggregation_fingerprint") or "")
        .strip()
        .lower(),
    }
    try:
        validated = controls.validate_historical_baseline_contract(
            normalized, cutover_date=cutover_date
        )
    except controls.MigrationControlError as exc:
        raise MaintenanceMigrationRunError(str(exc)) from exc
    return validated


def _normalize_specs(projects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not projects:
        raise MaintenanceMigrationRunError("迁移项目清单不能为空")
    if len(projects) > MAX_MIGRATION_PROJECTS:
        raise MaintenanceMigrationRunError("迁移项目总数超过安全上限")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_openings = 0
    for item in projects:
        project_id = _clean_text(item.get("project_id"), "项目稳定编号", max_length=36)
        if project_id in seen:
            raise MaintenanceMigrationRunError("迁移项目重复")
        seen.add(project_id)
        mode = str(item.get("historical_mode") or "")
        if mode not in {"approved_cost_baseline", "stable_site_issues"}:
            raise MaintenanceMigrationRunError("历史成本模式无效")
        cutover_date = _parse_date(item.get("cutover_date"), "切换日期")
        baseline = _normalize_candidate_baseline(
            item.get("historical_baseline"), cutover_date=cutover_date
        )
        if mode == "approved_cost_baseline" and baseline is None:
            # The pure calculator records the missing baseline as a blocker.  Keeping
            # it absent here is intentional: dry-run remains inspectable but unsafe.
            pass
        if mode == "stable_site_issues" and baseline is not None:
            raise MaintenanceMigrationRunError("可靠历史领用与成本基线不能同时提交")
        openings: list[dict[str, Any]] = []
        opening_keys: set[str] = set()
        raw_openings = item.get("opening_balances") or []
        if len(raw_openings) > MAX_OPENINGS_PER_PROJECT:
            raise MaintenanceMigrationRunError("单项目库存期初候选超过安全上限")
        total_openings += len(raw_openings)
        if total_openings > MAX_TOTAL_OPENINGS:
            raise MaintenanceMigrationRunError("库存期初候选总数超过安全上限")
        for row in raw_openings:
            if not isinstance(row, Mapping):
                raise MaintenanceMigrationRunError("库存期初项无效")
            balance_key = _clean_text(
                row.get("balance_key"), "库存期初稳定键", max_length=256
            )
            prefix, separator, raw_part_id = balance_key.partition(":")
            try:
                part_id = int(raw_part_id)
            except (TypeError, ValueError) as exc:
                raise MaintenanceMigrationRunError(
                    "库存期初稳定键必须为 project_id:part_id"
                ) from exc
            if (
                separator != ":"
                or prefix != project_id
                or raw_part_id != str(part_id)
                or part_id <= 0
                or balance_key != f"{project_id}:{part_id}"
            ):
                raise MaintenanceMigrationRunError(
                    "库存期初稳定键必须为 project_id:part_id"
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
                "cutover_date": cutover_date.isoformat(),
                "historical_mode": mode,
                "warehouse_ready_through": (
                    _parse_date(
                        item.get("warehouse_ready_through"), "仓库完整水位"
                    ).isoformat()
                    if item.get("warehouse_ready_through") is not None
                    else None
                ),
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
    warehouse_ready_through: date | None,
) -> tuple[Sequence[Mapping[str, Any]], bool, Sequence[Mapping[str, Any]]]:
    result = loader(db, project_id, cutover_date, warehouse_ready_through)
    if len(result) == 2:
        movements, ready = result
        ambiguities: Sequence[Mapping[str, Any]] = ()
    else:
        movements, ready, ambiguities = result
    try:
        validated = validate_cutover_inventory_movements(
            tuple(movements),
            cutover_date=cutover_date,
            project_id=project_id,
        )
    except MaintenanceMigrationWarehouseError as exc:
        raise MaintenanceMigrationRunError(str(exc)) from exc
    return validated, bool(ready) and not ambiguities, tuple(ambiguities)


def _source_payloads_from_specs(
    db: Session,
    *,
    specs: Sequence[Mapping[str, Any]],
    loader: WarehouseLoader,
    legacy_loader: LegacyTruthLoader,
    as_of: date,
    approvals: Mapping[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    total_fact_rows = 0
    total_reference_samples = 0
    total_snapshot_bytes = 0
    # Lock every project in stable order before the first global linkage or
    # warehouse table lock.  Per-project locking after a global SHARE lock can
    # deadlock with a concurrent #201 reassignment on a later project.
    lock_project_source_snapshots(
        db,
        project_ids=[str(spec["project_id"]) for spec in specs],
    )
    for spec in specs:
        project_id = str(spec["project_id"])
        cutover_date = _parse_date(spec["cutover_date"], "切换日期")
        spec_as_of = _parse_date(spec.get("as_of"), "迁移业务截止日")
        if spec_as_of != as_of:
            raise MaintenanceMigrationRunError("同一迁移运行的业务截止日不一致")
        warehouse_ready_through = (
            _parse_date(spec["warehouse_ready_through"], "仓库完整水位")
            if spec.get("warehouse_ready_through") is not None
            else None
        )
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
        movements, ready, warehouse_ambiguities = _load_warehouse(
            loader,
            db,
            project_id=project_id,
            cutover_date=cutover_date,
            warehouse_ready_through=warehouse_ready_through,
        )
        legacy_truth = legacy_loader(db, project_id, as_of)
        if not isinstance(legacy_truth, Mapping):
            raise MaintenanceMigrationRunError("旧口径双真值读取结果无效")
        payload = build_project_source_payload(
            db,
            project_id=project_id,
            cutover_date=cutover_date,
            historical_mode=str(spec["historical_mode"]),
            historical_baseline=baseline,
            opening_balances=openings,
            inventory_movements=movements,
            warehouse_ambiguities=warehouse_ambiguities,
            warehouse_source_ready=ready,
            warehouse_ready_through=warehouse_ready_through,
            as_of=as_of,
            legacy_truth=legacy_truth,
        )
        total_fact_rows += sum(
            len(payload.get(section) or [])
            for section in (
                "historical_site_issues",
                "post_cutover_site_issues",
                "approved_expenses",
                "opening_balances",
                "inventory_movements",
                "warehouse_ambiguities",
                "legacy_cost_lines",
                "legacy_expenses",
            )
        )
        total_reference_samples += sum(
            int(row.get("reference_sample_count") or 0)
            for section in ("historical_site_issues", "post_cutover_site_issues")
            for row in (payload.get(section) or [])
        )
        total_snapshot_bytes += len(
            json.dumps(
                _jsonable(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        if total_fact_rows > MAX_MIGRATION_FACT_ROWS:
            raise MaintenanceMigrationRunError("迁移来源事实总行数超过安全上限")
        if total_reference_samples > MAX_MIGRATION_REFERENCE_SAMPLES:
            raise MaintenanceMigrationRunError("迁移成本参考样本总数超过安全上限")
        if total_snapshot_bytes > MAX_MIGRATION_SNAPSHOT_BYTES:
            raise MaintenanceMigrationRunError("迁移来源快照序列化体积超过安全上限")
        payloads.append(payload)
    return payloads


def _wrapper(
    *,
    rule_version: str,
    specs: Sequence[Mapping[str, Any]],
    payloads: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    preview = controls.build_migration_preview(
        rule_version=rule_version,
        projects=payloads,
    )
    return {
        "preview": _jsonable(preview),
        "source_specs": _jsonable(specs),
    }


def _project_preview_map(wrapper: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["project_id"]): dict(item) for item in wrapper["preview"]["projects"]
    }


def _public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    public = {
        key: _jsonable(value) for key, value in preview.items() if key != "projects"
    }
    projects: list[dict[str, Any]] = []
    for raw_project in preview.get("projects") or []:
        project = dict(_jsonable(raw_project))
        evidence = project.pop("evidence", {}) or {}
        project["source_coverage"] = evidence.get("source_coverage", {})
        project["evidence_summary"] = {
            key: len(value) if isinstance(value, list) else int(value is not None)
            for key, value in evidence.items()
            if key != "source_coverage"
        }
        projects.append(project)
    public["projects"] = projects
    return public


def _public_manifest(manifest: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        key: _jsonable(value) for key, value in manifest.items() if key != "projects"
    } | {
        "project_count": len(manifest.get("projects") or []),
        "evidence_available_via_pagination": True,
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
            business_as_of=_parse_date(spec["as_of"], "迁移业务截止日"),
            historical_mode=str(spec["historical_mode"]),
            source_snapshot_hash=str(preview["source_snapshot_hash"]),
            input_fingerprint=str(preview["project_input_fingerprint"]),
            truth_comparison_hash=str(
                preview["truth_comparison"]["truth_comparison_hash"]
            ),
            historical_cost_ex_tax=Decimal(cost["historical_baseline_ex_tax"]),
            historical_cost_inc_tax=Decimal(cost["historical_baseline_inc_tax"]),
            post_cutover_cost_ex_tax=Decimal(cost["post_cutover_consumption_ex_tax"]),
            post_cutover_cost_inc_tax=Decimal(cost["post_cutover_consumption_inc_tax"]),
            approved_expense_ex_tax=Decimal(cost["approved_expense_ex_tax"]),
            approved_expense_inc_tax=Decimal(cost["approved_expense_inc_tax"]),
            sales_estimate_cost_ex_tax=Decimal(cost["sales_estimate_cost_ex_tax"]),
            sales_estimate_cost_inc_tax=Decimal(cost["sales_estimate_cost_inc_tax"]),
            sales_estimate_lines=int(cost["sales_estimate_lines"]),
            cost_progress_includes_sales_estimate=bool(
                cost["cost_progress_includes_sales_estimate"]
            ),
            cost_progress_label=str(cost["cost_progress_label"]),
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
                    coverage_from=_parse_date(
                        baseline["coverage_from"], "历史基线覆盖起点"
                    ),
                    coverage_through=_parse_date(
                        baseline["coverage_through"], "历史基线覆盖截止日"
                    ),
                    scope=str(baseline["scope"]),
                    excludes_expenses=bool(baseline["excludes_expenses"]),
                    source_artifact_locator=str(baseline["source_artifact_locator"]),
                    source_row_count=int(baseline["source_row_count"]),
                    aggregation_fingerprint=str(baseline["aggregation_fingerprint"]),
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
    operated_by: str,
) -> dict[str, Any] | None:
    event = db.scalar(
        select(MaintenanceMigrationEvent).where(
            MaintenanceMigrationEvent.operation_key == operation_key
        )
    )
    if event is None:
        return None
    if event.operated_by != operated_by:
        raise MaintenanceMigrationRunConflict("操作幂等键属于其他实名操作人")
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
    legacy_loader: LegacyTruthLoader = unavailable_legacy_truth_loader,
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

    run_as_of = business_today()
    frozen_specs = [{**spec, "as_of": run_as_of.isoformat()} for spec in specs]
    try:
        payloads = _source_payloads_from_specs(
            db,
            specs=frozen_specs,
            loader=warehouse_loader,
            legacy_loader=legacy_loader,
            as_of=run_as_of,
        )
        wrapper = _wrapper(
            rule_version=controls.RULE_VERSION,
            specs=frozen_specs,
            payloads=payloads,
        )
    except (controls.MigrationControlError, MaintenanceMigrationSourceError) as exc:
        raise MaintenanceMigrationRunError(str(exc)) from exc

    preview = wrapper["preview"]
    run = MaintenanceMigrationRun(
        run_id=_uuid(),
        idempotency_key=clean_key,
        request_fingerprint=request_fingerprint,
        rule_version=controls.RULE_VERSION,
        source_snapshot_hash=str(preview["source_snapshot_hash"]),
        business_as_of=run_as_of,
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


def _candidate_rows(
    db: Session, *, run_id: str
) -> tuple[
    list[MaintenanceProjectCutoverPlan],
    list[MaintenanceHistoricalCostBaseline],
    list[MaintenanceInventoryOpeningBalance],
]:
    plans = db.scalars(
        select(MaintenanceProjectCutoverPlan)
        .where(MaintenanceProjectCutoverPlan.run_id == run_id)
        .order_by(MaintenanceProjectCutoverPlan.project_id)
    ).all()
    baselines = db.scalars(
        select(MaintenanceHistoricalCostBaseline)
        .join(
            MaintenanceProjectCutoverPlan,
            MaintenanceProjectCutoverPlan.plan_id
            == MaintenanceHistoricalCostBaseline.plan_id,
        )
        .where(MaintenanceProjectCutoverPlan.run_id == run_id)
        .order_by(MaintenanceHistoricalCostBaseline.plan_id)
    ).all()
    openings = db.scalars(
        select(MaintenanceInventoryOpeningBalance)
        .join(
            MaintenanceProjectCutoverPlan,
            MaintenanceProjectCutoverPlan.plan_id
            == MaintenanceInventoryOpeningBalance.plan_id,
        )
        .where(MaintenanceProjectCutoverPlan.run_id == run_id)
        .order_by(
            MaintenanceInventoryOpeningBalance.plan_id,
            MaintenanceInventoryOpeningBalance.balance_key,
        )
    ).all()
    return list(plans), list(baselines), list(openings)


def _candidate_approvals(db: Session, *, run_id: str) -> dict[str, dict[str, Any]]:
    plans, baselines, openings = _candidate_rows(db, run_id=run_id)
    baseline_by_plan = {row.plan_id: row for row in baselines}
    openings_by_plan: dict[str, list[MaintenanceInventoryOpeningBalance]] = {}
    for row in openings:
        openings_by_plan.setdefault(row.plan_id, []).append(row)
    approvals: dict[str, dict[str, Any]] = {}
    for plan in plans:
        baseline = baseline_by_plan.get(plan.plan_id)
        approvals[plan.project_id] = {
            "historical_baseline": None
            if baseline is None
            else {
                "amount_ex_tax": format(baseline.amount_ex_tax, "f"),
                "amount_inc_tax": format(baseline.amount_inc_tax, "f"),
                "evidence_hash": baseline.evidence_hash,
                "coverage_from": baseline.coverage_from.isoformat(),
                "coverage_through": baseline.coverage_through.isoformat(),
                "scope": baseline.scope,
                "excludes_expenses": baseline.excludes_expenses,
                "source_artifact_locator": baseline.source_artifact_locator,
                "source_row_count": baseline.source_row_count,
                "aggregation_fingerprint": baseline.aggregation_fingerprint,
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
                for row in openings_by_plan.get(plan.plan_id, [])
            ],
        }
    return approvals


def _normalize_project_signoffs(
    project_signoffs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not project_signoffs:
        raise MaintenanceMigrationRunError("逐项目签字清单不能为空")
    if len(project_signoffs) > MAX_MIGRATION_PROJECTS:
        raise MaintenanceMigrationRunError("逐项目签字总数超过安全上限")
    normalized: list[dict[str, Any]] = []
    seen_projects: set[str] = set()
    total_openings = 0
    for signoff in project_signoffs:
        project_id = _clean_text(
            signoff.get("project_id"), "签字项目稳定编号", max_length=36
        )
        if project_id in seen_projects:
            raise MaintenanceMigrationRunError("逐项目签字清单存在重复项目")
        seen_projects.add(project_id)
        try:
            expected_plan_version = int(signoff.get("expected_plan_version"))
        except (TypeError, ValueError) as exc:
            raise MaintenanceMigrationRunError("签字项目版本无效") from exc
        if expected_plan_version < 1:
            raise MaintenanceMigrationRunError("签字项目版本无效")
        expected_truth_comparison_hash = _clean_text(
            signoff.get("expected_truth_comparison_hash"),
            "新旧口径对比哈希",
            max_length=64,
        ).lower()
        if len(expected_truth_comparison_hash) != 64 or any(
            char not in "0123456789abcdef" for char in expected_truth_comparison_hash
        ):
            raise MaintenanceMigrationRunError("新旧口径对比哈希无效")
        baseline = signoff.get("historical_baseline")
        normalized_baseline = None
        if baseline is not None:
            if not isinstance(baseline, Mapping):
                raise MaintenanceMigrationRunError("历史基线签字项无效")
            try:
                baseline_version = int(baseline.get("expected_version"))
            except (TypeError, ValueError) as exc:
                raise MaintenanceMigrationRunError("历史基线签字版本无效") from exc
            if baseline_version < 1:
                raise MaintenanceMigrationRunError("历史基线签字版本无效")
            normalized_baseline = {
                "baseline_id": _clean_text(
                    baseline.get("baseline_id"), "历史基线候选编号", max_length=36
                ),
                "expected_version": baseline_version,
            }
        normalized_openings: list[dict[str, Any]] = []
        seen_openings: set[str] = set()
        raw_openings = signoff.get("opening_balances") or []
        if len(raw_openings) > MAX_OPENINGS_PER_PROJECT:
            raise MaintenanceMigrationRunError("单项目库存期初签字超过安全上限")
        total_openings += len(raw_openings)
        if total_openings > MAX_TOTAL_OPENINGS:
            raise MaintenanceMigrationRunError("库存期初签字总数超过安全上限")
        for opening in raw_openings:
            if not isinstance(opening, Mapping):
                raise MaintenanceMigrationRunError("库存期初签字项无效")
            opening_id = _clean_text(
                opening.get("opening_balance_id"),
                "库存期初候选编号",
                max_length=36,
            )
            if opening_id in seen_openings:
                raise MaintenanceMigrationRunError("库存期初签字项重复")
            seen_openings.add(opening_id)
            try:
                opening_version = int(opening.get("expected_version"))
            except (TypeError, ValueError) as exc:
                raise MaintenanceMigrationRunError("库存期初签字版本无效") from exc
            if opening_version < 1:
                raise MaintenanceMigrationRunError("库存期初签字版本无效")
            normalized_openings.append(
                {
                    "opening_balance_id": opening_id,
                    "expected_version": opening_version,
                }
            )
        normalized.append(
            {
                "project_id": project_id,
                "expected_plan_version": expected_plan_version,
                "expected_truth_comparison_hash": expected_truth_comparison_hash,
                "reason": _clean_text(
                    signoff.get("reason"), "逐项目签字理由", max_length=1000
                ),
                "historical_baseline": normalized_baseline,
                "opening_balances": sorted(
                    normalized_openings, key=lambda row: row["opening_balance_id"]
                ),
            }
        )
    return sorted(normalized, key=lambda row: row["project_id"])


def _validate_project_signoffs(
    *,
    plans: Sequence[MaintenanceProjectCutoverPlan],
    baselines: Sequence[MaintenanceHistoricalCostBaseline],
    openings: Sequence[MaintenanceInventoryOpeningBalance],
    signoffs: Sequence[Mapping[str, Any]],
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    plans_by_project = {row.project_id: row for row in plans}
    signoffs_by_project = {str(row["project_id"]): dict(row) for row in signoffs}
    if set(plans_by_project) != set(signoffs_by_project):
        raise MaintenanceMigrationRunConflict("逐项目签字清单不完整")
    baselines_by_plan = {row.plan_id: row for row in baselines}
    openings_by_plan: dict[str, list[MaintenanceInventoryOpeningBalance]] = {}
    for row in openings:
        openings_by_plan.setdefault(row.plan_id, []).append(row)

    selected_ids: set[str] = set()
    for project_id, plan in plans_by_project.items():
        signoff = signoffs_by_project[project_id]
        if plan.version != signoff["expected_plan_version"]:
            raise MaintenanceMigrationRunConflict(
                f"项目 {project_id} 版本已变化，请刷新后重新签字"
            )
        if plan.truth_comparison_hash != signoff["expected_truth_comparison_hash"]:
            raise MaintenanceMigrationRunConflict(
                f"项目 {project_id} 的新旧口径差异已变化，请重新核对签字"
            )
        baseline = baselines_by_plan.get(plan.plan_id)
        baseline_signoff = signoff.get("historical_baseline")
        if (baseline is None) != (baseline_signoff is None):
            raise MaintenanceMigrationRunConflict(
                f"项目 {project_id} 的历史基线候选清单不完整"
            )
        if baseline is not None and baseline_signoff is not None:
            if (
                baseline.baseline_id != baseline_signoff["baseline_id"]
                or baseline.version != baseline_signoff["expected_version"]
                or baseline.approval_state != "pending"
            ):
                raise MaintenanceMigrationRunConflict(
                    f"项目 {project_id} 的历史基线候选已变化"
                )
            selected_ids.add(baseline.baseline_id)

        expected_openings = {
            row.opening_balance_id: row
            for row in openings_by_plan.get(plan.plan_id, [])
        }
        selected_openings = {
            str(row["opening_balance_id"]): row
            for row in signoff.get("opening_balances") or []
        }
        if set(expected_openings) != set(selected_openings):
            raise MaintenanceMigrationRunConflict(
                f"项目 {project_id} 的库存期初候选清单不完整"
            )
        for opening_id, opening in expected_openings.items():
            if (
                opening.version != selected_openings[opening_id]["expected_version"]
                or opening.approval_state != "pending"
            ):
                raise MaintenanceMigrationRunConflict(
                    f"项目 {project_id} 的库存期初候选已变化"
                )
            selected_ids.add(opening_id)
    return selected_ids, signoffs_by_project


def _rebuild(
    db: Session,
    *,
    run: MaintenanceMigrationRun,
    loader: WarehouseLoader,
    legacy_loader: LegacyTruthLoader,
    selected_candidate_ids: set[str] | None,
) -> dict[str, Any]:
    if run.rule_version != controls.RULE_VERSION:
        raise MaintenanceMigrationRunConflict(
            "迁移规则版本已变化，请生成新的 dry-run 后重新对账"
        )
    specs = list(run.preview_json["source_specs"])
    approvals = _candidate_approvals(db, run_id=run.run_id)
    if selected_candidate_ids is not None:
        plans, baselines, openings = _candidate_rows(db, run_id=run.run_id)
        project_by_plan = {row.plan_id: row.project_id for row in plans}
        for baseline in baselines:
            approvals[project_by_plan[baseline.plan_id]]["historical_baseline"][
                "approved"
            ] = baseline.baseline_id in selected_candidate_ids
        for opening in openings:
            project_openings = approvals[project_by_plan[opening.plan_id]][
                "opening_balances"
            ]
            for candidate in project_openings:
                if candidate["balance_key"] == opening.balance_key:
                    candidate["approved"] = (
                        opening.opening_balance_id in selected_candidate_ids
                    )
                    break
    try:
        payloads = _source_payloads_from_specs(
            db,
            specs=specs,
            loader=loader,
            legacy_loader=legacy_loader,
            as_of=run.business_as_of,
            approvals=approvals,
        )
        wrapper = _wrapper(
            rule_version=run.rule_version,
            specs=specs,
            payloads=payloads,
        )
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
    reconciliations: Mapping[str, Mapping[str, Any]] | None = None,
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
        plan.truth_comparison_hash = str(
            preview["truth_comparison"]["truth_comparison_hash"]
        )
        plan.historical_cost_ex_tax = Decimal(cost["historical_baseline_ex_tax"])
        plan.historical_cost_inc_tax = Decimal(cost["historical_baseline_inc_tax"])
        plan.post_cutover_cost_ex_tax = Decimal(cost["post_cutover_consumption_ex_tax"])
        plan.post_cutover_cost_inc_tax = Decimal(
            cost["post_cutover_consumption_inc_tax"]
        )
        plan.approved_expense_ex_tax = Decimal(cost["approved_expense_ex_tax"])
        plan.approved_expense_inc_tax = Decimal(cost["approved_expense_inc_tax"])
        plan.sales_estimate_cost_ex_tax = Decimal(cost["sales_estimate_cost_ex_tax"])
        plan.sales_estimate_cost_inc_tax = Decimal(cost["sales_estimate_cost_inc_tax"])
        plan.sales_estimate_lines = int(cost["sales_estimate_lines"])
        plan.cost_progress_includes_sales_estimate = bool(
            cost["cost_progress_includes_sales_estimate"]
        )
        plan.cost_progress_label = str(cost["cost_progress_label"])
        plan.total_cost_ex_tax = Decimal(cost["total_ex_tax"])
        plan.total_cost_inc_tax = Decimal(cost["total_inc_tax"])
        plan.blocker_count = len(preview["approval_blockers"])
        plan.status = status
        if reconciliations is not None:
            reconciliation = reconciliations[plan.project_id]
            plan.reconciled_by = reconciliation["operated_by"]
            plan.reconciled_at = reconciliation["operated_at"]
            plan.reconciliation_reason = reconciliation["reason"]
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
    project_signoffs: Sequence[Mapping[str, Any]],
    warehouse_loader: WarehouseLoader = unavailable_warehouse_loader,
    legacy_loader: LegacyTruthLoader = unavailable_legacy_truth_loader,
) -> dict[str, Any]:
    clean_run_id = _clean_text(run_id, "迁移运行编号", max_length=36)
    clean_reason = _clean_text(reason, "对账理由", max_length=1000)
    operator = _clean_text(operated_by, "操作人", max_length=64)
    signoffs = _normalize_project_signoffs(project_signoffs)
    event_key = _operation_key("reconcile", operation_key)
    command_fingerprint = controls.canonical_hash(
        {
            "run_id": clean_run_id,
            "expected_version": expected_version,
            "reason": clean_reason,
            "project_signoffs": signoffs,
            "operated_by": operator,
        }
    )
    _advisory_lock(db, event_key)
    run = db.scalar(
        select(MaintenanceMigrationRun)
        .where(MaintenanceMigrationRun.run_id == clean_run_id)
        .with_for_update()
    )
    if run is None:
        raise MaintenanceMigrationRunNotFound("迁移 dry-run 不存在")
    if operator == run.created_by:
        raise MaintenanceMigrationRunConflict("对账人必须独立于 dry-run 创建人")
    replay = _event_replay(
        db,
        run_id=clean_run_id,
        action="reconcile",
        operation_key=event_key,
        command_fingerprint=command_fingerprint,
        operated_by=operator,
    )
    if replay is not None:
        return replay
    if run.version != expected_version:
        raise MaintenanceMigrationRunConflict("迁移运行版本已变化，请刷新后重试")
    if run.status != "previewed":
        raise MaintenanceMigrationRunConflict("只有待对账 dry-run 可以执行对账")

    plans, baselines, openings = _candidate_rows(db, run_id=run.run_id)
    selected_candidate_ids, signoffs_by_project = _validate_project_signoffs(
        plans=plans,
        baselines=baselines,
        openings=openings,
        signoffs=signoffs,
    )

    wrapper = _rebuild(
        db,
        run=run,
        loader=warehouse_loader,
        legacy_loader=legacy_loader,
        selected_candidate_ids=selected_candidate_ids,
    )
    rebuilt_previews = _project_preview_map(wrapper)
    for project_id, signoff in signoffs_by_project.items():
        rebuilt_truth_hash = str(
            rebuilt_previews[project_id]["truth_comparison"]["truth_comparison_hash"]
        )
        if rebuilt_truth_hash != signoff["expected_truth_comparison_hash"]:
            raise MaintenanceMigrationRunConflict(
                f"项目 {project_id} 的候选应用后新旧口径差异已变化，请重新核对签字"
            )
    now = datetime.now(timezone.utc)
    for row in [*baselines, *openings]:
        if (
            getattr(row, "baseline_id", None) not in selected_candidate_ids
            and getattr(row, "opening_balance_id", None) not in selected_candidate_ids
        ):
            continue
        project_id = row.project_id
        row.approval_state = "approved"
        row.approved_by = operator
        row.approved_at = now
        row.approval_reason = signoffs_by_project[project_id]["reason"]
        row.version += 1

    _resolve_removed_discrepancies(
        db,
        run_id=run.run_id,
        wrapper=wrapper,
        reason=clean_reason,
        operated_by=operator,
        now=now,
    )
    _update_plans(
        db,
        run_id=run.run_id,
        wrapper=wrapper,
        status="reconciled",
        reconciliations={
            project_id: {
                "operated_by": operator,
                "operated_at": now,
                "reason": signoff["reason"],
            }
            for project_id, signoff in signoffs_by_project.items()
        },
    )
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
                "truth_comparison_hashes": {
                    project_id: rebuilt_previews[project_id]["truth_comparison"][
                        "truth_comparison_hash"
                    ]
                    for project_id in sorted(rebuilt_previews)
                },
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
    signing_key_id: str,
) -> tuple[dict[str, Any], str]:
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise MaintenanceMigrationRunError("manifest 签名密钥配置无效")
    key_id = _clean_text(signing_key_id, "manifest 签名 key_id", max_length=64)
    preview = wrapper["preview"]
    if (
        run.rule_version != controls.RULE_VERSION
        or preview.get("rule_version") != run.rule_version
    ):
        raise MaintenanceMigrationRunConflict("run、preview 与 manifest 规则版本不一致")
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
        "signature_algorithm": "HMAC-SHA256",
        "signing_key_id": key_id,
        "production_activation_included": False,
        "activation_requires_live_revalidation": True,
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
        "manifest_signature": signature,
    }, manifest_hash


def verify_signed_manifest(
    manifest: Mapping[str, Any],
    *,
    verification_keys: Mapping[str, bytes],
    expected_run_id: str,
    expected_rule_version: str,
    expected_source_snapshot_hash: str,
    expected_input_fingerprint: str,
) -> bool:
    if (
        manifest.get("manifest_version") != "maintenance-cutover-manifest-v1"
        or manifest.get("run_id") != expected_run_id
        or manifest.get("rule_version") != expected_rule_version
        or manifest.get("source_snapshot_hash") != expected_source_snapshot_hash
        or manifest.get("input_fingerprint") != expected_input_fingerprint
        or manifest.get("production_activation_included") is not False
        or manifest.get("activation_requires_live_revalidation") is not True
    ):
        return False
    if manifest.get("signature_algorithm") != "HMAC-SHA256":
        return False
    key_id = str(manifest.get("signing_key_id") or "")
    key = verification_keys.get(key_id)
    if not isinstance(key, bytes) or len(key) < 32:
        return False
    supplied_hash = str(manifest.get("manifest_hash") or "")
    supplied_signature = str(manifest.get("manifest_signature") or "")
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_hash", "manifest_signature"}
    }
    calculated_hash = controls.canonical_hash(unsigned)
    if not hmac.compare_digest(supplied_hash, calculated_hash):
        return False
    calculated_signature = hmac.new(
        key, calculated_hash.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, calculated_signature)


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
    signing_key_id: str,
    warehouse_loader: WarehouseLoader = unavailable_warehouse_loader,
    legacy_loader: LegacyTruthLoader = unavailable_legacy_truth_loader,
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
            "operated_by": operator,
        }
    )
    _advisory_lock(db, event_key)
    replay = _event_replay(
        db,
        run_id=clean_run_id,
        action="approve",
        operation_key=event_key,
        command_fingerprint=command_fingerprint,
        operated_by=operator,
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
        legacy_loader=legacy_loader,
        selected_candidate_ids=None,
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
        signing_key_id=signing_key_id,
    )
    _update_plans(db, run_id=run.run_id, wrapper=wrapper, status="approved")
    run.status = "approved"
    run.preview_json = wrapper
    run.manifest_json = manifest
    run.manifest_hash = manifest_hash
    run.manifest_key_id = signing_key_id
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
    plans, baselines, openings = _candidate_rows(db, run_id=run.run_id)
    preview_by_project = _project_preview_map(run.preview_json)
    baseline_by_plan = {row.plan_id: row for row in baselines}
    openings_by_plan: dict[str, list[MaintenanceInventoryOpeningBalance]] = {}
    for row in openings:
        openings_by_plan.setdefault(row.plan_id, []).append(row)
    discrepancies = db.scalars(
        select(MaintenanceMigrationDiscrepancy)
        .where(MaintenanceMigrationDiscrepancy.run_id == run.run_id)
        .order_by(
            MaintenanceMigrationDiscrepancy.plan_id,
            MaintenanceMigrationDiscrepancy.severity,
            MaintenanceMigrationDiscrepancy.code,
            MaintenanceMigrationDiscrepancy.entity_id,
        )
    ).all()
    discrepancies_by_plan: dict[str, list[MaintenanceMigrationDiscrepancy]] = {}
    for row in discrepancies:
        discrepancies_by_plan.setdefault(row.plan_id, []).append(row)
    plan_rows: list[dict[str, Any]] = []
    for plan in plans:
        baseline = baseline_by_plan.get(plan.plan_id)
        plan_rows.append(
            {
                "plan_id": plan.plan_id,
                "project_id": plan.project_id,
                "cutover_date": plan.cutover_date.isoformat(),
                "as_of": plan.business_as_of.isoformat(),
                "historical_mode": plan.historical_mode,
                "source_snapshot_hash": plan.source_snapshot_hash,
                "input_fingerprint": plan.input_fingerprint,
                "truth_comparison": _jsonable(
                    preview_by_project[plan.project_id]["truth_comparison"]
                ),
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
                    "sales_estimate_cost_ex_tax": format(
                        plan.sales_estimate_cost_ex_tax, "f"
                    ),
                    "sales_estimate_cost_inc_tax": format(
                        plan.sales_estimate_cost_inc_tax, "f"
                    ),
                    "sales_estimate_lines": plan.sales_estimate_lines,
                    "cost_progress_includes_sales_estimate": (
                        plan.cost_progress_includes_sales_estimate
                    ),
                    "cost_progress_label": plan.cost_progress_label,
                    "total_ex_tax": format(plan.total_cost_ex_tax, "f"),
                    "total_inc_tax": format(plan.total_cost_inc_tax, "f"),
                },
                "blocker_count": plan.blocker_count,
                "status": plan.status,
                "reconciled_by": plan.reconciled_by,
                "reconciled_at": _jsonable(plan.reconciled_at),
                "reconciliation_reason": plan.reconciliation_reason,
                "version": plan.version,
                "historical_baseline": None
                if baseline is None
                else {
                    "baseline_id": baseline.baseline_id,
                    "amount_ex_tax": format(baseline.amount_ex_tax, "f"),
                    "amount_inc_tax": format(baseline.amount_inc_tax, "f"),
                    "evidence_hash": baseline.evidence_hash,
                    "coverage_from": baseline.coverage_from.isoformat(),
                    "coverage_through": baseline.coverage_through.isoformat(),
                    "scope": baseline.scope,
                    "excludes_expenses": baseline.excludes_expenses,
                    "source_artifact_locator": baseline.source_artifact_locator,
                    "source_row_count": baseline.source_row_count,
                    "aggregation_fingerprint": baseline.aggregation_fingerprint,
                    "approval_state": baseline.approval_state,
                    "approved_by": baseline.approved_by,
                    "approved_at": _jsonable(baseline.approved_at),
                    "approval_reason": baseline.approval_reason,
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
                        "approval_reason": row.approval_reason,
                        "version": row.version,
                    }
                    for row in openings_by_plan.get(plan.plan_id, [])
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
                    for row in discrepancies_by_plan.get(plan.plan_id, [])
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
        "as_of": run.business_as_of.isoformat(),
        "preview": _public_preview(run.preview_json["preview"]),
        "manifest": _public_manifest(run.manifest_json),
        "manifest_hash": run.manifest_hash,
        "manifest_key_id": run.manifest_key_id,
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


def get_project_evidence(
    db: Session,
    *,
    run_id: str,
    project_id: str,
    section: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    if section not in _EVIDENCE_SECTIONS:
        raise MaintenanceMigrationRunError("迁移证据分区无效")
    if page < 1 or page_size < 1 or page_size > 100:
        raise MaintenanceMigrationRunError("迁移证据分页参数无效")
    run = db.get(MaintenanceMigrationRun, run_id)
    if run is None:
        raise MaintenanceMigrationRunNotFound("迁移 dry-run 不存在")
    project = next(
        (
            item
            for item in run.preview_json["preview"]["projects"]
            if str(item.get("project_id")) == project_id
        ),
        None,
    )
    if project is None:
        raise MaintenanceMigrationRunNotFound("迁移项目证据不存在")
    rows = list((project.get("evidence") or {}).get(section) or [])
    offset = (page - 1) * page_size
    return {
        "run_id": run.run_id,
        "project_id": project_id,
        "section": section,
        "source_snapshot_hash": project["source_snapshot_hash"],
        "items": _jsonable(rows[offset : offset + page_size]),
        "total": len(rows),
        "page": page,
        "page_size": page_size,
    }


def get_signed_manifest(
    db: Session,
    *,
    run_id: str,
    verification_keys: Mapping[str, bytes],
    warehouse_loader: WarehouseLoader = unavailable_warehouse_loader,
    legacy_loader: LegacyTruthLoader = unavailable_legacy_truth_loader,
) -> dict[str, Any]:
    run = db.get(MaintenanceMigrationRun, run_id)
    if run is None:
        raise MaintenanceMigrationRunNotFound("迁移 dry-run 不存在")
    if run.status != "approved" or run.manifest_json is None:
        raise MaintenanceMigrationRunConflict("迁移 manifest 尚未完成独立审批")
    wrapper = _rebuild(
        db,
        run=run,
        loader=warehouse_loader,
        legacy_loader=legacy_loader,
        selected_candidate_ids=None,
    )
    current_preview = wrapper["preview"]
    if (
        current_preview["input_fingerprint"]
        != run.manifest_json.get("input_fingerprint")
        or current_preview["source_snapshot_hash"]
        != run.manifest_json.get("source_snapshot_hash")
        or run.rule_version != controls.RULE_VERSION
    ):
        raise MaintenanceMigrationRunConflict(
            "迁移来源或规则已变化，旧 manifest 已失效"
        )
    manifest = _jsonable(run.manifest_json)
    expected_approval_chain = {
        "created_by": run.created_by,
        "reconciled_by": run.reconciled_by,
        "approved_by": run.approved_by,
        "approved_at": run.approved_at.isoformat() if run.approved_at else None,
    }
    if (
        manifest.get("run_id") != run.run_id
        or manifest.get("manifest_hash") != run.manifest_hash
        or manifest.get("signing_key_id") != run.manifest_key_id
        or manifest.get("approval_chain") != expected_approval_chain
    ):
        raise MaintenanceMigrationRunConflict("迁移 manifest 与持久化审批事实不一致")
    if not verify_signed_manifest(
        manifest,
        verification_keys=verification_keys,
        expected_run_id=run.run_id,
        expected_rule_version=controls.RULE_VERSION,
        expected_source_snapshot_hash=str(current_preview["source_snapshot_hash"]),
        expected_input_fingerprint=str(current_preview["input_fingerprint"]),
    ):
        raise MaintenanceMigrationRunConflict("迁移 manifest 签名或绑定事实无效")
    return manifest


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
                "as_of": row.business_as_of.isoformat(),
                "manifest_key_id": row.manifest_key_id,
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
