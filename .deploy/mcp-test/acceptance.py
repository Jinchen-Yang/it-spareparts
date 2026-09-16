"""Live smoke test for the explicitly named developer TEST deployment only."""
import asyncio
import hashlib
import io
import json
import os
import time
import uuid
from pathlib import Path
import httpx
from openpyxl import Workbook
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = 'https://mcp-test.yabowei.xyz'
PROJECT = '059f6ecd-55bb-43f2-94b0-cf9c36160916'
private = Path(os.environ['PARTFLOW_TEST_PRIVATE'])
creds = json.loads((private/'test-account.json').read_text())
scopes = ['mcp.use','mcp.files.upload','mcp.files.read','mcp.files.download','mcp.projects.read',
          'mcp.import.preview','mcp.jobs.read','mcp.export','mcp.parts.read','mcp.inventory.read','mcp.audit.read']
checks=[]
with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as c:
    page=c.get('/mcp-office'); page.raise_for_status()
    assert '开发测试环境 · 非生产系统' in page.text
    assert 'partflow_mcp_test_20260916' in page.text
    checks.append('Public HTTPS and visible TEST database label')
    denied=c.post('/mcp',json={}); assert denied.status_code==401
    r=c.post('/api/auth/login',json=creds); r.raise_for_status()
    login=r.json(); c.headers['Authorization']='Bearer '+login['token']
    def tool(name,**args):
        r=c.post('/api/mcp-office/tools',json={'name':name,'arguments':args}); r.raise_for_status()
        return r.json()['data']
    caps=tool('pf_get_capabilities')
    assert caps['environment']['is_test'] is True
    assert caps['environment']['base_url']==BASE
    assert 'partflow_mcp_test_20260916' in caps['environment']['dataset_label']
    r=c.post('/api/mcp-office/credentials',json={'scopes':scopes,'days':7}); r.raise_for_status()
    token=r.json()
    (private/'workbuddy-mcp-test.json').write_text(json.dumps({'mcpServers':{'partflow-TEST':{
        'type':'streamableHttp','url':BASE+'/mcp','headers':{'Authorization':'Bearer '+token['token']},'timeout':60000}}},indent=2))
    (private/'workbuddy-mcp-test.json').chmod(0o600)
    (private/'credential-metadata.json').write_text(json.dumps({k:v for k,v in token.items() if k!='token'}))
    async def protocol():
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token['token']},trust_env=False,timeout=60) as http:
            async with streamable_http_client(BASE+'/mcp',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    init=await session.initialize()
                    assert init.serverInfo.name=='PARTFLOW-TEST'
                    listed=await session.list_tools(); assert len(listed.tools)==16
                    result=await session.call_tool('pf_get_capabilities',{})
                    assert not result.isError and result.structuredContent['data']['environment']['is_test']
    asyncio.run(protocol()); checks.append('Official MCP client initialize / 16 tools / TEST capabilities over public HTTPS')
    wb=Workbook(); ws=wb.active; ws.title='验收清单'; ws.append(['验收需求','是否完成'])
    ws.append(['MCP 测试文件往返校验','是']); ws.append(['MacBook WorkBuddy 手工联调','否'])
    b=io.BytesIO(); wb.save(b); data=b.getvalue()
    up=tool('pf_create_upload_session',files=[{'name':'MCP-TEST-验收清单.xlsx','size_bytes':len(data),'mime':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}],purpose='document_import',idempotency_key=str(uuid.uuid4()))
    r=c.put(f"/api/mcp-office/uploads/{up['upload_session_id']}/{up['entries'][0]['entry_id']}",content=data); r.raise_for_status()
    fid=r.json()['file_id']
    def wait_job(job):
        for _ in range(60):
            out=tool('pf_get_job',job_id=job['job_id'])
            if out['status'] in ('ready','succeeded'): return out['result']
            assert out['status'] not in ('failed','cancelled'), out.get('error')
            time.sleep(2)
        raise RuntimeError('Worker job timeout')
    out=wait_job(tool('pf_preview_import',file_ids=[fid],family='acceptance_checklist',project_id=PROJECT,idempotency_key=str(uuid.uuid4())))
    preview_id=out['items'][0]['preview_id']
    preview=tool('pf_get_import_preview',preview_id=preview_id)
    assert preview['summary']['item_rows']==2
    r=c.post(f'/api/mcp-office/reviews/{preview_id}/confirm',json={'preview_hash':preview['preview_hash']}); r.raise_for_status()
    confirmed=tool('pf_get_import_preview',preview_id=preview_id); assert confirmed['status']=='applied'
    checks.append('Upload / durable worker preview / named TEST project confirmation and receipt')
    result=wait_job(tool('pf_create_export',export_kind='acceptance_checklist',project_ids=[PROJECT],idempotency_key=str(uuid.uuid4())))
    (private/'last-export-result.json').write_text(json.dumps(result))
    artifact=result['artifact_id']
    info=tool('pf_get_download',artifact_id=artifact)
    r=c.get(f'/api/mcp-office/artifacts/{artifact}/download'); r.raise_for_status()
    assert hashlib.sha256(r.content).hexdigest()==info['sha256']
    checks.append('Worker export / protected download / SHA-256 match')
    parts=tool('pf_search_parts',query='DELL',limit=5)
    assert isinstance(parts['items'],list)
    assert parts['items'], 'Expected restored PN catalog results'
    tool('pf_get_inventory',query='DELL',page=1)
    audit=tool('pf_search_audit',date_from='2026-09-16',date_to='2026-09-16',limit=200)
    assert any(e['stage']=='download_completed' for e in audit['events'])
    checks.append('Restored PN and inventory queries; download completion audit')
    tmp=c.post('/api/mcp-office/credentials',json={'scopes':['mcp.use'],'days':1}); tmp.raise_for_status()
    tmp=tmp.json()
    r=c.delete('/api/mcp-office/credentials/'+tmp['credential_id']); r.raise_for_status()
    denied=c.post('/mcp',json={},headers={'Authorization':'Bearer '+tmp['token']})
    assert denied.status_code==401
    checks.append('Revoked personal token rejected')
    (private/'acceptance-state.json').write_text(json.dumps({'base':BASE,'project_id':PROJECT,'preview_id':preview_id,'checks':checks,'export_result':result},ensure_ascii=False,indent=2))
print(json.dumps({'checks':checks,'status':'passed'},ensure_ascii=False))
