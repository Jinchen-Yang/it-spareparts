"""Explicit jinchen-dev drill: real owned Docker stops/restarts, fake DB/entry.

The four processes only sleep. Schema/import/TCP/HTTP are synthetic seams;
real PostgreSQL metadata queries and restores have separate isolated drills.
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / 'presales3_ops.py'
SPEC = importlib.util.spec_from_file_location('d49_docker_ops', PATH)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def run(argv):
    return subprocess.run(argv, capture_output=True, check=True, timeout=120).stdout


@unittest.skipUnless(os.environ.get('D49_HANDOFF_DOCKER_DRILL') == '1', 'explicit owned Docker drill only')
class DockerHoldDrill(unittest.TestCase):
    def test_real_partial_stop_recovery_and_shop_intent_boundary(self):
        self.assertEqual(socket.gethostname(), 'jinchen-dev')
        owner = 'd49-hold-' + secrets.token_hex(8)
        ids = {}
        with tempfile.TemporaryDirectory(prefix=owner + '-') as directory:
            root = Path(directory)
            peer_root = root / 'shop'
            peer_root.mkdir()
            try:
                for index in range(4):
                    name = owner + '-' + str(index)
                    cid = run(['docker', 'run', '-d', '--name', name, '--network', 'none', '--init',
                               '--memory', '64m', '--cpus', '0.25', '--pids-limit', '32',
                               '--label', 'codex.d49-hold=' + owner, '--health-cmd', 'true',
                               '--health-interval', '1s', '--health-timeout', '1s',
                               '--health-retries', '3', '--entrypoint', 'sleep', 'postgres:15', 'infinity']).decode().strip()
                    ids[name] = cid
                release = ops.Release.__new__(ops.Release)
                release.root, release.sha, release.manifest_bytes = root, 'a' * 40, b'{}'
                release.m = {'controller_sha256': ops.artifact(PATH)['sha256'], 'old_schema': 'synthetic-old',
                    'source_handoff': {'manifest_sha256': 'b' * 64,
                        'gate_sha256': ops.artifact(PATH.with_name('catalog_source_version_gate.py'))['sha256']}}
                release.peer = Mock(return_value=SimpleNamespace(root=peer_root))
                release.schema = Mock(return_value='synthetic-old')
                release.cas = Mock()
                release.import_gate = Mock(return_value={'mode': 'synthetic-empty'})
                release.source_tcp_zero = Mock()
                release.wait_http = Mock()
                for attempt in range(30):
                    rows = {name: release.inspect(cid) for name, cid in ids.items()}
                    if all(row['State'].get('Health', {}).get('Status') == 'healthy' for row in rows.values()):
                        break
                    time.sleep(1)
                else:
                    self.fail('synthetic sleep processes not healthy')
                for row in rows.values():
                    self.assertEqual(row['HostConfig']['NetworkMode'], 'none')
                    self.assertFalse(row['HostConfig'].get('PortBindings'))
                plan = {'schema': 'synthetic-old', 'import_gate': {'mode': 'synthetic-empty'},
                    'writers': [{'name': name, 'id': row['Id'], 'image': row['Image'],
                                 'started_at': row['State']['StartedAt']} for name, row in rows.items()],
                    'containers': {name: ops.fingerprint(row) for name, row in rows.items()},
                    'entry': {'data_hex': '', 'active': {}, 'inode': [1, 2]},
                    'shop_manifest_sha256': 'b' * 64, 'shop_release': 'c' * 40}
                release.hold_plan = Mock(return_value=plan)
                actual_run = release.run
                stop_count = 0
                def partial_stop(argv, **kwargs):
                    nonlocal stop_count
                    if argv[:2] == ['docker', 'stop']:
                        self.assertEqual(release.hold_intent()['writers'], plan['writers'])
                        stop_count += 1
                        if stop_count == 3:
                            raise RuntimeError('synthetic failure before third stop')
                    return actual_run(argv, **kwargs)
                release.run = partial_stop
                with patch.object(ops, 'BASE', root):
                    with self.assertRaisesRegex(RuntimeError, 'before third stop'):
                        release.execute('hold-source')
                    self.assertTrue((root / 'hold-source.intent.json').exists())
                    self.assertFalse((root / 'hold-source.json').exists())
                    self.assertEqual(sum(release.inspect(cid)['State']['Running'] for cid in ids.values()), 2)
                    release.run = actual_run
                    release.execute('recover-hold-source')
                recovered = json.loads((root / 'recover-hold-source.json').read_bytes())
                self.assertEqual(recovered['status'], 'PASS')
                self.assertFalse(recovered['database_restored'])
                for name, cid in ids.items():
                    row = release.inspect(cid)
                    self.assertEqual(row['Id'], plan['containers'][name]['id'])
                    self.assertEqual(row['Image'], plan['containers'][name]['image'])
                    self.assertTrue(row['State']['Running'])
                    self.assertEqual(row['State']['Health']['Status'], 'healthy')
                # Unknown shop result is already a hard boundary, before any
                # inspect/start/schema work by source recovery or rollback.
                (peer_root / 'freeze.intent.json').write_text('{}')
                release.run = Mock(side_effect=AssertionError('must not run Docker'))
                for method in (release.recover_hold_source, release.recover_freeze, release.rollback):
                    with self.assertRaisesRegex(RuntimeError, 'forward-only'):
                        method()
                release.run.assert_not_called()
                print(json.dumps({'owned_real_docker_processes': 4, 'actual_stops_before_failure': 2,
                                  'actual_original_recovery': True, 'shop_intent_resume_blocked': True,
                                  'database_entry_tcp_http': 'synthetic; separate PG15 drills cover SQL',
                                  'production_connected': False}))
            finally:
                for name, cid in ids.items():
                    row = json.loads(run(['docker', 'inspect', cid]))[0]
                    self.assertEqual(row['Name'], '/' + name)
                    self.assertEqual(row['Id'], cid)
                    self.assertEqual(row['Config']['Labels'].get('codex.d49-hold'), owner)
                    run(['docker', 'rm', '-f', '-v', cid])
                self.assertEqual(run(['docker', 'ps', '-aq', '--filter', 'label=codex.d49-hold=' + owner]).strip(), b'')


if __name__ == '__main__':
    unittest.main()
