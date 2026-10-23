"""Versioned commercial authorization and opaque artifact provenance.

Frozen legacy interpretation; no imports from mutable application permissions.
Revision ID: c9d4e8f2a6b1
Revises: a7c2e9f4b6d1
"""
from copy import deepcopy
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c9d4e8f2a6b1'
down_revision = 'a7c2e9f4b6d1'
branch_labels = None
depends_on = None
DATA = json.loads((Path(__file__).parent.parent / 'seed_data/authz_s2.json').read_text())
DEFINITIONS = {r['resource_id']: r for r in DATA['definitions']}
RESOURCES = frozenset(DEFINITIONS)
KEY = 'authz_s2'
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


def evaluate(expression, perms, role):
    if type(expression) is not dict or len(expression) != 1:
        raise RuntimeError('Malformed frozen seed expression')
    operator, value = next(iter(expression.items()))
    if operator == 'constant': return value is True
    if operator == 'permission': return perms.get(value, False)
    if operator == 'role_in': return role in value
    if operator == 'all': return all(evaluate(x, perms, role) for x in value)
    if operator == 'any': return any(evaluate(x, perms, role) for x in value)
    if operator == 'not': return not evaluate(value, perms, role)
    raise RuntimeError('Unknown frozen seed expression')


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


def sales_upgrade(config):
    validate('sales_analysis', config, old_sales=True)
    result = deepcopy(config); actions = set(config['actions']); upgraded = set()
    contract = DATA['sales_upgrade']
    if 'read' in actions:
        upgraded.update(contract['old_read_maps_to'])
        if 'data_purchase_cost' in config['field_permissions']:
            upgraded.update(contract['old_read_and_cost_maps_to'])
    if 'export' in actions: upgraded.add('export')
    for surface in contract['display_rows']:
        if set(surface['old_actions_all']) <= actions and set(surface['old_fields_all']) <= set(config['field_permissions']):
            upgraded.add(surface['new_display_action'])
    result['actions'] = sorted(upgraded)
    validate('sales_analysis', result)
    return result


def upgrade_bundle(bundle):
    if type(bundle) is not dict: raise RuntimeError('Invalid canonical bundle')
    result = deepcopy(bundle)
    for resource, config in bundle.items():
        if resource == 'sales_analysis': result[resource] = sales_upgrade(config)
        else: validate(resource, config)
    return result


def _allows(rule, bundle):
    return any(all(need['resource_id'] in bundle
                   and set(need['actions']) <= set(bundle[need['resource_id']]['actions'])
                   and set(need.get('field_permissions', [])) <= set(bundle[need['resource_id']]['field_permissions'])
                   for need in alternative) for alternative in rule['anyOf'])


def seed(row, existing):
    if row.get('authorization_legacy_seed_eligible') is not True or row.get('deleted_at') is not None: return {}
    perms = effective(row); result = {}
    for resource, rules in DATA['resources'].items():
        if resource in existing: continue  # Presence includes an explicit empty denial.
        actions = sorted(a for a, predicate in rules['actions'].items() if evaluate(predicate, perms, row['role']))
        fields = sorted(f for f, predicate in rules['fields'].items() if evaluate(predicate, perms, row['role']))
        result[resource] = {'actions': actions, 'scope': 'all', 'field_permissions': fields if actions else []}
    combined = {**existing, **result}
    for surface in DATA['surfaces']:
        resource = surface['resource_id']; rule = surface['legacy_display_seed']
        if resource not in result: continue
        if (evaluate(rule['route_predicate'], perms, row['role']) and _allows(rule['data_predicate'], combined)
                and set(rule['fields_all']) <= set(result[resource]['field_permissions'])):
            result[resource]['actions'].append(surface['display_action'])
    for resource, config in result.items():
        config['actions'] = sorted(set(config['actions']))
        validate(resource, config)
    return result


def _profile(value):
    if type(value) is not dict or set(value) != {'schema_version', 'template_snapshot', 'resource_overrides'} or value['schema_version'] != 1:
        raise RuntimeError('Invalid source profile')
    result = deepcopy(value); overrides = upgrade_bundle(result['resource_overrides'])
    snapshot = result['template_snapshot']
    if snapshot is not None:
        if type(snapshot) is not dict or set(snapshot) != {'code', 'version', 'modules'}:
            raise RuntimeError('Invalid saved template snapshot')
        snapshot['modules'] = upgrade_bundle(snapshot['modules'])
    result['resource_overrides'] = overrides
    return result


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


