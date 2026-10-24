"""Versioned maintenance authorization, stable artifact owners and exact rollback receipts.

Frozen legacy interpretation; no imports from mutable application permissions.
Revision ID: d6e9f2a4b7c1
Revises: c9d4e8f2a6b1
"""
from copy import deepcopy
import json
import hashlib
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd6e9f2a4b7c1'
down_revision = 'c9d4e8f2a6b1'
branch_labels = None
depends_on = None
DATA = json.loads((Path(__file__).parent.parent / 'seed_data/authz_s3.json').read_text())
DEFINITIONS = {r['resource_id']: r for r in DATA['definitions']}
RESOURCES = frozenset(DATA['resources'])
KEY = 'authz_s3'
MAX_COUNTER = 2**31 - 1
USER_AUTH = ('username', 'role', 'is_active', 'permissions', 'template_perms', 'perm_overrides',
             'template_code', 'template_version', 'authorization_legacy_seed_eligible',
             'authorization_profile', 'token_version')
TEMPLATE_AUTH = ('code', 'base_role', 'permissions', 'is_active', 'authorization_legacy_seed_eligible',
                 'authorization_bundle', 'version')


def _next(*versions):
    if any(type(v) is not int or v < 0 for v in versions) or max(versions, default=0) >= MAX_COUNTER:
        raise RuntimeError('Authorization migration counter invalid or exhausted')
    return max(versions, default=0) + 1


def _object(value):
    if value is not None and type(value) is not dict:
        raise RuntimeError('Malformed authorization source')
    return value or {}


def effective(row):
    frozen = DATA['legacy_inputs']
    def normalize(raw):
        raw = _object(raw)
        return {k: bool(raw.get(k, False)) for k in frozen['all_keys']}
    role = row['role']; snapshot = row.get('template_perms'); override = _object(row.get('perm_overrides'))
    legacy = _object(row.get('permissions')); _object(snapshot)
    if role == 'admin':
        result = dict(frozen['admin_permissions'])
        stored = normalize(snapshot if snapshot is not None else legacy)
        if snapshot is not None:
            stored.update({k: bool(v) for k, v in override.items() if k in stored})
        for k in frozen['account_scoped_page_keys'] + frozen['account_scoped_action_keys']:
            result[k] = stored[k]
    elif snapshot is not None:
        result = normalize(snapshot)
        result.update({k: bool(v) for k, v in override.items() if k in result})
    else:
        result = normalize(frozen['role_templates'].get(role, frozen['unknown_role_default']))
        result.update({k: bool(v) for k, v in legacy.items() if k in result})
    for collection in ('data_dependencies', 'page_dependencies'):
        for key, required in frozen[collection].items():
            if result[key] and not result[required]: result[key] = False
    return result


def validate(resource, config, *, old_sales=False):
    if type(config) is not dict or set(config) != {'actions', 'scope', 'field_permissions'}:
        raise RuntimeError('Malformed canonical configuration')
    if resource not in DEFINITIONS: raise RuntimeError('Unknown canonical resource')
    definition = DEFINITIONS[resource]
    actions = config['actions']; fields = config['field_permissions']
    allowed = {'read', 'export'} if old_sales and resource == 'sales_analysis' else {a['id'] for a in definition['actions']}
    for values, vocabulary in ((actions, allowed), (fields, set(definition['field_permissions']))):
        if (type(values) is not list or any(type(x) is not str for x in values)
                or len(values) != len(set(values)) or not set(values) <= vocabulary):
            raise RuntimeError('Invalid canonical configuration vocabulary')
    if config['scope'] not in definition['scopes']: raise RuntimeError('Invalid canonical scope')
    if old_sales and resource == 'sales_analysis':
        if 'export' in actions and 'read' not in actions: raise RuntimeError('Invalid old export dependency')
    else:
        for action in definition['actions']:
            if action['id'] not in actions: continue
            if (not set(action['requires_actions']) <= set(actions)
                    or not set(action['requires_fields']) <= set(fields)
                    or (action['requires_any_actions'] and not set(action['requires_any_actions']).intersection(actions))):
                raise RuntimeError('Invalid canonical action dependencies')
    for field in definition['field_dependencies']:
        if field['field'] in fields and not set(field['requires_fields']) <= set(fields):
            raise RuntimeError('Invalid canonical field dependencies')


