"""Independent regression for remaining legacy authority ingress."""
# ruff: noqa: F811 -- imported pytest fixtures.

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.authorization import SysAuthorizationGrant
from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.pn_catalog.models import PartState
from app.pn_catalog.errors import CatalogError
from tests.test_pn_catalog_access import authority, cerbos_url  # noqa: F401
from tests.test_pn_catalog_legacy_fence import attrs, context


def test_import_derived_specs_require_current_field_grant(db, authority):
    user = authority[0]
    grant = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == user.id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    grant.field_permissions = [
        field for field in grant.field_permissions if field != "specs.capacity"
    ]
    grant.version += 1
    db.commit()
    with pytest.raises(CatalogError) as caught:
        loader._upsert_parts(db, attrs(), False, catalog_context=context(user))
    assert caught.value.code == "auth.forbidden"
    assert (
        db.scalar(select(DimPart.id)) is None
    )  # Rejected before even the parent insert.
    db.rollback()
    assert db.scalar(select(ProductSpec.id)) is None


def hide_capacity(db, user):
    grant = db.scalar(
        select(SysAuthorizationGrant).where(
            SysAuthorizationGrant.user_id == user.id,
            SysAuthorizationGrant.resource == "part_master",
        )
    )
    grant.field_permissions = [
        v for v in grant.field_permissions if v != "specs.capacity"
    ]
    grant.version += 1
    db.commit()


def test_import_auto_spec_removal_also_requires_field_before_any_batch_write(
    db, authority
):
    user = authority[0]
    part = DimPart(pn_std="CUTOVER-PN", description=None)
    db.add(part)
    db.flush()
    db.add(
        ProductSpec(
            part_id=part.id,
            spec_key="capacity",
            spec_value="600GB",
            spec_unit="GB",
            numeric_value=600,
            source="auto",
        )
    )
    db.commit()
    pid = part.id
    hide_capacity(db, user)
    incoming = loader._merge_part_attrs(
        [
            {"pn_std": "A-ALLOWED", "pn_raw": "A-ALLOWED", "description": "no specs"},
            {"pn_std": "CUTOVER-PN", "pn_raw": "CUTOVER-PN", "description": "no specs"},
        ],
        is_sales=False,
    )
    with pytest.raises(CatalogError) as caught:
        loader._upsert_parts(db, incoming, False, catalog_context=context(user))
    assert caught.value.code == "auth.forbidden"
    assert db.get(DimPart, pid, populate_existing=True).description is None
    assert db.scalar(select(DimPart.id).where(DimPart.pn_std == "A-ALLOWED")) is None
    assert (
        db.scalar(select(ProductSpec.spec_value).where(ProductSpec.part_id == pid))
        == "600GB"
    )


@pytest.mark.parametrize(
    "protection", ["manual", "tombstone", "description_lock", "unchanged"]
)
def test_import_preserves_protected_specs_without_requiring_unaffected_field(
    db, authority, protection
):
    user = authority[0]
    part = DimPart(
        pn_std="CUTOVER-PN",
        description="600GB SAS HDD" if protection == "unchanged" else None,
        locked_fields=["description"] if protection == "description_lock" else [],
    )
    db.add(part)
    db.flush()
    if protection in {"manual", "unchanged"}:
        db.add(
            ProductSpec(
                part_id=part.id,
                spec_key="capacity",
                spec_value="800GB",
                spec_unit="GB",
                numeric_value=800,
                source="manual" if protection == "manual" else "auto",
            )
        )
    elif protection == "tombstone":
        db.add(PartState(part_id=part.id, deleted_specs=["capacity"]))
    db.commit()
    pid = part.id
    hide_capacity(db, user)
    loader._upsert_parts(db, attrs(), False, catalog_context=context(user))
    db.commit()
    capacity = db.scalar(
        select(ProductSpec).where(
            ProductSpec.part_id == pid, ProductSpec.spec_key == "capacity"
        )
    )
    if protection in {"manual", "unchanged"}:
        assert capacity.spec_value == "800GB"
    else:
        assert capacity is None
    if protection == "tombstone":
        assert db.get(PartState, pid).deleted_specs == ["capacity"]
    if protection == "description_lock":
        assert db.get(DimPart, pid, populate_existing=True).description is None
