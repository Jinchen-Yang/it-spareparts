"""Lossless upload/inbox conversion; remote side effects and leases are never replayed."""

import hashlib
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.authz.pn_models import CatalogSubjectBinding
from app.config import get_settings
from app.models.system import SysUser
from .errors import CatalogError
from .media_models import (
    Asset,
    Derivative,
    GalleryEntry,
    InboxGroup,
    InboxItem,
    UploadTicket,
    MultipartUpload,
)
from .migration_models import LegacySourceRecord


def _invalid(message):
    raise CatalogError("migration.invalid_media", message, status_code=422)


def _date(value):
    if value is None:
        return None
    try:
        result = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(result, datetime) or result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        _invalid("历史媒体时间缺少有效时区")


def owner_binding(db, namespace, subject):
    settings = get_settings()
    if namespace != settings.pn_shop_source_namespace or subject is None:
        raise CatalogError("migration.owner_binding_required", "未建立该来源的账号绑定")
    try:
        subject = UUID(str(subject))
    except ValueError:
        _invalid("历史账号编号无效")
    row = db.execute(
        select(CatalogSubjectBinding, SysUser)
        .join(SysUser, SysUser.id == CatalogSubjectBinding.user_id)
        .where(
            CatalogSubjectBinding.issuer == settings.pn_shop_issuer,
            CatalogSubjectBinding.subject == subject,
            CatalogSubjectBinding.active.is_(True),
            CatalogSubjectBinding.version >= 1,
            SysUser.is_active.is_(True),
            SysUser.deleted_at.is_(None),
        )
    ).first()
    if row is None:
        raise CatalogError(
            "migration.owner_binding_required", "历史账号绑定缺失或已失效"
        )
    binding, user = row
    return f"account:{user.id}", {
        "issuer": binding.issuer,
        "subject": str(subject),
        "mapping_version": binding.version,
        "canonical_user_id": user.id,
    }


def _source(db, row, kind, identifier):
    return db.scalar(
        select(LegacySourceRecord).where(
            LegacySourceRecord.namespace == row.namespace,
            LegacySourceRecord.kind == kind,
            LegacySourceRecord.source_id == str(identifier),
        )
    )