def upgrade():
    op.create_table('sys_authorization_migration_receipt',
        sa.Column('migration_key',sa.String(64),primary_key=True), sa.Column('entity_kind',sa.String(32),primary_key=True),
        sa.Column('entity_id',sa.String(64),primary_key=True), sa.Column('before_state',postgresql.JSONB(),nullable=False),
        sa.Column('after_state',postgresql.JSONB(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()))
    op.add_column('chat_session',sa.Column('owner_user_id',sa.Integer(),nullable=True))
    op.create_foreign_key('fk_chat_session_owner_user_id_sys_user','chat_session','sys_user',['owner_user_id'],['id'])
    op.create_index('ix_chat_session_owner_user_updated','chat_session',['owner_user_id','updated_at'])
    op.add_column('chat_message',sa.Column('authorization_provenance',postgresql.JSONB(none_as_null=True),nullable=True))
    bind=op.get_bind()
    op.execute('LOCK TABLE sys_role_template, sys_user, sys_authorization_grant, sys_authorization_cutover IN ACCESS EXCLUSIVE MODE')
    entities=[]
    for raw in bind.execute(sa.text('SELECT * FROM sys_role_template ORDER BY id')).mappings().all():
        row=dict(raw);before=_template_state(row)
        bundle=upgrade_bundle(row['authorization_bundle'])
        source={**row,'role':row['base_role'],'template_perms':row['permissions'],'perm_overrides':None}
        bundle.update(seed(source,bundle))
        if bundle==row['authorization_bundle']:
            if bundle:
                _receipt(bind,'template',row['id'],before,before);entities.append(['template',str(row['id'])])
            continue
        row['authorization_bundle']=bundle;row['version']=_next(row['version'])
        bind.execute(sa.text('UPDATE sys_role_template SET authorization_bundle=CAST(:bundle AS jsonb),version=:version WHERE id=:id'),
                     {'bundle':json.dumps(bundle),'version':row['version'],'id':row['id']})
        _receipt(bind,'template',row['id'],before,_template_state(row));entities.append(['template',str(row['id'])])
    for raw in bind.execute(sa.text('SELECT * FROM sys_user ORDER BY id')).mappings().all():
        row=dict(raw);grants=_grants(bind,row['id']);before=_user_state(row,deepcopy(grants))
        original=_materialized(row['authorization_profile'])
        if set(grants)!=set(original) or any({k:v for k,v in g.items() if k!='version'}!=original[r] for r,g in grants.items()):
            raise RuntimeError('Canonical source and materialized Grants disagree; migration refused')
        profile=_profile(row['authorization_profile']);effective_bundle=_materialized(profile)
        additions=seed(row,effective_bundle)
        # Newly interpreted resources are personal whole overrides, never a forged old template snapshot.
        profile['resource_overrides'].update(additions);effective_bundle.update(additions)
        if profile==row['authorization_profile']:
            if grants:
                _receipt(bind,'user',row['id'],before,before);entities.append(['user',str(row['id'])])
            continue
        floor=_next(row['token_version'],*(g['version'] for g in grants.values()))
        row['authorization_profile']=profile;row['token_version']=floor
        for resource, config in effective_bundle.items():
            old=grants.get(resource)
            if old is None or config!={k:v for k,v in old.items() if k!='version'}:
                _write_grant(bind,row['id'],resource,config,floor)
                grants[resource]={**deepcopy(config),'version':floor}
        bind.execute(sa.text('UPDATE sys_user SET authorization_profile=CAST(:profile AS jsonb),token_version=:epoch WHERE id=:id'),
                     {'profile':json.dumps(profile),'epoch':floor,'id':row['id']})
        _receipt(bind,'user',row['id'],before,_user_state(row,grants));entities.append(['user',str(row['id'])])
    _receipt(bind,'boundary','global',{}, {'schema_version':1,'entities':sorted(entities)})


def downgrade():
    bind=op.get_bind()
    op.execute('LOCK TABLE sys_authorization_migration_receipt, sys_authorization_cutover, sys_role_template, sys_user, sys_authorization_grant, chat_session, chat_message IN ACCESS EXCLUSIVE MODE')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM sys_authorization_cutover)')).scalar():
        raise RuntimeError('Cannot downgrade after authorization cutover')
    receipts=bind.execute(sa.text('SELECT entity_kind,entity_id,before_state,after_state FROM sys_authorization_migration_receipt WHERE migration_key=:key'),{'key':KEY}).mappings().all()
    by_key={(r['entity_kind'],r['entity_id']):r for r in receipts}
    if any(kind=='artifact_protocol' for kind,_ in by_key):
        raise RuntimeError('Cannot downgrade after unified artifact format publication')
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM chat_session WHERE owner_user_id IS NOT NULL) OR EXISTS(SELECT 1 FROM chat_message WHERE authorization_provenance IS NOT NULL)')).scalar():
        raise RuntimeError('Cannot downgrade with persistent chat authorization provenance')
    boundary=by_key.get(('boundary','global'))
    actual=sorted([list(k) for k in by_key if k!=('boundary','global')])
    if boundary is None or boundary['after_state']!={'schema_version':1,'entities':actual}:
        raise RuntimeError('Missing or damaged authorization migration receipts')
    users={str(r['id']):dict(r) for r in bind.execute(sa.text('SELECT * FROM sys_user')).mappings()}
    templates={str(r['id']):dict(r) for r in bind.execute(sa.text('SELECT * FROM sys_role_template')).mappings()}
    for (kind,entity_id), receipt in by_key.items():
        if kind=='boundary':continue
        if kind=='user':
            row=users.get(entity_id)
            current=_user_state(row,_grants(bind,row['id'])) if row else None
        elif kind=='template':
            row=templates.get(entity_id);current=_template_state(row) if row else None
        else:raise RuntimeError('Unknown authorization migration receipt')
        if current!=receipt['after_state']:
            raise RuntimeError('Authorization changed after migration; downgrade would erase current decisions')
    # New S1-representable empty/legacy fixtures are safe; any unrecorded S2 canonical state is not.
    for entity_id,row in users.items():
        if ('user',entity_id) not in by_key and (_grants(bind,row['id']) or _materialized(row['authorization_profile'])):
            raise RuntimeError('Unrecorded canonical user authorization prevents downgrade')
    for entity_id,row in templates.items():
        if ('template',entity_id) not in by_key and row['authorization_bundle']:
            raise RuntimeError('Unrecorded canonical template authorization prevents downgrade')
    for (kind,entity_id), receipt in by_key.items():
        before=receipt['before_state']
        if kind=='user':
            row=users[entity_id];current=_grants(bind,row['id'])
            floor=_next(row['token_version'],*(g['version'] for g in current.values()))
            for resource in set(current)-set(before['grants']):
                bind.execute(sa.text('DELETE FROM sys_authorization_grant WHERE user_id=:id AND resource=:resource'),{'id':row['id'],'resource':resource})
            for resource,g in before['grants'].items(): _write_grant(bind,row['id'],resource,g,floor)
            bind.execute(sa.text('UPDATE sys_user SET authorization_profile=CAST(:profile AS jsonb),token_version=:epoch WHERE id=:id'),
                         {'profile':json.dumps(before['auth']['authorization_profile']),'epoch':floor,'id':row['id']})
        elif kind=='template':
            row=templates[entity_id]
            bind.execute(sa.text('UPDATE sys_role_template SET authorization_bundle=CAST(:bundle AS jsonb),version=:version WHERE id=:id'),
                         {'bundle':json.dumps(before['authorization_bundle']),'version':_next(row['version']),'id':row['id']})
    op.drop_column('chat_message','authorization_provenance')
    op.drop_index('ix_chat_session_owner_user_updated',table_name='chat_session')
    op.drop_constraint('fk_chat_session_owner_user_id_sys_user','chat_session',type_='foreignkey')
    op.drop_column('chat_session','owner_user_id')
    op.drop_table('sys_authorization_migration_receipt')
