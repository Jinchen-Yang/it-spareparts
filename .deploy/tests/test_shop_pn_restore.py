"""显式开启后仅在 jinchen-dev 运行：两个真实 PG15 network-none 恢复，纯合成记录。"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'shop_pn_ops.py'
SPEC=importlib.util.spec_from_file_location('shop_pn_ops_restore',SOURCE)
ops=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


@unittest.skipUnless(os.environ.get('SHOP_PN_RESTORE_DRILL')=='1','explicit isolated dev drill only')
class RestoreDrill(unittest.TestCase):
    def test_actual_docker_api_clone_preserves_configuration(self):
        self.assertEqual(subprocess.check_output(['hostname'],text=True).strip(),'jinchen-dev')
        owner=secrets.token_hex(10)
        prefix='shop-pn-clone-'+owner
        net=subprocess.check_output(['docker','network','create','--label','codex.shop-pn-drill='+owner,prefix],text=True).strip()
        ids=[]
        try:
            old_id=subprocess.check_output(['docker','create','--name',prefix+'-old','--network',prefix,'--network-alias','backend',
                '--read-only','--tmpfs','/tmp','--label','codex.shop-pn-drill='+owner,'-e','SYNTHETIC_CONFIG=unchanged','postgres:15'],text=True).strip()
            ids.append(old_id)
            old=json.loads(subprocess.check_output(['docker','inspect',old_id]))[0]
            cfg,host=ops.candidate_config(old,old['Image'],'e'*40,'f'*64)
            networks={name:{'Aliases':row.get('Aliases') or []} for name,row in old['NetworkSettings']['Networks'].items()}
            body={**cfg,'HostConfig':host,'NetworkingConfig':{'EndpointsConfig':networks}}
            created=json.loads(subprocess.check_output(['curl','-fsS','--unix-socket','/var/run/docker.sock','-H','Content-Type: application/json',
                '--data-binary','@-','http://localhost/containers/create?name='+prefix+'-new'],input=json.dumps(body).encode()))
            ids.append(created['Id'])
            current=json.loads(subprocess.check_output(['docker','inspect',created['Id']]))[0]
            for group,expected in [('Config',cfg),('HostConfig',host)]:
                self.assertEqual(set(current[group]),set(expected))
                for key,value in expected.items():
                    actual=current[group][key]
                    if group=='HostConfig' and key=='OomKillDisable':
                        value,actual=bool(value),bool(actual)
                    self.assertEqual(actual,value,(group,key))
            self.assertEqual(set(current['NetworkSettings']['Networks']),set(networks))
            for name,expected in networks.items():
                self.assertEqual(set(current['NetworkSettings']['Networks'][name]['Aliases']),set(expected['Aliases']))
            self.assertFalse(current['State']['Running'])
            print(json.dumps({'actual_docker_api_clone':'passed','started_candidate':False}))
        finally:
            for cid in ids:
                row=json.loads(subprocess.check_output(['docker','inspect',cid]))[0]
                self.assertEqual(row['Config']['Labels'].get('codex.shop-pn-drill'),owner)
                subprocess.run(['docker','rm','-v',cid],check=True,capture_output=True)
            network=json.loads(subprocess.check_output(['docker','network','inspect',net]))[0]
            self.assertEqual(network['Labels'].get('codex.shop-pn-drill'),owner)
            subprocess.run(['docker','network','rm',net],check=True,capture_output=True)

    def test_old_and_new_head_restore(self):
        self.assertEqual(subprocess.check_output(['hostname'],text=True).strip(),'jinchen-dev')
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix='shop-pn-drill-') as temporary:
            root=Path(temporary)
            owner=secrets.token_hex(10)
            name='shop-pn-drill-'+owner
            envfile=root/'source.env'
            envfile.write_text('POSTGRES_USER=shop\nPOSTGRES_DB=shop_next\nPOSTGRES_PASSWORD='+secrets.token_hex(24)+'\n')
            cid=subprocess.check_output(['docker','run','-d','--name',name,'--network','none','--memory','1g','--cpus','1',
                '--label','codex.shop-pn-drill='+owner,'--env-file',str(envfile),'postgres:15'],text=True).strip()
            try:
                for _ in range(60):
                    ready=subprocess.run(['docker','exec',cid,'pg_isready','-h','127.0.0.1','-U','shop','-d','shop_next'],capture_output=True)
                    if ready.returncode==0:
                        break
                    time.sleep(1)
                else:
                    self.fail('synthetic source did not start')
                release=ops.Release.__new__(ops.Release)
                release.root=root
                release.release='e'*40
                release.prefix='shop-pn-drill-'+owner
                original_inspect=release.inspect
                release.inspect=lambda value,image=False:original_inspect(cid if value==ops.DB else value,image)
                release.mark=lambda phase:{'db_image':release.inspect(ops.DB)['Image']}
                def mutate(sql):
                    subprocess.run(['docker','exec',cid,'psql','-U','shop','-d','shop_next','-v','ON_ERROR_STOP=1','-c',sql],check=True,capture_output=True)
                mutate("CREATE TABLE alembic_version(version_num varchar(64) primary key); INSERT INTO alembic_version VALUES('"+ops.OLD_HEAD+"'); CREATE TABLE pn(id integer primary key,description text); INSERT INTO pn VALUES(1,'synthetic'),(2,'fixture'); CREATE TABLE stock_txn(id integer primary key,qty integer); INSERT INTO stock_txn VALUES(1,3)")
                results=[]
                for suffix,head in [('old',ops.OLD_HEAD),('new',ops.NEW_HEAD)]:
                    if suffix=='new':
                        mutate("UPDATE alembic_version SET version_num='"+head+"'; CREATE TABLE catalog_delivery_receipt(delivery_id uuid primary key)")
                    counts=release.counts(container=cid)
                    dump=root/(suffix+'.dump')
                    with dump.open('wb') as output:
                        subprocess.run(['docker','exec',cid,'pg_dump','-U','shop','-d','shop_next','-Fc'],stdout=output,check=True)
                    receipt=release.restore(dump,head,counts,suffix)
                    self.assertTrue(receipt['actual_restore_performed'])
                    self.assertEqual(receipt['network'],'none')
                    results.append({'head':head,'tables':len(counts),'status':'passed'})
                print(json.dumps({'isolated_restore_drill':'passed','results':results}))
            finally:
                row=json.loads(subprocess.check_output(['docker','inspect',cid]))[0]
                self.assertEqual(row['Name'],'/'+name)
                self.assertEqual(row['Config']['Labels'].get('codex.shop-pn-drill'),owner)
                subprocess.run(['docker','rm','-f','-v',cid],check=True,capture_output=True)


if __name__=='__main__':
    unittest.main()
