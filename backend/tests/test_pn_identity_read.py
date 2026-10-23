"""Real PostgreSQL selector contract; authentication port injected deliberately."""
# ruff: noqa: F811 - imported pytest fixtures are injected by name.
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductMergeLog
from app.pn_catalog.authz import CatalogAccess
from app.pn_catalog.dto import ActorContext, AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from tests.test_pn_catalog_http import catalog_app, client  # noqa: F401


def _seed(db):
    a, b = DimPart(pn_std="IDENTITY-READ-A"), DimPart(pn_std="IDENTITY-READ-B")
    db.add_all([a, b])
    db.flush()
    first = ProductMergeLog(source_part_id=a.id, target_part_id=b.id,
        affected_records={"secret_fact_ids": [123]}, source_snapshot={"purchase_cost": "secret"})
    second = ProductMergeLog(source_part_id=b.id, target_part_id=a.id)
    db.add_all([first, second])
    db.add_all([PartAlias(pn_raw="ALIAS%_ONE", pn_std=a.pn_std, part_id=a.id, status="active"),
                PartAlias(pn_raw="ALIAS-TWO", pn_std=b.pn_std, part_id=b.id, status="pending")])
    db.commit()
    return a, b, first, second


def test_merge_list_is_bounded_and_does_not_expose_fact_snapshots(client, db):
    _, _, first, second = _seed(db)
    result = client.get("/api/pn-catalog/v1/identity/merge-logs", params={"limit": 1})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["data"][0]["merge_log_id"] == second.id
    assert data["data"][0]["relationship_active"] is False
    assert "secret" not in result.text and "affected_records" not in result.text
    page = client.get("/api/pn-catalog/v1/identity/merge-logs", params={"limit": 1, "cursor": data["meta"]["next_cursor"]})
    assert page.status_code == 200, page.text
    assert page.json()["data"][0]["merge_log_id"] == first.id
    assert page.json()["meta"]["next_cursor"] is None
    assert result.headers["cache-control"] == "no-store"


def test_alias_query_is_literal_and_cursor_is_bound_to_selector(client, db):
    _seed(db)
    result = client.get("/api/pn-catalog/v1/identity/aliases", params={"q": "%_"})
    assert result.status_code == 200, result.text
    assert [row["pn_raw"] for row in result.json()["data"]] == ["ALIAS%_ONE"]
    page = client.get("/api/pn-catalog/v1/identity/aliases", params={"limit": 1}).json()
    assert page["meta"]["next_cursor"]
    wrong_query = client.get("/api/pn-catalog/v1/identity/aliases", params={"cursor": page["meta"]["next_cursor"], "q": "TWO"})
    assert wrong_query.status_code == 422
    wrong_selector = client.get("/api/pn-catalog/v1/identity/merge-logs", params={"cursor": page["meta"]["next_cursor"]})
    assert wrong_selector.status_code == 422


def test_merge_selector_does_not_offer_an_older_cycle_for_same_pair(client, db):
    a, b, first, _ = _seed(db)
    a.status = "merged"
    a.merged_into_id = b.id
    latest = ProductMergeLog(source_part_id=a.id, target_part_id=b.id)
    db.add(latest)
    db.commit()
    response = client.get("/api/pn-catalog/v1/identity/merge-logs")
    assert response.status_code == 200, response.text
    records = {row["merge_log_id"]: row for row in response.json()["data"]}
    assert records[first.id]["relationship_active"] is False
    assert records[latest.id]["relationship_active"] is True


def test_identity_selectors_recheck_current_read_grants_and_fields(client, catalog_app, db):
    _seed(db)
    actor = ActorContext(principal_id="account:9", client_id="itsp", executor_id="http", entrypoint="itsp_ui")
    fields = {"aliases"}
    scope = "all"
    revoked = False
    def authorize(_db, _context, action, _part_id):
        assert action == "manage_identity"
        if revoked:
            raise CatalogError("auth.forbidden", "grant revoked", status_code=403)
        return AuthorizationDecision(frozenset(fields), frozenset(), {"scope": scope})
    catalog_app.state.pn_catalog_access_resolver = lambda *_: CatalogAccess(actor, authorize, b"test-selector-secret")
    assert client.get("/api/pn-catalog/v1/identity/aliases").status_code == 200
    assert client.get("/api/pn-catalog/v1/identity/merge-logs").status_code == 403
    fields.add("identity")
    assert client.get("/api/pn-catalog/v1/identity/merge-logs").status_code == 200
    scope = "selected"
    assert client.get("/api/pn-catalog/v1/identity/aliases").status_code == 403
    scope = "all"
    revoked = True
    assert client.get("/api/pn-catalog/v1/identity/aliases").status_code == 403
