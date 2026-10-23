"""Bounded exact-ID snapshots without per-row HTTP, SQL or authorization calls."""

from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec

from . import api
from .attribute_models import AttributeState, AttributeValue
from .attributes import _item
from .content import _content_projection
from .content_models import ConditionContent
from .errors import CatalogError
from .models import PartState
from .persistence import _spec_snapshot, acquire_identity_lock, snapshot


def get_snapshots(db, part_ids, *, cursor, context, authorize):
    if not 1 <= len(part_ids) <= 50 or any(
        type(pid) is not int or pid < 1 for pid in part_ids
    ):
        raise CatalogError(
            "request.validation_failed", "需要 1–50 个明确 PN 编号", status_code=422
        )
    master = api._authorize(db, context, authorize, "list", None)
    if master.metadata.get("scope") != "all":
        raise CatalogError(
            "auth.forbidden", "当前资料范围不支持此批量查询", status_code=403
        )

    def optional(action):
        try:
            decision = api._authorize(db, context, authorize, action, None)
        except CatalogError as exc:
            if exc.code == "auth.forbidden" and exc.status_code == 403:
                return None
            raise
        if decision.metadata.get("scope") != "all":
            raise CatalogError(
                "auth.forbidden", "当前资料范围不支持此批量查询", status_code=403
            )
        return decision

    content, attributes = optional("read_content"), optional("read_attributes")
    can_attributes = attributes is not None and api._allowed(
        attributes.readable_fields, "attributes"
    )
    grants = sorted(
        set(master.readable_fields)
        | (set(content.readable_fields) if content else set())
        | (set(attributes.readable_fields) if can_attributes else set())
    )
    ids = sorted(set(part_ids))
    fingerprint = api._digest(
        {
            "part_ids": ids,
            "principal": context.actor.principal_id,
            "client": context.actor.client_id,
            "resource": "bulk_snapshot",
        }
    )
    after = api._decode_cursor(cursor, fingerprint)
    ids = [pid for pid in ids if pid > after]
    if not ids:
        return {
            "data": [],
            "meta": {
                "request_id": str(context.request_id),
                "replayed": False,
                "next_cursor": None,
            },
        }
    acquire_identity_lock(db, shared=True)
    # Sorted parent SHARE locks stabilize identity/profile/specs and attributes.
    # Grade content uses independent locks; its one SQL statement is its snapshot.
    parts = {
        p.id: p
        for p in db.scalars(
            select(DimPart)
            .where(DimPart.id.in_(ids))
            .order_by(DimPart.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    }
    states = {
        s.part_id: s
        for s in db.scalars(
            select(PartState)
            .where(PartState.part_id.in_(parts))
            .execution_options(populate_existing=True)
        )
    }
    specs = {}
    for row in db.scalars(
        select(ProductSpec)
        .where(ProductSpec.part_id.in_(parts))
        .execution_options(populate_existing=True)
    ):
        specs.setdefault(row.part_id, {}).update(_spec_snapshot([row]))
    contents = (
        {
            (r.part_id, r.grade): r
            for r in db.scalars(
                select(ConditionContent)
                .where(ConditionContent.part_id.in_(parts))
                .execution_options(populate_existing=True)
            )
        }
        if content
        else {}
    )
    attr_revisions, attr_values = {}, {}
    if can_attributes:
        for pid, revision, row in db.execute(
            select(AttributeState.part_id, AttributeState.revision, AttributeValue)
            .outerjoin(AttributeValue, AttributeValue.part_id == AttributeState.part_id)
            .where(AttributeState.part_id.in_(parts))
            .execution_options(populate_existing=True)
        ):
            attr_revisions[pid] = revision
            if row is not None:
                attr_values.setdefault(pid, {})[row.key] = _item(row)
    values, remaining = [], 512 * 1024 - 4096
    for pid in ids:
        item = {
            "part_id": pid,
            "state": "found" if pid in parts else "not_found",
            "profile": None,
            "content_good": None,
            "content_bad": None,
            "attributes": None,
            "unavailable_resources": [],
            "readable_fields": grants,
        }
        if pid in parts:
            value = snapshot(
                db,
                parts[pid],
                state=states.get(pid),
                specs=specs.get(pid, {}),
                aliases=[],
            )
            item["profile"] = api._project(value, master)
            for grade in ("good", "bad"):
                if content and any(
                    api._allowed(content.readable_fields, f"content.{grade}.{key}")
                    for key in ("title", "description")
                ):
                    item[f"content_{grade}"] = _content_projection(
                        contents.get((pid, grade)),
                        pid,
                        grade,
                        value["identity_revision"],
                        content,
                    )
                else:
                    item["unavailable_resources"].append(f"content.{grade}")
            if can_attributes:
                item["attributes"] = {
                    "part_id": pid,
                    "identity_revision": value["identity_revision"],
                    "revision": attr_revisions.get(pid, 0),
                    "items": attr_values.get(pid, {}),
                }
            else:
                item["unavailable_resources"].append("attributes")
        size = len(api._json(item).encode()) + 1
        if size > remaining:
            if not values:
                raise CatalogError(
                    "request.payload_too_large",
                    "单份资料超过批量响应预算，请分别读取",
                    status_code=413,
                    details={"part_id": pid, "max_bytes": 512 * 1024},
                )
            break
        values.append(item)
        remaining -= size
    next_cursor = (
        api._encode_cursor(values[-1]["part_id"], fingerprint)
        if len(values) < len(ids)
        else None
    )
    return {
        "data": values,
        "meta": {
            "request_id": str(context.request_id),
            "replayed": False,
            "next_cursor": next_cursor,
        },
    }