def _materialized(profile):
    snapshot = profile['template_snapshot']
    return {**(snapshot['modules'] if snapshot else {}), **profile['resource_overrides']}


def _grants(bind, user_id):
    rows = bind.execute(sa.text('SELECT resource, actions, scope, field_permissions, version FROM sys_authorization_grant WHERE user_id=:id ORDER BY resource'), {'id':user_id}).mappings()
    return {r['resource']: {k:r[k] for k in ('actions', 'scope', 'field_permissions', 'version')} for r in rows}


def _user_state(row, grants):
    return {'auth': {k:row[k] for k in USER_AUTH}, 'deleted': row['deleted_at'] is not None, 'grants':grants}


def _template_state(row): return {k:row[k] for k in TEMPLATE_AUTH}


def _receipt(bind, kind, entity_id, before, after):
    bind.execute(sa.text('INSERT INTO sys_authorization_migration_receipt (migration_key,entity_kind,entity_id,before_state,after_state) VALUES (:key,:kind,:id,CAST(:before AS jsonb),CAST(:after AS jsonb))'),
                 {'key':KEY,'kind':kind,'id':str(entity_id),'before':json.dumps(before),'after':json.dumps(after)})


def _write_grant(bind, user_id, resource, config, version):
    bind.execute(sa.text('''INSERT INTO sys_authorization_grant (user_id,resource,actions,scope,field_permissions,version)
      VALUES (:user,:resource,CAST(:actions AS jsonb),:scope,CAST(:fields AS jsonb),:version)
      ON CONFLICT (user_id,resource) DO UPDATE SET actions=EXCLUDED.actions,scope=EXCLUDED.scope,
      field_permissions=EXCLUDED.field_permissions,version=EXCLUDED.version'''),
      {'user':user_id,'resource':resource,'actions':json.dumps(config['actions']),'scope':config['scope'],
       'fields':json.dumps(config['field_permissions']),'version':version})


OWNER_TABLES = (
    ('maintenance_doc_import_batch', 'maintenance_doc_import', 'uq_maintenance_doc_import_idempotency'),
    ('maintenance_ckd_import_batch', 'maintenance_ckd_import', 'uq_maintenance_ckd_import_idempotency'),
    ('maintenance_ledger_import_batch', 'maintenance_ledger_import', 'uq_maintenance_ledger_import_idempotency'),
    ('maintenance_acceptance_checklist_batch', 'acceptance_checklist_batch', 'uq_acceptance_checklist_batch_idempotency'),
    ('maintenance_wbdd_import_receipt', 'maintenance_wbdd_import', 'uq_maintenance_wbdd_import_idempotency'),
    ('replenishment_application', 'replenishment_application', None),
)
SCOPES = sorted({s for d in DEFINITIONS.values() for s in d['scopes']})
SCOPE_CHECK = 'scope IN (' + ', '.join(repr(s) for s in SCOPES) + ')'


def predicates(row):
    graph = effective(row)
    snapshot = row.get('template_perms')
    stored = _object(snapshot if snapshot is not None else row.get('permissions')).copy()
    if snapshot is not None:
        stored.update({k: v for k, v in _object(row.get('perm_overrides')).items() if k in DATA['legacy_inputs']['all_keys']})
    role = row['role']; admin = role == 'admin'; full = role in ('admin', 'boss', 'phase1_full_access')
    values = {
        'P': admin or graph['page_maintenance'], 'Beta': graph['page_maintenance_beta'], 'admin': admin,
        'full_scope': full, 'FULL': full, 'maintenance_scoped': not full and graph['own_maintenance_projects_only'],
        'source_sales_scoped': graph['own_customers_only'], 'C': graph['data_purchase_cost'],
        'F': graph['data_profit'], 'K': graph['data_customer'],
        'upload_action': graph['action_maintenance_expense_collection_upload'],
        'project_manage_action': graph['action_maintenance_project_manage'],
        'roundtrip_apply_action': graph['action_maintenance_roundtrip_apply'],
        'manager_workbook_apply_action': graph['action_maintenance_manager_workbook_apply'], 'named': True,
        'BOARD': admin or graph['page_maintenance_boss'] or graph['page_maintenance'],
        'admin_or_boss': role in ('admin', 'boss'), 'maintenance_manager': role == 'maintenance_manager',
        'ReplenishmentBeta': graph['page_replenishment_beta'], 'live_F': graph['data_profit'],
        'live_runtime_safe_F': graph['data_profit'], 'live_G': graph['data_pool_price_governance'],
        'live_replenishment_create': graph['action_replenishment_create'],
        'explicit_follow_up': bool(stored.get('action_maintenance_collection_follow_up')),
        'explicit_collection_plan_import': bool(stored.get('action_maintenance_collection_plan_import')),
        'explicit_P': graph['page_maintenance'] is True, 'explicit_C': graph['data_purchase_cost'] is True,
        'explicit_F': graph['data_profit'] is True, 'explicit_migration_review': graph['action_maintenance_migration_review'] is True,
    }
    for name, definition in DATA['predicates'].items():
        if isinstance(definition, dict):
            if definition['evaluator'] != 'legacy_require_action': raise RuntimeError('Unknown legacy predicate')
            data = definition['data_key']
            values[name] = admin or (graph[definition['action']] and (data is None or graph[data]))
    if set(values) != set(DATA['predicates']): raise RuntimeError('Incomplete frozen predicates')
    return values


