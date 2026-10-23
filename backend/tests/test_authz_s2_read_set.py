"""Shared multi-resource primitive: actual PDP, paired fields and closed requests."""
from copy import deepcopy
from types import MappingProxyType

from fastapi import HTTPException
import pytest

from app.authz import catalog, service
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.models.authorization import SysAuthorizationGrant
from app.security import UserContext, is_field_hidden
from tests.test_authz_s2_b1_adversarial import b1, cerbos_url, subject  # noqa: F401


def _identity(user):
    return VerifiedIdentity(user.username, user.token_version, False, 'sys_user', MappingProxyType({}))


def test_batch_pairs_fields_and_records_only_successful_dependencies(db, b1):
    user, _ = subject(db, 'inventory', ['read'], ['data_purchase_cost'], role='admin')
    db.add(SysAuthorizationGrant(user_id=user.id, resource='purchase_records', actions=['read'],
                                scope='all', field_permissions=['data_supplier'], version=7))
    db.commit()
    recorded = []
    with service.access_recorder(recorded.append):
        reads = service.read_accesses(db, _identity(user), {
            'inventory': ('read', 'adjust'), 'purchase_records': ('read',),
            'purchase_analysis': ('read',)})
    assert {(a.resource, a.action, a.grant_version) for a in recorded} == {
        ('inventory', 'read', 1), ('purchase_records', 'read', 7)}
    assert reads.require('inventory', 'read').field_permissions == {'data_purchase_cost'}
    assert reads.require('purchase_records', 'read').field_permissions == {'data_supplier'}
    assert reads.decision('inventory', 'adjust')['effect'] == 'deny'
    assert reads.get('purchase_analysis', 'read') is None
    with pytest.raises(HTTPException): reads.require('inventory', 'adjust')
    with pytest.raises(ValueError): reads.get('profit_analysis', 'export')
    with pytest.raises(TypeError): reads.accesses[('inventory', 'adjust')] = recorded[0]
    legacy = UserContext(user_id='forged', role='admin', permissions={key: True for key in ('data_supplier','data_profit')})
    ctx = service.field_context(reads.require('inventory', 'read'), legacy)
    assert ctx.user_id == user.username and ctx.role == 'admin'
    assert ctx.explicit_field_policy is True and is_field_hidden(ctx, 'supplier')
    assert is_field_hidden(ctx, 'gross_profit')
    assert not is_field_hidden(ctx, 'unit_cost')


def test_invalid_present_grant_aborts_whole_batch_before_any_record(db, b1):
    user, _ = subject(db, 'inventory', ['read'])
    db.add(SysAuthorizationGrant(user_id=user.id, resource='purchase_records', actions=['read', 7],
                                scope='all', field_permissions=[], version=1))
    db.commit()
    recorded = []
    with service.access_recorder(recorded.append), pytest.raises(HTTPException) as error:
        service.read_accesses(db, _identity(user), {'inventory': ('read',), 'purchase_records': ('read',)})
    assert error.value.status_code == 403 and recorded == []


def test_pdp_failure_aborts_whole_batch_without_partial_allow(db, b1, monkeypatch):
    user, _ = subject(db, 'inventory', ['read'])
    monkeypatch.setattr(get_settings(), 'cerbos_url', 'http://127.0.0.1:1')
    recorded = []
    with service.access_recorder(recorded.append), pytest.raises(HTTPException) as error:
        service.read_accesses(db, _identity(user), {'inventory': ('read',), 'purchase_records': ('read',)})
    assert error.value.status_code == 503 and recorded == []


def test_batch_cannot_treat_customer_scope_as_unrestricted(db, b1):
    user, _ = subject(db, 'inventory', ['read'])
    with pytest.raises(ValueError):
        service.read_accesses(db, _identity(user), {'sales_analysis': ('read_orders',)})


def test_recorder_restores_previous_owner_after_exception(db, b1):
    user, _ = subject(db, 'inventory', ['read'])
    outer, inner = [], []
    with service.access_recorder(outer.append):
        with pytest.raises(RuntimeError), service.access_recorder(inner.append):
            service.resource_access(db, _identity(user), 'inventory', 'read')
            raise RuntimeError('synthetic')
        service.resource_access(db, _identity(user), 'inventory', 'read')
    service.resource_access(db, _identity(user), 'inventory', 'read')
    assert len(outer) == len(inner) == 1


@pytest.mark.parametrize('dependency', ['requires_actions', 'requires_any_actions'])
def test_self_referential_catalog_dependency_fails_closed(monkeypatch, dependency):
    descriptors = deepcopy(catalog.resource_descriptors())
    resource = next(r for r in descriptors if r['id'] == 'inventory')
    action = next(a for a in resource['actions'] if a['id'] == 'read')
    action[dependency] = ['read']
    monkeypatch.setattr(catalog, '_DESCRIPTORS', descriptors)
    with pytest.raises(ValueError, match='环'):
        catalog.validate_catalog()
