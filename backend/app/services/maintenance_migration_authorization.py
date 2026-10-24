"""Run contexts, stable actors and immutable receipts for migration controls.

Run approval never activates production. Every read and replay needs its current
entry grant; a historical receipt binds content and cannot grant access.
"""
from functools import partial
from types import SimpleNamespace
from sqlalchemy import select, exists, func, Text, cast, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker, load_only

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, relation_revision
from app.authz.context_registry import ENTRY_CONTRACTS
from app.authz.errors import authz_error
from app.authz.principal import load_principal
from app.models.authorization import SysAuthorizationArtifact
from app.models.maintenance_migration import (
    MaintenanceMigrationRun as Run, MaintenanceMigrationEvent as Event,
    MaintenanceMigrationDiscrepancy as Discrepancy,
    MaintenanceProjectCutoverPlan as Plan,
)
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services.maintenance_workflow_authorization import require_complete
from app.services import maintenance_migration_runs as runs
from app.services.maintenance_migration_source import lock_project_source_snapshots

BASE = '/api/maintenance/migration-runs'
PREVIEW = 'POST ' + BASE + '/preview'
SEARCH = 'POST ' + BASE + '/search'
READ = 'GET ' + BASE + '/{run_id}'
EVIDENCE = READ + '/projects/{project_id}/evidence'
MANIFEST = READ + '/manifest'
RECONCILE = 'POST ' + BASE + '/{run_id}/reconcile'
APPROVE = 'POST ' + BASE + '/{run_id}/approve'
ENTRIES = (PREVIEW, SEARCH, READ, EVIDENCE, MANIFEST, RECONCILE, APPROVE)


def preview_params(projects, reason):
    specs = runs._normalize_specs(projects)
    reason = runs._clean_text(reason, '生成 dry-run 理由', max_length=1000)
    return {'projects': specs, 'command_plan_sha256': relation_revision({'projects': specs, 'reason': reason})}


def _event_state(db, target):
    content=func.jsonb_build_object(*[value for column in Event.__table__.columns
        for value in (column.name,column)])
    digest=func.encode(func.sha256(func.convert_to(content.cast(Text),'UTF8')),'hex')
    row=db.execute(select(Event.event_id,Event.run_id,
        Event.payload_json['actor_user_id'].label('actor_id'),digest.label('digest'))
        .where(Event.event_id==target.id)).first()
    if row is None or type(row.actor_id) is not int:
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(artifacts.ArtifactTarget('migration_event', row.event_id, 1),
        ObjectRef('migration_run', row.run_id), row.digest, row.actor_id)


artifacts.register_artifact_domain('migration_event', artifacts.ArtifactDomainAdapter(_event_state, None, ()))


def _latest_event(db, run):
    if run.created_by_user_id is None:
        raise artifacts.unavailable()
    latest = list(db.execute(select(Event.event_id,Event.to_status,
        Event.payload_json['actor_user_id'].label('actor_id'),
        Event.payload_json['content_sha256'].label('content_sha256'))
        .where(Event.run_id==run.run_id,Event.payload_json['run_version'].as_integer()==run.version)))
    if len(latest) != 1:
        raise artifacts.unavailable()
    event = latest[0]
    artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget('migration_event', event.event_id, 1))
    expected_actor = {'previewed': run.created_by_user_id, 'reconciled': run.reconciled_by_user_id,
                      'approved': run.approved_by_user_id}[run.status]
    if event.to_status != run.status or event.actor_id != expected_actor:
        raise artifacts.unavailable()
    return SimpleNamespace(event_id=event.event_id,payload_json={'actor_user_id':event.actor_id,
        'run_version':run.version,'content_sha256':event.content_sha256})


def _content_digest(db, run):
    def values(row):
        return {column.name: runs._jsonable(getattr(row, column.name)) for column in row.__table__.columns}
    plans, baselines, openings = runs._candidate_rows(db, run_id=run.run_id)
    discrepancies = list(db.scalars(select(Discrepancy).where(Discrepancy.run_id == run.run_id)
                                    .order_by(Discrepancy.discrepancy_id)))
    return relation_revision({'run': values(run), 'plans': [values(row) for row in plans],
        'baselines': [values(row) for row in baselines], 'openings': [values(row) for row in openings],
        'discrepancies': [values(row) for row in discrepancies]})


