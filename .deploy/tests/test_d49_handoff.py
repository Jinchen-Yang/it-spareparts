"""D49 state-machine failure paths; execute only on jinchen-dev."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


def load(name):
    path = Path(__file__).resolve().parents[1] / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


itsp, shop = load('presales3_ops'), load('shop_pn_ops')


def container(name, running=False):
    return {'Id': name + '-id', 'Name': '/' + name, 'Image': name + '-image', 'RestartCount': 0,
            'State': {'Running': running, 'StartedAt': 'original-start', 'OOMKilled': False,
                      'Health': {'Status': 'healthy'}}}


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.peer_root = self.root / 'shop'
        self.peer_root.mkdir()
        self.source = itsp.Release.__new__(itsp.Release)
        self.source.root = self.root
        self.source.sha = 'a' * 40
        self.source.manifest_bytes = b'{}'
        self.source.m = {'controller_sha256': 'reviewed', 'old_schema': 'old',
                         'source_handoff': {'manifest_sha256': 'b' * 64, 'gate_sha256': 'reviewed'}}
        self.peer = SimpleNamespace(root=self.peer_root)
        self.source.peer = Mock(return_value=self.peer)
        self.source.schema = Mock(return_value='old')
        self.source.cas = Mock()
        self.source.import_gate = Mock(return_value={'mode': 'all_zero'})
        self.source.source_tcp_zero = Mock()
        self.source.wait_http = Mock()
        self.rows = {name: container(name) for name in ('one', 'two', 'three', 'four')}
        writers = [{'name': name, 'id': row['Id'], 'image': row['Image'], 'started_at': row['State']['StartedAt']}
                   for name, row in self.rows.items()]
        self.plan = {'release': self.source.sha, 'manifest_sha256': itsp.digest(b'{}'),
                     'phase': 'hold-source', 'shop_manifest_sha256': 'b' * 64, 'shop_release': 'c' * 40,
                     'schema': 'old', 'import_gate': {'mode': 'all_zero'}, 'writers': writers,
                     'entry': {'data_hex': '', 'active': {}, 'inode': [1, 2]},
                     'containers': {name: {**itsp.fingerprint(row), 'running': True} for name, row in self.rows.items()}}
        self.source.inspect = Mock(side_effect=lambda name: copy.deepcopy(self.rows[name.removesuffix('-id')]))
        self.source.run = Mock()

    def intent(self):
        itsp.save(self.root / 'hold-source.intent.json', self.plan)

    def test_reviewed_helper_rejected_before_module_code_runs(self):
        path = self.root / 'helper.py'
        path.write_text("raise AssertionError('executed unreviewed module')")
        for module in (itsp, shop):
            with self.subTest(module=module.__name__), self.assertRaisesRegex(RuntimeError, 'checksum'):
                module.reviewed_module(path, 'wrong')

    def test_hold_intent_durable_and_bound_before_first_stop(self):
        source = self.source
        source.hold_plan = Mock(return_value=self.plan)
        source.hold_state = Mock(return_value=self.plan)
        def stop(argv, **kwargs):
            saved = source.hold_intent()
            self.assertEqual(saved['writers'], self.plan['writers'])
            self.assertEqual(saved['import_gate'], self.plan['import_gate'])
            self.assertEqual(saved['entry'], self.plan['entry'])
            raise RuntimeError('partial stop failure')
        source.run = Mock(side_effect=stop)
        with patch.object(itsp, 'BASE', self.root), patch.object(itsp, 'artifact', return_value={'sha256': 'reviewed'}):
            with self.assertRaisesRegex(RuntimeError, 'partial stop failure'):
                source.execute('hold-source')
        self.assertTrue((self.root / 'hold-source.intent.json').exists())
        self.assertFalse((self.root / 'hold-source.json').exists())

    def test_hold_rejects_started_at_drift_even_with_same_id_image(self):
        self.intent()
        self.rows['two']['State']['StartedAt'] = 'restarted'
        with self.assertRaisesRegex(RuntimeError, 'identity/protected drift'):
            self.source.hold_state()
        self.source.source_tcp_zero.assert_not_called()

    def test_hold_rejects_tcp_or_import_drift(self):
        self.intent()
        self.source.source_tcp_zero.side_effect = RuntimeError('source TCP clients remain')
        with self.assertRaisesRegex(RuntimeError, 'TCP'):
            self.source.hold_state()
        self.source.source_tcp_zero.side_effect = None
        self.source.import_gate.return_value = {'mode': 'changed'}
        with self.assertRaisesRegex(RuntimeError, 'import gate drift'):
            self.source.hold_state()

    def test_recover_partial_or_completed_hold_before_shop_mutation(self):
        self.intent()
        # A hold PASS does not prohibit recovery before the *shop* mutation.
        (self.root / 'hold-source.json').write_text('{}')
        self.rows['one']['State']['Running'] = True
        def start(argv):
            self.rows[argv[-1].removesuffix('-id')]['State']['Running'] = True
        self.source.run.side_effect = start
        result = self.source.recover_hold_source()
        self.assertTrue(result['restored_original_processes'])
        self.assertEqual(self.source.run.call_count, 3)
        self.assertFalse(result['database_restored'])

    def test_shop_freeze_intent_alone_forbids_any_source_resume(self):
        self.intent()
        (self.peer_root / 'freeze.intent.json').write_text('{}')
        for method in (self.source.recover_hold_source, self.source.recover_freeze, self.source.rollback):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(RuntimeError, 'forward-only'):
                method()
        self.source.inspect.assert_not_called()
        self.source.run.assert_not_called()

    def test_recover_legacy_without_docker_health_probes_each_exact_instance_twice(self):
        self.intent()
        for row in self.rows.values():
            del row['State']['Health']
        probes = []
        def command(argv, **kwargs):
            if argv[:2] == ['docker', 'start']:
                self.rows[argv[2].removesuffix('-id')]['State']['Running'] = True
                return b''
            self.assertEqual(argv[:2], ['docker', 'exec'])
            self.assertEqual(argv[3:], ['python', '-c', itsp.ORIGINAL_HEALTH_PROBE])
            self.assertEqual(kwargs, {'timeout': 8})
            probes.append(argv[2])
            return b'true\n'
        self.source.run.side_effect = command
        result = self.source.recover_hold_source()
        self.assertTrue(result['restored_original_processes'])
        self.assertEqual(probes, [row['Id'] for row in self.rows.values()] * 2)
        self.source.wait_http.assert_called_once_with('https://ybwznt.com/health/db')
        self.assertTrue(all('Health' not in row['State'] for row in self.rows.values()))

    def test_legacy_probe_failure_never_claims_recovered(self):
        self.intent()
        for row in self.rows.values():
            row['State']['Running'] = True
            del row['State']['Health']
        self.source.run.return_value = b'false\n'
        with patch.object(itsp.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'health timeout'):
            self.source.recover_hold_source()
        self.source.wait_http.assert_not_called()

    def test_legacy_final_health_drift_blocks_recovery(self):
        self.intent()
        for row in self.rows.values():
            row['State']['Running'] = True
            del row['State']['Health']
        self.source.run.side_effect = [b'true\n'] * 4 + [b'false\n']
        with self.assertRaisesRegex(RuntimeError, 'final original health changed'):
            self.source.recover_hold_source()

    def test_source_migration_unknown_result_forbids_resume(self):
        self.intent()
        (self.root / 'migrate.intent.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'forward-only'):
            self.source.recover_hold_source()
        self.source.run.assert_not_called()

    def test_completed_hold_receipt_without_intent_cannot_restart_phase(self):
        itsp.save(self.root / '.presales3-active-owner.json', {'release': self.source.sha, 'manifest_sha256': itsp.digest(b'{}')})
        (self.root / 'hold-source.json').write_text('{}')
        self.source.hold_plan = Mock()
        with patch.object(itsp, 'BASE', self.root), patch.object(itsp, 'artifact', return_value={'sha256': 'reviewed'}):
            with self.assertRaisesRegex(RuntimeError, 'already completed'):
                self.source.execute('hold-source')
        self.source.hold_plan.assert_not_called()

    def shop_fixture(self):
        release = shop.Release.__new__(shop.Release)
        release.root = self.peer_root
        release.release = 'c' * 40
        release.raw = b'{}'
        release.m = {'controller_sha256': 'reviewed', 'source_hold': {'gate_sha256': 'reviewed'}, 'old': {}}
        release.protected = Mock()
        release.source_release = Mock(return_value=SimpleNamespace(assert_hold=Mock(return_value={'bound': True})))
        return release

    def test_shop_backup_refuses_freeze_without_completed_version_gate(self):
        release = self.shop_fixture()
        release.mark = Mock(return_value={'schema': shop.OLD_HEAD})
        release.inspect = Mock()
        release.restore = Mock()
        with self.assertRaisesRegex(RuntimeError, 'version gate required'):
            release.backup('old')
        release.inspect.assert_not_called()
        release.restore.assert_not_called()

    def test_completed_shop_freeze_binds_started_at_until_activation(self):
        release = self.shop_fixture()
        current = container('receiver')
        held = {'manifest_sha256': 'source', 'hold_sha256': 'held'}
        release.m['old'] = {'receiver': {'id': current['Id'], 'image': current['Image']}}
        release.mark = Mock(return_value={'version_gate': {'status': 'PASS'}, 'source_hold': held,
                                         'source_manifest_sha256': 'source', 'shop_writers': {'receiver': shop.fingerprint(current)}})
        release.source_release.return_value.assert_hold.return_value = held
        current['State']['StartedAt'] = 'later-restart'
        release.inspect = Mock(return_value=current)
        with self.assertRaisesRegex(RuntimeError, 'freeze fingerprint changed'):
            release.frozen()

    def test_failed_final_version_gate_never_writes_freeze_pass(self):
        release = self.shop_fixture()
        rows = {name: container(name, True) for name in shop.MANAGED}
        release.m['old'] = {name: {'id': row['Id'], 'image': row['Image']} for name, row in rows.items()}
        release.mark = Mock(return_value={'source_hold': {'bound': True}})
        release.inspect = Mock(side_effect=lambda name: rows[name])
        release.schema = Mock(return_value=shop.OLD_HEAD)
        release.sql = Mock(return_value='0')
        def stop(argv, **kwargs):
            rows[argv[-1].removesuffix('-id')]['State']['Running'] = False
        release.run = Mock(side_effect=stop)
        def gate():
            self.assertTrue(all(not row['State']['Running'] for row in rows.values()))
            self.assertEqual(release.run.call_count, 5)
            self.assertTrue((self.peer_root / 'freeze.intent.json').exists())
            raise RuntimeError('source version gate: mirror ahead')
        release.version_gate = Mock(side_effect=gate)
        shop.save(self.peer_root / '.shop-pn-active-owner.json', {'release': release.release, 'manifest_sha256': shop.sha(b'{}')})
        with patch.object(shop, 'BASE', self.peer_root), patch.object(shop, 'artifact', return_value={'sha256': 'reviewed'}):
            with self.assertRaisesRegex(RuntimeError, 'mirror ahead'):
                release.execute('freeze')
        self.assertFalse((self.peer_root / 'freeze.json').exists())
        self.assertTrue((self.peer_root / 'freeze.intent.json').exists())

    def test_itsp_preflight_refuses_missing_or_different_seal_before_snapshots(self):
        source = self.source
        source.assert_hold = Mock(return_value={'bound': True})
        self.peer.mark = Mock(return_value={'protected_final': False})
        source.run = Mock()
        with self.assertRaisesRegex(RuntimeError, 'completed shop handoff required'):
            source.preflight()
        source.run.assert_not_called()

    def test_shop_seal_requires_prior_verify_and_final_original_protected_check(self):
        release = self.shop_fixture()
        release.verify = Mock(return_value={'checks': {}})
        with self.assertRaisesRegex(RuntimeError, 'completed shop verify'):
            release.seal_handoff()
        release.verify.assert_not_called()
        shop.save(self.peer_root / 'verify-001.json', {'phase': 'verify', 'status': 'PASS', 'manifest_sha256': shop.sha(b'{}')})
        release.mark = Mock(return_value={'version_gate': {'status': 'PASS'}, 'source_hold': {'bound': True}})
        release.protected.side_effect = RuntimeError('final protected drift')
        with self.assertRaisesRegex(RuntimeError, 'final protected drift'):
            release.seal_handoff()

    def test_sealed_shop_rejects_old_verify_before_writing_intent(self):
        release = self.shop_fixture()
        (self.peer_root / 'seal-handoff.json').write_text('{}')
        with patch.object(shop, 'artifact', return_value={'sha256': 'reviewed'}), patch.object(shop, 'save') as save:
            with self.assertRaisesRegex(RuntimeError, 'handoff sealed'):
                release.execute('verify')
            save.assert_not_called()

    def test_handoff_observation_uses_live_shop_checks_without_old_caddy(self):
        release = self.shop_fixture()
        current = shop.fingerprint(container('receiver', True))
        seal = {'protected_final': True, 'freeze_sha256': 'frozen', 'source_hold': {'bound': True},
                'started': {'receiver': current}, 'bridge': current}
        release.mark = Mock(side_effect=lambda phase: seal if phase == 'seal-handoff' else
                            {'version_gate': {'status': 'PASS'}, 'source_hold': {'bound': True}})
        release.schema = Mock(return_value=shop.NEW_HEAD)
        release.inspect = Mock(return_value=container('receiver', True))
        release.http = Mock(return_value={'status': 200})
        release.entry = Mock(side_effect=AssertionError('old Caddy must not be checked'))
        with patch.object(shop, 'artifact', return_value={'sha256': 'frozen'}):
            result = release.handoff_health()
        self.assertEqual(release.http.call_count, 5)
        release.protected.assert_not_called()
        self.assertEqual(result['seal_sha256'], 'frozen')


class OriginalHealthTests(unittest.TestCase):
    def setUp(self):
        self.source = itsp.Release.__new__(itsp.Release)
        self.source.run = Mock(return_value=b'true\n')
        self.row = container('original', True)

    def test_configured_health_never_falls_back(self):
        for status in ('starting', 'unhealthy', 'healthy'):
            self.row['State']['Health']['Status'] = status
            self.assertEqual(self.source.original_healthy(self.row), status == 'healthy')
        self.source.run.assert_not_called()

    def test_missing_configured_health_is_rejected(self):
        del self.row['State']['Health']
        self.row['Config'] = {'Healthcheck': {'Test': ['CMD', 'health']}}
        self.assertFalse(self.source.original_healthy(self.row))
        self.source.run.assert_not_called()

    def test_legacy_exec_errors_and_unexpected_output_rejected(self):
        del self.row['State']['Health']
        for value in (b'false', b'{"status":"ok"}', b'true\nextra', b''):
            self.source.run.return_value = value
            self.assertFalse(self.source.original_healthy(self.row))
        self.source.run.side_effect = RuntimeError('exec failed')
        self.assertFalse(self.source.original_healthy(self.row))

    def test_probe_requires_bounded_200_database_health(self):
        for status, body, expected in (
            (200, b'{"status":"ok","db":"reachable"}', True),
            (503, b'{"status":"ok","db":"reachable"}', False),
            (200, b'{"status":"error","db":"unreachable"}', False),
            (200, b'{"status":"ok"}', False),
            (200, b'[]', False),
            (200, b'bad-json', False),
            (200, b'x' * 4097, False),
        ):
            with self.subTest(status=status, body=body[:50]):
                response = SimpleNamespace(status=status, read=Mock(return_value=body))
                connection = Mock()
                connection.getresponse.return_value = response
                output = io.StringIO()
                with patch('http.client.HTTPConnection', return_value=connection) as factory, redirect_stdout(output):
                    exec(itsp.ORIGINAL_HEALTH_PROBE, {})
                factory.assert_called_once_with('127.0.0.1', 8000, timeout=3)
                connection.request.assert_called_once_with('GET', '/health/db')
                response.read.assert_called_once_with(4097)
                self.assertIs(json.loads(output.getvalue()), expected)


if __name__ == '__main__':
    unittest.main()
