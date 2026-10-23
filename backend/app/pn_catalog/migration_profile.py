"""Explicit conversion of archived Shop PN profiles, without manufacturing confirmation."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.models.master_data import ProductSpec
from app.services.spec_extract import SPEC_KEYS, validate_manual_specs
from . import api
from .attribute_dto import validate_attributes
from .attribute_models import AttributeState, AttributeValue
from .dto import FIELD_LIMITS
from .errors import CatalogError
from .persistence import lock_part, snapshot, state_for_write


class LegacyProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    brand: str = ""
    manufacturer: str = ""
    category_major: str = ""
    category_minor: str = ""
    summary: str = ""
    specs: dict = Field(default_factory=dict)
    manual_catalog: dict = Field(default_factory=dict)
    field_sources: dict = Field(default_factory=dict)
    fill_status: str = "pending"
    catalog_active: bool = True


def _origin(row, key, *, manual=False):
    source = row.payload.get("field_sources", {}).get(key, {})
    kind = (
        "manual"
        if manual
        else {"itsp": "upstream", "legacy": "unknown"}.get(
            source.get("source"), source.get("source", "unknown")
        )
    )
    if kind not in {"manual", "ai", "excel", "upstream", "unknown"}:
        kind = "unknown"
    return {
        "kind": kind,
        "source_namespace": row.namespace,
        "source_record_id": row.source_id,
        "source_sha256": row.payload_sha256,
        "source_field": key,
        "historical_evidence": source,
        "principal_id": None,
        "confirmed_by": None,
    }


def _conflict(fields):
    raise CatalogError(
        "migration.profile_conflict",
        "目标已有不同的生效资料，需显式核对",
        details={"fields": sorted(fields)},
    )


def _plan_specs(source):
    # Canonical numeric semantics are imported only when explicit validated source
    # details exist. Otherwise the original JSON type lives in attributes.
    specs, attributes, tombstones = {}, dict(source.specs), set()
    manual = source.manual_catalog.get("specs", {})
    if not isinstance(manual, dict):
        raise ValueError("manual specs must be an object")
    for key, value in manual.items():
        if value is None or value == "":
            tombstones.add(key)
            attributes[key] = None
        else:
            attributes[key] = value
    for key, value in list(attributes.items()):
        if key not in SPEC_KEYS:
            continue
        if key in tombstones:
            specs[key] = None
            attributes.pop(key)
            continue
        details = source.field_sources.get(f"specs.{key}", {})
        if SPEC_KEYS[key][1] is None and type(value) is str:
            details = {**details, "unit": None, "numeric_value": None}
        if type(value) is str and "unit" in details:
            candidate = {
                "value": value,
                "unit": details["unit"],
                "numeric_value": details.get("numeric_value"),
            }
            try:
                valid = validate_manual_specs({key: candidate})[key]
            except ValueError:
                continue
            if valid["spec_value"] != value:
                continue  # Preserve whitespace/type exactly in the extension.
            specs[key] = valid
            attributes.pop(key)
    validate_attributes(attributes)
    return specs, attributes, tombstones, set(manual)


def materialize_profile(db, row, part_id, *, context, authorization):
    try:
        source = LegacyProfile.model_validate(row.payload)
        if UUID(source.id) != row.source_pn_id or source.id != row.source_id:
            raise ValueError("profile identity mismatch")
        if source.fill_status not in {
            "pending",
            "ai_filled",
            "confirmed",
            "needs_review",
        }:
            raise ValueError("unknown profile fill status")
        changes = {}
        manual = source.manual_catalog
        for key in (
            "brand",
            "manufacturer",
            "category_major",
            "category_minor",
            "summary",
        ):
            value = manual[key] if key in manual else getattr(source, key)
            if value is not None and not isinstance(value, str):
                raise ValueError("profile field requires text")
            path = "description" if key == "summary" else key
            if value is not None and len(value) > FIELD_LIMITS[path]:
                raise ValueError("legacy profile exceeds shared field limit")
            if value or key in manual:
                changes[path] = value or None
        specs, attributes, tombstones, manual_specs = _plan_specs(source)
    except (ValueError, ValidationError, TypeError):
        raise CatalogError(
            "migration.invalid_profile", "历史 PN 字段形态需核对", status_code=422
        ) from None
    part = lock_part(db, part_id)
    state = state_for_write(db, part_id)
    before = snapshot(db, part, state)
    conflicts = []
    to_write = {}
    for key, value in changes.items():
        current = before["profile"][key]
        if current == value:
            continue
        if (
            current is not None
            or key in before["locked_fields"]
            or before["field_versions"].get(key, 0)
        ):
            conflicts.append(key)
        else:
            to_write[key] = value
    current_specs = {
        value.spec_key: value
        for value in db.scalars(
            select(ProductSpec).where(ProductSpec.part_id == part_id)
        )
    }
    spec_writes, spec_promotions = {}, set()
    for key, value in specs.items():
        current = current_specs.get(key)
        same = (current is None and key in state.deleted_specs and value is None) or (
            current is not None
            and value is not None
            and current.spec_value == value["spec_value"]
            and current.spec_unit == value["spec_unit"]
            and current.numeric_value == value["numeric_value"]
        )
        if same:
            if (
                current is not None
                and key in manual_specs
                and current.source != "manual"
            ):
                spec_promotions.add(key)
            continue
        if (
            current is not None
            or key in state.deleted_specs
            or state.field_versions.get(f"specs.{key}", 0)
        ):
            conflicts.append(f"specs.{key}")
        else:
            spec_writes[key] = value
    current_attrs = {
        value.key: value
        for value in db.scalars(
            select(AttributeValue).where(AttributeValue.part_id == part_id)
        )
    }
    attr_writes = {}
    for key, value in attributes.items():
        deleted = key in tombstones
        current = current_attrs.get(key)
        if current:
            if (
                api._json(current.value) != api._json(value)
                or current.deleted != deleted
            ):
                conflicts.append(f"attributes.{key}")
            elif key in manual_specs and not current.protected:
                attr_writes[key] = (value, deleted)
        else:
            attr_writes[key] = (value, deleted)
    try:
        validate_attributes(
            {**{key: value.value for key, value in current_attrs.items()}, **attributes}
        )
    except ValueError:
        raise CatalogError(
            "migration.attribute_limit", "合并后的自定义参数超过边界", status_code=422
        ) from None
    if state.fill_status is not None and state.fill_status != source.fill_status:
        conflicts.append("fill_status")
    target_availability = "active" if source.catalog_active else "inactive"
    if state.availability_revision and state.availability != target_availability:
        conflicts.append("availability")
    if conflicts:
        _conflict(conflicts)
    # This path deliberately does not run description extraction or auto-label
    # every legacy field manual. Existing upstream facts must retain their origin.
    from app.services.master_data import bind_part_dictionaries

    for key, value in to_write.items():
        setattr(state if key == "manufacturer" else part, key, value)
    bind_part_dictionaries(
        db, part, {k: v for k, v in to_write.items() if k != "manufacturer"}
    )
    protected = {
        "description" if key == "summary" else key for key in manual if key != "specs"
    }
    part.locked_fields = sorted(
        set(part.locked_fields or []) | (protected & set(changes))
    )
    if protected & {"category_major", "category_minor"}:
        part.locked_fields = sorted(
            set(part.locked_fields) | {"category_major", "category_minor"}
        )
        part.category_source = "MANUAL"
    for key in spec_promotions:
        current_specs[key].source = "manual"
    for key, value in spec_writes.items():
        if value is not None:
            db.add(
                ProductSpec(
                    part_id=part_id,
                    spec_key=key,
                    spec_value=value["spec_value"],
                    spec_unit=value["spec_unit"],
                    numeric_value=value["numeric_value"],
                    source="manual" if key in manual_specs else "auto",
                )
            )
    result = api.record_legacy_change(
        db,
        part_id,
        before,
        context=context,
        action="migration.profile",
        deleted_spec_keys=[k for k, v in spec_writes.items() if v is None],
        force_fields=set(to_write) | {f"specs.{k}" for k in spec_writes},
        authorization=authorization,
        payload_hash=row.payload_sha256,
    )
    origins = dict(state.field_provenance)
    for key in set(to_write) | (
        (protected - set(before["locked_fields"])) & set(changes)
    ):
        source_key = "summary" if key == "description" else key
        origins[key] = _origin(row, source_key, manual=source_key in manual)
    for key in set(spec_writes) | spec_promotions:
        origins[f"specs.{key}"] = _origin(
            row, f"specs.{key}", manual=key in manual_specs
        )
    state.field_provenance = origins
    metadata_changes, revisions = {}, {"before": {}, "after": {}}
    if state.fill_status is None:
        revisions["before"]["fill_status"] = state.fill_status_revision
        state.fill_status, state.fill_status_revision = (
            source.fill_status,
            state.fill_status_revision + 1,
        )
        state.field_provenance = {
            **state.field_provenance,
            "fill_status": _origin(row, "fill_status"),
        }
        metadata_changes["fill_status"] = {"before": None, "after": source.fill_status}
        revisions["after"]["fill_status"] = state.fill_status_revision
    if state.availability != target_availability:
        revisions["before"]["availability"] = state.availability_revision
        metadata_changes["availability"] = {
            "before": state.availability,
            "after": target_availability,
        }
        state.availability, state.availability_revision = (
            target_availability,
            state.availability_revision + 1,
        )
        revisions["after"]["availability"] = state.availability_revision
    if attr_writes:
        attrs = db.get(AttributeState, part_id)
        if attrs is None:
            attrs = AttributeState(part_id=part_id, revision=0)
            db.add(attrs)
            db.flush()
        revisions["before"]["attributes"] = attrs.revision
        for key, (value, deleted) in attr_writes.items():
            attribute = current_attrs.get(key)
            if attribute is None:
                attribute = AttributeValue(part_id=part_id, key=key, version=0)
                db.add(attribute)
            attribute.value, attribute.deleted = value, deleted
            attribute.version += 1
            attribute.protected = key in manual_specs
            attribute.provenance = _origin(
                row, f"specs.{key}", manual=key in manual_specs
            )
        attrs.revision += 1
        revisions["after"]["attributes"] = attrs.revision
        metadata_changes["attributes"] = {
            key: {"value": value, "deleted": deleted}
            for key, (value, deleted) in attr_writes.items()
        }
    if metadata_changes:
        api.record_audit(
            db,
            part_id,
            context=context,
            action="migration.profile_metadata",
            changes=metadata_changes,
            before_revision=0,
            after_revision=1,
            authorization=authorization,
            payload_hash=row.payload_sha256,
            revisions={"axis": "profile_metadata", **revisions},
        )
    db.flush()
    return {
        "part_id": part_id,
        "profile_revision": state.profile_revision,
        "fill_status_revision": state.fill_status_revision,
        "audit_id": result["audit_id"],
    }
