"""Preserve valid legacy media references without fabricating physical uniqueness."""

from uuid import UUID

from sqlalchemy import select

from app.pn_catalog.media_models import Derivative, GalleryEntry
from app.pn_catalog.migration import stage_manifest
from app.pn_catalog.migration_dto import MigrationManifest, fingerprint
from app.pn_catalog.migration_materialize import materialize_batch
from tests.test_pn_catalog_migration import (
    allow,
    complete_image_manifest,
    context,
    proof,
)


def test_two_legacy_assets_may_retain_the_same_thumbnail_object(db):
    values = []
    for index in (1, 2):
        value = complete_image_manifest(status="deleted")
        raw = value.model_dump(mode="python")
        row = raw["records"][0]
        row["payload"].update(
            oss_key=f"img/retained-{index}.jpg", sha256=str(index) * 64
        )
        row["payload_sha256"] = fingerprint(row["payload"])
        value = MigrationManifest.model_validate(raw)
        stage_manifest(
            db, value, verified_source=proof(value), context=context(), authorize=allow
        )
        db.commit()
        materialize_batch(db, value.batch_id, context=context(), authorize=allow)
        db.commit()
        values.append(value)
    entries = [
        db.get(GalleryEntry, UUID(value.records[0].source_id)) for value in values
    ]
    derivatives = list(
        db.scalars(
            select(Derivative).where(
                Derivative.asset_id.in_([entry.asset_id for entry in entries])
            )
        )
    )
    assert len(derivatives) == 2
    assert {row.object_key for row in derivatives} == {"original/retained-thumb.jpg"}
    assert len({row.asset_id for row in derivatives}) == 2
