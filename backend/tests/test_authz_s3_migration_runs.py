"""Migration approval actors, historical receipts and actual main-router gates."""
from copy import deepcopy
from types import MappingProxyType
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, event

from app.api import maintenance_migration as api
from app.auth import _make_token
from app.authz import runtime
from app.authz.principal import VerifiedIdentity
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models.authorization import SysAuthorizationGrant, SysAuthorizationArtifact
from app.models.maintenance_migration import MaintenanceMigrationRun as Run, MaintenanceMigrationEvent as Event
from app.models.system import SysUser
from app.services import maintenance_migration_runs as runs
from app.services import maintenance_migration_authorization as authorization
from tests.test_maintenance_migration_api import _seed_project, _preview_body, _project_signoffs, _loader, _legacy_loader
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401

BASE = '/api/maintenance/migration-runs'

@pytest.fixture
def setup(db, monkeypatch, cerbos_url):
    settings = get_settings()
    for key, value in {'authorization_mode': 'unified_modules',
        'authz_enabled_resources': ['maintenance_migration'], 'authz_suspended_resources': [],
        'authz_sales_analysis_enabled': False, 'authz_sales_analysis_suspended': False,
        'maintenance_beta_enabled': True, 'cerbos_url': cerbos_url, 'cerbos_timeout_seconds': 3.0}.items():
        monkeypatch.setattr(settings, key, value)
    runtime.activate_configured_resources(db.get_bind())
    _seed_project(db)
    monkeypatch.setattr(api, 'load_project_inventory_movements', _loader)
    monkeypatch.setattr(api, 'load_project_legacy_truth', _legacy_loader)
    app.dependency_overrides[get_db] = lambda: db
    def account(name, actions, role='readonly'):
        user = SysUser(username=name, role=role, password_hash='synthetic')
        db.add(user); db.flush()
        db.add(SysAuthorizationGrant(user_id=user.id, resource='maintenance_migration',
            actions=actions, scope='all', field_permissions=['data_purchase_cost', 'data_profit'], version=1))
        db.commit()
        return user, client_for(user)
    yield account
    app.dependency_overrides.pop(get_db, None)


def client_for(user):
    token, _ = _make_token(user.role, user.username, None, token_version=user.token_version, authn='sys_user')
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer ' + token
    return client


def preview(client):
    response = client.post(BASE + '/preview', json=_preview_body())
    assert response.status_code == 201, response.text
    return response.json()


def reconcile_body(item):
    return {'expected_version': item['version'], 'operation_key': 'reconcile-exact', 'reason': '独立核对',
            'project_signoffs': _project_signoffs(item)}


def test_preview_only_grant_actual_mount_view_has_zero_writes_and_binds_input(db, setup):
    user, client = setup('preview-only', ['preview'])
    response = client.post(BASE + '/preview?view=authorization', json=_preview_body())
    assert response.status_code == 201, response.text
    context = response.json()['authorization_context']
    assert context['context']['object_type'] == 'operation_input'
    assert db.scalar(select(func.count()).select_from(Run)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 0
    item = preview(client)
    assert item['status'] == 'previewed'
    row = db.get(Run, item['run_id'])
    assert row.created_by_user_id == user.id
    binding = db.scalar(select(SysAuthorizationArtifact).where(SysAuthorizationArtifact.domain_kind == 'migration_event'))
    assert binding.owner_user_id == user.id and binding.authorization_provenance is None
    assert client.get(BASE + '/' + item['run_id']).status_code == 403
    assert preview(client)['run_id'] == item['run_id']
    changed = _preview_body(); changed['reason'] = 'changed'
    changed['authorization_precondition'] = {key: context[key] for key in ('catalog_version', 'authorization_epoch', 'context')}
    denied = client.post(BASE + '/preview', json=changed)
    assert denied.status_code == 409, denied.text
    assert db.scalar(select(func.count()).select_from(Run)) == 1


def test_admin_has_no_implicit_action_and_fields_are_complete(db, setup):
    user, client = setup('admin-without-preview', ['read'], role='admin')
    assert client.post(BASE + '/preview', json=_preview_body()).status_code == 403
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == user.id))
    grant.actions = ['preview']; grant.field_permissions = ['data_purchase_cost']; grant.version += 1; db.commit()
    assert client.post(BASE + '/preview', json=_preview_body()).status_code == 403
    assert db.scalar(select(func.count()).select_from(Run)) == 0


def test_renamed_creator_cannot_reconcile_and_reused_name_is_distinct_actor(db, setup):
    creator, client = setup('migration-person', ['preview', 'reconcile'])
    item = preview(client)
    creator.username = 'migration-person-renamed'; db.commit()
    renamed = client_for(creator)
    assert preview(renamed)['run_id'] == item['run_id']
    own = renamed.post(BASE + '/' + item['run_id'] + '/reconcile', json=reconcile_body(item))
    assert own.status_code == 409, own.text
    reviewer, review_client = setup('migration-person', ['reconcile'])
    result = review_client.post(BASE + '/' + item['run_id'] + '/reconcile', json=reconcile_body(item))
    assert result.status_code == 200, result.text
    row = db.get(Run, item['run_id'], populate_existing=True)
    assert row.created_by_user_id == creator.id and row.reconciled_by_user_id == reviewer.id
    assert row.created_by == row.reconciled_by  # Audit names may coincide; stable actors do not.


