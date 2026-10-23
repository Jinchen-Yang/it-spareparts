"""Cutover closes old writes; queued imports retain real revocable authority."""

# Ruff: fixture injection intentionally shadows the imported fixture.
# ruff: noqa: F811
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.etl import loader
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import AuditEvent
from app.services import master_data, master_edit, part_rename, spec_extract
from app.services.pn_catalog_legacy_guard import import_context
from tests.test_pn_catalog_access import authority, cerbos_url  # noqa: F401


def context(user):
    return import_context(
        actor_user_id=user.id,
        actor_epoch=user.token_version,
        display_name=user.username,
        evidence_id="import_batch:1",
    )


def attrs():
    return loader._merge_part_attrs(
        [
            {
                "pn_std": "CUTOVER-PN",
                "pn_raw": "CUTOVER-PN",
                "description": "600GB SAS HDD",
            }
        ],
        is_sales=False,
    )


@pytest.mark.parametrize(
    "writer",
    [
        lambda db: master_edit.create_part_in_transaction(
            db, pn_std="BYPASS", force=True
        ),
        lambda db: master_edit.edit_part_in_transaction(
            db, pn_std="BYPASS", updates={"brand": "x"}
        ),
        lambda db: master_data.refresh(db),
        lambda db: master_data.classify_backfill(db),
        lambda db: spec_extract.backfill_specs(db),
        lambda db: part_rename.rename_part_in_transaction(
            db, 1, "BYPASS", None, "admin"
        ),
        lambda db: loader._upsert_parts(db, attrs(), False),
    ],
)
def test_cutover_blocks_display_name_or_anonymous_legacy_writers(db, authority, writer):
    with pytest.raises(CatalogError) as exc:
        writer(db)
    assert exc.value.code == "catalog.legacy_writer_retired"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_source_ingest_has_original_actor_permission_evidence_and_atomic_derived_specs(
    db, authority
):
    user, _, _ = authority
    ctx = context(user)
    result = loader._upsert_parts(db, attrs(), False, catalog_context=ctx)
    db.flush()
    event = db.scalar(select(AuditEvent))
    assert event.actor["authorization_epoch"] == 4
    assert event.actor["issuer"] == "itsp"
    assert event.actor["provenance"]["kind"] == "excel"
    assert event.authorization["source_edit"]["decisions"]["None"]["grant_version"] == 1
    from app.models.master_data import ProductSpec

    assert (
        db.scalar(
            select(ProductSpec.spec_value).where(
                ProductSpec.part_id == result["CUTOVER-PN"],
                ProductSpec.spec_key == "capacity",
            )
        )
        == "600GB"
    )
    db.rollback()
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0
    assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_original_import_epoch_is_not_refreshed_when_account_reauthorized(
    db, authority
):
    user, _, _ = authority
    batch = SysImportBatch(
        filename="original.xlsx",
        file_type="sales",
        file_hash=str(uuid4()),
        actor_user_id=user.id,
        actor_authorization_epoch=user.token_version,
    )
    db.add(batch)
    db.commit()
    ctx = loader._catalog_import_context(db, batch.id, user.username)
    user.token_version += 1
    db.commit()
    with pytest.raises(CatalogError) as exc:
        loader._upsert_parts(db, attrs(), False, catalog_context=ctx)
    assert exc.value.code == "auth.session_revoked"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0


def test_source_import_cannot_write_hidden_fields(db, authority):
    user, _, _ = authority
    grant = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == user.id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    grant.field_permissions = [v for v in grant.field_permissions if v != "brand"]
    db.commit()
    with pytest.raises(CatalogError) as exc:
        loader._upsert_parts(db, attrs(), False, catalog_context=context(user))
    assert exc.value.code == "auth.forbidden"
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0


def test_suspended_cutover_does_not_reopen_legacy_writer(db, authority, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "authz_enabled_resources", [])
    with pytest.raises(CatalogError) as exc:
        master_edit.create_part_in_transaction(db, pn_std="BYPASS", force=True)
    assert exc.value.code == "catalog.legacy_writer_retired"
    with pytest.raises(CatalogError):
        loader._upsert_parts(db, attrs(), False, catalog_context=context(authority[0]))
    assert db.scalar(select(func.count()).select_from(DimPart)) == 0
