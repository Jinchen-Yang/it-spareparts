"""Cross-author checks of resource cutover and administrator repair boundaries."""
import pytest
from sqlalchemy import select, func

from app.authz import catalog, runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.models.system import SysAuditLog
from tests.test_authz_s1_adversarial import s1_client, _user, _headers, _json  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401


def test_management_protocol_does_not_prematurely_cut_over_never_enabled_account_resources(db, s1_client, monkeypatch):
    actor = _user(db, 'independent-old-account-admin', 'admin')
    target = _user(db, 'independent-old-account-target')
    headers = _headers(actor)
    assert runtime.canonical_management_required(db)
    states = runtime.runtime_states(db)
    assert states['account_directory'].runtime_state == states['account_profile'].runtime_state == 'legacy'
    snapshot = _json(s1_client.get('/api/authz/me', headers=headers))
    assert snapshot['actor_capabilities']['read_accounts']['effect'] == 'allow'
    assert snapshot['actor_capabilities']['edit_account_profiles']['effect'] == 'allow'
    assert s1_client.get('/api/accounts', headers=headers).status_code == 200
    # S1 canonical wire is still mandatory independently of S4 data cutover.
    assert s1_client.put('/api/accounts/'+target.username, headers=headers,
        json={'display_name':'old wire', 'permissions':{}}).status_code == 422
    body = {'schema_version':1, 'catalog_version':catalog.CATALOG_VERSION,
            'profile':{'display_name':'known target'}}
    assert s1_client.put('/api/accounts/'+target.username, headers=headers, json=body).status_code == 200
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['account_directory'])
    runtime.activate_configured_resources(db.get_bind())
    assert s1_client.get('/api/accounts', headers=headers).status_code == 403
    snapshot = _json(s1_client.get('/api/authz/me', headers=headers))
    assert snapshot['actor_capabilities']['read_accounts']['effect'] == 'deny'
    assert snapshot['actor_capabilities']['edit_account_profiles']['effect'] == 'allow'
    # A directory transition cannot retire an independent never-cutover profile
    # permission; nor can an empty directory grant be repaired through old bools.
    assert s1_client.put('/api/accounts/'+target.username, headers=headers, json=body).status_code == 200
    assert s1_client.get('/api/authz/accounts', headers=headers).status_code == 200


@pytest.mark.parametrize('boundary', ['authorization_admin', 'account_lifecycle', 'resale_catalog', 'framework_documentation'])
def test_boundary_identifier_is_rejected_by_actual_canonical_grant_writer(db, s1_client, boundary):
    admin = _user(db, 'independent-boundary-admin', 'admin')
    target = _user(db, 'independent-boundary-target')
    before_epoch = target.token_version
    before_audit = db.scalar(select(func.count()).select_from(SysAuditLog))
    response = s1_client.put(f'/api/authz/users/{target.username}/modules', headers=_headers(admin), json={
        'schema_version':1, 'catalog_version':catalog.CATALOG_VERSION, 'expected_epoch':before_epoch,
        'updates':[{'resource_id':boundary, 'expected_version':None, 'operation':'replace_resource',
                    'configuration':{'actions':[], 'scope':'all', 'field_permissions':[]}}]})
    assert response.status_code == 422, response.text
    assert response.headers['cache-control'] == 'no-store'
    db.refresh(target)
    assert target.token_version == before_epoch
    assert db.scalar(select(func.count()).select_from(SysAuthorizationGrant).where(
        SysAuthorizationGrant.user_id == target.id)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == before_audit


@pytest.mark.parametrize('bad_source', [None, {'schema_version':1,'resource_overrides':{'unknown':None}},
    {'schema_version':1,'template_snapshot':{'code':{},'version':[]},'resource_overrides':{}}])
def test_bad_source_never_hides_target_or_becomes_empty_permission_summary(db, s1_client, monkeypatch, bad_source):
    admin = _user(db, 'independent-finder-admin', 'admin')
    target = _user(db, 'independent-finder-target', authorization_profile=bad_source)
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['account_directory'])
    runtime.activate_configured_resources(db.get_bind())
    db.add(SysAuthorizationGrant(user_id=admin.id, resource='account_directory', actions=['read'],
                                scope='all', field_permissions=[], version=1)); db.commit()
    finder = _json(s1_client.get('/api/authz/accounts', headers=_headers(admin)))['items']
    ordinary = _json(s1_client.get('/api/accounts', headers=_headers(admin)))
    for rows in (finder, ordinary):
        row = next(item for item in rows if item['id'] == target.id)
        assert row['authorization_summary'] is None
        assert row['authorization_summary_status'] == 'invalid'
        assert 'last_login_at' not in row and 'permissions' not in row
    assert s1_client.get(f'/api/authz/users/{target.username}/modules', headers=_headers(admin)).status_code == 409
