"""S4 platform separation through real HTTP, PostgreSQL and Cerbos."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session

from app.authz import catalog, control_access, management, runtime
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.models.system import SysAccessLog, SysAuditLog, SysUser
from tests.test_authz_s1_adversarial import s1_client, _user, _headers, _json  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def platform(db, s1_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['account_directory', 'account_profile'])
    runtime.activate_configured_resources(db.get_bind())
    return s1_client


def grant(db, user, resource, actions):
    db.add(SysAuthorizationGrant(user_id=user.id, resource=resource, actions=actions,
                                 scope='all', field_permissions=[], version=1))
    db.commit()


def profile_body(name):
    return {'schema_version': 1, 'catalog_version': catalog.CATALOG_VERSION,
            'profile': {'display_name': name}}


def test_admin_has_no_ordinary_bypass_but_can_repair_with_minimal_finder(db, platform):
    admin = _user(db, 'platform-admin', 'admin')
    disabled = _user(db, 'platform-disabled', is_active=False)
    headers = _headers(admin)
    assert platform.get('/api/accounts', headers=headers).status_code == 403
    assert platform.get('/api/accounts/'+disabled.username+'/activity', headers=headers).status_code == 403
    found = _json(platform.get('/api/authz/accounts', headers=headers))
    item = next(x for x in found['items'] if x['id'] == disabled.id)
    assert set(item) == {'id','username','display_name','role','salesperson_name','is_active',
                         'authorization_epoch','authorization_summary','authorization_summary_status'}
    assert item['is_active'] is False and item['authorization_summary_status'] == 'available'
    snapshot = _json(platform.get('/api/authz/me', headers=headers))
    assert snapshot['actor_capabilities']['manage_authorization']['effect'] == 'allow'
    assert snapshot['actor_capabilities']['read_accounts']['effect'] == 'deny'
    assert snapshot['actor_capabilities']['edit_account_profiles']['effect'] == 'deny'
    assert platform.get('/api/accounts/_meta', headers=headers).status_code == 410
    assert platform.get('/api/role-templates', headers=headers).status_code == 200


def test_invalid_source_keeps_real_target_locator_and_specific_diagnostic(db, platform):
    admin = _user(db, 'locator-admin', 'admin')
    broken = _user(db, 'locator-broken', authorization_profile={'invalid': 'secret-untrusted'})
    response = _json(platform.get('/api/authz/accounts', headers=_headers(admin)))
    row = next(x for x in response['items'] if x['id'] == broken.id)
    assert row['username'] == broken.username and row['role'] == broken.role
    assert row['authorization_summary'] is None and row['authorization_summary_status'] == 'invalid'
    assert 'secret-untrusted' not in str(response)
    assert platform.get(f'/api/authz/users/{broken.username}/modules', headers=_headers(admin)).status_code == 409


def test_activity_only_is_independent_and_sensitive_audit_columns_not_selected(db, platform):
    actor = _user(db, 'activity-actor')
    target = _user(db, 'activity-target')
    grant(db, actor, 'account_directory', ['read_activity'])
    db.add(SysAccessLog(username=target.username, action='view', resource='secret-customer',
                        ip_address='127.0.0.1', detail={'secret': 'detail'}))
    db.add(SysAuditLog(entity_type='sys_user', entity_id=target.id, action='changed',
                      operated_by=actor.username, reason='secret-reason',
                      before_json={'secret': 'before'}, after_json={'secret': 'after'}))
    db.commit()
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany): sql.append(statement.lower())
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        result = _json(platform.get(f'/api/accounts/{target.username}/activity', headers=_headers(actor)))
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert result['total_actions'] == 1 and result['recent'][0]['resource'] is None
    assert result['changes'][0]['before'] is None and result['changes'][0]['reason'] is None
    forbidden = ('sys_access_log.resource', 'sys_access_log.detail', 'sys_access_log.user_agent',
                 'sys_audit_log.reason', 'sys_audit_log.before_json', 'sys_audit_log.after_json')
    assert not any(column in statement for column in forbidden for statement in sql)
    assert platform.get('/api/accounts', headers=_headers(actor)).status_code == 403
    assert platform.get('/api/authz/accounts', headers=_headers(actor)).status_code == 403


def test_directory_only_has_closed_projection_and_does_not_unlock_activity(db, platform):
    actor = _user(db, 'directory-actor')
    grant(db, actor, 'account_directory', ['read'])
    result = _json(platform.get('/api/accounts', headers=_headers(actor)))
    assert result and all('permissions' not in r and 'last_login_at' not in r for r in result)
    assert platform.get(f'/api/accounts/{actor.username}/activity', headers=_headers(actor)).status_code == 403
    assert platform.get('/api/role-templates', headers=_headers(actor)).status_code == 403
    snapshot = _json(platform.get('/api/authz/me', headers=_headers(actor)))
    assert snapshot['actor_capabilities']['read_accounts']['effect'] == 'allow'
    assert snapshot['actor_capabilities']['edit_account_profiles']['effect'] == 'deny'


def test_profile_only_known_username_no_directory_or_epoch_bump(db, platform):
    actor = _user(db, 'profile-actor')
    target = _user(db, 'profile-target', token_version=8)
    grant(db, actor, 'account_profile', ['update'])
    result = _json(platform.put(f'/api/accounts/{target.username}', headers=_headers(actor), json=profile_body('new')))
    assert result['subject']['display_name'] == 'new' and result['authorization_epoch'] == 8
    assert platform.get('/api/accounts', headers=_headers(actor)).status_code == 403
    assert platform.put(f'/api/accounts/{target.username}', headers=_headers(actor), json={
        **profile_body('poison'), 'security_change': {'role':'admin'}}).status_code == 403
    db.refresh(target); assert target.display_name == 'new' and target.role == 'readonly'


@pytest.mark.parametrize('withdraw', ['resource', 'mode', 'suspend'])
def test_persistent_cutover_cannot_restore_legacy_account_permission(db, platform, monkeypatch, withdraw):
    actor = _user(db, 'withdraw-'+withdraw, 'admin')
    grant(db, actor, 'account_directory', ['read'])
    assert platform.get('/api/accounts', headers=_headers(actor)).status_code == 200
    settings = get_settings()
    if withdraw == 'resource': monkeypatch.setattr(settings, 'authz_enabled_resources', [])
    elif withdraw == 'suspend': monkeypatch.setattr(settings, 'authz_suspended_resources', ['account_directory'])
    else:
        monkeypatch.setattr(settings, 'authz_enabled_resources', [])
        monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
        monkeypatch.setattr(settings, 'authorization_mode', 'legacy_compat')
    assert platform.get('/api/accounts', headers=_headers(actor)).status_code == 503
    assert platform.get('/api/accounts/_meta', headers=_headers(actor)).status_code == 410
    assert platform.get('/api/authz/accounts', headers=_headers(actor)).status_code == 200


def test_control_api_survives_pdp_failure_ordinary_data_fails_closed(db, platform, monkeypatch):
    actor = _user(db, 'pdp-outage-admin', 'admin')
    grant(db, actor, 'account_directory', ['read'])
    monkeypatch.setattr(get_settings(), 'cerbos_url', 'http://127.0.0.1:1')
    assert platform.get('/api/accounts', headers=_headers(actor)).status_code == 503
    assert platform.get('/api/authz/me', headers=_headers(actor)).status_code == 503
    assert platform.get('/api/authz/accounts', headers=_headers(actor)).status_code == 200


def test_mutual_profile_edits_use_one_sorted_lock_set(db, platform, monkeypatch):
    left = _user(db, 'mutual-left'); right = _user(db, 'mutual-right')
    for actor in (left, right): grant(db, actor, 'account_profile', ['update'])
    rendezvous = Barrier(2)
    original = management.lock_users
    def synchronized(*args, **kwargs):
        rendezvous.wait(timeout=10)
        return original(*args, **kwargs)
    monkeypatch.setattr(management, 'lock_users', synchronized)
    tasks = [(left, right), (right, left)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(platform.put, f'/api/accounts/{target.username}',
                   headers=_headers(actor), json=profile_body('edited')) for actor, target in tasks]
        for future in futures:
            result = future.result(timeout=20)
            assert result.status_code == 200, result.text


@pytest.mark.parametrize('change', ['revoke', 'promote'])
def test_profile_permission_and_target_revalidated_after_preflight(db, platform, monkeypatch, change):
    actor = _user(db, 'race-actor-'+change); target = _user(db, 'race-target-'+change)
    grant(db, actor, 'account_profile', ['update'])
    entered, resume = Event(), Event()
    original = management.lock_users
    def wait_for_change(*args, **kwargs):
        entered.set(); assert resume.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(management, 'lock_users', wait_for_change)
    actor_id, target_id = actor.id, target.id
    headers = _headers(actor); target_name = target.username
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(platform.put, f'/api/accounts/{target_name}', headers=headers, json=profile_body('forbidden'))
        assert entered.wait(10)
        try:
            with Session(db.get_bind()) as writer:
                if change == 'revoke':
                    writer.execute(delete(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == actor_id))
                else:
                    writer.execute(update(SysUser).where(SysUser.id == target_id).values(role='admin'))
                writer.commit()
        finally:
            resume.set()
        result = future.result(timeout=20)
    assert result.status_code == 403, result.text
    db.refresh(target); assert target.display_name is None
    assert db.scalar(select(SysAuditLog.id).where(SysAuditLog.entity_id == target_id,
                     SysAuditLog.action == 'account_profile_update')) is None


def test_boundary_ids_cannot_become_employee_grants(db, platform):
    from app.authz.boundaries import boundary_descriptors, BOUNDARY_ENTRY_CONTRACTS, validate_boundaries
    descriptors = boundary_descriptors()
    assert len(descriptors) == 10 and len(BOUNDARY_ENTRY_CONTRACTS) == 46
    assert validate_boundaries(catalog.RESOURCE_DEFINITIONS)
    for boundary in descriptors:
        with pytest.raises(ValueError): catalog.validate_grant(boundary['id'], [], 'none', [])
    snapshot = _json(platform.get('/api/authz/catalog', headers=_headers(_user(db, 'catalog-admin', 'admin'))))
    assert snapshot['boundaries'] == descriptors
    assert not set(r['id'] for r in snapshot['modules']) & {d['id'] for d in descriptors}


def test_snapshot_reuses_one_pdp_batch_and_denies_old_all_true(db, platform, monkeypatch):
    from app import permissions
    from app.authz import engine
    actor = _user(db, 'one-batch', permissions=dict.fromkeys(permissions.ALL_KEYS, True))
    grant(db, actor, 'account_directory', ['read'])
    grant(db, actor, 'account_profile', ['update'])
    calls = []
    original = engine.check_resources
    def observed(*args, **kwargs):
        calls.append(args[1].keys())
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, 'check_resources', observed)
    before = _json(platform.get('/api/authz/me', headers=_headers(actor)))
    assert len(calls) == 1 and set(calls[0]) == {'account_directory','account_profile'}
    assert before['actor_capabilities']['edit_account_profiles']['effect'] == 'allow'
    db.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == actor.id,
        SysAuthorizationGrant.resource == 'account_profile').values(actions=[])); db.commit()
    calls.clear()
    after = _json(platform.get('/api/authz/me', headers=_headers(actor)))
    assert len(calls) == 1 and after['actor_capabilities']['edit_account_profiles']['effect'] == 'deny'
    assert after['version'] != before['version']