def evaluate(node, values):
    if type(node) is not dict or len(node) != 1: raise RuntimeError('Invalid seed expression')
    kind, body = next(iter(node.items()))
    if kind == 'predicate': return values[body]
    if kind == 'constant' and type(body) is bool: return body
    if kind == 'not': return not evaluate(body, values)
    if kind not in ('all', 'any') or type(body) is not list or not body: raise RuntimeError('Invalid seed operator')
    children = [evaluate(child, values) for child in body]
    return all(children) if kind == 'all' else any(children)


def select_scope(node, values):
    if type(node) is str: return node
    if type(node) is not dict or set(node) != {'cases', 'else'}: raise RuntimeError('Invalid scope expression')
    for case in node['cases']:
        if evaluate(case['if'], values): return select_scope(case['scope'], values)
    return select_scope(node['else'], values)


def seed(row, existing):
    if row.get('authorization_legacy_seed_eligible') is not True or row.get('deleted_at') is not None: return {}
    values = predicates(row); result = {}
    for resource, item in DATA['resources'].items():
        if resource in existing: continue
        actions, fields = set(), set()
        for rule in item['seed']['rules']:
            if evaluate(rule['if'], values):
                actions.update(rule['actions']); fields.update(rule['field_permissions'])
        result[resource] = {'actions': sorted(actions), 'scope': select_scope(item['seed']['scope'], values),
                            'field_permissions': sorted(fields) if actions else []}
    combined = {**existing, **result}
    for resource, config in result.items():
        definition = DEFINITIONS[resource]
        for descriptor in definition['actions']:
            if descriptor['kind'] != 'display': continue
            # A display switch is seeded only when its entire real data rule
            # and all same-resource dependencies were already present.
            surfaces = [s for s in DATA['resources'][resource]['surfaces'] if descriptor['id'] in s['display_actions']]
            def allows(surface):
                return any(all(need['resource_id'] in combined
                    and set(need['actions']) <= set(combined[need['resource_id']]['actions'])
                    and set(need['field_permissions']) <= set(combined[need['resource_id']]['field_permissions'])
                    for need in alternative) for alternative in surface['data_rule']['anyOf'])
            if (surfaces and any(allows(surface) for surface in surfaces)
                    and set(descriptor['requires_actions']) <= set(config['actions'])
                    and set(descriptor['requires_fields']) <= set(config['field_permissions'])
                    and (not descriptor['requires_any_actions'] or set(descriptor['requires_any_actions']).intersection(config['actions']))):
                config['actions'].append(descriptor['id'])
        config['actions'] = sorted(set(config['actions']))
        validate(resource, config)
    return result


def validate_bundle(value):
    if type(value) is not dict: raise RuntimeError('Invalid canonical bundle')
    for resource, config in value.items(): validate(resource, config)
    return deepcopy(value)


