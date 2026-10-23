"""Business identity/facts survive cutover while live master fields cannot bypass it."""
from sqlalchemy import select

from app.api import parts
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.services.pn_catalog_projection import project_business_master
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_pn_catalog_access import authority  # noqa: F401
from tests.test_pn_catalog_legacy_http import client, headers, seed


def test_master_mask_does_not_rewrite_historical_document_facts():
    source = {"part": {"id": 1, "pn_std": "PN", "description": "current", "brand": "hidden", "locked_fields": ["brand"]},
              "substitutes": [{"pn_std": "SUB", "description": "current substitute", "stock_qty": 3}],
              "sales_recent": [{"description": "original sale text", "qty": 2, "unit_cost": 5}],
              "inventory": {"description": "original snapshot", "qty": 8}}
    result = project_business_master(source, set(), runtime_state="enabled", availability="restricted")
    assert result["part"]["description"] is None
    assert result["part"]["locked_fields"] == []
    assert result["substitutes"][0] == {"pn_std": "SUB", "description": None, "stock_qty": 3}
    assert result["sales_recent"] == source["sales_recent"]
    assert result["inventory"] == source["inventory"]
    assert source["part"]["description"] == "current"


def test_business_search_and_overview_intersect_current_master_grant(db, authority):  # noqa: F811
    user, admin, _ = authority
    pid = seed(db, "BUSINESS-MASK")
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user.id,
        SysAuthorizationGrant.resource == "part_master"))
    grant.actions = ["read"]
    grant.field_permissions = ["brand"]
    db.commit()
    for url, key in (("/api/parts/search?q=BUSINESS-MASK", "items"),
                     ("/api/parts/search?browse=true&q=BUSINESS-MASK", "items"),
                     (f"/api/parts/overview?part_id={pid}", "part")):
        response = client.get(url, headers=headers(user))
        assert response.status_code == 200, response.text
        body = response.json()
        row = body[key][0] if key == "items" else body[key]
        assert row["pn_std"] == "BUSINESS-MASK"
        assert row["brand"] == "old"
        assert row["description"] is None
        assert body["shared_master"]["readable_fields"] == ["brand"]
        assert response.headers["Cache-Control"] == "no-store"
    # Existing business admin stays allowed to retrieve identity/facts, but has no new grant.
    response = client.get(f"/api/parts/overview?part_id={pid}", headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["part"]["brand"] is None
    assert response.json()["shared_master"]["status"] == "restricted"


def test_suspension_masks_live_fields_without_legacy_fallback(db, authority, monkeypatch):  # noqa: F811
    user, _, _ = authority
    pid = seed(db, "BUSINESS-SUSPENDED")
    monkeypatch.setattr(get_settings(), "authz_enabled_resources", [])
    response = client.get(f"/api/parts/overview?part_id={pid}", headers=headers(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["part"]["pn_std"] == "BUSINESS-SUSPENDED"
    assert body["part"]["description"] is None
    assert body["part"]["brand"] is None
    assert body["shared_master"] == {"runtime_state": "suspended", "status": "unavailable", "readable_fields": []}


def test_resolver_cannot_repeat_hidden_master_text_in_matches(db, authority, monkeypatch):  # noqa: F811
    _, admin, _ = authority
    monkeypatch.setattr(parts.part_resolver, "resolve", lambda *_args, **_kw: {
        "items": [{"part_id": 1, "pn_std": "PN", "description": "private", "brand": "private",
                   "match_type": "description", "matched_text": "private", "match_reason": "private",
                   "specs": {"capacity": "1TB"}}],
        "similar_items": [{"part_id": 2, "pn_std": "SIM", "description": "private"}],
        "exact": False, "low_confidence": False, "ambiguous": False})
    response = client.get("/api/parts/search?q=PN", headers=headers(admin))
    assert response.status_code == 200, response.text
    assert "private" not in response.text
    assert response.json()["items"][0]["specs"] == {}
