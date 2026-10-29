"""只在 jinchen-dev 执行的纯合成发布控制器测试。"""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'shop_pn_ops.py'
SPEC = importlib.util.spec_from_file_location('shop_pn_ops', SOURCE)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def row(name, running=True):
    return {'Id':name+'-id','Name':'/'+name,'Image':name+'-image','RestartCount':0,
            'State':{'Running':running,'StartedAt':'before','OOMKilled':False},
            'Config':{'Image':'original:tag','Env':['DATABASE_URL=postgresql://example/db','OTHER=unchanged'],
                      'Labels':{'com.docker.compose.service':'backend'},'Cmd':['uvicorn','app.main:app']},
            'HostConfig':{'ReadonlyRootfs':True,'NetworkMode':'it-shop-next_private','PortBindings':{}},
            'NetworkSettings':{'Networks':{'it-shop-next_private':{'Aliases':[name,'backend']}}}}


class ShopOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.release=ops.Release.__new__(ops.Release)
        self.release.root=self.root
        self.release.release='a'*40
        self.release.raw=b'{}'
        self.release.m={'old':{name:{'id':name+'-id','image':name+'-image'} for name in ops.MANAGED}}

    def test_python310_syntax(self):
        ast.parse(SOURCE.read_text(),feature_version=(3,10))

    def test_clone_changes_only_image_provenance_and_receiver_environment(self):
        old=row('old')
        before=copy.deepcopy(old)
        cfg,host=ops.candidate_config(old,'sha256:new','a'*40,'f'*64)
        self.assertEqual(old,before)
        self.assertEqual(host,old['HostConfig'])
        self.assertEqual(cfg['Cmd'],old['Config']['Cmd'])
        values=dict(x.split('=',1) for x in cfg['Env'])
        self.assertEqual(values,{'DATABASE_URL':'postgresql://example/db','OTHER':'unchanged',
                                'CATALOG_SYNC_ENABLED':'true','CATALOG_SYNC_TOKEN_SHA256':'f'*64})
        self.assertEqual(cfg['Labels']['com.docker.compose.service'],'backend')

    def test_frontend_environment_is_unchanged(self):
        old=row('front')
        cfg,host=ops.candidate_config(old,'sha256:new','a'*40,'f'*64,True)
        self.assertEqual(cfg['Env'],old['Config']['Env'])
        self.assertEqual(host,old['HostConfig'])

    def test_credential_reuse_is_rejected(self):
        for key,value,hashed in [('GALLERY_INTEGRATION_TOKEN_SHA256','f'*64,'f'*64),
                                 ('ITSP_CATALOG_TOKEN','synthetic-token',ops.sha(b'synthetic-token'))]:
            with self.subTest(key=key),self.assertRaises(RuntimeError):
                original=row('old')
                original['Config']['Env'].append(key+'='+value)
                ops.candidate_config(original,'new','a'*40,hashed)

    def test_recover_partial_freeze_after_stop_without_freeze_receipt(self):
        release=self.release
        (self.root/'freeze.intent.json').write_text('{}')
        release.mark=Mock(return_value={'entry':{}})
        release.entry=Mock(return_value={})
        release.protected=Mock()
        release.schema=Mock(return_value=ops.OLD_HEAD)
        started=set()
        def inspect(cid):
            current=row(cid.removesuffix('-id'),running=cid in started)
            current['State']['Health']={'Status':'healthy'}
            return current
        release.inspect=inspect
        release.run=Mock(side_effect=lambda argv:started.add(argv[-1]))
        release.http=Mock(return_value={'status':200})
        result=release.recover_freeze()
        self.assertTrue(result['restored_original_processes'])
        self.assertEqual(release.run.call_count,5)
        self.assertFalse(result['database_restored'])
        self.assertEqual(set(result['containers']),set(ops.MANAGED))
        self.assertEqual(set(result['checks']),{'/api/ready','/healthz'})

    def recovery_fixture(self):
        release=self.release
        release.mark=Mock(return_value={'entry':{}})
        release.entry=Mock(return_value={})
        release.schema=Mock(return_value=ops.OLD_HEAD)
        release.run=Mock(return_value=b'')
        release.http=Mock(return_value={'status':200})
        (self.root/'freeze.intent.json').write_text('{}')
        return release

    def test_recovery_start_success_but_process_exits_is_not_pass(self):
        release=self.recovery_fixture()
        release.inspect=lambda cid:row(cid.removesuffix('-id'),running=False)
        with self.assertRaisesRegex(RuntimeError,'failed after restart'):
            release.recover_freeze()
        self.assertEqual(release.run.call_count,5)
        release.http.assert_not_called()

    def test_recovery_running_but_container_health_fails_is_not_pass(self):
        release=self.recovery_fixture()
        def inspect(cid):
            current=row(cid.removesuffix('-id'),running=True)
            current['State']['Health']={'Status':'unhealthy'}
            return current
        release.inspect=inspect
        with patch.object(ops.time,'sleep'),self.assertRaisesRegex(RuntimeError,'health timeout'):
            release.recover_freeze()
        release.http.assert_not_called()

    def test_recovery_healthy_containers_but_public_ready_fails_is_not_pass(self):
        release=self.recovery_fixture()
        def inspect(cid):
            current=row(cid.removesuffix('-id'),running=True)
            current['State']['Health']={'Status':'healthy'}
            return current
        release.inspect=inspect
        release.http=Mock(side_effect=RuntimeError('synthetic readiness failure'))
        with patch.object(ops.time,'sleep'),self.assertRaisesRegex(RuntimeError,'public readiness failed'):
            release.recover_freeze()

    def test_every_phase_rechecks_controller_before_any_write(self):
        self.release.m['controller_sha256']='reviewed'
        for phase in ('preflight','freeze','backup-old','migrate','backup-new','create','activate','verify','recover-freeze'):
            with self.subTest(phase=phase),patch.object(ops,'artifact',return_value={'sha256':'different'}),patch.object(ops,'save') as save:
                with self.assertRaisesRegex(RuntimeError,'controller changed'):
                    self.release.execute(phase)
                save.assert_not_called()

    def test_recover_refuses_after_migration_intent_or_exposure(self):
        release=self.release
        release.mark=Mock(return_value={'entry':{}})
        release.entry=Mock(return_value={})
        (self.root/'freeze.intent.json').write_text('{}')
        for marker in ('migrate.intent.json','exposure-started.json'):
            path=self.root/marker
            path.write_text('{}')
            with self.subTest(marker=marker),self.assertRaises(RuntimeError):
                release.recover_freeze()
            path.unlink()

    def test_recover_refuses_completed_freeze_before_starting_any_writer(self):
        release=self.recovery_fixture()
        release.inspect=Mock()
        (self.root/'freeze.json').write_text('{"phase":"freeze","status":"PASS"}')
        with self.assertRaisesRegex(RuntimeError,'not an incomplete freeze'):
            release.recover_freeze()
        release.inspect.assert_not_called()
        release.run.assert_not_called()
        release.http.assert_not_called()

    def test_completed_phase_cannot_execute_when_intent_is_missing(self):
        release=self.release
        release.m['controller_sha256']='reviewed'
        (self.root/'.shop-pn-active-owner.json').write_text(json.dumps({
            'release':release.release,'manifest_sha256':ops.sha(release.raw)}))
        (self.root/'freeze.json').write_text('{"phase":"freeze","status":"PASS"}')
        release.freeze=Mock()
        with patch.object(ops,'BASE',self.root),patch.object(ops,'artifact',return_value={'sha256':'reviewed'}),patch.object(ops,'save') as save:
            with self.assertRaisesRegex(RuntimeError,'phase already completed'):
                release.execute('freeze')
            save.assert_not_called()
        release.freeze.assert_not_called()

    def test_recover_does_not_start_changed_old_container(self):
        release=self.release
        release.mark=Mock(return_value={'entry':{}})
        release.entry=Mock(return_value={})
        release.protected=Mock()
        release.schema=Mock(return_value=ops.OLD_HEAD)
        release.inspect=Mock(return_value=row('unexpected',running=False))
        release.run=Mock()
        (self.root/'freeze.intent.json').write_text('{}')
        with self.assertRaises(RuntimeError):
            release.recover_freeze()
        release.run.assert_not_called()

    def test_exposure_is_persisted_before_starting_candidate(self):
        release=self.release
        release.mark=Mock(return_value={'created':{name:name+'-new' for name in ops.APPS}})
        release.frozen=Mock()
        release.schema=Mock(return_value=ops.NEW_HEAD)
        def command(argv,**kwargs):
            self.assertTrue((self.root/'exposure-started.json').exists())
            raise RuntimeError('synthetic start failure')
        release.run=command
        with self.assertRaises(RuntimeError):
            release.activate()
        self.assertTrue(json.loads((self.root/'exposure-started.json').read_text())['forward_only'])

    def test_counts_rejects_nonidentifier_before_query(self):
        release=self.release
        release.sql=Mock()
        with self.assertRaises(RuntimeError):
            release.counts(['pn;DROP TABLE pn'])
        release.sql.assert_not_called()


if __name__=='__main__':
    unittest.main()
