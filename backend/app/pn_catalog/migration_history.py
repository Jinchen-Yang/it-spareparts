"""Typed historical activity projections; no legacy worker becomes a runnable job."""

import base64
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.config import get_settings
from . import api
from .errors import CatalogError
from .migration_media import _date, _source, owner_binding
from .migration_models import LegacySourceRecord
from .migration_state_models import MigrationRecordApplication

HISTORY_KINDS = frozenset(
    {
        "inbox_job",
        "agent_job",
        "agent_run",
        "bundle",
        "audit",
        "identity_event",
        "manual_research",
        "image_recovery",
        "catalog_link",
        "catalog_conflict",
    }
)
TERMINAL = {
    "inbox_job": {"done", "failed", "cancelled"},
    "agent_job": {"done", "failed", "cancelled"},
    "agent_run": {"done", "failed"},
    "identity_event": {"applied", "cancelled"},
    "manual_research": {"done", "failed", "cancelled"},
    "image_recovery": {"recovered", "failed", "skipped"},
    "catalog_conflict": {"resolved"},
}
STATES = {
    "inbox_job": TERMINAL["inbox_job"] | {"queued", "running", "waiting_cloud"},
    "agent_job": TERMINAL["agent_job"] | {"queued", "running", "paused"},
    "agent_run": TERMINAL["agent_run"] | {"running", "waiting_cloud", "expired"},
    "identity_event": TERMINAL["identity_event"] | {"queued", "running", "failed"},
    "manual_research": TERMINAL["manual_research"] | {"queued", "running"},
    "image_recovery": TERMINAL["image_recovery"]
    | {"pending", "running", "waiting_cloud"},
    "catalog_conflict": {"open", "resolved"},
    "catalog_link": {"active", "merged"},
}


class HistoryProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_record_id: int = Field(gt=0)
    namespace: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=32)
    legacy_status: str = Field(min_length=1, max_length=32)
    part_id: int | None = Field(default=None, gt=0)
    inbox_id: str | None = Field(default=None, max_length=36)
    owner_principal_id: str | None = Field(default=None, max_length=256)
    historical_actor_id: str | None = Field(default=None, max_length=36)
    event_at: str | None = Field(default=None, max_length=64)
    attempts: int | None = Field(default=None, ge=0)
    parent_refs: dict[str, str] = Field(default_factory=dict, max_length=5)
    action: str | None = Field(default=None, max_length=64)
    entity: str | None = Field(default=None, max_length=32)
    # Provider bills, model traces, raw outputs and object keys are deliberately absent.


def _owner_subject(db, row, seen=None):
    seen = set() if seen is None else seen
    if row.id in seen:
        raise CatalogError(
            "migration.invalid_history", "历史任务父子引用存在循环", status_code=422
        )
    seen.add(row.id)
    source = row.payload
    direct = {
        "audit": "actor_id",
        "identity_event": "created_by",
        "manual_research": "actor_id",
        "bundle": "user_id",
    }.get(row.kind)
    if direct:
        return source.get(direct)
    parent = None
    if row.kind == "inbox_job":
        inbox = _source(db, row, "inbox", source.get("inbox_id"))
        if inbox is None:
            raise CatalogError(
                "migration.dependency_missing", "历史收件箱任务缺少来源图片"
            )
        return inbox.payload.get("uploaded_by")
    if row.kind == "agent_job":
        if source.get("manual_request_id"):
            parent = _source(db, row, "manual_research", source["manual_request_id"])
            if parent is None:
                raise CatalogError(
                    "migration.dependency_missing", "历史研究任务缺少原人工请求"
                )
        else:
            bundle = db.scalar(
                select(LegacySourceRecord).where(
                    LegacySourceRecord.namespace == row.namespace,
                    LegacySourceRecord.kind == "bundle",
                    LegacySourceRecord.payload["job_id"].as_string() == row.source_id,
                )
            )
            if bundle is not None:
                return bundle.payload.get("user_id")
    elif row.kind == "agent_run":
        parent = _source(db, row, "agent_job", source.get("job_id"))
        if parent is None:
            raise CatalogError("migration.dependency_missing", "历史运行记录缺少原任务")
    elif row.kind == "image_recovery" and source.get("run_id"):
        parent = _source(db, row, "agent_run", source["run_id"])
        if parent is None:
            raise CatalogError(
                "migration.dependency_missing", "历史图片恢复缺少原运行记录"
            )
    return _owner_subject(db, parent, seen) if parent else None


