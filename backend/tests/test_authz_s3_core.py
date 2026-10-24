"""Real PDP batching, contextual provenance and transaction-bound evidence."""
from types import SimpleNamespace
from dataclasses import replace
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.authz import catalog, engine, provenance, service
from app.authz.context import ObjectRef, read_snapshot
from app.authz.principal import Principal, load_principal, resume_task_identity
from app.models.system import SysUser
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401

def test_canonical_fields_do_not_inherit_legacy_global_profit_cost_dependency(monkeypatch):
    from app import config
    from app.security import UserContext, apply_field_visibility, is_field_hidden
    monkeypatch.setattr(config, 'ENABLE_RBAC', True)
    field = config.FIELD_GROUPS['profit_amount'][0]
    cost = config.FIELD_GROUPS['purchase_cost'][0]
    canonical = UserContext('user', 'admin', permissions={'data_profit': True},
                            explicit_field_policy=True)
    assert apply_field_visibility({field: 12, cost: 8}, canonical) == {field: 12, cost: config.MASK_VALUE}
    assert not is_field_hidden(canonical, field)
    # The old template protocol retains its dependency until it is migrated.
    assert is_field_hidden(replace(canonical, explicit_field_policy=False), field)
    assert is_field_hidden(replace(canonical, permissions=None), field)

@pytest.fixture
def configured(monkeypatch, cerbos_url):
    monkeypatch.setattr(engine, 'get_settings', lambda: SimpleNamespace(cerbos_url=cerbos_url, cerbos_timeout_seconds=3.0))

@pytest.mark.parametrize('count', [0, 1, 50, 51, 101])
def test_object_batch_boundaries_with_real_engine(configured, monkeypatch, count):
    original = engine._client
    calls = []
    class Observed:
        def __enter__(self):
            self.client = original().__enter__(); return self
        def __exit__(self, *args): return self.client.__exit__(*args)
        def check_resources(self, principal, resources):
            calls.append(resources); return self.client.check_resources(principal, resources)
    monkeypatch.setattr(engine, '_client', Observed)
    grant = engine.Grant(('read',), 'assigned_or_sales_projects', (), 1, resource='maintenance_project')
    checks = [engine.ObjectCheck(str(i), grant.resource, ('read',), {'is_manager': i % 3 == 0,
        'is_viewer': i % 3 == 1, 'is_sales': False, 'is_active': True}) for i in range(count)]
    result = engine.check_objects(1, {grant.resource: grant} if checks else {}, checks)
    assert len(result) == count and len(calls) == (count + 49) // 50
    assert {key for key, d in result.items() if 'read' in d.allowed_actions} == {str(i) for i in range(count) if i % 3 != 2}

def test_full_module_snapshot_exceeds_default_engine_limit_safely(configured):
    grants = {r: engine.Grant(tuple(sorted(d.actions)), d.denial_scope,
        tuple(sorted(d.field_permissions)), 1, resource=r) for r, d in catalog.RESOURCE_DEFINITIONS.items()}
    assert len(grants) > 50
    decisions = engine.check_resources(1, grants, {r: {} for r in grants}, {r: g.actions for r, g in grants.items()})
    assert set(decisions) == set(grants)
    assert all(d.allowed_actions == set(grants[r].actions) for r, d in decisions.items())

def test_second_real_batch_failure_returns_no_partial_result(configured, monkeypatch):
    original = engine._client
    calls = []
    class BrokenSecond:
        def __enter__(self):
            self.client = original().__enter__(); return self
        def __exit__(self, *args): return self.client.__exit__(*args)
        def check_resources(self, principal, resources):
            response = self.client.check_resources(principal, resources); calls.append(response)
            if len(calls) == 2: response.results.clear()
            return response
    monkeypatch.setattr(engine, '_client', BrokenSecond)
    grant = engine.Grant(('read',), 'all', (), 1, resource='inventory')
    checks = [engine.ObjectCheck(str(i), 'inventory', ('read',), {}) for i in range(51)]
    with pytest.raises(engine.AuthorizationUnavailable, match='批量响应'):
        engine.check_objects(1, {'inventory': grant}, checks)
    assert len(calls) == 2

def test_read_proof_cannot_move_session_or_outlive_transaction(db):
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        proof.validate(snapshot)
        with pytest.raises(HTTPException): proof.validate(db)
    with pytest.raises(HTTPException): proof.validate(snapshot)

def test_worker_follows_stable_id_and_rejects_name_reuse(db):
    original = SysUser(username='core-worker', role='readonly', password_hash='synthetic')
    db.add(original); db.commit()
    old = resume_task_identity(db, original.id, original.token_version)
    original.username = 'core-renamed'; db.commit()
    replacement = SysUser(username='core-worker', role='readonly', password_hash='synthetic')
    db.add(replacement); db.commit()
    with pytest.raises(HTTPException): load_principal(db, old)
    current = resume_task_identity(db, original.id, original.token_version)
    assert load_principal(db, current).id == original.id and current.username == 'core-renamed'
    original.token_version += 1; db.commit()
    with pytest.raises(HTTPException): load_principal(db, current)

def contextual_access(ref, *, revision='relation-v1'):
    principal = Principal(1, 'named', 2, 'readonly', None, None, frozenset())
    return service.ResourceAccess('maintenance_project', 'read', principal, 2, 3, catalog.CATALOG_VERSION,
        'assigned_or_sales_projects', frozenset(), None, ref.id, revision, ref,
        'GET /api/maintenance/projects/stable/{project_id}', 'default')

def test_provenance_preserves_each_actual_carrier_and_forbids_downgrade():
    collector = provenance.AccessCollector(1, 2, catalog.CATALOG_VERSION)
    one, two = ObjectRef('project', 'A'), ObjectRef('project', 'B')
    collector.record(contextual_access(one)); collector.record(contextual_access(two))
    saved = collector.snapshot()
    assert saved['schema_version'] == 2 and len(saved['resources']) == 2
    assert {r['object_ref']['id'] for r in saved['resources']} == {'A', 'B'}
    downgrade = {**saved, 'schema_version': 1, 'resources': [
        {k: row[k] for k in ('resource_id', 'actions', 'grant_version')} for row in saved['resources']]}
    with pytest.raises(HTTPException): provenance.validate_provenance_shape(downgrade)
    with pytest.raises(HTTPException): collector.record(contextual_access(one, revision='changed'))

def test_transient_input_cannot_be_saved_as_historical_authority():
    ref = ObjectRef('operation_input', 'actual-sha', {'purpose': 'exact-command'})
    collector = provenance.AccessCollector(1, 2, catalog.CATALOG_VERSION)
    collector.record(contextual_access(ref))
    with pytest.raises(HTTPException): collector.snapshot()

def test_context_objects_are_deeply_immutable_and_reject_nonfinite_selectors():
    value = {'purpose': 'registered', 'selection': {'ids': ['A'], 'ratio': 0.25}}
    ref = ObjectRef('operation_input', 'command', value)
    value['selection']['ids'].append('B')
    assert ref.selector['selection']['ids'] == ('A',)
    with pytest.raises(TypeError): ref.selector['selection']['ratio'] = 1
    for number in [float('nan'), float('inf')]:
        with pytest.raises(ValueError): ObjectRef('operation_input', 'command', {'value': number})