def profile(value):
    if (type(value) is not dict or set(value) != {'schema_version','template_snapshot','resource_overrides'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise RuntimeError('Invalid canonical source profile')
    result = deepcopy(value); validate_bundle(result['resource_overrides'])
    snapshot = result['template_snapshot']
    if snapshot is not None:
        if type(snapshot) is not dict or set(snapshot) != {'code','version','modules'}: raise RuntimeError('Invalid saved template')
        validate_bundle(snapshot['modules'])
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _predecessor(bind, kind, entity_id):
    row = bind.execute(sa.text("SELECT before_state, after_state FROM sys_authorization_migration_receipt WHERE migration_key='authz_s2' AND entity_kind=:kind AND entity_id=:id"), {'kind':kind,'id':str(entity_id)}).mappings().first()
    return dict(row) if row else None


def _save_receipt(bind, kind, entity_id, before, after):
    prior = _predecessor(bind, kind, entity_id)
    _receipt(bind, kind, entity_id, {'state':before, 'predecessor_digest':_digest(prior) if prior else None,
        'predecessor_matches': bool(prior and prior['after_state'] == before)}, after)


def _schema_upgrade():
    op.drop_constraint('ck_authorization_grant_scope', 'sys_authorization_grant', type_='check')
    op.create_check_constraint('ck_authorization_grant_scope', 'sys_authorization_grant', SCOPE_CHECK)
    op.create_table('sys_authorization_artifact',
        sa.Column('domain_kind',sa.String(64),primary_key=True),sa.Column('domain_id',sa.String(128),primary_key=True),
        sa.Column('owner_user_id',sa.Integer(),sa.ForeignKey('sys_user.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('generation',sa.BigInteger(),nullable=False),sa.Column('enqueued_epoch',sa.BigInteger(),nullable=False),
        sa.Column('input_digest',sa.String(64),nullable=False),sa.Column('command_namespace',sa.String(64)),
        sa.Column('idempotency_key',sa.String(128)),sa.Column('authorization_provenance',postgresql.JSONB(none_as_null=True)),
        sa.Column('artifact_manifests',postgresql.JSONB(none_as_null=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint('generation >= 1 AND generation <= 9007199254740991',name='ck_authorization_artifact_generation'),
        sa.CheckConstraint('enqueued_epoch >= 0 AND enqueued_epoch <= 9007199254740991',name='ck_authorization_artifact_epoch'),
        sa.CheckConstraint("input_digest ~ '^[0-9a-f]{64}$'",name='ck_authorization_artifact_digest'),
        sa.CheckConstraint("(command_namespace IS NULL AND idempotency_key IS NULL) OR (command_namespace IS NOT NULL AND command_namespace <> '' AND idempotency_key IS NOT NULL AND idempotency_key <> '')",name='ck_authorization_artifact_command'))
    op.create_index('ux_authorization_artifact_command','sys_authorization_artifact',['command_namespace','owner_user_id','idempotency_key'],unique=True,postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_index('ix_authorization_artifact_owner_updated','sys_authorization_artifact',['owner_user_id','updated_at'])
    for table, prefix, constraint in OWNER_TABLES:
        op.add_column(table,sa.Column('owner_user_id',sa.Integer(),nullable=True))
        op.create_foreign_key(f'fk_{prefix}_owner_user_id',table,'sys_user',['owner_user_id'],['id'],ondelete='RESTRICT')
        key = 'idempotency_key' if constraint else 'client_request_id'
        owner = 'uploaded_by' if constraint else 'owner_username'
        if constraint: op.drop_constraint(constraint,table,type_='unique')
        else: op.drop_index(f'ux_{prefix}_owner_request',table_name=table)
        suffix = '' if constraint else ' AND client_request_id IS NOT NULL'
        op.create_index(f'ux_{prefix}_owner_request',table,['owner_user_id',key],unique=True,postgresql_where=sa.text('owner_user_id IS NOT NULL'+suffix))
        op.create_index(f'ux_{prefix}_legacy_request',table,[owner,key],unique=True,postgresql_where=sa.text('owner_user_id IS NULL'+suffix))


    for table in STABLE_OWNER_TABLES:
        op.add_column(table, sa.Column('owner_user_id', sa.Integer(), nullable=True))
        op.create_foreign_key(f'fk_{table}_owner_user_id', table, 'sys_user', ['owner_user_id'], ['id'], ondelete='RESTRICT')

    for column in ('created_by_user_id', 'reconciled_by_user_id', 'approved_by_user_id'):
        op.add_column('maintenance_migration_run',sa.Column(column,sa.Integer(),nullable=True))
        op.create_foreign_key('fk_maintenance_migration_run_'+column,'maintenance_migration_run','sys_user',[column],['id'],ondelete='RESTRICT')
    op.drop_constraint('ck_maintenance_migration_run_independent_reconciliation','maintenance_migration_run',type_='check')
    op.create_check_constraint('ck_maintenance_migration_run_independent_reconciliation','maintenance_migration_run','(created_by_user_id IS NULL AND (reconciled_by IS NULL OR reconciled_by <> created_by)) OR (created_by_user_id IS NOT NULL AND (reconciled_by_user_id IS NULL OR reconciled_by_user_id <> created_by_user_id))')
    op.drop_constraint('ck_maintenance_migration_run_independent_approval','maintenance_migration_run',type_='check')
    op.create_check_constraint('ck_maintenance_migration_run_independent_approval','maintenance_migration_run','(created_by_user_id IS NULL AND (approved_by IS NULL OR (approved_by <> created_by AND approved_by <> reconciled_by))) OR (created_by_user_id IS NOT NULL AND (approved_by_user_id IS NULL OR (approved_by_user_id <> created_by_user_id AND approved_by_user_id <> reconciled_by_user_id)))')
    op.create_check_constraint('ck_maintenance_migration_run_stable_actors','maintenance_migration_run',"(created_by_user_id IS NULL AND reconciled_by_user_id IS NULL AND approved_by_user_id IS NULL) OR (created_by_user_id IS NOT NULL AND ((status = 'previewed' AND reconciled_by_user_id IS NULL AND approved_by_user_id IS NULL) OR (status = 'reconciled' AND reconciled_by_user_id IS NOT NULL AND approved_by_user_id IS NULL) OR (status = 'approved' AND reconciled_by_user_id IS NOT NULL AND approved_by_user_id IS NOT NULL)))")


STABLE_OWNER_TABLES = ('maintenance_project_workbook_operation', 'business_file', 'maintenance_acceptance_operation', 'replenishment_application_version', 'maintenance_warehouse_import_batch', 'maintenance_ai_mapping_proposal', 'maintenance_bad_return_command', 'maintenance_bad_salvage', 'maintenance_rkd_return_line', 'sys_import_batch', 'maintenance_roundtrip_operation', 'maintenance_demand_delete_intent')


def upgrade():
    # Serialize with producers in their domain -> actor -> Grant order before
    # DDL takes its stronger locks; never hold Grant while waiting for an actor.
    op.execute('LOCK TABLE ' + ', '.join([t[0] for t in OWNER_TABLES] + [
        'maintenance_migration_run', 'sys_role_template', 'sys_user', 'sys_authorization_grant',
        'sys_authorization_cutover', 'sys_authorization_migration_receipt']) + ' IN ACCESS EXCLUSIVE MODE')
    _schema_upgrade()
    bind = op.get_bind()
    op.execute('LOCK TABLE sys_authorization_migration_receipt, sys_role_template, sys_user, sys_authorization_grant, sys_authorization_cutover IN ACCESS EXCLUSIVE MODE')
    entities = []
    for raw in bind.execute(sa.text('SELECT * FROM sys_role_template ORDER BY id')).mappings().all():
        row = dict(raw); before = _template_state(row); bundle = validate_bundle(row['authorization_bundle'])
        source = {**row,'role':row['base_role'],'template_perms':row['permissions'],'perm_overrides':None}
        bundle.update(seed(source,bundle))
        if bundle != row['authorization_bundle']:
            row['authorization_bundle'] = bundle; row['version'] = _next(row['version'])
            bind.execute(sa.text('UPDATE sys_role_template SET authorization_bundle=CAST(:bundle AS jsonb),version=:version WHERE id=:id'),{'bundle':json.dumps(bundle),'version':row['version'],'id':row['id']})
        if bundle:
            _save_receipt(bind,'template',row['id'],before,_template_state(row));entities.append(['template',str(row['id'])])
    for raw in bind.execute(sa.text('SELECT * FROM sys_user ORDER BY id')).mappings().all():
        row = dict(raw); grants = _grants(bind,row['id']);before = _user_state(row,deepcopy(grants))
        saved = profile(row['authorization_profile']);original = _materialized(saved)
        if set(grants) != set(original) or any({k:v for k,v in g.items() if k!='version'} != original[r] for r,g in grants.items()):
            raise RuntimeError('Canonical source and materialized Grants disagree; migration refused')
        additions = seed(row,original);saved['resource_overrides'].update(additions)
        if saved != row['authorization_profile']:
            floor = _next(row['token_version'],*(g['version'] for g in grants.values()))
            row['authorization_profile'] = saved;row['token_version'] = floor
            for resource, config in additions.items():
                _write_grant(bind,row['id'],resource,config,floor);grants[resource] = {**deepcopy(config),'version':floor}
            bind.execute(sa.text('UPDATE sys_user SET authorization_profile=CAST(:profile AS jsonb),token_version=:epoch WHERE id=:id'),{'profile':json.dumps(saved),'epoch':floor,'id':row['id']})
        if grants:
            _save_receipt(bind,'user',row['id'],before,_user_state(row,grants));entities.append(['user',str(row['id'])])
    _receipt(bind,'boundary','global',{}, {'schema_version':1,'entities':sorted(entities)})


def _contains_s3(bundle):
    return bool(set(bundle).intersection(RESOURCES))


def downgrade():
    bind=op.get_bind()
    tables = [t[0] for t in OWNER_TABLES]+list(STABLE_OWNER_TABLES)+['maintenance_migration_run','sys_authorization_artifact',
        'sys_role_template','sys_user','sys_authorization_grant','sys_authorization_cutover',
        'sys_authorization_migration_receipt']
    op.execute('LOCK TABLE '+', '.join(tables)+' IN ACCESS EXCLUSIVE MODE')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM sys_authorization_artifact)')).scalar():
        raise RuntimeError('Cannot downgrade with stable artifact/task bindings')
    for table,_,_ in OWNER_TABLES:
        if bind.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table} WHERE owner_user_id IS NOT NULL)')).scalar():
            raise RuntimeError('Cannot downgrade after stable artifact owner use')
    for table in STABLE_OWNER_TABLES:
        if bind.execute(sa.text(f'SELECT EXISTS(SELECT 1 FROM {table} WHERE owner_user_id IS NOT NULL)')).scalar():
            raise RuntimeError('Cannot downgrade after stable command or file owner use')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM maintenance_migration_run WHERE created_by_user_id IS NOT NULL OR reconciled_by_user_id IS NOT NULL OR approved_by_user_id IS NOT NULL)')).scalar():
        raise RuntimeError('Cannot downgrade stable migration review actors')
    active = set(bind.execute(sa.text('SELECT resource FROM sys_authorization_cutover')).scalars())
    if active.intersection(RESOURCES): raise RuntimeError('Cannot downgrade after maintenance authorization cutover')
    receipts = bind.execute(sa.text('SELECT entity_kind,entity_id,before_state,after_state FROM sys_authorization_migration_receipt WHERE migration_key=:key'),{'key':KEY}).mappings().all()
    by_key={(x['entity_kind'],x['entity_id']):x for x in receipts}
    if any(k[0]=='artifact_protocol' for k in by_key): raise RuntimeError('Cannot downgrade after contextual artifact publication')
    boundary=by_key.get(('boundary','global'));actual=sorted([list(k) for k in by_key if k!=('boundary','global')])
    if boundary is None or boundary['after_state'] != {'schema_version':1,'entities':actual}: raise RuntimeError('Missing or damaged maintenance migration receipts')
    users={str(x['id']):dict(x) for x in bind.execute(sa.text('SELECT * FROM sys_user')).mappings()}
    templates={str(x['id']):dict(x) for x in bind.execute(sa.text('SELECT * FROM sys_role_template')).mappings()}
    for (kind,entity_id),receipt in by_key.items():
        if kind=='boundary': continue
        if kind=='user':
            row=users.get(entity_id);current=_user_state(row,_grants(bind,row['id'])) if row else None
        elif kind=='template':
            row=templates.get(entity_id);current=_template_state(row) if row else None
        else: raise RuntimeError('Unknown maintenance migration receipt')
        if current != receipt['after_state']: raise RuntimeError('Authorization changed after migration; downgrade refused')
        before=receipt['before_state']
        if type(before) is not dict or set(before) != {'state','predecessor_digest','predecessor_matches'}: raise RuntimeError('Invalid predecessor receipt')
    for entity_id,row in users.items():
        if ('user',entity_id) not in by_key and (_contains_s3(_grants(bind,row['id'])) or _contains_s3(_materialized(profile(row['authorization_profile'])))):
            raise RuntimeError('Unrecorded maintenance user authorization prevents downgrade')
    for entity_id,row in templates.items():
        if ('template',entity_id) not in by_key and _contains_s3(row['authorization_bundle']): raise RuntimeError('Unrecorded maintenance template authorization prevents downgrade')
    for (kind,entity_id),receipt in by_key.items():
        if kind=='boundary': continue
        recorded=receipt['before_state'];before=recorded['state']
        if kind=='user':
            row=users[entity_id];current=_grants(bind,row['id']);floor=_next(row['token_version'],*(g['version'] for g in current.values()))
            for resource in set(current)-set(before['grants']):
                bind.execute(sa.text('DELETE FROM sys_authorization_grant WHERE user_id=:id AND resource=:resource'),{'id':row['id'],'resource':resource})
            for resource,g in before['grants'].items(): _write_grant(bind,row['id'],resource,g,floor)
            bind.execute(sa.text('UPDATE sys_user SET authorization_profile=CAST(:profile AS jsonb),token_version=:epoch WHERE id=:id'),{'profile':json.dumps(before['auth']['authorization_profile']),'epoch':floor,'id':row['id']})
            restored=deepcopy(before);restored['auth']['token_version']=floor
            for grant in restored['grants'].values(): grant['version']=floor
        else:
            row=templates[entity_id];version=_next(row['version'])
            bind.execute(sa.text('UPDATE sys_role_template SET authorization_bundle=CAST(:bundle AS jsonb),version=:version WHERE id=:id'),{'bundle':json.dumps(before['authorization_bundle']),'version':version,'id':row['id']})
            restored=deepcopy(before);restored['version']=version
        prior=_predecessor(bind,kind,entity_id)
        if (recorded['predecessor_matches'] is True and prior is not None
                and _digest(prior)==recorded['predecessor_digest'] and prior['after_state']==before):
            # Carry only monotonic counters through a proven adjacent chain.
            bind.execute(sa.text("UPDATE sys_authorization_migration_receipt SET after_state=CAST(:after AS jsonb) WHERE migration_key='authz_s2' AND entity_kind=:kind AND entity_id=:id"),{'after':json.dumps(restored),'kind':kind,'id':entity_id})
    bind.execute(sa.text('DELETE FROM sys_authorization_migration_receipt WHERE migration_key=:key'),{'key':KEY})
    for table in reversed(STABLE_OWNER_TABLES):
        op.drop_constraint(f'fk_{table}_owner_user_id', table, type_='foreignkey')
        op.drop_column(table, 'owner_user_id')
    for table,prefix,constraint in reversed(OWNER_TABLES):
        op.drop_index(f'ux_{prefix}_owner_request',table_name=table);op.drop_index(f'ux_{prefix}_legacy_request',table_name=table)
        if constraint: op.create_unique_constraint(constraint,table,['uploaded_by','idempotency_key'])
        else: op.create_index(f'ux_{prefix}_owner_request',table,['owner_username','client_request_id'],unique=True,postgresql_where=sa.text('client_request_id IS NOT NULL'))
        op.drop_constraint(f'fk_{prefix}_owner_user_id',table,type_='foreignkey');op.drop_column(table,'owner_user_id')
    op.drop_constraint('ck_maintenance_migration_run_independent_reconciliation','maintenance_migration_run',type_='check')
    op.drop_constraint('ck_maintenance_migration_run_independent_approval','maintenance_migration_run',type_='check')
    op.drop_constraint('ck_maintenance_migration_run_stable_actors','maintenance_migration_run',type_='check')
    op.create_check_constraint('ck_maintenance_migration_run_independent_reconciliation','maintenance_migration_run','reconciled_by IS NULL OR reconciled_by <> created_by')
    op.create_check_constraint('ck_maintenance_migration_run_independent_approval','maintenance_migration_run','approved_by IS NULL OR (approved_by <> created_by AND approved_by <> reconciled_by)')
    for column in ('created_by_user_id','reconciled_by_user_id','approved_by_user_id'):
        op.drop_constraint('fk_maintenance_migration_run_'+column,'maintenance_migration_run',type_='foreignkey')
        op.drop_column('maintenance_migration_run',column)
    op.drop_table('sys_authorization_artifact')
    op.drop_constraint('ck_authorization_grant_scope','sys_authorization_grant',type_='check')
    op.create_check_constraint('ck_authorization_grant_scope','sys_authorization_grant',"scope IN ('all', 'assigned_customers')")