def verify_content(db, run_id):
    run = db.get(Run, run_id, populate_existing=True)
    if run is None:
        raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '迁移运行不存在')
    event = _latest_event(db, run)
    if event.payload_json.get('content_sha256') != _content_digest(db, run):
        raise artifacts.unavailable()
    # Check all immutable audit events too; no deleted/transplanted history.
    events = list(db.scalars(select(Event).where(Event.run_id == run_id)))
    if sorted(row.payload_json.get('run_version', 0) for row in events) != list(range(1, run.version + 1)):
        raise artifacts.unavailable()
    for row in events:
        artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget('migration_event', row.event_id, 1))
    return run


def finish_event(db, identity, run_id, actor_id, event):
    # The audit table is append-only. Complete the receipt before its sole INSERT.
    db.flush()
    run = db.get(Run, run_id)
    event.payload_json = {**event.payload_json, 'actor_user_id': actor_id,
                          'run_version': run.version, 'content_sha256': _content_digest(db, run)}
    db.add(event)
    db.flush()
    target = artifacts.ArtifactTarget('migration_event', event.event_id, 1)
    artifacts.register_artifact_binding(db, identity, target=target, input_digest=_event_state(db, target).input_digest)


def _run_projects(db, run):
    # Authoritative topology is the persisted cutover plans, checked against the
    # source specs; request-supplied project IDs can only narrow an evidence view.
    ids = sorted(db.scalars(select(Plan.project_id).where(Plan.run_id==run.run_id)))
    source=func.jsonb_array_elements(Run.preview_json['source_specs']).table_valued('value').alias('migration_source')
    source_ids=sorted(db.scalars(select(cast(source.c.value,JSONB)['project_id'].astext)
        .select_from(Run).join(source,true()).where(Run.run_id==run.run_id)))
    if not ids or len(ids) != len(set(ids)) or ids != source_ids:
        raise artifacts.unavailable()
    return ids


def _resolve(db, identity, *, entry_id, params, branch=None, read_snapshot=None, locked_user=None):
    contract = ENTRY_CONTRACTS[entry_id]
    project_ids = []
    version = None
    if entry_id == PREVIEW:
        # The digest is server-derived from the whole normalized request; never
        # accept a standalone caller-supplied hash as project evidence.
        project_ids = sorted(str(p['project_id']) for p in params['projects'])
        selection = {'command_plan_sha256': params['command_plan_sha256']}
        principal, projects = foundation.resolve_project_subjects(db, identity, project_ids,
            read_snapshot=read_snapshot, locked_user=locked_user)
        oid = relation_revision({'actor': principal.id, 'selection': selection})
        carrier = foundation.slot_ref(entry_id, 'context_0', oid, selection=selection)
    elif entry_id == SEARCH:
        statuses = sorted(set(params.get('statuses', [])))
        carrier = foundation.slot_ref(entry_id, 'context_0', 'migration_runs_list', selection={'statuses': statuses})
        principal, projects = foundation.resolve_project_subjects(db, identity, [],
            read_snapshot=read_snapshot, locked_user=locked_user)
    else:
        run = db.scalar(select(Run).options(load_only(Run.run_id,Run.version,Run.status,
            Run.created_by_user_id,Run.reconciled_by_user_id,Run.approved_by_user_id,raiseload=True))
            .where(Run.run_id==params['run_id']).execution_options(populate_existing=True))
        if run is None:
            raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '迁移运行不存在')
        event = _latest_event(db, run)
        project_ids = _run_projects(db, run)
        selection = {}
        if entry_id == EVIDENCE:
            if params['project_id'] not in project_ids:
                raise authz_error(404, 'AUTHZ_OBJECT_NOT_FOUND', '项目不属于此迁移运行')
            selection = {key: params[key] for key in ('project_id', 'section', 'page', 'page_size')}
        carrier = foundation.slot_ref(entry_id, 'context_0', run.run_id, selection=selection)
        principal, projects = foundation.resolve_project_subjects(db, identity, project_ids,
            read_snapshot=read_snapshot, locked_user=locked_user)
        version = [run.version, run.status, event.payload_json['content_sha256']]
    slots = {'context_0': (carrier,)}
    if 'context_1' in contract['slots']:
        slot_ids = [params['project_id']] if entry_id == EVIDENCE else project_ids
        slots['context_1'] = tuple(foundation.slot_ref(entry_id, 'context_1', pid) for pid in slot_ids)
    subjects = tuple(PolicySubject(pid, {}) for pid in project_ids) or (PolicySubject(carrier.id, {}),)
    context = foundation.resolved_context(db, entry_id=entry_id, branch='default', carrier=carrier,
        slots=slots, subjects={'maintenance_migration': subjects}, revision=relation_revision({
            'carrier': carrier.to_dict(), 'run': version,
            'projects': {pid: evidence.authorization_revision for pid, evidence in projects.items()},
            'actor': [principal.id, principal.authorization_epoch]}))
    return create_context_batch(db, [context])


