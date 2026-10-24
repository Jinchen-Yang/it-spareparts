"""PN 主数据对外写接口（shop → itsp，方案 v1 第 3/4 节）。

口径：
- itsp 是 PN 身份、基础资料、扩展参数、别名、合并/改名的唯一所有方；
- shop 只能凭独立服务密钥（只存 SHA-256）调用本组接口，操作人 = ``shop:<用户名>``；
- 每个写请求带 UUID 幂等键，首次成功响应与业务写入同事务落 ``integration_request_log``，
  重放直接回放首次状态码/响应体，绝不重复执行；
- 全部复用既有服务函数（master_edit / merge / part_rename / resale_pn_lookup /
  part_resolver），本模块只做鉴权、请求头校验、幂等与响应编排，不复制业务规则。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.resale_catalog_export import _decode_list_cursor, _encode_list_cursor
from app.config import get_settings
from app.db import get_db
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import IntegrationRequestLog, ProductSpec
from app.models.system import SysAuditLog
from app.services import (
    master_edit,
    master_recompute,
    merge,
    part_rename,
    part_resolver,
    spec_extract,
)
from app.services.resale_pn_lookup import canonical_part, find_pn_candidates

router = APIRouter(prefix="/integrations/pn-master", tags=["pn-master-integration"])

_ACTOR_SYSTEM = "shop"
_NO_STORE = {"Cache-Control": "no-store"}


# ---------------------------------------------------------------- 鉴权 / 请求头


@dataclass(frozen=True)
class IntegrationActor:
    system: str
    user: str
    request_id: str | None

    @property
    def operated_by(self) -> str:
        """审计操作人统一 ``shop:<用户名>``。"""
        return f"{self.system}:{self.user}"


def require_pn_master_actor(
    authorization: Annotated[str | None, Header()] = None,
    x_actor_system: Annotated[str | None, Header()] = None,
    x_actor_user: Annotated[str | None, Header()] = None,
) -> IntegrationActor:
    """独立服务密钥 + 转交用户名。未配置密钥时整组接口 503（默认关闭）。"""
    expected = get_settings().pn_master_integration_token_sha256.get_secret_value()
    if not expected:
        raise HTTPException(503, "PN 主数据集成接口尚未启用")

    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not 32 <= len(token) <= 256:
        raise HTTPException(401, "PN 主数据集成凭据无效")
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected):
        raise HTTPException(401, "PN 主数据集成凭据无效")

    # 方案未定义其他调用方；非 shop 一律视为非法请求头（400，不是 403）。
    if x_actor_system != _ACTOR_SYSTEM:
        raise HTTPException(400, "X-Actor-System 缺失或非法")
    if x_actor_user is None:
        raise HTTPException(400, "X-Actor-User 缺失")
    user = unquote(x_actor_user)
    if not 1 <= len(user) <= 64 or not user.strip():
        raise HTTPException(400, "X-Actor-User 非法（URL 解码后 1–64 字符）")

    return IntegrationActor(_ACTOR_SYSTEM, user, None)


def require_write_actor(
    actor: Annotated[IntegrationActor, Depends(require_pn_master_actor)],
    x_request_id: Annotated[str | None, Header()] = None,
) -> IntegrationActor:
    """写接口额外要求 UUID 幂等键（POST /parts/lookup、/parts/search 是读语义，不挂这里）。"""
    if not x_request_id:
        raise HTTPException(400, "X-Request-Id 缺失")
    try:
        request_id = str(uuid.UUID(x_request_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, "X-Request-Id 必须是 UUID") from None
    return IntegrationActor(actor.system, actor.user, request_id)


Actor = Annotated[IntegrationActor, Depends(require_pn_master_actor)]
WriteActor = Annotated[IntegrationActor, Depends(require_write_actor)]
DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------- 幂等回执


def _replay_response(row: IntegrationRequestLog) -> JSONResponse:
    return JSONResponse(
        status_code=row.status_code, content=row.response_json, headers=_NO_STORE
    )


def _request_fingerprint(request: Request, body: BaseModel | None) -> str:
    """method + path + 规范化请求体 JSON 的 SHA-256。

    ``exclude_unset=True`` 保留 PATCH 的“省略字段”与“显式 null”差异；sort_keys +
    紧凑分隔符保证同一内容的键顺序/空白变化不影响指纹。
    """
    payload = (
        body.model_dump(mode="json", exclude_unset=True) if body is not None else {}
    )
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    material = f"{request.method}\n{request.url.path}\n{canonical}"
    return hashlib.sha256(material.encode()).hexdigest()


def _replay(
    db: Session, actor: IntegrationActor, fingerprint: str
) -> JSONResponse | None:
    if actor.request_id is None:
        return None
    row = db.scalar(
        select(IntegrationRequestLog).where(
            IntegrationRequestLog.request_id == actor.request_id
        )
    )
    if row is None:
        return None
    if row.request_fingerprint != fingerprint:
        raise HTTPException(
            422,
            detail={
                "code": "request_id_reused",
                "message": "X-Request-Id 已用于不同的请求内容，请使用新的幂等键",
            },
        )
    return _replay_response(row)


def _commit_with_receipt(
    db: Session,
    actor: IntegrationActor,
    request: Request,
    payload: dict,
    fingerprint: str,
    *,
    status_code: int = 200,
    post_commit: Callable[[dict], None] | None = None,
) -> JSONResponse:
    """业务写入 + 幂等回执同一事务提交；并发同键撞唯一约束时回放胜者。

    ``post_commit`` 只在本次真正提交后执行（用于合并后的自管事务重算）；重放不执行。
    回执保存提交前的响应体，故重放只回放已存内容，不会带上后置重算结果。
    """
    body = jsonable_encoder(payload)
    assert actor.request_id is not None
    db.add(
        IntegrationRequestLog(
            request_id=actor.request_id,
            request_fingerprint=fingerprint,
            actor_system=actor.system,
            actor_user=actor.user,
            method=request.method,
            path=request.url.path[:255],
            status_code=status_code,
            response_json=copy.deepcopy(body),
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            != "uq_integration_request_log_request_id"
        ):
            raise
        row = db.scalar(
            select(IntegrationRequestLog).where(
                IntegrationRequestLog.request_id == actor.request_id
            )
        )
        if row is None:
            raise
        if row.request_fingerprint != fingerprint:
            raise HTTPException(
                422,
                detail={
                    "code": "request_id_reused",
                    "message": "X-Request-Id 已用于不同的请求内容，请使用新的幂等键",
                },
            ) from None
        return _replay_response(row)
    if post_commit is not None:
        post_commit(body)
        # 重算结果可能含 Decimal/date，JSONResponse 不走 FastAPI 的 jsonable_encoder。
        body = jsonable_encoder(body)
    return JSONResponse(status_code=status_code, content=body, headers=_NO_STORE)


# ---------------------------------------------------------------- 响应小工具


def _part_version(part: DimPart) -> str | None:
    return part.updated_at.isoformat() if part.updated_at else None


def _version_by_id(db: Session, part_id: int) -> str | None:
    updated_at = db.scalar(select(DimPart.updated_at).where(DimPart.id == part_id))
    return updated_at.isoformat() if updated_at else None


def _identity(part: DimPart) -> dict:
    return {
        "part_id": part.id,
        "pn_std": part.pn_std,
        "status": part.status,
        "merged_into_id": part.merged_into_id,
    }


def _alias_item(alias: PartAlias) -> dict:
    return {
        "alias_id": alias.id,
        "pn_raw": alias.pn_raw,
        "pn_std": alias.pn_std,
        "part_id": alias.part_id,
        "source": alias.source,
        "status": alias.status,
        "needs_review": bool(alias.needs_review),
        "created_at": alias.created_at.isoformat() if alias.created_at else None,
    }


def _specs_snapshot(db: Session, part_id: int) -> dict:
    """当前事务内的规格快照（不能另开只读 Session，否则看不到未提交的替换）。"""
    rows = db.execute(
        select(
            ProductSpec.spec_key,
            ProductSpec.spec_value,
            ProductSpec.spec_unit,
            ProductSpec.numeric_value,
            ProductSpec.source,
        )
        .where(ProductSpec.part_id == part_id)
        .order_by(ProductSpec.spec_key)
    ).all()
    return {
        key: {
            "value": value,
            "unit": unit,
            "numeric_value": (
                format(number.normalize(), "f") if number is not None else None
            ),
            "source": source,
        }
        for key, value, unit, number, source in rows
    }


def _spec_payload(specs: dict[str, "SpecValue | None"] | None) -> dict:
    if not specs:
        return {}
    return {
        key: (
            None
            if spec is None
            else {
                "value": spec.value,
                "unit": spec.unit,
                "numeric_value": spec.numeric_value,
            }
        )
        for key, spec in specs.items()
    }


# ---------------------------------------------------------------- 请求模型


class PartLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pn_raw: str = Field(min_length=1, max_length=256)


class PartSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=20, ge=1, le=50)


class SpecValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=128)
    unit: str | None = None
    numeric_value: str | None = None


class PartCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pn_std: str = Field(min_length=1, max_length=128)
    force: bool = False
    description: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    summary: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    machine_or_part: str | None = None
    unit: str | None = None
    specs: dict[str, SpecValue | None] | None = None


class PartPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_version: str = Field(min_length=1, max_length=64)
    description: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    summary: str | None = None
    category_major: str | None = None
    category_minor: str | None = None
    machine_or_part: str | None = None
    unit: str | None = None


class SpecsReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_version: str = Field(min_length=1, max_length=64)
    specs: dict[str, SpecValue | None]


class BatchGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=200)


class MergeRequest(BaseModel):
    """source/target 可用 part_id（推荐，全局身份）或 pn_std（兼容服务原生签名）。"""

    model_config = ConfigDict(extra="forbid")
    source_part_id: int | None = Field(default=None, gt=0)
    target_part_id: int | None = Field(default=None, gt=0)
    source_pn: str | None = Field(default=None, max_length=128)
    target_pn: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class UnmergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merge_log_id: int = Field(gt=0)


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_pn_std: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class AliasCreateRequest(BaseModel):
    """part_id（推荐）或 pn_std 二选一，用来定位别名归属的现有型号。"""

    model_config = ConfigDict(extra="forbid")
    pn_raw: str = Field(min_length=1, max_length=256)
    part_id: int | None = Field(default=None, gt=0)
    pn_std: str | None = Field(default=None, max_length=128)


# ---------------------------------------------------------------- 读接口


def _part_detail_payload(state: dict, aliases: list[PartAlias],
                         master_source: str | None) -> dict:
    identity = state["identity"]
    return {
        "part_id": state["part_id"],
        "pn_std": state["pn_std"],
        "status": identity["status"],
        "merged_into_id": identity["merged_into_id"],
        "version": state["version"],
        "fields": state["fields"],
        "specs": state["specs"],
        "aliases": [_alias_item(a) for a in aliases],
        "master_source": master_source,
    }


@router.get("/parts/{part_id}")
def get_part(part_id: int, _actor: Actor, db: DbSession) -> dict:
    state = master_edit.get_part_edit_state(db, part_id)
    if state is None:
        raise HTTPException(404, "型号不存在")
    aliases = db.scalars(
        select(PartAlias).where(PartAlias.part_id == part_id).order_by(PartAlias.id)
    ).all()
    master_source = db.scalar(
        select(DimPart.master_source).where(DimPart.id == part_id)
    )
    return _part_detail_payload(state, list(aliases), master_source)


@router.post("/parts/batch-get")
def batch_get_parts(body: BatchGetRequest, _actor: Actor, db: DbSession) -> dict:
    """批量读取，返回与 GET /parts/{id} 相同的单项结构；不存在的 id 单列 missing。"""
    ids = list(dict.fromkeys(body.part_ids))
    states = master_edit.get_parts_edit_states(db, ids)
    aliases_by_part: dict[int, list[PartAlias]] = {}
    master_sources: dict[int, str | None] = {}
    found_ids = [part_id for part_id in ids if part_id in states]
    if found_ids:
        for alias in db.scalars(
            select(PartAlias)
            .where(PartAlias.part_id.in_(found_ids))
            .order_by(PartAlias.part_id, PartAlias.id)
        ):
            aliases_by_part.setdefault(alias.part_id, []).append(alias)
        master_sources = dict(
            db.execute(
                select(DimPart.id, DimPart.master_source)
                .where(DimPart.id.in_(found_ids))
            ).all()
        )
    return {
        "items": [
            _part_detail_payload(
                states[part_id],
                aliases_by_part.get(part_id, []),
                master_sources.get(part_id),
            )
            for part_id in found_ids
        ],
        "missing": [part_id for part_id in ids if part_id not in states],
    }


@router.post("/parts/lookup")
def lookup_part(body: PartLookupRequest, _actor: Actor, db: DbSession) -> dict:
    raw = body.pn_raw.strip()
    if not raw:
        raise HTTPException(400, "PN 不能为空白")
    matches, pending = find_pn_candidates(db, raw)
    bound = list(matches.values())
    if len(bound) == 1 and not pending:
        original, kind = bound[0]
        part, merged = canonical_part(db, original)
        if part is None:
            return {"query": raw, "status": "ambiguous", "exact": None,
                    "candidates": [], "pending_alias": pending}
        return {
            "query": raw,
            "status": "merged" if merged else kind,
            "exact": {**_identity(part),
                      "redirected_from_part_id": original.id if merged else None},
            "candidates": [],
            "pending_alias": pending,
        }
    return {
        "query": raw,
        "status": "ambiguous" if (bound or pending) else "not_found",
        "exact": None,
        "candidates": [
            {**_identity(part), "match": kind} for part, kind in bound
        ],
        "pending_alias": pending,
    }


@router.post("/parts/search")
def search_parts(body: PartSearchRequest, _actor: Actor, db: DbSession) -> dict:
    q = body.q.strip()
    if not q:
        raise HTTPException(400, "查询条件不能为空白")
    # 统一文本→part_id 出口；log_miss=False：读接口不写 search_miss 审计。
    result = part_resolver.resolve(db, q, limit=body.limit, log_miss=False)
    return {"schema_version": 1, **result}


@router.get("/changes")
def list_changes(
    _actor: Actor,
    db: DbSession,
    after: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    statement = (
        select(DimPart)
        .order_by(DimPart.updated_at.asc(), DimPart.id.asc())
        .limit(limit + 1)
    )
    if after is not None:
        cursor_at, cursor_id = _decode_list_cursor(after)
        statement = statement.where(
            tuple_(DimPart.updated_at, DimPart.id) > tuple_(cursor_at, cursor_id)
        )
    fetched = db.scalars(statement).all()
    has_more = len(fetched) > limit
    page = fetched[:limit]
    items = [
        {
            "part_id": part.id,
            "pn_std": part.pn_std,
            "status": part.status,
            "merged_into_id": part.merged_into_id,
            "version": _part_version(part),
            "description": part.description,
            "brand": part.brand,
            "manufacturer": part.manufacturer,
            "summary": part.summary,
            "category_major": part.category_major,
            "category_minor": part.category_minor,
            "machine_or_part": part.machine_or_part,
            "unit": part.unit,
            "master_source": part.master_source,
            "is_excluded": bool(part.is_excluded),
        }
        for part in page
    ]
    return {
        "schema_version": 1,
        "items": items,
        "has_more": has_more,
        "next_cursor": (
            _encode_list_cursor(page[-1].updated_at, page[-1].id) if page else None
        ),
    }


# ---------------------------------------------------------------- 写接口


@router.post("/parts")
def create_part(
    body: PartCreateRequest, request: Request, actor: WriteActor, db: DbSession
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay

    pn = body.pn_std.strip().upper()
    if not pn:
        raise HTTPException(400, "PN 不能为空")
    existing = db.scalar(select(DimPart).where(DimPart.pn_std == pn))
    if existing is not None:
        raise HTTPException(
            409,
            detail={
                "code": "pn_exists",
                "message": f"型号已存在: {pn}",
                "part_id": existing.id,
                "status": existing.status,
            },
        )

    fields = body.model_dump(
        exclude={"pn_std", "specs", "force"}, exclude_unset=True
    )
    try:
        result = master_edit.create_part_in_transaction(
            db,
            pn_std=body.pn_std,
            operated_by=actor.operated_by,
            force=body.force,
            spec_updates=_spec_payload(body.specs),
            master_source="shop",
            **fields,
        )
    except master_edit.MasterEditError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not result.get("created"):
        raise HTTPException(
            409,
            detail={
                "code": "near_duplicate",
                "message": result.get("message", "存在近似型号"),
                "near_duplicates": result.get("near_duplicates", []),
            },
        )

    db.flush()
    version = _version_by_id(db, result["id"])
    edit_state = {**result.get("edit_state", {}), "version": version}
    payload = {
        "created": True,
        "part_id": result["id"],
        "pn_std": result["pn_std"],
        "version": version,
        "near_duplicates": result.get("near_duplicates", []),
        "audit_id": result.get("audit_id"),
        "edit_state": edit_state,
    }
    return _commit_with_receipt(db, actor, request, payload, fingerprint)


@router.patch("/parts/{part_id}")
def patch_part(
    part_id: int,
    body: PartPatchRequest,
    request: Request,
    actor: WriteActor,
    db: DbSession,
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay

    part = master_edit.lock_part(db, part_id=part_id)
    if part is None:
        raise HTTPException(404, "型号不存在")
    current_version = _part_version(part)
    if body.baseline_version != current_version:
        raise HTTPException(
            409,
            detail={
                "code": "version_conflict",
                "message": "baseline_version 与当前版本不一致，请重新读取型号",
                "current_version": current_version,
            },
        )
    updates = body.model_dump(exclude_unset=True, exclude={"baseline_version"})
    if not updates:
        raise HTTPException(400, "没有可编辑的字段")
    try:
        result = master_edit.edit_part_in_transaction(
            db,
            pn_std=part.pn_std,
            part_id=part_id,
            updates=updates,
            operated_by=actor.operated_by,
        )
    except master_edit.MasterEditConflict as exc:
        raise HTTPException(409, detail=exc.detail) from exc
    except master_edit.MasterEditError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "型号不存在")

    db.flush()
    version = _version_by_id(db, part_id)
    payload = {
        "part_id": part_id,
        "pn_std": result["pn_std"],
        "version": version,
        "updated": result["updated"],
        "locked_fields": result["locked_fields"],
        "audit_id": result["audit_id"],
        "edit_state": {**result.get("edit_state", {}), "version": version},
    }
    return _commit_with_receipt(db, actor, request, payload, fingerprint)


@router.put("/parts/{part_id}/specs")
def replace_specs(
    part_id: int,
    body: SpecsReplaceRequest,
    request: Request,
    actor: WriteActor,
    db: DbSession,
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay

    part = master_edit.lock_part(db, part_id=part_id)
    if part is None:
        raise HTTPException(404, "型号不存在")
    current_version = _part_version(part)
    if body.baseline_version != current_version:
        raise HTTPException(
            409,
            detail={
                "code": "version_conflict",
                "message": "baseline_version 与当前版本不一致，请重新读取型号",
                "current_version": current_version,
            },
        )
    if part.status != "active":
        raise HTTPException(400, f"型号 {part.pn_std} 已合并入他档，请对目标型号操作")

    # “整体替换 manual specs”：请求里没出现的手工键一律删除，auto 派生行不动。
    current_manual = set(
        db.scalars(
            select(ProductSpec.spec_key).where(
                ProductSpec.part_id == part_id, ProductSpec.source == "manual"
            )
        ).all()
    )
    spec_updates = {key: None for key in current_manual - set(body.specs)}
    spec_updates.update(_spec_payload(body.specs))

    if spec_updates:
        try:
            result = master_edit.edit_part_in_transaction(
                db,
                pn_std=part.pn_std,
                part_id=part_id,
                updates={},
                spec_updates=spec_updates,
                operated_by=actor.operated_by,
            )
        except master_edit.MasterEditConflict as exc:
            raise HTTPException(409, detail=exc.detail) from exc
        except master_edit.MasterEditError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "型号不存在")
        audit_id = result["audit_id"]
    else:
        audit_id = None

    db.flush()
    version = _version_by_id(db, part_id)
    payload = {
        "part_id": part_id,
        "pn_std": part.pn_std,
        "version": version,
        "specs": _specs_snapshot(db, part_id),
        "audit_id": audit_id,
    }
    return _commit_with_receipt(db, actor, request, payload, fingerprint)


@router.post("/parts/merge")
def merge_parts(
    body: MergeRequest, request: Request, actor: WriteActor, db: DbSession
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay

    def _resolve(part_id: int | None, pn_std: str | None) -> DimPart | None:
        if part_id is not None:
            return db.get(DimPart, part_id)
        return db.scalar(
            select(DimPart).where(DimPart.pn_std == pn_std.strip().upper())
        )

    if (body.source_part_id is None) == (body.source_pn is None):
        raise HTTPException(400, "source_part_id 与 source_pn 必须二选一")
    if (body.target_part_id is None) == (body.target_pn is None):
        raise HTTPException(400, "target_part_id 与 target_pn 必须二选一")
    source = _resolve(body.source_part_id, body.source_pn)
    target = _resolve(body.target_part_id, body.target_pn)
    if source is None or target is None:
        raise HTTPException(404, "源或目标型号不存在")
    if source.id == target.id:
        raise HTTPException(400, "源与目标是同一型号")
    try:
        result = merge.merge_parts(
            db,
            source.pn_std,
            target.pn_std,
            body.reason,
            actor.operated_by,
            commit=False,
        )
    except merge.MergeError as exc:
        raise HTTPException(400, str(exc)) from exc

    db.flush()
    payload = {
        **result,
        "source_part_id": source.id,
        "target_part_id": target.id,
        "source_version": _version_by_id(db, source.id),
        "target_version": _version_by_id(db, target.id),
    }
    return _commit_with_receipt(
        db, actor, request, payload, fingerprint,
        post_commit=lambda committed: master_recompute.recompute_after_master_write(
            db, committed, operation="merge"
        ),
    )


@router.post("/parts/unmerge")
def unmerge_parts(
    body: UnmergeRequest, request: Request, actor: WriteActor, db: DbSession
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay
    try:
        result = merge.unmerge(
            db, body.merge_log_id, actor.operated_by, commit=False
        )
    except merge.MergeError as exc:
        raise HTTPException(400, str(exc)) from exc

    db.flush()
    source_id = db.scalar(
        select(DimPart.id).where(DimPart.pn_std == result["source_pn"])
    )
    payload = {
        **result,
        "source_version": _version_by_id(db, source_id) if source_id else None,
    }
    return _commit_with_receipt(
        db, actor, request, payload, fingerprint,
        post_commit=lambda committed: master_recompute.recompute_after_master_write(
            db, committed, operation="unmerge"
        ),
    )


@router.post("/parts/{part_id}/rename")
def rename_part(
    part_id: int,
    body: RenameRequest,
    request: Request,
    actor: WriteActor,
    db: DbSession,
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay
    try:
        result = part_rename.rename_part(
            db,
            part_id,
            body.new_pn_std,
            body.reason,
            actor.operated_by,
            commit=False,
        )
    except part_rename.RenameError as exc:
        raise HTTPException(400, str(exc)) from exc

    db.flush()
    payload = {
        **result,
        "version": _version_by_id(db, part_id),
    }
    return _commit_with_receipt(db, actor, request, payload, fingerprint)


@router.post("/aliases")
def create_alias(
    body: AliasCreateRequest, request: Request, actor: WriteActor, db: DbSession
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, body)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay

    raw = body.pn_raw.strip()
    if not raw:
        raise HTTPException(400, "pn_raw 不能为空白")
    if (body.part_id is None) == (body.pn_std is None):
        raise HTTPException(400, "part_id 与 pn_std 必须二选一")
    part = (
        db.get(DimPart, body.part_id)
        if body.part_id is not None
        else db.scalar(
            select(DimPart).where(DimPart.pn_std == body.pn_std.strip().upper())
        )
    )
    if part is None:
        raise HTTPException(404, "型号不存在")
    if part.status != "active":
        raise HTTPException(400, f"型号 {part.pn_std} 已合并入他档，不能登记别名")
    existing = db.scalar(select(PartAlias).where(PartAlias.pn_raw == raw))
    if existing is not None:
        raise HTTPException(
            409,
            detail={
                "code": "alias_exists",
                "message": f"别名已存在: {raw}",
                "alias_id": existing.id,
                "part_id": existing.part_id,
            },
        )

    alias = PartAlias(
        pn_raw=raw,
        pn_std=part.pn_std,
        part_id=part.id,
        source="shop",
        needs_review=False,
        status="active",
    )
    db.add(alias)
    try:
        db.flush()
        db.refresh(alias)
    except IntegrityError as exc:
        db.rollback()
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            == "part_alias_pn_raw_key"
        ):
            raise HTTPException(409, detail={"code": "alias_exists"}) from exc
        raise

    spec_extract.touch_parts(db, [part.id])
    db.add(
        SysAuditLog(
            entity_type="part_alias",
            entity_id=alias.id,
            action="alias_create",
            before_json=None,
            after_json=_alias_item(alias),
            reason="shop 登记别名",
            operated_by=actor.operated_by,
        )
    )
    db.flush()
    payload = {**_alias_item(alias), "part_version": _version_by_id(db, part.id)}
    return _commit_with_receipt(db, actor, request, payload, fingerprint)


@router.delete("/aliases/{alias_id}")
def disable_alias(
    alias_id: int, request: Request, actor: WriteActor, db: DbSession
) -> JSONResponse:
    fingerprint = _request_fingerprint(request, None)
    replay = _replay(db, actor, fingerprint)
    if replay is not None:
        return replay

    alias = db.get(PartAlias, alias_id)
    if alias is None:
        raise HTTPException(404, "别名不存在")
    if alias.source != "shop":
        raise HTTPException(403, "只允许停用 shop 来源的别名")

    before = _alias_item(alias)
    alias.status = "rejected"
    alias.needs_review = False
    db.flush()
    if alias.part_id is not None:
        spec_extract.touch_parts(db, [alias.part_id])
    db.add(
        SysAuditLog(
            entity_type="part_alias",
            entity_id=alias.id,
            action="alias_disable",
            before_json=before,
            after_json=_alias_item(alias),
            reason="shop 停用别名",
            operated_by=actor.operated_by,
        )
    )
    db.flush()
    payload = {
        **_alias_item(alias),
        "part_version": (
            _version_by_id(db, alias.part_id) if alias.part_id is not None else None
        ),
    }
    return _commit_with_receipt(db, actor, request, payload, fingerprint)