def test_read_current_grant_no_owner_role_override_and_tampering_fails_closed(db, setup):
    _, creator = setup('artifact-creator', ['preview'])
    item = preview(creator)
    reader, client = setup('artifact-reader', ['read', 'read_project_evidence'])
    response = client.get(BASE + '/' + item['run_id'])
    assert response.status_code == 200, response.text
    searched = client.post(BASE + '/search', json={'statuses': ['previewed']})
    assert searched.status_code == 200 and searched.json()['total'] == 1, searched.text
    evidence = client.get(BASE + '/' + item['run_id'] + '/projects/migration-api-project/evidence',
        params={'section': 'opening_balances', 'page': 1, 'page_size': 50})
    assert evidence.status_code == 200, evidence.text
    row = db.get(Run, item['run_id']); changed = deepcopy(row.preview_json); changed['source_specs'][0]['historical_mode'] = 'stable_site_issues'
    row.preview_json = changed; db.commit()
    assert client.get(BASE + '/' + item['run_id']).status_code == 403


def test_unknown_historical_run_is_not_claimed_by_matching_username(db, setup):
    _, client = setup('old-name', ['read'])
    body = _preview_body()
    item = runs.create_preview_run(db, idempotency_key='old-run', projects=body['projects'], reason=body['reason'],
        operated_by='old-name', warehouse_loader=_loader, legacy_loader=_legacy_loader)
    db.commit()
    assert client.get(BASE + '/' + item['run_id']).status_code == 403
    response = client.post(BASE + '/search', json={})
    assert response.status_code == 200 and response.json()['total'] == 0, response.text


def test_three_independent_actions_approve_without_read_manifest(db, setup):
    creator, first = setup('chain-creator', ['preview'])
    item = preview(first)
    reviewer, second = setup('chain-reviewer', ['reconcile'])
    reconciled = second.post(BASE + '/' + item['run_id'] + '/reconcile', json=reconcile_body(item))
    assert reconciled.status_code == 200, reconciled.text
    approver, third = setup('chain-approver', ['approve'])
    body = {'expected_version': reconciled.json()['version'], 'operation_key': 'approval-exact', 'reason': '独立审批',
        'supplied_fingerprint': reconciled.json()['preview']['input_fingerprint']}
    approved = third.post(BASE + '/' + item['run_id'] + '/approve', json=body)
    assert approved.status_code == 200, approved.text
    chain = approved.json()['manifest']['approval_chain']
    assert [chain[k] for k in ('created_by_user_id', 'reconciled_by_user_id', 'approved_by_user_id')] == [creator.id, reviewer.id, approver.id]
    assert third.post(BASE + '/' + item['run_id'] + '/approve', json=body).status_code == 200
    assert third.get(BASE + '/' + item['run_id'] + '/manifest').status_code == 403
    grant = db.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id == approver.id))
    grant.actions = []; grant.version += 1; db.commit()
    assert third.post(BASE + '/' + item['run_id'] + '/approve', json=body).status_code == 403
    assert db.scalar(select(func.count()).select_from(Event)) == 3


def test_manifest_read_is_repeatable_read_only_and_has_complete_stable_chain(db, setup, monkeypatch):
    _, first = setup('manifest-creator', ['preview'])
    item = preview(first)
    _, second = setup('manifest-reviewer', ['reconcile'])
    reconciled = second.post(BASE + '/' + item['run_id'] + '/reconcile', json=reconcile_body(item))
    assert reconciled.status_code == 200, reconciled.text
    _, third = setup('manifest-approver', ['approve'])
    response = third.post(BASE + '/' + item['run_id'] + '/approve', json={
        'expected_version': reconciled.json()['version'], 'operation_key': 'manifest-approve', 'reason': '独立审批',
        'supplied_fingerprint': reconciled.json()['preview']['input_fingerprint']})
    assert response.status_code == 200, response.text
    _, reader = setup('manifest-reader', ['read_manifest'])
    from app.services import maintenance_migration_warehouse as warehouse
    from app.services import maintenance_migration_legacy as legacy
    from sqlalchemy import text
    observed = []
    def warehouse_facts(snapshot, *args, read_snapshot=None):
        read_snapshot.validate(snapshot)
        observed.append((snapshot.scalar(text('SHOW transaction_isolation')), snapshot.scalar(text('SHOW transaction_read_only'))))
        return _loader(snapshot, *args)
    def legacy_facts(snapshot, *args, read_snapshot=None):
        read_snapshot.validate(snapshot)
        return _legacy_loader(snapshot, *args)
    monkeypatch.setattr(warehouse, 'load_project_inventory_movements', warehouse_facts)
    monkeypatch.setattr(legacy, 'load_project_legacy_truth', legacy_facts)
    statements = []
    def record(conn, cursor, sql, params, context, many):
        statements.append(sql.upper())
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        response = reader.get(BASE + '/' + item['run_id'] + '/manifest')
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)
    assert response.status_code == 200, response.text
    assert observed == [('repeatable read', 'on')]
    assert response.json()['production_activation_included'] is False
    assert not any(sql.lstrip().startswith(('INSERT ', 'UPDATE ', 'DELETE ', 'LOCK TABLE ')) or
                   'FOR UPDATE' in sql or 'FOR SHARE' in sql for sql in statements)
    assert reader.get(BASE + '/' + item['run_id']).status_code == 403