def materialize_history(db, row, part_id):
    if row.kind not in HISTORY_KINDS:
        raise CatalogError("migration.converter_pending", "该历史类型尚未支持")
    source = row.payload
    identifier_field = {
        "manual_research": "request_id",
        "image_recovery": "image_id",
    }.get(row.kind, "id")
    if (
        row.kind != "catalog_link"
        and str(source.get(identifier_field)) != row.source_id
    ):
        raise CatalogError(
            "migration.invalid_history", "历史来源编号不一致", status_code=422
        )
    if row.kind == "audit":
        status, terminal = "recorded", True
    elif row.kind == "bundle":
        parent = _source(db, row, "agent_job", source.get("job_id"))
        if parent is None:
            raise CatalogError("migration.dependency_missing", "历史压缩包缺少原任务")
        status = parent.payload.get("status")
        if status not in STATES["agent_job"]:
            raise CatalogError(
                "migration.invalid_history", "压缩包原任务状态无效", status_code=422
            )
        terminal = status in TERMINAL["agent_job"] and (
            status != "done" or bool(source.get("archives"))
        )
    else:
        status = source.get("status")
        if status not in STATES[row.kind]:
            raise CatalogError(
                "migration.invalid_history", "历史状态不属于已知协议", status_code=422
            )
        terminal = status in TERMINAL.get(row.kind, {"active", "merged"})
    # Terminal source metadata with a live lease is still uncertain; never transfer it.
    if source.get("lease_token") or source.get("operation_token"):
        terminal = False
    if row.kind == "manual_research" and source.get("reserved_tokens", 0):
        terminal = False
    subject = _owner_subject(db, row)
    canonical, binding = None, None
    if subject:
        try:
            UUID(str(subject))
        except ValueError as exc:
            raise CatalogError(
                "migration.invalid_history", "历史账号编号无效", status_code=422
            ) from exc
        try:
            canonical, binding = owner_binding(db, row.namespace, subject)
        except CatalogError as exc:
            if exc.code != "migration.owner_binding_required":
                raise
            # Unknown historic identity stays unknown. It gains no access or execution right.
    parents = {}
    for key in ("job_id", "run_id", "manual_request_id", "retry_of_id", "image_id"):
        if source.get(key):
            try:
                parents[key] = str(UUID(source[key]))
            except ValueError as exc:
                raise CatalogError(
                    "migration.invalid_history", "历史父子引用无效", status_code=422
                ) from exc
    event_at = next(
        (
            source[key]
            for key in ("created_at", "first_seen_at", "last_seen_at")
            if source.get(key)
        ),
        None,
    )
    event = _date(event_at)
    try:
        inbox = str(UUID(source["inbox_id"])) if source.get("inbox_id") else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise CatalogError(
            "migration.invalid_history", "历史收件箱编号无效", status_code=422
        ) from exc
    try:
        projection = HistoryProjection(
            source_record_id=row.id,
            namespace=row.namespace,
            source_id=row.source_id,
            kind=row.kind,
            legacy_status=status,
            part_id=part_id,
            inbox_id=inbox,
            owner_principal_id=canonical,
            historical_actor_id=str(subject) if subject else None,
            event_at=event.isoformat() if event else None,
            attempts=source.get("attempts", source.get("attempt")),
            parent_refs=parents,
            action=source.get("action") if row.kind == "audit" else None,
            entity=source.get("entity") if row.kind == "audit" else None,
        )
    except ValidationError as exc:
        raise CatalogError(
            "migration.invalid_history", "历史投影字段无效", status_code=422
        ) from exc
    return (
        "historical_terminal" if terminal else "recovery_required",
        None if terminal else "migration.fenced_work_reconcile",
        {
            "history": projection.model_dump(),
            "owner_binding": binding,
            "lease_transferred": False,
        },
    )


def _authorize(db, item, context, authorize):
    if item.namespace != get_settings().pn_shop_source_namespace:
        raise CatalogError("auth.forbidden", "当前来源不可见", status_code=403)
    if item.owner_principal_id != context.actor.principal_id:
        raise CatalogError("auth.forbidden", "历史任务仅限原账号查看", status_code=403)
    grant = api._authorize(db, context, authorize, "read_legacy_history", item.part_id)
    if item.inbox_id:
        from .media_inbox import get_inbox

        get_inbox(db, UUID(item.inbox_id), context=context, authorize=authorize)
    elif item.part_id:
        api._authorize(db, context, authorize, "read", item.part_id)
    return grant


def list_history(
    db, *, context, authorize, part_id=None, inbox_id=None, cursor=None, limit=50
):
    if not 1 <= limit <= 100 or (cursor and len(cursor) > 4096):
        raise CatalogError("request.invalid", "历史分页大小无效", status_code=422)
    binding = [
        context.actor.principal_id,
        context.actor.client_id,
        part_id,
        str(inbox_id) if inbox_id else None,
    ]
    after = 0
    if cursor:
        try:
            raw = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )
            if (
                raw["binding"] != binding
                or type(raw["after"]) is not int
                or raw["after"] < 0
            ):
                raise ValueError()
            after = raw["after"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CatalogError(
                "request.invalid_cursor", "历史游标与当前范围不匹配", status_code=422
            ) from exc
    api._authorize(db, context, authorize, "read_legacy_history", part_id)
    history = MigrationRecordApplication.target_ref["history"]
    filters = [
        MigrationRecordApplication.source_record_id > after,
        MigrationRecordApplication.namespace == get_settings().pn_shop_source_namespace,
        history["owner_principal_id"].as_string() == context.actor.principal_id,
    ]
    if part_id is not None:
        filters.append(history["part_id"].as_integer() == part_id)
    if inbox_id is not None:
        filters.append(history["inbox_id"].as_string() == str(inbox_id))
    rows = db.execute(
        select(
            MigrationRecordApplication.source_record_id,
            history,
            MigrationRecordApplication.state,
            MigrationRecordApplication.revision,
        )
        .where(*filters)
        .order_by(MigrationRecordApplication.source_record_id)
        .limit(201)
    ).all()
    items, scanned, has_more = [], after, len(rows) > 200
    for row_id, value, state, revision in rows[:200]:
        if len(items) == limit:
            has_more = True
            break
        scanned = row_id
        item = HistoryProjection.model_validate(value)
        try:
            _authorize(db, item, context, authorize)
        except CatalogError as exc:
            if exc.status_code in {403, 404}:
                continue
            raise
        items.append(
            {
                **item.model_dump(exclude={"owner_principal_id"}),
                "state": state,
                "revision": revision,
                "continuation": "needs_recovery"
                if state == "recovery_required"
                else "none",
            }
        )
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"binding": binding, "after": scanned}, separators=(",", ":")
            ).encode()
        )
        .decode()
        .rstrip("=")
        if has_more
        else None
    )
    return {
        "data": items,
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": cursor,
        },
    }
