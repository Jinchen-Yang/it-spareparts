"""Shared field adapter for fixed business facades; legacy applies only before cutover."""
from app.authz import runtime, service
from app.authz.principal import VerifiedIdentity
from app.security import UserContext, require_page


def read_context(db, identity: VerifiedIdentity, resource: str, action: str, *,
                 legacy_context: UserContext, legacy_page: str | None = None, locked_user=None):
    if runtime.is_authoritative(db, resource):
        access = service.resource_access(db, identity, resource, action, locked_user=locked_user)
        return service.field_context(access, legacy_context), access
    if legacy_page is not None:
        require_page(legacy_page)(legacy_context)
    return legacy_context, None
