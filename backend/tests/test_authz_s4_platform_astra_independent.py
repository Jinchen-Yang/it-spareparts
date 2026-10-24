"""Independent partial-cutover and privilege-confusion checks through real HTTP/PDP."""
from sqlalchemy import select

from app.authz import runtime
from app.config import get_settings
from app.models.system import SysAuditLog
from tests.test_authz_s4_platform import platform, grant, profile_body  # noqa: F401
from tests.test_authz_s1_adversarial import s1_client, _user, _headers  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


def test_one_account_resource_cutover_does_not_enable_other_grant_or_erase_old_gate(db, s1_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['account_directory'])
    runtime.activate_configured_resources(db.get_bind())
    actor = _user(db, 'partial-account-actor', permissions={'page_accounts': False, 'action_account_manage': False})
    target = _user(db, 'partial-account-target')
    grant(db, actor, 'account_directory', ['read'])
    grant(db, actor, 'account_profile', ['update'])
    headers = _headers(actor)
    assert s1_client.get('/api/accounts', headers=headers).status_code == 200
    rejected = s1_client.put(f'/api/accounts/{target.username}', headers=headers, json=profile_body('not yet enabled'))
    assert rejected.status_code == 403, rejected.text
    db.refresh(target)
    assert target.display_name is None
    monkeypatch.setattr(settings, 'authz_enabled_resources', ['account_directory', 'account_profile'])
    runtime.activate_configured_resources(db.get_bind())
    accepted = s1_client.put(f'/api/accounts/{target.username}', headers=headers, json=profile_body('enabled'))
    assert accepted.status_code == 200, accepted.text
    db.refresh(target)
    assert target.display_name == 'enabled'


def test_profile_permission_cannot_smuggle_security_or_lifecycle_changes(db, platform):
    actor = _user(db, 'profile-injection-actor')
    target = _user(db, 'profile-injection-target')
    grant(db, actor, 'account_profile', ['update'])
    headers = _headers(actor)
    for injected in ({'role': 'admin'}, {'password': 'should-never-apply'}, {'is_active': False},
                     {'authorization_profile': {'schema_version': 1}}):
        body = profile_body('changed')
        body['profile'].update(injected)
        result = platform.put(f'/api/accounts/{target.username}', headers=headers, json=body)
        assert result.status_code == 422, result.text
    result = platform.put(f'/api/accounts/{target.username}', headers=headers,
        json={**profile_body('changed'), 'security_change': {'role': 'admin'}, 'expected_epoch': 0, 'dry_run': True})
    assert result.status_code == 403, result.text
    db.refresh(target)
    assert target.display_name is None and target.role == 'readonly' and target.is_active
    assert db.scalar(select(SysAuditLog.id).where(SysAuditLog.entity_type == 'sys_user',
        SysAuditLog.entity_id == target.id, SysAuditLog.action == 'account_profile_update')) is None
