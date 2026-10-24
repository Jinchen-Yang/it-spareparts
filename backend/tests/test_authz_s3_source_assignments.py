"""Actual source directory SQL scopes, sorting oracle and source write isolation."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.authz import runtime
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from tests.test_authz_s3_checklist import setup, cerbos_url  # noqa: F401
from tests.test_maintenance_project_master_v2_editable import _make_project_with_line


@pytest.fixture
def sources(db, setup, monkeypatch):
    from app.api import maintenance_source_assignments as api
    uid, identity, pid, _, headers = setup
    monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['maintenance_source_assignment', 'maintenance_source_assignment_write'])
    runtime.activate_configured_resources(db.get_bind())
    project, _, order, _ = _make_project_with_line(db)
    app = FastAPI()
    app.include_router(api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.headers.update(headers)
    return uid, pid, project.project_id, order.raw_order_id, client


def test_source_directory_scope_precedes_total_and_foreign_sort_contract_lookup(db, sources):
    uid, foreign, owned, source_id, client = sources
    db.add(SysAuthorizationGrant(user_id=uid, resource='maintenance_source_assignment',
        actions=['read'], scope='assigned_projects', field_permissions=[], version=1))
    db.commit()
    url = '/api/maintenance/project-assignments/orders'
    response = client.get(url, params={'assignment_status': 'all'})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()['total'] == 0 and response.json()['rows'] == []
    assignment = MaintenanceProjectUserAssignment(assignment_id='source-directory-manager', project_id=owned,
        user_id=uid, responsibility_type='primary_manager', assigned_by='synthetic', assignment_reason='test')
    db.add(assignment)
    db.commit()
    response = client.get(url, params={'assignment_status': 'all', 'include_candidates': 'true'})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()['total'] == 1
    assert response.json()['rows'][0]['raw_order_id'] == source_id
    assert response.json()['rows'][0]['candidates'] == []
    assert client.get(url, params={'assignment_status': 'all', 'xsdd_project_id': foreign}).status_code == 403
    assignment.archived_at = datetime.now(timezone.utc)
    assignment.archived_by = 'synthetic'
    assignment.archive_reason = 'revoke relation'
    assignment.version += 1
    db.commit()
    assert client.get(url, params={'assignment_status': 'all'}).json()['total'] == 0


def test_source_all_scope_keeps_unassigned_rows_without_old_boss_role(db, sources):
    uid, _, _, source_id, client = sources
    assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == source_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True)))
    assignment.is_active = False
    assignment.archived_at = datetime.now(timezone.utc)
    assignment.archived_by = 'synthetic'
    assignment.version += 1
    db.add(SysAuthorizationGrant(user_id=uid, resource='maintenance_source_assignment',
        actions=['read'], scope='all_projects', field_permissions=[], version=1))
    db.commit()
    response = client.get('/api/maintenance/project-assignments/orders', params={'include_candidates': 'true'})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()['total'] == 1
    assert response.json()['rows'][0]['raw_order_id'] == source_id
    assert response.json()['rows'][0]['assigned_project'] is None


@pytest.mark.parametrize('revoke', [False, True])
def test_source_commands_require_exact_action_and_final_epoch_after_domain_writes(db, sources, monkeypatch, revoke):
    from sqlalchemy.orm import Session
    from app.models.system import SysUser
    from app.services.maintenance_source_assignment_access import SourceWriteAuthorization
    from app.models.maintenance import FMaintenanceOrder
    uid, target, old, source_id, client = sources
    source = db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.raw_order_id == source_id))
    source.linked_sales_order_no = None
    original = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == source_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True)))
    old_id, old_version = original.assignment_id, original.version
    write = SysAuthorizationGrant(user_id=uid, resource='maintenance_source_assignment_write',
        actions=['assign'], scope='all', field_permissions=['data_profit'], version=1)
    db.add(write)
    db.commit()
    endpoint = '/api/maintenance/project-assignments/orders'
    payload = {'project_id': target, 'items': [{'source_order_id': source_id,
        'expected_assignment_id': old_id, 'expected_version': old_version}], 'reason': 'actual reassignment'}
    view = client.post(endpoint + '/assign?view=authorization', json=payload)
    assert view.status_code == 200, view.text
    assert view.headers["cache-control"] == "no-store"
    assert db.get(MaintenanceSourceOrderAssignment, old_id).is_active
    finish = SourceWriteAuthorization.finish
    def final(self, current_db):
        moved = current_db.scalar(select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == source_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True)))
        assert moved.project_id == target
        if revoke:
            with Session(current_db.get_bind()) as other:
                user = other.get(SysUser, uid)
                user.token_version += 1
                other.commit()
        return finish(self, current_db)
    monkeypatch.setattr(SourceWriteAuthorization, 'finish', final)
    result = client.post(endpoint + '/assign', json=payload)
    assert result.status_code == (401 if revoke else 200), result.text
    db.expire_all()
    current = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == source_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True)))
    if revoke:
        assert current.assignment_id == old_id and current.project_id == old and current.version == old_version
    else:
        assert current.project_id == target
        unassign = {'items': [{'assignment_id': current.assignment_id, 'expected_version': current.version}], 'reason': 'undo'}
        assert client.post(endpoint + '/unassign', json=unassign).status_code == 403
        write.actions = ['unassign']
        write.version += 1
        db.commit()
        monkeypatch.setattr(SourceWriteAuthorization, 'finish', finish)
        result = client.post(endpoint + '/unassign', json=unassign)
        assert result.status_code == 200, result.text
        assert db.scalar(select(MaintenanceSourceOrderAssignment.assignment_id).where(
            MaintenanceSourceOrderAssignment.source_order_id == source_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True))) is None


@pytest.mark.parametrize('revoke', [False, True])
def test_auto_creation_complete_command_and_deferred_stable_user_lock(db, sources, monkeypatch, revoke):
    from sqlalchemy.orm import Session
    from app.models.system import SysUser
    from app.models.maintenance import FMaintenanceOrder
    from app.models.maintenance_project import MaintenanceProject
    from app.services.maintenance_source_assignment_access import SourceAutoAuthorization
    uid, target, old, source_id, client = sources
    for pid in (target, old):
        project = db.get(MaintenanceProject, pid)
        project.salesperson = 'kept existing owner'
        project.project_manager_id = 'kept existing owner'
    user = db.get(SysUser, uid)
    user.salesperson_name = 'AUTO-ACTUAL-OWNER'
    source = db.scalar(select(FMaintenanceOrder).where(FMaintenanceOrder.raw_order_id == source_id))
    source.linked_sales_order_no = None
    source.project_std = 'AUTO-NEW-CANONICAL-PROJECT'
    source.project_raw = source.project_std
    source.salesperson = user.salesperson_name
    old_assignment = db.scalar(select(MaintenanceSourceOrderAssignment).where(
        MaintenanceSourceOrderAssignment.source_order_id == source_id,
        MaintenanceSourceOrderAssignment.is_active.is_(True)))
    old_assignment.is_active = False
    old_assignment.archived_at = datetime.now(timezone.utc)
    old_assignment.archived_by = 'synthetic'
    old_assignment.version += 1
    db.add(SysAuthorizationGrant(user_id=uid, resource='maintenance_source_assignment_write',
        actions=['auto_assign'], scope='all', field_permissions=['data_profit'], version=1))
    db.commit()
    url = '/api/maintenance/project-assignments/orders/auto-assign'
    view = client.post(url + '?view=authorization')
    assert view.status_code == 200, view.text
    assert view.headers["cache-control"] == "no-store"
    assert db.scalar(select(MaintenanceProject.project_id).where(
        MaintenanceProject.display_name == source.project_std)) is None
    finish = SourceAutoAuthorization.finish
    def final(self, current_db):
        assert self.batch.contexts[0].branch == 'create_only'
        assert self.pending_users and self.pending_users[0]['user_id'] == uid
        if revoke:
            with Session(current_db.get_bind()) as other:
                actor = other.get(SysUser, uid)
                actor.token_version += 1
                other.commit()
        return finish(self, current_db)
    monkeypatch.setattr(SourceAutoAuthorization, 'finish', final)
    result = client.post(url)
    assert result.status_code == (401 if revoke else 200), result.text
    db.expire_all()
    created = db.scalar(select(MaintenanceProject).where(MaintenanceProject.display_name == 'AUTO-NEW-CANONICAL-PROJECT'))
    if revoke:
        assert created is None
        assert db.scalar(select(MaintenanceSourceOrderAssignment.assignment_id).where(
            MaintenanceSourceOrderAssignment.source_order_id == source_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True))) is None
    else:
        assert created is not None
        assert result.json()['result']['created_projects'] == 1
        assert result.json()['result']['assignments_created'] == 1
        manager = db.scalar(select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.project_id == created.project_id,
            MaintenanceProjectUserAssignment.archived_at.is_(None)))
        assert manager.user_id == uid


def test_auto_empty_and_owner_only_still_need_explicit_action(db, sources):
    from app.models.system import SysUser
    from app.models.maintenance_project import MaintenanceProject
    uid, target, old, _, client = sources
    for pid in (target, old):
        row = db.get(MaintenanceProject, pid)
        row.salesperson = 'fully assigned'
        row.project_manager_id = 'fully assigned'
    db.commit()
    url = '/api/maintenance/project-assignments/orders/auto-assign'
    assert client.post(url).status_code == 403
    grant = SysAuthorizationGrant(user_id=uid, resource='maintenance_source_assignment_write',
        actions=['auto_assign'], scope='all', field_permissions=['data_profit'], version=1)
    db.add(grant)
    db.commit()
    empty = client.post(url)
    assert empty.status_code == 200, empty.text
    assert empty.json()['result']['assigned_orders'] == 0
    row = db.get(MaintenanceProject, target)
    row.project_manager_id = None
    db.commit()
    owned = client.post(url)
    assert owned.status_code == 200, owned.text
    assert owned.json()['result']['assigned_orders'] == 0
    grant.field_permissions = []
    grant.version += 1
    db.commit()
    assert client.post(url).status_code == 403
