"""itsp 原生机型目录，以及供 Shop 获取同一份目录的只读接口。"""

import hashlib
import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import permissions
from app.auth import current_identity
from app.config import get_settings
from app.db import get_db
from app.models.system import SysUser
from app.schemas.model_parts import Candidates, CommandBody
from app.services import model_parts_catalog, model_parts_commands
from app.services.model_parts_stock import enrich_model_parts_stock

router = APIRouter(prefix="/model-parts", tags=["model-parts"])
integration = APIRouter(prefix="/integrations/model-parts/v1", tags=["model-parts-readonly"])
Db = Annotated[Session, Depends(get_db)]
Identity = Annotated[dict, Depends(current_identity)]


def page(identity: dict, name: str) -> bool:
    return permissions.page_permission_allowed(
        role=identity["role"], permission_map=identity.get("perms"), page_key=name
    )


def reader(identity: Identity):
    if not page(identity, "page_parts"):
        raise HTTPException(403, "没有查看机型备件池的权限")
    return identity


def editor_name(db: Session, identity: dict) -> str | None:
    if identity.get("authn") != "sys_user" or identity.get("fb") or not identity.get("sub"):
        return None
    user = db.scalar(
        select(SysUser).where(
            SysUser.username == identity["sub"], SysUser.is_active.is_(True), SysUser.deleted_at.is_(None)
        )
    )
    if not user or not page(identity, "page_master_data"):
        return None
    actual = permissions.runtime_safe(permissions.effective_for_user(user))
    if not all(
        permissions.page_permission_allowed(role=user.role, permission_map=actual, page_key=required)
        for required in ("page_parts", "page_master_data")
    ):
        return None
    return user.username


Reader = Annotated[dict, Depends(reader)]


def maintainer(identity: Reader, db: Db) -> str:
    name = editor_name(db, identity)
    if name is None:
        raise HTTPException(403, "请使用有备件主数据维护权限的实名 itsp 账号")
    return name


def catalog_reader(authorization: Annotated[str | None, Header()] = None) -> None:
    settings = get_settings()
    expected = settings.model_parts_catalog_token_sha256.get_secret_value()
    other = settings.model_parts_shop_stock_token.get_secret_value()
    if not expected or (other and hmac.compare_digest(hashlib.sha256(other.encode()).hexdigest(), expected)):
        raise HTTPException(503, "机型目录只读集成尚未配置独立凭据")
    scheme, sep, token = (authorization or "").partition(" ")
    if (
        not sep
        or scheme.lower() != "bearer"
        or not 32 <= len(token) <= 256
        or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)
    ):
        raise HTTPException(401, "机型目录只读凭据无效")


def begin_read(db: Session):
    # 持一份 PostgreSQL 快照读机型、实时 PN 和版本；结束本库事务后才访问 Shop。
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


@integration.get("/catalog", dependencies=[Depends(catalog_reader)])
def export_catalog(response: Response, db: Db) -> dict:
    response.headers["Cache-Control"] = "no-store"
    begin_read(db)
    return model_parts_catalog.catalog(db)


@router.get("/catalog")
def native_catalog(response: Response, identity: Reader, db: Db) -> dict:
    response.headers["Cache-Control"] = "no-store"
    begin_read(db)
    can_maintain = editor_name(db, identity) is not None
    catalog = model_parts_catalog.catalog(db)
    db.rollback()
    enriched, stock_status, stock_as_of = enrich_model_parts_stock(catalog)
    return {
        "catalog": enriched,
        "canMaintain": can_maintain,
        "stockStatus": stock_status,
        "stockAsOf": stock_as_of,
        "maintenanceUrl": None,
    }


@router.post("/candidates")
def list_candidates(body: Candidates, response: Response, db: Db, _identity: Reader) -> dict:
    response.headers["Cache-Control"] = "no-store"
    begin_read(db)
    return model_parts_catalog.candidates(db, body)


@router.post("/commands")
def apply_command(
    body: CommandBody, response: Response, db: Db, actor: Annotated[str, Depends(maintainer)]
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return model_parts_commands.execute(db, actor, body)
