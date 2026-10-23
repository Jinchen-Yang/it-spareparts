from pathlib import Path
from types import SimpleNamespace as NS
import json, importlib.metadata as im
import casbin
from casbin.model import Model
from sqlalchemy import Table, MetaData, Column, String, Integer, create_engine, insert, select
BASE=Path(__file__).parent
model_text='''[request_definition]
r = sub, obj, act
[policy_definition]
p = sub, scope, act
[role_definition]
g = _, _
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = g(r.sub.id, p.sub) && r.act == p.act && (p.scope == "all" || (p.scope == "own" && r.sub.salesperson != "" && r.obj.owner_id == r.sub.salesperson))
'''
model=Model();model.load_model_from_text(model_text)
e=casbin.Enforcer(model)
e.add_policy('all_reader','all','read');e.add_policy('own_reader','own','read')
rows=[{'id':'1','owner_id':'alice','amount':100},{'id':'2','owner_id':'bob','amount':200},{'id':'3','owner_id':None,'amount':300}]
result={'packages':{d.metadata['Name']:d.version for d in im.distributions()},'cases':[],'sql_adapter':'not included: SQL scope must be authored by application'}
for label,role,binding,expected in [('own','own_reader','alice',['1']),('all','all_reader','alice',['1','2','3']),('unbound','own_reader','',[]),('denied','no_grant','alice',[])]:
 e.add_role_for_user(label,role)
 p=NS(id=label,salesperson=binding)
 actual=[r['id'] for r in rows if e.enforce(p,NS(**r),'read')]
 assert actual==expected,(label,actual,expected)
 assert not e.enforce(p,NS(**rows[0]),'write')
 result['cases'].append({'label':label,'check_ids':actual,'unknown_action_denied':True})
# Revoke in this process, then check again; not a cross-worker propagation test.
e.delete_role_for_user('own','own_reader')
assert not e.enforce(NS(id='own',salesperson='alice'),NS(**rows[0]),'read')
result['in_process_revocation']='passed';result['status']='passed'
(BASE/'casbin-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
(BASE/'casbin-model.conf').write_text(model_text)
print(json.dumps(result,ensure_ascii=False,indent=2))
