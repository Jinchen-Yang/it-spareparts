"""Independent relation facts and real Cerbos SQL plans for S3 project scopes."""
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.authz import engine
from app.authz.context import read_snapshot
from app.authz.principal import VerifiedIdentity, load_principal
from app.config import get_settings
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectUserAssignment
from app.models.system import SysUser
from app.services import maintenance_authorization as domain
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.fixture
def projects(db):
    actor = SysUser(username='s3-foundation', role='admin', password_hash='unused',
                    token_version=3, salesperson_name='canonical-sale')
    db.add(actor); db.flush()
    ids = {label: str(uuid4()) for label in ['manager', 'viewer', 'sales', 'other', 'inactive_manager']}
    for label, project_id in ids.items():
        db.add(MaintenanceProject(project_id=project_id, project_code=f'S3-{label}',
            display_name=f'Secret {label}', lifecycle_status='active',
            salesperson='canonical-sale' if label == 'sales' else None,
            is_active=label != 'inactive_manager', version=1))
    db.flush()
    for label, responsibility in [('manager', 'primary_manager'), ('viewer', 'viewer'), ('inactive_manager', 'primary_manager')]:
        db.add(MaintenanceProjectUserAssignment(assignment_id=str(uuid4()), project_id=ids[label],
            user_id=actor.id, responsibility_type=responsibility, version=1,
            assigned_by='fixture', assignment_reason='scope fixture'))
    db.commit()
    identity = VerifiedIdentity(actor.username, 3, False, 'sys_user', MappingProxyType({}))
    return actor, identity, ids


@pytest.mark.parametrize('resource,scope,action,fields,expected', [
    ('maintenance_project_directory', 'assigned_projects', 'read', [], {'manager', 'viewer', 'inactive_manager'}),
    ('maintenance_project_directory', 'assigned_or_sales_projects', 'read', [], {'manager', 'viewer', 'sales', 'inactive_manager'}),
    ('maintenance_project_directory', 'all_projects', 'read', [], {'manager', 'viewer', 'sales', 'other', 'inactive_manager'}),
    ('maintenance_managed_workbook', 'primary_managed_projects', 'download', ['data_purchase_cost', 'data_profit', 'data_customer'], {'manager', 'inactive_manager'}),
    ('maintenance_manager_workbook', 'primary_managed_active_projects', 'download', ['data_profit'], {'manager'}),
    ('maintenance_general_workbook_write', 'canonical_sales_projects', 'validate', ['data_purchase_cost', 'data_profit', 'data_customer'], {'sales'}),
])
def test_real_pdp_boolean_columns_filter_before_aggregate(db, projects, cerbos_url, monkeypatch,
        resource, scope, action, fields, expected):
    monkeypatch.setattr(get_settings(), 'cerbos_url', cerbos_url)
    actor, identity, ids = projects
    principal = load_principal(db, identity)
    bindings, project_column = domain.project_query_bindings(principal, resource)
    assert all(column.table is bindings.table for column in bindings.attributes.values())
    grant = engine.Grant([action], scope, fields, 1, resource=resource)
    plan = engine.plan_access(actor.id, grant, {}, action, bindings)
    authorized_ids = select(project_column).where(plan.predicate)
    actual = set(db.scalars(select(MaintenanceProject.project_id).where(
        MaintenanceProject.project_id.in_(authorized_ids))))
    assert actual == {ids[x] for x in expected}
    count = db.scalar(select(func.count()).select_from(MaintenanceProject).where(
        MaintenanceProject.project_id.in_(authorized_ids)))
    assert count == len(expected)
    assert plan.field_permissions == frozenset(fields)


def test_actual_relationships_do_not_inherit_admin_or_global_field_permission(db, projects):
    actor, identity, ids = projects
    principal = load_principal(db, identity)
    evidence = domain.project_evidence(db, principal, list(ids.values()))
    assert evidence[ids['other']].subject.attributes == {
        'is_manager': False, 'is_viewer': False, 'is_sales': False, 'is_active': True}
    assert evidence[ids['manager']].subject.attributes['is_manager'] is True
    assert evidence[ids['viewer']].subject.attributes['is_manager'] is False
    assert evidence[ids['viewer']].subject.attributes['is_viewer'] is True
    assert evidence[ids['sales']].subject.attributes['is_sales'] is True
    unrelated_role = replace(principal, role='readonly', legacy_permissions=frozenset())
    assert domain.project_evidence(db, unrelated_role, [ids['other']])[ids['other']] == evidence[ids['other']]


