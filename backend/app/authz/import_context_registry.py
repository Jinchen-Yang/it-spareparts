"""Exact import input/source branches; no caller may choose the branch or source."""
from copy import deepcopy
from itertools import combinations
from app.authz.import_contract import *

CLOSED_KINDS = ('generic_import_job', 'generic_import_batch', 'generic_import_item')
COLLECTION_REFS = ('generic_import_jobs', 'generic_import_batches')
ENTRY_CONTRACTS = {}
AUTHORIZATION_VIEWS = {}
_EMPTY = {'type':'object','additionalProperties':False,'properties':{},'required':[]}
_HASH = {'type':'object','additionalProperties':False,'properties':{
    'command_sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'}},'required':['command_sha256']}

def source_branch(sources, mode=None):
    return '_'.join(sorted(sources)) + ('_' + mode if mode else '')

def _rule(requirements, any_of=False):
    result = {'requirements':requirements,'slots':['context_0'],'carrier_slots':['context_0']}
    if any_of:
        result['admission_rule']={'anyOf':[{'resource_id':r,'action':a,'required_fields':[]}
            for r, aa in requirements.items() for a in aa]}
    return result

def _add(entry, slot, branches):
    ENTRY_CONTRACTS[entry]={'channel':'worker' if entry==WORKER else 'http',
        'empty_target_set_allowed':False,'slots':{'context_0':slot},'branches':branches}
    if entry!=WORKER:
        method,path=entry.split(' ',1)
        view={'method':method,'path':path,'view':'authorization',
              'registry_sections':['platform_authorization_views'],
              'only_entry_actions':True,'no_business_payload':True,
              'target_entry_ids':[entry],'carrier_slots':['context_0']}
        if entry==JOB:view['locator_path_parameter']='job_id'
        elif entry in (BATCH,ERRORS):view['locator_path_parameter']='batch_id'
        AUTHORIZATION_VIEWS[entry]=view

for entry,purpose in ((PRECHECK,'import.precheck'),(UPLOAD,'import.upload'),
                      (UPLOAD_BATCH,'import.upload_batch'),(VOID,'import.expense_void_preview')):
    branches={}
    if entry==VOID:
        branches['default']=_rule({'import_expense_void':['preview']})
    else:
        for length in range(1,6):
            if entry==UPLOAD and length!=1:continue
            for sources in combinations(SOURCES,length):
                for mode in ((None,) if entry==PRECHECK else ('skip','upsert')):
                    branches[source_branch(sources,mode)]=_rule({f'import_{s}':
                        ['precheck' if entry==PRECHECK else f'submit_{mode}'] for s in sources})
        if entry==PRECHECK:
            branches['unrecognized_input']=_rule({f'import_{s}':['precheck'] for s in SOURCES},True)
    _add(entry,{'kind':'object','object_type':'operation_input','purpose':purpose,
        'authorizing':True,'selector_schema':deepcopy(_HASH)},branches)
for entry,collection in ((JOBS,'generic_import_jobs'),(BATCHES,'generic_import_batches')):
    _add(entry,{'kind':'collection','collection_ref':collection,'purpose':collection,
        'authorizing':True,'selector_schema':deepcopy(_EMPTY)},
        {'default':_rule({f'import_{s}':['read_status'] for s in SOURCES},True)})
branches={}
for length in range(1,6):
    for sources in combinations(SOURCES,length):
        branches[source_branch(sources)]=_rule({f'import_{s}':['read_status'] for s in sources},True)
_add(JOB,{'kind':'object','object_type':'generic_import_job','authorizing':True,
          'selector_schema':deepcopy(_EMPTY)},branches)
for entry in (BATCH,ERRORS,WORKER):
    branches={}
    for source in SOURCES:
        if entry==WORKER:
            for mode in ('skip','upsert'):
                branches[source_branch((source,),mode)]=_rule({f'import_{source}':[f'submit_{mode}']})
        else:
            branches[source]=_rule({f'import_{source}':
                ['read_status','read_report'] if entry==BATCH else ['export_errors']},entry==BATCH)
    _add(entry,{'kind':'object','object_type':'generic_import_item' if entry==WORKER else 'generic_import_batch',
        'authorizing':True,'selector_schema':deepcopy(_EMPTY)},branches)
