"""Build the small runtime registry from the reviewed audit specification."""
import json,pathlib,itertools,pprint,re
ROOT=pathlib.Path(__file__).resolve().parents[3];d=json.loads((ROOT/'docs/reviews/authz-rewrite-20261023/s3-execution-spec.json').read_text())
cp=d['context_protocol']
def unique_options(xs):return sorted(set(frozenset(x) for x in xs),key=lambda x:sorted(x))
def cross(left,right):return unique_options(a|b for a in left for b in right)
def opts(b):
 result=[frozenset()]
 if 'resource_id'in b and 'action'in b:result=[frozenset([(b['resource_id'],b['action'])])]
 if 'require'in b:result=cross(result,[frozenset([tuple(b['require'])])])
 if 'also_require'in b:result=cross(result,[frozenset(map(tuple,b['also_require']))])
 if 'select_one_complete_contract'in b:result=cross(result,[frozenset([tuple(p)]) for p in b['select_one_complete_contract']])
 if 'branches'in b:result=cross(result,[x for branch in b['branches'] for x in opts(branch)])
 if 'conditional_action'in b:result=cross(result,[frozenset()]+opts(b['conditional_action']))
 if 'conditional_block'in b:result=cross(result,opts(b['conditional_block']))
 if 'operation_requirements'in b:
  for conditional in b['operation_requirements']:result=cross(result,[frozenset()]+opts(conditional))
 if 'composite'in b:
  for block in b['composite']['blocks']:result=cross(result,opts(block))
 return result
keep={'type','properties','items','required','additionalProperties','anyOf','oneOf','allOf','const','enum','pattern','minimum','maximum','exclusiveMinimum','exclusiveMaximum','minLength','maxLength','minItems','maxItems','uniqueItems','format','default'}
def schema(v):
 if isinstance(v,dict):return {k:({kk:schema(vv) for kk,vv in x.items()} if k=='properties' else schema(x)) for k,x in v.items() if k in keep}
 if isinstance(v,list):return [schema(x) for x in v]
 return v
entries={}
for e in d['entries']:
 eid=e['entry_id'];q=e['context_requirement'];ss=q.get('all_of',[q]);slots={};conditional_slots={}
 extras=[x for x in q.get('additional_domain_slots',[]) if 'runtime_kind' in x]
 ss=[*ss,*[{**x,'kind':x['runtime_kind']} for x in extras]]
 for x in extras:conditional_slots[x['slot']]=[tuple(pair) for pair in x['required_when_actions']]
 for s in ss:
  if s['kind']=='none':continue
  item={'kind':s['kind']}
  if s['kind']=='object':item['object_type']=s['object_type']
  if s['kind']=='collection':item['collection_ref']=s['id']
  if s.get('purpose'):item['purpose']=s['purpose']
  item['selector_schema']=schema(s.get('selector_schema',{'type':'object','properties':{},'additionalProperties':False}))
  item['selector_schema'].get('properties',{}).pop('purpose',None)
  if 'purpose' in item['selector_schema'].get('required',[]):item['selector_schema']['required'].remove('purpose')
  item['authorizing']=s.get('authority') not in {'discovery_only','discovery_only_non_authorizing'}
  slots[s['slot']]=item
 variants=[frozenset()]
 for b in e['bindings']:variants=cross(variants,opts(b))
 # Ratio is a supplied-query action on each actual endpoint accepting that field.
 if any('value_retention_ratio' in s.get('selector_schema',{}).get('properties',{}) for s in ss):
  variants=cross(variants,[frozenset(),frozenset([('maintenance_dashboard','adjust_valuation_parameters')])])
 branches={}
 for i,req in enumerate(unique_options(variants)):
  if not req:continue
  requirements={r:sorted(a for rr,a in req if rr==r) for r in sorted({r for r,a in req})}
  branch='default' if len(variants)==1 else f'variant_{i}'
  carriers=[k for k,v in slots.items() if k not in conditional_slots and v.get('object_type')=='operation_input'] or [k for k,v in slots.items() if k not in conditional_slots and v['kind']=='object'] or [k for k,v in slots.items() if k not in conditional_slots and v['authorizing']]
  selected_slots=[key for key in slots if key not in conditional_slots or all(pair in req for pair in conditional_slots[key])]
  branches[branch]={'requirements':requirements,'slots':selected_slots,'carrier_slots':carriers[:1]}
  if eid=='POST /api/maintenance/warehouse-imports/{import_id}/apply':
   branches={'existing':{'requirements':requirements,'slots':['context_0'],'carrier_slots':['context_0']},'new':{'requirements':requirements,'slots':['context_1'],'carrier_slots':['context_1']}}
 if q['empty_target_set_allowed']:
  assert len(branches)==1,eid
  initial=next(iter(branches.values()));es=[k for k,v in slots.items() if v['kind']=='collection' or v.get('object_type')=='operation_input'];branches['empty']={'requirements':initial['requirements'],'slots':es,'carrier_slots':[k for k in es if slots[k].get('object_type')=='operation_input'],'empty_target_set':True}
 for name,alternative in e.get('runtime_topology_alternatives',{}).items():
  slots.update(alternative['slots'])
  branches[name]={'requirements':alternative['requirements'],'slots':alternative.get('existing_slots',list(alternative['slots'])),'carrier_slots':alternative['carrier_slots']}
 if replay:=e.get('persistent_replay_contract'):
  receipt=replay['slot']
  slots[receipt]={'kind':'object','object_type':replay['object_type'],'selector_schema':schema(replay['selector_schema']),'authorizing':True}
  for name,original in list(branches.items()):
   branches[replay['branch_prefix']+name]={**original,'slots':[*original['slots'],receipt],'carrier_slots':[receipt]}
 if 'admission_rule' in e:
  for branch_definition in branches.values():branch_definition['admission_rule']=e['admission_rule']
 entries[eid]={'branches':branches,'slots':slots,'empty_target_set_allowed':q['empty_target_set_allowed']}