def _reference(db, identity, ref, *, entry_id, branch, read_snapshot=None, locked_user=None):
    params = {'run_id': ref.id, **dict(ref.selector.get('selection', {}))}
    return _resolve(db, identity, entry_id=entry_id, params=params, branch=branch,
                    read_snapshot=read_snapshot, locked_user=locked_user)


for _entry in ENTRIES:
    foundation.register_entry_resolver(_entry, _resolve,
        reference_resolver=None if _entry == PREVIEW else _reference)


def preflight(db, identity, entry, params, *, view=False, precondition=None):
    with foundation.read_context_snapshot(sessionmaker(bind=db.get_bind()), identity,
            entry_id=entry, params=params) as (snapshot, _, batch, accesses):
        if view:
            return service.context_resolution(accesses, batch.contexts[0].ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        if entry not in (PREVIEW, SEARCH):
            verify_content(snapshot, params['run_id'])
    return None


def command_authorizer(identity, entry, params, precondition):
    def authorize(db):
        actor = service.lock_actor(db, identity)
        batch = foundation.resolve_entry_context(db, identity, entry_id=entry,
            params=params, locked_user=actor)
        accesses = service.context_accesses(db, identity, batch, locked_user=actor)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, batch.contexts[0].ref)
        if entry != PREVIEW:
            verify_content(db, params['run_id'])
        authorize.authorization_context = accesses.wire(batch.contexts[0].ref)
        return accesses.principal.id
    return authorize


def lock_replay_projects(db, run_id):
    run = db.get(Run, run_id)
    lock_project_source_snapshots(db, project_ids=_run_projects(db, run))


def read(db, identity, entry, params, *, view=False, precondition=None, verification_keys=None):
    with foundation.read_context_snapshot(sessionmaker(bind=db.get_bind()), identity,
            entry_id=entry, params=params) as (snapshot, proof, batch, accesses):
        ref = batch.contexts[0].ref
        if view:
            return service.context_resolution(accesses, ref)
        require_complete(accesses)
        service.check_context_precondition(precondition, accesses, ref)
        if entry == SEARCH:
            # The only registered migration scope is all. Apply its SQL plan
            # and the canonical receipt boundary before count and pagination.
            predicate = accesses.require(ref, 'maintenance_migration', 'read').predicate
            canonical = Run.created_by_user_id.is_not(None) & exists(select(Event.event_id).join(
                SysAuthorizationArtifact, (SysAuthorizationArtifact.domain_id == Event.event_id)
                & (SysAuthorizationArtifact.domain_kind == 'migration_event')).where(Event.run_id == Run.run_id))
            result = runs.search_runs(snapshot, statuses=params['statuses'], page=params['page'],
                page_size=params['page_size'], authorization_filters=(predicate, canonical))
            for row in result['items']:
                verify_content(snapshot, row['run_id'])
        else:
            verify_content(snapshot, params['run_id'])
            if entry == READ:
                result = runs.get_run_detail(snapshot, run_id=params['run_id'])
            elif entry == EVIDENCE:
                result = runs.get_project_evidence(snapshot, **params)
            elif entry == MANIFEST:
                from app.services.maintenance_migration_warehouse import load_project_inventory_movements
                from app.services.maintenance_migration_legacy import load_project_legacy_truth
                result = runs.get_signed_manifest(snapshot, run_id=params['run_id'],
                    verification_keys=verification_keys, read_snapshot=proof,
                    warehouse_loader=partial(load_project_inventory_movements, read_snapshot=proof),
                    legacy_loader=partial(load_project_legacy_truth, read_snapshot=proof))
            else:
                raise ValueError('Not a registered read entry')
        return {**result, 'authorization_context': accesses.wire(ref)}
