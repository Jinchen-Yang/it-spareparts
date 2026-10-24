"""Platform/import authorization and normalized stable import manifests.

Frozen S4 legacy interpretation; application permissions are never imported.
Revision ID: e4a7c9f2b6d8
Revises: d6e9f2a4b7c1
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = 'e4a7c9f2b6d8'
down_revision = 'd6e9f2a4b7c1'
branch_labels = None
depends_on = None
DATA = json.loads((Path(__file__).parent.parent / 'seed_data/authz_s4.json').read_text())
DEFINITIONS = {r['resource_id']: r for r in DATA['definitions']}
RESOURCES = frozenset(DATA['resources'])
KEY = 'authz_s4'
MAX_COUNTER = 2**31 - 1
USER_AUTH = ('username', 'role', 'is_active', 'permissions', 'template_perms', 'perm_overrides',
             'template_code', 'template_version', 'authorization_legacy_seed_eligible',
             'authorization_profile', 'token_version')
TEMPLATE_AUTH = ('code', 'base_role', 'permissions', 'is_active', 'authorization_legacy_seed_eligible',
                 'authorization_bundle', 'version')
SCOPES = sorted({s for d in DEFINITIONS.values() for s in d['scopes']})
SCOPE_CHECK = 'scope IN (' + ', '.join(repr(s) for s in SCOPES) + ')'


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
    row = bind.execute(sa.text("SELECT before_state, after_state FROM sys_authorization_migration_receipt WHERE migration_key='authz_s3' AND entity_kind=:kind AND entity_id=:id"), {'kind':kind,'id':str(entity_id)}).mappings().first()
    return dict(row) if row else None

def _save_receipt(bind, kind, entity_id, before, after):
    prior = _predecessor(bind, kind, entity_id)
    _receipt(bind, kind, entity_id, {'state':before, 'predecessor_digest':_digest(prior) if prior else None,
        'predecessor_matches': bool(prior and prior['after_state'] == before)}, after)

def evaluate(node, graph):
    if type(node) is not dict or len(node) != 1: raise RuntimeError('Invalid frozen seed predicate')
    if 'key' in node: return graph.get(node['key']) is True
    if 'constant' in node and type(node['constant']) is bool: return node['constant']
    if 'all' in node: return all(evaluate(x, graph) for x in node['all'])
    if 'any' in node: return any(evaluate(x, graph) for x in node['any'])
    raise RuntimeError('Unknown frozen seed predicate')


def seed(row, existing):
    if row.get('authorization_legacy_seed_eligible') is not True or row.get('deleted_at') is not None: return {}
    graph = effective(row); result = {}
    for resource, definition in DATA['resources'].items():
        if resource in existing: continue
        actions = [a['id'] for a in definition['actions'] if evaluate(a['legacy_seed_if'], graph)]
        fields = [f for f, expression in definition['field_seed'].items() if evaluate(expression, graph)] if actions else []
        result[resource] = {'actions': sorted(actions), 'scope': definition['denial_scope'], 'field_permissions': sorted(fields)}
    combined = {**existing, **result}
    for resource, config in result.items():
        for action in DATA['resources'][resource]['actions']:
            if action['kind'] != 'display' or action['id'] not in config['actions']: continue
            surfaces = [s for s in DATA['resources'][resource]['surfaces'] if action['id'] in s['display_actions']]
            allowed = any(any(all(n['resource_id'] in combined
                and set(n['actions']) <= set(combined[n['resource_id']]['actions'])
                and set(n['field_permissions']) <= set(combined[n['resource_id']]['field_permissions'])
                for n in clause) for clause in s['data_rule']['anyOf']) for s in surfaces)
            if not allowed: config['actions'].remove(action['id'])
        validate(resource, config)
    return result


def _schema_upgrade():
    op.add_column('sys_import_job', sa.Column('owner_user_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_import_job_owner', 'sys_import_job', 'sys_user', ['owner_user_id'], ['id'], ondelete='RESTRICT')
    for name, typ in [('authorization_protocol',sa.String(16)), ('enqueued_epoch',sa.Integer()),
                      ('authorization_generation',sa.Integer()), ('manifest_digest',sa.String(64))]:
        op.add_column('sys_import_job', sa.Column(name, typ, nullable=True))
    op.add_column('sys_import_batch', sa.Column('authorization_protocol',sa.String(16),nullable=True))
    op.add_column('sys_import_batch', sa.Column('source_domain',sa.String(16),nullable=True))
    op.create_index('ix_import_batch_authorization_source','sys_import_batch',['authorization_protocol','source_domain','id'])
    op.create_table('sys_import_job_item',
        sa.Column('id',sa.String(32),primary_key=True),
        sa.Column('job_id',sa.Integer(),sa.ForeignKey('sys_import_job.id',ondelete='CASCADE'),nullable=False),
        sa.Column('ordinal',sa.Integer(),nullable=False),sa.Column('source_domain',sa.String(16),nullable=False),
        sa.Column('filename',sa.String(256),nullable=False),sa.Column('content_sha256',sa.String(64),nullable=False),
        sa.Column('content_size',sa.BigInteger(),nullable=False),sa.Column('mode',sa.String(16),nullable=False),
        sa.Column('parser_version',sa.Integer(),nullable=False),sa.Column('input_manifest',postgresql.JSONB(),nullable=False),
        sa.Column('status',sa.String(32),nullable=False,server_default='queued'),
        sa.Column('batch_id',sa.Integer(),sa.ForeignKey('sys_import_batch.id',ondelete='RESTRICT')),
        sa.Column('error_code',sa.String(64)),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.UniqueConstraint('job_id','ordinal',name='uq_import_job_item_ordinal'),
        sa.CheckConstraint('ordinal >= 0',name='ck_import_job_item_ordinal'),
        sa.CheckConstraint("source_domain IN ('purchase','sales','inventory','maintenance','expense')",name='ck_import_job_item_source'),
        sa.CheckConstraint("mode IN ('skip','upsert')",name='ck_import_job_item_mode'),
        sa.CheckConstraint("status IN ('queued','processing','success','partial','failed','authorization_failed')",name='ck_import_job_item_status'),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'",name='ck_import_job_item_digest'),
        sa.CheckConstraint('content_size >= 0',name='ck_import_job_item_size'))
    op.create_index('ix_import_job_item_source_job','sys_import_job_item',['source_domain','job_id'])
    op.drop_constraint('ck_authorization_grant_scope','sys_authorization_grant',type_='check')
    op.create_check_constraint('ck_authorization_grant_scope','sys_authorization_grant',SCOPE_CHECK)


def upgrade():
    op.execute('LOCK TABLE sys_import_job, sys_import_batch, sys_role_template, sys_user, sys_authorization_grant, sys_authorization_cutover, sys_authorization_migration_receipt IN ACCESS EXCLUSIVE MODE')
    _schema_upgrade()
    bind = op.get_bind()
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

def _contains_s4(bundle):
    return bool(set(bundle).intersection(RESOURCES))


def downgrade():
    bind = op.get_bind()
    op.execute('LOCK TABLE sys_import_job_item, sys_import_job, sys_import_batch, sys_authorization_artifact, sys_role_template, sys_user, sys_authorization_grant, sys_authorization_cutover, sys_authorization_migration_receipt IN ACCESS EXCLUSIVE MODE')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM sys_import_job_item)')).scalar():
        raise RuntimeError('Cannot downgrade canonical import manifests')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM sys_import_job WHERE owner_user_id IS NOT NULL OR authorization_protocol IS NOT NULL OR enqueued_epoch IS NOT NULL OR authorization_generation IS NOT NULL OR manifest_digest IS NOT NULL)')).scalar():
        raise RuntimeError('Cannot downgrade stable import job authorization')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM sys_import_batch WHERE authorization_protocol IS NOT NULL OR source_domain IS NOT NULL)')).scalar():
        raise RuntimeError('Cannot downgrade canonical import source metadata')
    active = set(bind.execute(sa.text('SELECT resource FROM sys_authorization_cutover')).scalars())
    if active.intersection(RESOURCES): raise RuntimeError('Cannot downgrade after platform authorization cutover')
    receipts = bind.execute(sa.text('SELECT entity_kind,entity_id,before_state,after_state FROM sys_authorization_migration_receipt WHERE migration_key=:key'),{'key':KEY}).mappings().all()
    by_key={(x['entity_kind'],x['entity_id']):x for x in receipts}
    if any(k[0]=='artifact_protocol' for k in by_key): raise RuntimeError('Cannot downgrade after contextual artifact publication')
    boundary=by_key.get(('boundary','global'));actual=sorted([list(k) for k in by_key if k!=('boundary','global')])
    if boundary is None or boundary['after_state'] != {'schema_version':1,'entities':actual}: raise RuntimeError('Missing or damaged platform migration receipts')
    users={str(x['id']):dict(x) for x in bind.execute(sa.text('SELECT * FROM sys_user')).mappings()}
    templates={str(x['id']):dict(x) for x in bind.execute(sa.text('SELECT * FROM sys_role_template')).mappings()}
    for (kind,entity_id),receipt in by_key.items():
        if kind=='boundary': continue
        if kind=='user':
            row=users.get(entity_id);current=_user_state(row,_grants(bind,row['id'])) if row else None
        elif kind=='template':
            row=templates.get(entity_id);current=_template_state(row) if row else None
        else: raise RuntimeError('Unknown platform migration receipt')
        if current != receipt['after_state']: raise RuntimeError('Authorization changed after migration; downgrade refused')
        before=receipt['before_state']
        if type(before) is not dict or set(before) != {'state','predecessor_digest','predecessor_matches'}: raise RuntimeError('Invalid predecessor receipt')
    for entity_id,row in users.items():
        if ('user',entity_id) not in by_key and (_contains_s4(_grants(bind,row['id'])) or _contains_s4(_materialized(profile(row['authorization_profile'])))):
            raise RuntimeError('Unrecorded platform user authorization prevents downgrade')
    for entity_id,row in templates.items():
        if ('template',entity_id) not in by_key and _contains_s4(row['authorization_bundle']): raise RuntimeError('Unrecorded platform template authorization prevents downgrade')
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
            bind.execute(sa.text("UPDATE sys_authorization_migration_receipt SET after_state=CAST(:after AS jsonb) WHERE migration_key='authz_s3' AND entity_kind=:kind AND entity_id=:id"),{'after':json.dumps(restored),'kind':kind,'id':entity_id})
    bind.execute(sa.text('DELETE FROM sys_authorization_migration_receipt WHERE migration_key=:key'),{'key':KEY})

    op.drop_table('sys_import_job_item')
    op.drop_index('ix_import_batch_authorization_source',table_name='sys_import_batch')
    op.drop_column('sys_import_batch','source_domain')
    op.drop_column('sys_import_batch','authorization_protocol')
    op.drop_constraint('fk_import_job_owner','sys_import_job',type_='foreignkey')
    for name in ('owner_user_id','authorization_protocol','enqueued_epoch','authorization_generation','manifest_digest'):
        op.drop_column('sys_import_job',name)
    op.drop_constraint('ck_authorization_grant_scope','sys_authorization_grant',type_='check')
    prior_scopes = [scope for scope in SCOPES if scope != 'self']
    op.create_check_constraint('ck_authorization_grant_scope','sys_authorization_grant','scope IN ('+', '.join(repr(s) for s in prior_scopes)+')')
