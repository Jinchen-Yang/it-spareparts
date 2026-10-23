"""Exact lookup is bounded, read-only and never promotes fuzzy or pending matches."""
# ruff: noqa: F811
import json

import pytest
from sqlalchemy import event, select, text

from app.models.dimensions import DimPart, PartAlias
from app.models.authorization import SysAuthorizationGrant
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.lookup import lookup_parts
from app.pn_catalog.models import PartState
from tests.test_pn_catalog_media import context
from tests.test_pn_catalog_references import allow
from tests.test_pn_catalog_access import authority, cerbos_url  # noqa: F401
from tests.test_pn_catalog_authority_http import http, native, exchange  # noqa: F401


def test_exact_lookup_normalization_alias_merge_ambiguity_and_missing(db):
    parts = [DimPart(pn_std=pn) for pn in ("A-12", "A12", "MANUAL", "MANUAL V00000000000", "OLD", "INACTIVE")]
    db.add_all(parts)
    db.flush()
    ids = [p.id for p in parts]
    parts[4].status, parts[4].merged_into_id = "merged", ids[0]
    db.add(PartState(part_id=ids[-1], availability="inactive"))
    db.add_all([
        PartAlias(pn_raw="alias", pn_std="A-12", part_id=ids[0], status="active"),
        PartAlias(pn_raw="pending", pn_std="A12", part_id=ids[1], status="pending"),
        PartAlias(pn_raw="rejected", pn_std="A12", part_id=ids[1], status="rejected"),
    ])
    db.commit()
    db.execute(text("SET TRANSACTION READ ONLY"))
    result = lookup_parts(db, ["  a-12 ", "alias", "pending", "rejected", "MANUAL V00000000000", "OLD", "A-1", "INACTIVE"], context=context(), authorize=allow)["data"]
    assert [r["status"] for r in result] == ["matched", "matched", "ambiguous", "unresolved", "ambiguous", "matched", "unresolved", "unresolved"]
    assert result[0]["matches"] == result[1]["matches"] == result[5]["matches"]
    assert result[0]["normalized_pn"] == "A-12"
    assert len(result[4]["matches"]) == 2
    assert result[4]["normalized_pn"] == "MANUAL V00000000000"
    assert result[4]["import_normalized_pn"] == "MANUAL"


def test_lookup_batch_does_not_query_per_input_and_has_strict_bounds(db):
    db.add_all([DimPart(pn_std=f"LOOK-{i}") for i in range(100)])
    db.commit()
    seen = []
    def capture(_conn, _cursor, statement, *_rest):
        seen.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        result = lookup_parts(db, [f"look-{i}" for i in range(100)], context=context(), authorize=allow)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert len(seen) == 3  # topology + exact PN + active/pending aliases
    assert all(row["status"] == "matched" for row in result["data"])
    for raw in ([], [" "], ["a"] * 101, ["x"] * 0, ["a" * 129]):
        with pytest.raises(CatalogError):
            lookup_parts(db, raw, context=context(), authorize=allow)


def test_global_access_does_not_require_gallery_or_an_existing_pn(db, authority, http):
    gallery = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == authority[0].id,
                                                          SysAuthorizationGrant.resource == "part_gallery"))
    db.delete(gallery)
    db.commit()
    response = http.get("/api/pn-catalog/v1/access", headers=native(authority[0]))
    assert response.status_code == 200, response.text
    value = response.json()["data"]
    assert "read" in value["allowed_actions"]
    assert "gallery.read" not in value["allowed_actions"]
    assert "gallery" not in value["readable_fields"]
    assert "part_id" not in value
    denied = http.get("/api/pn-catalog/v1/access", headers=native(authority[1]))
    assert denied.status_code == 403
    path = "/api/pn-catalog/v1/parts/lookup"
    body = json.dumps({"raw_values": ["UNKNOWN"]}).encode()
    headers, _ = exchange(http, authority[2], "POST", path, body)
    found = http.post(path, content=body, headers={**headers, "content-type": "application/json"})
    assert found.status_code == 200, found.text
    assert found.json()["data"][0]["status"] == "unresolved"