def test_actual_warehouse_legacy_source_readers_accept_only_sealed_same_transaction_proof(db, setup):
    setup('snapshot-fixture', ['read_manifest'])
    from sqlalchemy.orm import Session
    from app.authz.context import read_snapshot
    from app.services.maintenance_migration_warehouse import load_project_inventory_movements
    from app.services.maintenance_migration_legacy import load_project_legacy_truth
    from app.services.maintenance_migration_source import build_project_source_payload
    from app.business_time import business_today
    from types import SimpleNamespace
    commands = []
    def record(conn, cursor, sql, params, context, many):
        commands.append(sql.upper())
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
            project = _preview_body()['projects'][0]
            from datetime import date
            cutover = date.fromisoformat(project['cutover_date'])
            load_project_inventory_movements(snapshot, project['project_id'], cutover, read_snapshot=proof)
            old = load_project_legacy_truth(snapshot, project['project_id'], business_today(), read_snapshot=proof)
            build_project_source_payload(snapshot, project_id=project['project_id'], cutover_date=cutover,
                historical_mode=project['historical_mode'], historical_baseline=project['historical_baseline'],
                opening_balances=project['opening_balances'], warehouse_source_ready=False,
                legacy_truth=old, read_snapshot=proof)
            with pytest.raises(ValueError, match='真实一致读'):
                load_project_legacy_truth(snapshot, project['project_id'], business_today(),
                    read_snapshot=SimpleNamespace(validate=lambda _: None))
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)
    assert not any(sql.lstrip().startswith(('INSERT ', 'UPDATE ', 'DELETE ', 'LOCK TABLE ')) or
                   'FOR UPDATE' in sql or 'FOR SHARE' in sql for sql in commands)


def test_revoked_while_waiting_for_domain_lock_creates_no_run_or_receipt(db, setup):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from fastapi import HTTPException
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
    creator, _ = setup('waiting-preview', ['preview'])
    identity = VerifiedIdentity(creator.username, creator.token_version, False, 'sys_user', MappingProxyType({}), creator.id)
    actor_id = creator.id
    db.rollback()
    body = _preview_body()
    def create_in_connection():
        with Session(db.get_bind()) as worker:
            try:
                value = runs.create_preview_run(worker, idempotency_key=body['idempotency_key'],
                    projects=body['projects'], reason=body['reason'], operated_by=identity.username,
                    warehouse_loader=_loader, legacy_loader=_legacy_loader, identity=identity)
                worker.commit()
                return value
            except HTTPException as exc:
                worker.rollback()
                return exc.status_code
    with Session(db.get_bind()) as blocker:
        blocker.scalar(select(MaintenanceProjectWorkbookState).where(
            MaintenanceProjectWorkbookState.project_id == 'migration-api-project').with_for_update())
        blocker_pid = blocker.scalar(text('SELECT pg_backend_pid()'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(create_in_connection)
            deadline = time.monotonic() + 10
            blocked = False
            try:
                while time.monotonic() < deadline and not future.done():
                    with db.get_bind().connect() as inspector:
                        blocked = inspector.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))'), {'pid': blocker_pid})
                    if blocked:
                        break
                    time.sleep(.02)
                assert blocked, 'Request never reached the actual domain lock'
                with Session(db.get_bind()) as administrator:
                    user = administrator.get(SysUser, actor_id)
                    user.token_version += 1
                    administrator.commit()
            finally:
                blocker.rollback()
            assert future.result(timeout=10) == 401
    assert db.scalar(select(func.count()).select_from(Run)) == 0
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    assert db.scalar(select(func.count()).select_from(SysAuthorizationArtifact)) == 0


def test_denied_migration_read_never_materializes_preview_or_candidate_facts(db,setup):
    user,client=setup('migration-metadata-only',['preview'])
    item=preview(client);columns=[]
    def capture(_conn,cursor,statement,_params,_ctx,_many):
        if cursor.description:columns.extend(str(column[0]) for column in cursor.description)
    event.listen(db.get_bind(),'after_cursor_execute',capture)
    try:
        response=client.get(BASE+'/'+item['run_id'])
        assert response.status_code==403,response.text
    finally:event.remove(db.get_bind(),'after_cursor_execute',capture)
    assert not any(name.endswith(('preview_json','manifest_json','payload_json','historical_cost_amount','opening_amount')) for name in columns),columns
