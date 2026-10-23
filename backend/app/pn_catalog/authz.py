"""Host authorization port; the catalog never infers grants from legacy roles.

The authz migration owns VerifiedIdentity, resource_access and delegated actors.
Its integration installs a resolver only after those paths are implemented. An
absent resolver fails closed even when an existing administrator is logged in.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.pn_catalog.dto import ActorContext, Authorize
from app.pn_catalog.errors import CatalogError


@dataclass(frozen=True)
class CatalogAccess:
    actor: ActorContext
    authorize: Authorize
    confirmation_secret: bytes | None = None
    authorize_job: Callable | None = None


AccessResolver = Callable[[Request, Session], CatalogAccess]


def current_access(request: Request, db: Annotated[Session, Depends(get_db)]) -> CatalogAccess:
    resolver = getattr(request.app.state, "pn_catalog_access_resolver", None)
    if resolver is None:
        raise CatalogError("auth.unavailable", "共享主数据授权尚未接入", status_code=503,
                           retryable=True)
    access = resolver(request, db)
    if not isinstance(access, CatalogAccess):
        raise CatalogError("auth.unavailable", "共享主数据授权适配无效", status_code=503)
    return access