def test_revision_observes_assignment_archive_and_actor_mapping_epoch(db, projects):
    actor, identity, ids = projects
    principal = load_principal(db, identity)
    project_id = ids['manager']
    before = domain.project_evidence(db, principal, [project_id])[project_id]
    db.execute(update(MaintenanceProjectUserAssignment).where(
        MaintenanceProjectUserAssignment.project_id == project_id).values(
            archived_at=datetime.now(timezone.utc), archived_by='fixture', archive_reason='revoke relationship', version=2))
    after = domain.project_evidence(db, principal, [project_id])[project_id]
    assert not after.subject.attributes['is_manager']
    assert before.authorization_revision != after.authorization_revision
    remapped = domain.project_evidence(db, replace(principal, authorization_epoch=4,
        salesperson_name='other-sale'), [project_id])[project_id]
    assert remapped.authorization_revision != after.authorization_revision
    assert remapped.content_revision == after.content_revision
    assert before.content_revision != after.content_revision


def test_content_revision_is_reader_independent_while_authorization_is_not(db, projects):
    _, identity, ids = projects
    principal = load_principal(db, identity)
    one = domain.project_evidence(db, principal, [ids['manager']])[ids['manager']]
    other = replace(principal, id=principal.id + 100, username='other-reader', authorization_epoch=9)
    two = domain.project_evidence(db, other, [ids['manager']])[ids['manager']]
    assert one.content_revision == two.content_revision
    assert one.authorization_revision != two.authorization_revision
    assert one.subject.attributes['is_manager'] and not two.subject.attributes['is_manager']


def test_read_workbook_state_uses_revision_zero_without_insert_even_in_read_only_snapshot(db, projects):
    from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
    from app.services.maintenance_project_operations import read_workbook_state, _workbook_data_version
    _, _, ids = projects
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, _):
        state = read_workbook_state(snapshot, project_id=ids['manager'])
        assert state.revision == 0 and state.data_version == _workbook_data_version(ids['manager'], 0)
    assert db.get(MaintenanceProjectWorkbookState, ids['manager']) is None


def test_rr_relation_evidence_and_fact_projection_share_one_observation(db, projects):
    actor, identity, ids = projects
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    with read_snapshot(factory) as (snapshot, proof):
        principal, evidence = domain.resolve_project_subjects(snapshot, identity, [ids['manager']], read_snapshot=proof)
        with factory() as writer:
            writer.execute(update(MaintenanceProjectUserAssignment).where(
                MaintenanceProjectUserAssignment.project_id == ids['manager']).values(
                    archived_at=datetime.now(timezone.utc), archived_by='fixture', archive_reason='revoke relationship', version=2))
            writer.commit()
        same = domain.project_evidence(snapshot, principal, [ids['manager']])
        assert same == evidence
        bindings, project_id = domain.project_query_bindings(principal, 'maintenance_project_directory')
        assert ids['manager'] in set(snapshot.scalars(select(project_id).where(bindings.table.c.is_manager)))
    with read_snapshot(factory) as (snapshot, proof):
        _, latest = domain.resolve_project_subjects(snapshot, identity, [ids['manager']], read_snapshot=proof)
        assert not latest[ids['manager']].subject.attributes['is_manager']
    with pytest.raises(HTTPException) as stale:
        proof.validate(snapshot)
    assert stale.value.status_code == 409


def test_domain_requires_real_observation_not_unlocked_actor_or_dict(db, projects):
    _, identity, ids = projects
    with pytest.raises(ValueError):
        domain.resolve_project_subjects(db, identity, [ids['manager']])
    with pytest.raises(HTTPException) as invalid:
        domain.resolve_project_subjects(db, {'sub': identity.username}, [ids['manager']])
    assert invalid.value.status_code == 401


@pytest.mark.parametrize('payload', [[], {'catalog_version':'x','authorization_epoch':True,'context':{}},
    {'catalog_version':'x','authorization_epoch':3,'context':{'kind':'object','id':'1','object_type':'arbitrary','authorization_revision':'x'}},
    {'catalog_version':'x','authorization_epoch':3,'context':{'kind':'collection','id':'arbitrary','authorization_revision':'x'}},
    '{invalid json',
])
def test_untrusted_precondition_cannot_mint_unknown_context(payload):
    with pytest.raises(HTTPException) as invalid:
        domain.decode_precondition(payload)
    assert invalid.value.status_code == 422