# Deliberate base snapshot branch is separate from each business entry.
for view in cp['authorization_view_resolvers']:
 eid=f"{view['method']} {view['path']}";contract=entries[eid]
 base=next(iter(contract['branches'].values()))
 contract['branches']['authorization_view']={'requirements':view['authorization_requirements'],'slots':base['slots'],'carrier_slots':base['carrier_slots'],'authorization_view_only':True}
views={}
for v in cp['authorization_bootstrap_entries']:
 eid=v['entry_id'];method,path=eid.split(' ',1)
 views[eid]={'method':method,'path':path,'view':'authorization','registry_sections':v['registry_sections'],'only_entry_actions':True,'no_business_payload':True}
 base=next((x for x in cp['authorization_view_resolvers'] if f"{x['method']} {x['path']}"==eid),None)
 if base:views[eid].update(only_entry_actions=False,authorization_requirements=base['authorization_requirements'],target_entry_ids=base['target_entry_ids'],deferred_command_entries=base['deferred_command_entries'])
# Only exact path-key carriers can be checked by the transport before any facts.
for entry in d['entries']:
 eid=entry['entry_id']
 if eid not in views:continue
 requirement=entry['context_requirement'];source_slots={s['slot']:s for s in requirement.get('all_of',[requirement]) if 'slot' in s}
 carriers={key for branch in entries[eid]['branches'].values() for key in branch['carrier_slots']}
 paths={source_slots.get(key,{}).get('id_from') for key in carriers}
 if len(paths)==1:
  source=next(iter(paths))
  match=re.fullmatch(r'path\.([a-z_]+)',source or '')
  if match and '{'+match.group(1)+'}' in views[eid]['path']:
   views[eid]['locator_path_parameter']=match.group(1)

# plain literal metadata; no runtime docs load, SQL, dynamic import or condition evaluator.
p=ROOT/'backend/app/authz/context_registry.py'
header='"""Closed S3 domain entry contracts and authorization views.\n\nGenerated literal data: no runtime docs import, legacy roles or condition eval.\nDomains select registered branches from actual input and stored topology; core\nchecks exact action sets and mandatory slots. Audit evidence remains in docs.\n"""\n\n'
parts=[header, 'CLOSED_KINDS = '+repr(tuple(cp['closed_internal_context_kinds']))+'\n\n', 'COLLECTION_REFS = '+repr(tuple(x['id'] for x in cp['closed_collection_refs']))+'\n\n']
for name, values in [('ENTRY_CONTRACTS',entries),('AUTHORIZATION_VIEWS',views)]:
 parts.append(name+' = {\n')
 parts.extend('    '+repr(key)+': '+repr(value)+',\n' for key,value in values.items())
 parts.append('}\n\n')
p.write_text(''.join(parts))
print(len(entries),len(views),p.stat().st_size)
