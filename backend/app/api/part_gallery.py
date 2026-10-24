"""型号图库代理（itsp 前端 → itsp 后端 → shop 图库服务）。

图库归 shop 所有，服务密钥只留在后端；本模块只做权限、请求头/幂等键透传、
错误映射与访问审计，不复制 shop 的图库业务规则。

- 读：登录即可（与 ``api/parts.py`` 的 ``/search`` 同一准入：``current_role``）；
- 写：与主数据编辑同一权限 ``page_master_data``；
- ``X-Actor-User`` 取当前 itsp 登录用户名；
- 写请求必须带前端生成的 ``X-Request-Id``（UUID），失败重试复用同一个值。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.auth import current_role
from app.security import UserContext, get_current_user_context, record_access_log, require_page
from app.services.shop_gallery_client import (
    ShopGalleryClient,
    ShopGalleryNotConfigured,
    ShopGalleryResponse,
    ShopGalleryUnavailable,
    get_shop_gallery_client,
)

router = APIRouter(prefix="/parts/{part_id}/gallery", tags=["part-gallery"])

PartId = Annotated[int, Path(ge=1, le=2**63 - 1)]
ImageId = Annotated[UUID, Path()]
UploadId = Annotated[UUID, Path()]
MAX_GALLERY_REVISION = 2_147_483_647
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_IMAGE_MIME = Literal["image/jpeg", "image/png", "image/webp"]


class UploadSignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES)
    content_type: _IMAGE_MIME


class ResumableUploadInput(UploadSignInput):
    client_request_id: UUID


class RegisterImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    source: Literal["phone", "upload"] = "upload"
    is_defect: bool = False


class ImageUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_gallery_revision: int = Field(ge=0, le=MAX_GALLERY_REVISION)
    sort_order: int | None = Field(default=None, ge=0)
    is_defect: bool | None = None
    status: Literal["gallery"] | None = None

    @model_validator(mode="after")
    def require_changes(self):
        changes = self.model_fields_set - {"baseline_gallery_revision"}
        if not changes or any(getattr(self, field) is None for field in changes):
            raise ValueError("请提供排序、瑕疵标记或移入图库操作")
        return self


def require_gallery_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> str:
    """写请求必须携带前端生成的 UUID；重试复用同号由 shop 幂等收敛。"""
    if x_request_id is None or not x_request_id.strip():
        raise HTTPException(400, "写操作缺少 X-Request-Id")
    try:
        return str(UUID(x_request_id.strip()))
    except (ValueError, AttributeError):
        raise HTTPException(400, "X-Request-Id 必须是 UUID") from None


def get_gallery_client() -> ShopGalleryClient:
    try:
        return get_shop_gallery_client()
    except ShopGalleryNotConfigured:
        raise HTTPException(503, "图库服务未配置") from None


GalleryClient = Annotated[ShopGalleryClient, Depends(get_gallery_client)]
WriteRequestId = Annotated[str, Depends(require_gallery_request_id)]
Role = Annotated[str, Depends(current_role)]
Ctx = Annotated[UserContext, Depends(get_current_user_context)]
MasterPage = Annotated[None, Depends(require_page("page_master_data"))]


def _actor(ctx: UserContext) -> str:
    username = (ctx.user_id or "").strip()
    if not username:
        raise HTTPException(401, "缺少登录身份")
    if len(username) > 64:
        raise HTTPException(400, "当前登录用户名过长")
    return username


def _detail(body: object, fallback: str) -> object:
    if isinstance(body, dict):
        detail = body.get("detail")
        if detail is not None:
            return detail
    return fallback


def _shop_call(
    client: ShopGalleryClient,
    method: str,
    path: str,
    *,
    actor: str,
    request_id: str | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
) -> ShopGalleryResponse:
    try:
        result = client.request(
            method,
            path,
            actor_user=actor,
            request_id=request_id,
            json_body=json_body,
            params=params,
        )
    except ShopGalleryUnavailable:
        raise HTTPException(503, "图库服务暂不可用") from None

    status = result.status_code
    if status == 404:
        raise HTTPException(404, "该型号在商城尚无商品或未对接")
    if status == 409:
        raise HTTPException(409, _detail(result.body, "图库已被他人修改，请刷新后重试"))
    if status in (401, 403):
        # 服务密钥错是服务端配置问题，不能表现为 itsp 用户未登录/无权限。
        raise HTTPException(502, "图库服务鉴权失败")
    if status >= 500:
        raise HTTPException(503, "图库服务暂不可用")
    if status >= 400:
        raise HTTPException(status, _detail(result.body, "图库请求失败"))
    return result


def _json_response(result: ShopGalleryResponse) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    etag = result.header("etag")
    if etag:
        headers["ETag"] = etag
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


def _response_with_etag(result: ShopGalleryResponse) -> Response:
    headers: dict[str, str] = {}
    etag = result.header("etag")
    if etag:
        headers["ETag"] = etag
    return Response(status_code=result.status_code, headers=headers)


def _audit(ctx: UserContext, action: str, part_id: int, **extra: object) -> None:
    record_access_log(ctx, action, "part", {"part_id": part_id, **extra})


@router.get("")
def list_part_gallery(
    part_id: PartId,
    # 与 /parts/search 同款准入：登录即可查看型号；page_master_data 是该能力的超集。
    _role: Role,
    ctx: Ctx,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_list", part_id)
    result = _shop_call(
        client,
        "GET",
        f"/api/integrations/gallery/parts/{part_id}/images",
        actor=actor,
    )
    return _json_response(result)


@router.post("/uploads/sign")
def sign_upload(
    part_id: PartId,
    payload: UploadSignInput,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_upload_sign", part_id, request_id=request_id)
    result = _shop_call(
        client,
        "POST",
        f"/api/integrations/gallery/parts/{part_id}/uploads/sign",
        actor=actor,
        request_id=request_id,
        json_body=payload.model_dump(mode="json"),
    )
    return _json_response(result)


@router.post("/uploads/resumable")
def initialize_resumable_upload(
    part_id: PartId,
    payload: ResumableUploadInput,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_upload_resumable", part_id, request_id=request_id)
    result = _shop_call(
        client,
        "POST",
        f"/api/integrations/gallery/parts/{part_id}/uploads/resumable",
        actor=actor,
        request_id=request_id,
        json_body=payload.model_dump(mode="json"),
    )
    return _json_response(result)


@router.get("/uploads/{upload_id}/resumable")
def resumable_upload_status(
    part_id: PartId,
    upload_id: UploadId,
    _role: Role,
    ctx: Ctx,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_upload_status", part_id, upload_id=str(upload_id))
    result = _shop_call(
        client,
        "GET",
        f"/api/integrations/gallery/parts/{part_id}/uploads/{upload_id}/resumable",
        actor=actor,
    )
    return _json_response(result)


@router.post("/uploads/{upload_id}/parts/{number}/sign")
def sign_resumable_part(
    part_id: PartId,
    upload_id: UploadId,
    number: int,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_upload_part_sign", part_id, upload_id=str(upload_id),
           number=number, request_id=request_id)
    result = _shop_call(
        client,
        "POST",
        f"/api/integrations/gallery/parts/{part_id}/uploads/{upload_id}/parts/{number}/sign",
        actor=actor,
        request_id=request_id,
    )
    return _json_response(result)


@router.post("/uploads/{upload_id}/complete")
def complete_resumable_upload(
    part_id: PartId,
    upload_id: UploadId,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_upload_complete", part_id, upload_id=str(upload_id),
           request_id=request_id)
    result = _shop_call(
        client,
        "POST",
        f"/api/integrations/gallery/parts/{part_id}/uploads/{upload_id}/complete",
        actor=actor,
        request_id=request_id,
    )
    return _json_response(result)


@router.delete("/uploads/{upload_id}/resumable", status_code=204)
def cancel_resumable_upload(
    part_id: PartId,
    upload_id: UploadId,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> Response:
    actor = _actor(ctx)
    _audit(ctx, "gallery_upload_cancel", part_id, upload_id=str(upload_id),
           request_id=request_id)
    result = _shop_call(
        client,
        "DELETE",
        f"/api/integrations/gallery/parts/{part_id}/uploads/{upload_id}/resumable",
        actor=actor,
        request_id=request_id,
    )
    return _response_with_etag(result)


@router.post("/images", status_code=201)
def register_image(
    part_id: PartId,
    payload: RegisterImageInput,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_register", part_id, upload_id=str(payload.upload_id),
           request_id=request_id)
    result = _shop_call(
        client,
        "POST",
        f"/api/integrations/gallery/parts/{part_id}/images",
        actor=actor,
        request_id=request_id,
        json_body=payload.model_dump(mode="json"),
    )
    return _json_response(result)


@router.patch("/images/{image_id}")
def update_image(
    part_id: PartId,
    image_id: ImageId,
    payload: ImageUpdateInput,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
) -> JSONResponse:
    actor = _actor(ctx)
    _audit(ctx, "gallery_update", part_id, image_id=str(image_id),
           request_id=request_id)
    result = _shop_call(
        client,
        "PATCH",
        f"/api/integrations/gallery/images/{image_id}",
        actor=actor,
        request_id=request_id,
        json_body=payload.model_dump(mode="json", exclude_unset=True),
    )
    return _json_response(result)


@router.delete("/images/{image_id}", status_code=204)
def delete_image(
    part_id: PartId,
    image_id: ImageId,
    _auth: Role,
    request_id: WriteRequestId,
    ctx: Ctx,
    _page: MasterPage,
    client: GalleryClient,
    baseline_gallery_revision: Annotated[
        int | None, Query(ge=0, le=MAX_GALLERY_REVISION)
    ] = None,
) -> Response:
    actor = _actor(ctx)
    _audit(ctx, "gallery_delete", part_id, image_id=str(image_id),
           request_id=request_id)
    params = (
        {"baseline_gallery_revision": baseline_gallery_revision}
        if baseline_gallery_revision is not None
        else None
    )
    result = _shop_call(
        client,
        "DELETE",
        f"/api/integrations/gallery/images/{image_id}",
        actor=actor,
        request_id=request_id,
        params=params,
    )
    return _response_with_etag(result)