def _ticket(db, identifier):
    ticket = db.scalar(
        select(UploadTicket)
        .where(UploadTicket.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ticket is None:
        raise CatalogError("migration.dependency_missing", "上传票据尚未迁移")
    # A live command advances this revision. Conversion may add an initial child,
    # but must never rewind a ticket that a current actor already operated on.
    if ticket.revision != 1 or ticket.job_id is not None:
        raise CatalogError(
            "migration.target_changed", "上传票据已由新系统操作，需明确恢复裁决"
        )
    return ticket


class LegacyUpload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    user_id: UUID
    target_pn_id: UUID | None = None
    staging_key: str = Field(min_length=1, max_length=1024)
    filename: str = Field(max_length=512)
    bytes: int = Field(gt=0, le=20971520)
    content_type: str = Field(min_length=1, max_length=128)
    expires_at: datetime
    created_at: datetime
    consumed_image_id: UUID | None = None


def materialize_upload(db, row, part_id, identity_revision):
    try:
        source = LegacyUpload.model_validate(row.payload)
    except ValidationError:
        _invalid("历史上传票据字段不完整")
    if str(source.id) != row.source_id or source.target_pn_id != row.source_pn_id:
        _invalid("历史上传票据身份与清单不一致")
    owner, ownership = owner_binding(db, row.namespace, source.user_id)
    if db.get(UploadTicket, source.id) is not None:
        raise CatalogError("migration.resource_conflict", "上传票据编号已存在")
    consumed = (
        db.get(GalleryEntry, source.consumed_image_id)
        if source.consumed_image_id
        else None
    )
    if source.consumed_image_id and (
        consumed is None or consumed.origin_namespace != row.namespace
    ):
        raise CatalogError("migration.dependency_missing", "上传票据所指图片尚未迁移")
    multipart = _source(db, row, "multipart", source.id)
    client_key = multipart.payload.get("client_request_id") if multipart else None
    try:
        client_key = (
            UUID(client_key)
            if client_key
            else uuid5(NAMESPACE_URL, f"{row.namespace}:upload:{source.id}")
        )
    except ValueError:
        _invalid("历史上传客户端请求号无效")
    expires, created = _date(source.expires_at), _date(source.created_at)
    status = (
        "committed"
        if consumed
        else "cancelled"
        if expires <= datetime.now(timezone.utc)
        else "issued"
    )
    recovery = multipart and (
        multipart.payload.get("status") in {"initializing", "completing", "cancelling"}
        or multipart.payload.get("operation_token") is not None
    )
    if recovery and not consumed:
        status = "failed"
    ticket = UploadTicket(
        id=source.id,
        owner_principal_id=owner,
        client_id="shop",
        client_request_id=client_key,
        part_id=part_id,
        identity_revision=identity_revision or 0,
        purpose=consumed.purpose if consumed else "common",
        filename=source.filename,
        declared_bytes=source.bytes,
        content_type=source.content_type,
        staging_key=source.staging_key,
        status=status,
        revision=1,
        generation=1,
        asset_id=consumed.asset_id if consumed else None,
        entry_id=consumed.id if consumed else None,
        error_code="migration.remote_outcome_unknown"
        if recovery and not consumed
        else None,
        expires_at=expires,
        created_at=created,
    )
    db.add(ticket)
    db.flush()
    # Ticket conversion is complete; any uncertain multipart outcome is tracked by
    # its own source record and cannot be silently omitted from conservation.
    return (
        "applied",
        None,
        {
            "upload_id": str(ticket.id),
            "owner_binding": ownership,
            "legacy_consumed_image_id": str(source.consumed_image_id)
            if consumed
            else None,
        },
    )


def _inbox_asset(db, row, source, owner):
    if not all(
        source.get(key) is not None
        for key in ("sha256", "width", "height", "bytes", "content_type", "durable_key")
    ):
        raise CatalogError(
            "migration.dependency_missing", "收件箱缺少已收妥图片的元数据"
        )
    if (
        not isinstance(source["sha256"], str)
        or len(source["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in source["sha256"])
    ):
        _invalid("历史图片摘要无效")
    if any(
        type(source[key]) is not int or source[key] <= 0
        for key in ("width", "height", "bytes")
    ):
        _invalid("历史图片尺寸无效")
    asset_id = uuid5(
        NAMESPACE_URL, f"pn-catalog/legacy/{row.namespace}/inbox/{row.source_id}"
    )
    existing = db.scalar(select(Asset).where(Asset.object_key == source["durable_key"]))
    if existing is not None:
        if (existing.sha256, existing.size_bytes, existing.content_type) != (
            source["sha256"],
            source["bytes"],
            source["content_type"],
        ):
            raise CatalogError(
                "migration.resource_conflict", "历史收件箱对象与已有资产不一致"
            )
        return existing
    asset = Asset(
        id=asset_id,
        object_key=source["durable_key"],
        sha256=source["sha256"],
        etag=source.get("source_etag"),
        verification_status="legacy_metadata",
        size_bytes=source["bytes"],
        content_type=source["content_type"],
        width=source["width"],
        height=source["height"],
        owner_principal_id=owner,
        source_namespace=row.namespace,
        source_id=f"inbox:{row.source_id}",
    )
    db.add(asset)
    db.flush()
    if source.get("thumb_key"):
        db.add(
            Derivative(
                id=uuid5(asset.id, "legacy-thumbnail-v1"),
                asset_id=asset.id,
                recipe_id="legacy-thumbnail-v1",
                object_key=source["thumb_key"],
                status="ready",
            )
        )
    return asset


def materialize_inbox(db, row, part_id, identity_revision):
    source = row.payload
    try:
        identifier, upload_id, group_id = (
            UUID(str(source[key])) for key in ("id", "upload_id", "group_id")
        )
    except (KeyError, ValueError):
        _invalid("历史收件箱编号无效")
    if str(identifier) != row.source_id:
        _invalid("历史收件箱编号与清单不一致")
    if db.get(InboxItem, identifier) is not None:
        raise CatalogError("migration.resource_conflict", "收件箱编号已存在")
    ticket = _ticket(db, upload_id)
    owner, ownership = owner_binding(db, row.namespace, source.get("uploaded_by"))
    if owner != ticket.owner_principal_id:
        raise CatalogError(
            "migration.resource_conflict", "收件箱与上传票据的所有者不一致"
        )
    client_order = source.get("client_order", 0)
    if type(client_order) is not int or not 0 <= client_order <= 1999:
        raise CatalogError(
            "migration.group_order_requires_review",
            "旧组顺序超过当前组边界，需明确分组映射",
        )
    group = db.get(InboxGroup, group_id)
    if group is None:
        group = InboxGroup(
            id=group_id,
            owner_principal_id=owner,
            client_id="shop",
            client_group_id=group_id,
            capture_mode="batch",
            revision=1,
        )
        db.add(group)
        db.flush()
    elif (group.owner_principal_id, group.client_id, group.client_group_id) != (
        owner,
        "shop",
        group_id,
    ):
        raise CatalogError(
            "migration.resource_conflict", "历史收件箱组编号已有其他归属"
        )
    if (
        db.scalar(
            select(InboxItem.id).where(
                InboxItem.group_id == group_id, InboxItem.client_order == client_order
            )
        )
        is not None
    ):
        raise CatalogError(
            "migration.group_order_conflict", "历史组内捕获顺序重复，需明确裁决"
        )
    status = source.get("status")
    if status not in {
        "receiving",
        "ready",
        "assigning",
        "assigned",
        "failed",
        "deleted",
    }:
        _invalid("历史收件箱状态无效")
    asset = (
        _inbox_asset(db, row, source, owner)
        if source.get("received_at") or status in {"ready", "assigning", "assigned"}
        else None
    )
    entry = None
    if status == "assigned":
        try:
            entry = db.get(GalleryEntry, UUID(str(source["image_id"])))
        except (KeyError, ValueError):
            _invalid("已归档收件箱没有图片编号")
        if entry is None or part_id is None:
            raise CatalogError(
                "migration.dependency_missing", "已归档收件箱的目标图片尚未迁移"
            )
        if entry.origin_part_id != part_id or entry.origin_namespace != row.namespace:
            raise CatalogError("migration.resource_conflict", "已归档图片目标不一致")
    claimed, claim_evidence = None, None
    expiry = _date(source.get("claim_expires_at"))
    if source.get("claimed_by") and expiry and expiry > datetime.now(timezone.utc):
        claimed, claim_evidence = owner_binding(db, row.namespace, source["claimed_by"])
    item = InboxItem(
        id=identifier,
        upload_id=upload_id,
        asset_id=asset.id if asset else None,
        group_id=group_id,
        position=client_order,
        client_order=client_order,
        owner_principal_id=owner,
        status="failed" if status == "assigning" else status,
        revision=max(1, source.get("revision", 0)),
        generation=1,
        claim_revision=1 if claimed else 0,
        claimed_by=claimed,
        claim_expires_at=expiry if claimed else None,
        assignment_key=UUID(source["assignment_key"])
        if source.get("assignment_key")
        else None,
        part_id=part_id,
        entry_id=entry.id if entry else None,
        deleted_at=_date(source.get("deleted_at")),
    )
    # Legacy source lacks canonical CAS and the authority's audit receipt.
    # Keep original evidence; do not fabricate an accepted command replay.
    item.assignment_intent = None
    db.add(item)
    if asset and ticket.status not in {"committed", "cancelled"}:
        ticket.asset_id, ticket.status = asset.id, "ready"
    if entry:
        ticket.entry_id, ticket.status = entry.id, "committed"
    ticket.purpose = "defect" if source.get("is_defect") else "common"
    db.flush()
    unresolved = status in {"receiving", "assigning", "assigned"}
    return (
        "recovery_required" if unresolved else "applied",
        "migration.receive_reconcile"
        if status == "receiving"
        else "migration.assignment_reconcile"
        if status == "assigning"
        else "migration.assignment_receipt_recovery"
        if status == "assigned"
        else None,
        {
            "inbox_id": str(item.id),
            "group_id": str(group_id),
            "owner_binding": ownership,
            "claim_binding": claim_evidence,
            "legacy_state": status,
            "original_revision": source.get("revision"),
        },
    )


def materialize_multipart(db, row):
    from .media_dto import MultipartPart

    source = row.payload
    try:
        identifier = UUID(source["upload_id"])
        if str(identifier) != row.source_id or source["part_size"] != 1048576:
            raise ValueError()
        parts = [MultipartPart.model_validate(part) for part in source.get("parts", [])]
        if len(parts) > 20 or len({part.part_number for part in parts}) != len(parts):
            raise ValueError()
    except (KeyError, ValueError, ValidationError):
        _invalid("历史分片字段无效")
    if db.get(MultipartUpload, identifier) is not None:
        raise CatalogError("migration.resource_conflict", "分片会话编号已存在")
    ticket = _ticket(db, identifier)
    owner, ownership = owner_binding(db, row.namespace, source.get("user_id"))
    if owner != ticket.owner_principal_id:
        raise CatalogError("migration.resource_conflict", "分片与上传票据的所有者不同")
    legacy_state = source.get("status")
    if legacy_state not in {
        "initializing",
        "uploading",
        "completing",
        "completed",
        "cancelling",
        "cancelled",
        "expired",
    }:
        _invalid("历史分片状态无效")
    remote_id = source.get("storage_upload_id")
    if remote_id is not None and (
        not isinstance(remote_id, str) or not 0 < len(remote_id) <= 1024
    ):
        _invalid("历史存储分片编号无效")
    uncertain = (
        legacy_state in {"initializing", "completing", "cancelling"}
        or source.get("operation_token") is not None
    )
    if legacy_state == "uploading" and not remote_id:
        uncertain = True
    # An uncertain remote result is fenced as cancelled locally, but never emits
    # an abort job or deletes an object. An explicit recovery decision is required.
    state = (
        "cancelled"
        if uncertain or legacy_state in {"cancelled", "expired"}
        else legacy_state
    )
    fingerprint = hashlib.sha256(f"multipart:{identifier}".encode()).hexdigest()
    multipart = MultipartUpload(
        upload_id=identifier,
        storage_upload_id=remote_id,
        state=state,
        storage_fingerprint=fingerprint,
        part_size=1048576,
        parts=[p.model_dump() for p in parts],
        operation_token=uuid4(),
        revision=1,
        aborted_at=_date(source.get("cleaned_at")),
    )
    db.add(multipart)
    if uncertain and ticket.status != "committed":
        ticket.status, ticket.error_code = "failed", "migration.remote_outcome_unknown"
    elif legacy_state in {"cancelled", "expired"} and ticket.status != "committed":
        ticket.status = "cancelled"
    db.flush()
    return (
        "recovery_required" if uncertain else "applied",
        "migration.remote_outcome_unknown" if uncertain else None,
        {
            "upload_id": str(identifier),
            "owner_binding": ownership,
            "legacy_state": legacy_state,
            "remote_fingerprint_algorithm": "shop-multipart-sha256-v1",
            "lease_transferred": False,
        },
    )


def materialize_upload_intent(db, row):
    source = row.payload
    try:
        identifier, image_id = UUID(source["upload_id"]), UUID(source["image_id"])
    except (KeyError, ValueError):
        _invalid("历史上传意图编号无效")
    if str(identifier) != row.source_id or source.get("status") not in {
        "prepared",
        "done",
    }:
        _invalid("历史上传意图与清单不一致")
    ticket, entry = _ticket(db, identifier), db.get(GalleryEntry, image_id)
    if source["status"] == "done":
        if entry is None or ticket.entry_id != entry.id:
            raise CatalogError(
                "migration.dependency_missing", "已完成上传意图的图片尚未匹配"
            )
        return (
            "historical_terminal",
            None,
            {
                "upload_id": str(identifier),
                "entry_id": str(image_id),
                "legacy_source_record_id": row.id,
            },
        )
    # Prepared may mean the cloud copy succeeded but DB publication did not.
    # Do not retry external copy or manufacture a successful catalog receipt.
    if ticket.status != "committed":
        ticket.status, ticket.error_code = "failed", "migration.upload_intent_reconcile"
    return (
        "recovery_required",
        "migration.upload_intent_reconcile",
        {
            "upload_id": str(identifier),
            "expected_entry_id": str(image_id),
            "legacy_source_record_id": row.id,
        },
    )
