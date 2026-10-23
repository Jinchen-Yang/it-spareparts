"""Fence transitional writers after persistent authorization cutover.

Authorization precedes topology/PN locks. Old display-name-only callers cannot
acquire new authority; imports retain the submitting account's original epoch.
"""

from collections.abc import Iterable
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy.orm import Session

from app.authz.runtime import is_authoritative
from app.pn_catalog.dto import ActorContext, CommandContext, Provenance
from app.pn_catalog.errors import CatalogError

_INTERNAL_COMMAND = "pn_authority_legacy_command"


def guard_legacy_write(
    db: Session,
    *,
    context: CommandContext | None,
    action: str,
    part_ids: Iterable[int] = (),
    fields: Iterable[str] = (),
) -> dict:
    resource = "part_identity" if action.startswith("identity.") else "part_master"
    if not is_authoritative(db, resource):
        return {}
    if context is None:
        raise CatalogError(
            "catalog.legacy_writer_retired",
            "此旧写入入口已停用，请使用共享资料入口",
            status_code=409,
        )
    from app.services.pn_catalog_access import resolve_job_access

    access = resolve_job_access(db, context.actor)
    metadata = {}
    for part_id in sorted(set(part_ids)) or [None]:
        decision = access.authorize(db, context, action, part_id)
        metadata[str(part_id)] = dict(decision.metadata)
        missing = set(fields) - decision.writable_fields
        if "*" not in decision.writable_fields and missing:
            raise CatalogError(
                "auth.forbidden",
                "缺少本次写入字段的权限",
                status_code=403,
                details={"fields": sorted(missing)},
            )

    return {"action": action, "decisions": metadata}


def require_authority_command(
    db: Session, *, context=None, resource="part_master"
) -> None:
    """Old complex writers are reachable only from a checked facade command."""
    if not is_authoritative(db, resource):
        return
    permitted = db.info.get(_INTERNAL_COMMAND)
    if permitted is None or permitted != (db.get_transaction(), context):
        raise CatalogError(
            "catalog.legacy_writer_retired",
            "此旧写入入口已停用，请使用共享资料入口",
            status_code=409,
        )


@contextmanager
def _checked_identity_command(db: Session, context: CommandContext):
    """Internal bridge, entered after facade authorization and version checks."""
    previous = db.info.get(_INTERNAL_COMMAND)
    db.info[_INTERNAL_COMMAND] = (db.get_transaction(), context)
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_INTERNAL_COMMAND, None)
        else:
            db.info[_INTERNAL_COMMAND] = previous


def import_context(
    *, actor_user_id, actor_epoch, display_name=None, evidence_id=None, file_sha256=None
) -> CommandContext:
    from app.pn_catalog import api as catalog

    if actor_user_id is None or actor_epoch is None:
        return catalog.legacy_context(
            operated_by=display_name,
            source="excel",
            entrypoint="import_job",
            evidence_id=evidence_id,
        )
    return CommandContext(
        request_id=uuid4(),
        actor=ActorContext(
            principal_id=f"account:{actor_user_id}",
            actor_kind="human",
            client_id="itsp",
            executor_id="itsp-import-worker",
            entrypoint="import_job",
            display_name=display_name,
            issuer="itsp",
            subject=str(actor_user_id),
            authorization_epoch=actor_epoch,
            provenance=Provenance(
                kind="excel", evidence_id=evidence_id, file_sha256=file_sha256
            ),
        ),
    )


def prepare_import_actor(db, context):
    """Take the verified account lock before global fact/import locks.

    Non-PN imports keep their existing permissions. Missing historical identity
    becomes an error only if the file actually reaches a PN mutation below.
    """
    if (
        not is_authoritative(db, "part_master")
        or context.actor.authorization_epoch is None
    ):
        return
    from app.services.pn_catalog_access import resolve_job_access

    resolve_job_access(db, context.actor)


def guard_import(db, context):
    if not is_authoritative(db, "part_master"):
        return {}
    # Source files can create PN identities and enrich source-authoritative fields;
    # arbitrary manufacturer/manual-spec writes are not part of this ingress.
    fields = {
        "description",
        "brand",
        "machine_or_part",
        "unit",
        "category_major",
        "category_minor",
    }
    return {
        "source_create": guard_legacy_write(
            db, context=context, action="create", fields=fields
        ),
        "source_edit": guard_legacy_write(
            db, context=context, action="update_profile", fields=fields
        ),
    }
