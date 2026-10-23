from pathlib import Path
import json, time, socket, subprocess, importlib.metadata as im
import httpx
from sqlalchemy import Table, MetaData, Column, String, Integer, create_engine, insert, select, func
from cerbos.sdk.client import CerbosClient
from cerbos.sdk.model import Principal, Resource, ResourceDesc
from cerbos_sqlalchemy import get_query
BASE=Path(__file__).parent
policies=BASE/'policies';policies.mkdir(exist_ok=True)
(policies/'sales_order.yaml').write_text('''apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: default
  resource: sales_order
  rules:
    - actions: [read, export]
      effect: EFFECT_ALLOW
      roles: [all_reader]
    - actions: [read, export]
      effect: EFFECT_ALLOW
      roles: [own_reader]
      condition:
        match:
          expr: request.principal.attr.salesperson != "" && request.resource.attr.owner_id == request.principal.attr.salesperson
''')
ports=[]
for _ in range(3):
 with socket.socket() as s:
  s.bind(('127.0.0.1',0));ports.append(s.getsockname()[1])
http_port,grpc_port,debug_port=ports
cfg=BASE/'cerbos.yaml'
cfg.write_text(f'''server:
  httpListenAddr: 127.0.0.1:{http_port}
  grpcListenAddr: 127.0.0.1:{grpc_port}
storage:
  driver: disk
  disk:
    directory: {policies}
    watchForChanges: false
telemetry:
  disabled: true
''')
log=(BASE/'cerbos.log').open('w')
proc=subprocess.Popen([str(BASE/'binary/cerbos'),'server','--config',str(cfg),'--debug-listen-addr',f'127.0.0.1:{debug_port}'],stdout=log,stderr=subprocess.STDOUT)
result={'pdp_version':'0.57.0','ports':ports,'python_packages':{d.metadata['Name']:d.version for d in im.distributions()},'cases':[]}
try:
 for _ in range(100):
  if proc.poll() is not None: raise RuntimeError((BASE/'cerbos.log').read_text())
  try:
   with socket.create_connection(('127.0.0.1',http_port),timeout=.2):break
  except OSError:time.sleep(.05)
 else:raise RuntimeError('PDP did not open listener')
 table=Table('orders',MetaData(),Column('id',String,primary_key=True),Column('owner_id',String),Column('amount',Integer))
 engine=create_engine('sqlite:///:memory:');table.metadata.create_all(engine)
 rows=[{'id':'1','owner_id':'alice','amount':100},{'id':'2','owner_id':'bob','amount':200},{'id':'3','owner_id':None,'amount':300}]
 with engine.begin() as conn:conn.execute(insert(table),rows)
 identities=[('own','own_reader','alice',['1']),('all','all_reader','alice',['1','2','3']),('unbound','own_reader','',[]),('denied','no_grant','alice',[])]
 with CerbosClient(f'http://127.0.0.1:{http_port}',raise_on_error=True,timeout_secs=2.0) as client:
  for label,role,binding,expected in identities:
   p=Principal(id=f'synthetic-{label}',roles={role},attr={'salesperson':binding})
   single=[r['id'] for r in rows if client.is_allowed('read',p,Resource(id=r['id'],kind='sales_order',attr={'owner_id':r['owner_id']}))]
   plan=client.plan_resources('read',p,ResourceDesc(kind='sales_order'))
   query=get_query(plan,table,{'request.resource.attr.owner_id':table.c.owner_id})
   with engine.connect() as conn:
    actual=list(conn.execute(query.with_only_columns(table.c.id)).scalars())
    scoped=query.subquery(); count,total=conn.execute(select(func.count(),func.coalesce(func.sum(scoped.c.amount),0)).select_from(scoped)).one()
   assert single==actual==expected,(label,single,actual,expected)
   result['cases'].append({'label':label,'check_ids':single,'sql_ids':actual,'count':count,'sum':total,'sql':str(query),'plan':plan.to_dict()})
  allowed=client.is_allowed('write',Principal(id='synthetic',roles={'all_reader'}),Resource(id='1',kind='sales_order',attr={'owner_id':'alice'}))
  assert not allowed
  result['undefined_action_denied']=not allowed
 result['listeners']=subprocess.run(['ss','-ltnp'],capture_output=True,text=True).stdout.splitlines()
 result['listeners']=[line for line in result['listeners'] if any(f':{port} ' in line for port in ports)]
 proc.terminate();proc.wait(timeout=10)
 with CerbosClient(f'http://127.0.0.1:{http_port}',raise_on_error=False,timeout_secs=.2) as client:
  try:
   client.is_allowed('read',Principal(id='synthetic',roles={'all_reader'}),Resource(id='1',kind='sales_order'))
  except httpx.ConnectError as exc:
   result['pdp_unavailable']={'behavior':'raises','type':type(exc).__name__,'raise_on_error':False}
  else:
   raise AssertionError('Expected network error was not raised')
 result['status']='passed'
finally:
 if proc.poll() is None:proc.terminate();proc.wait(timeout=10)
 result['pdp_exit_code']=proc.returncode
 log.close()
 (BASE/'cerbos-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
print(json.dumps({k:v for k,v in result.items() if k not in ['python_packages','listeners']},ensure_ascii=False,indent=2,default=str))
