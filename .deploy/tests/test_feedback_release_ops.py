"""Synthetic safety contracts. No Docker/SSH/network or production data access."""
import ast
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'feedback_release_ops.py'
SPEC = importlib.util.spec_from_file_location('feedback_release_ops', SOURCE)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)
HELPER = SOURCE.with_name('presales3_ops.py')
core = ops.load_helper(HELPER, ops.sha256(HELPER.read_bytes()))
Release = ops.release_class(core)


def manifest():
    value = {'release': '1' * 40, 'old_revision': ops.OLD_REVISION, 'old_schema': ops.OLD_HEAD,
        'target_head': ops.TARGET_HEAD, 'old_app': 'itsp-presales3-' + ops.OLD_REVISION[:12] + '-app',
        'old_front': 'itsp-presales3-' + ops.OLD_REVISION[:12] + '-frontend',
        'old_app_id': '2' * 64, 'old_front_id': '3' * 64, 'old_app_image': 'sha256:' + '4' * 64,
        'old_front_image': 'sha256:' + '5' * 64, 'app_image': 'sha256:' + '6' * 64,
        'frontend_image': 'sha256:' + '7' * 64, 'port': 18371, 'caddyfile': '/opt/it-spareparts/Caddyfile',
        'subnet': '172.30.241.0/24', 'nginx_upstream_count': 3, 'additional_writers': []}
    value.update({field: 'a' * 64 for field in ops.HASH_FIELDS})
    value['controller_sha256'] = ops.sha256(SOURCE.read_bytes())
    value['helper_sha256'] = ops.sha256(HELPER.read_bytes())
    return value


def container(name, cid, image, running=True, host='db'):
    return {'Name': '/' + name, 'Id': cid, 'Image': image, 'RestartCount': 0,
        'State': {'Running': running, 'StartedAt': '2026-10-31T00:00:00.123456789Z', 'OOMKilled': False},
        'Config': {'Env': ['DATABASE_URL=postgresql://user:secret@' + host + ':5432/spareparts'],
                   'Labels': {}, 'Hostname': name}, 'HostConfig': {'PortBindings': {}, 'NetworkMode': 'it-spareparts_default'},
        'NetworkSettings': {'Networks': {'it-spareparts_default': {'IPAddress': '172.20.0.3', 'Aliases': [name]}}}}


class ManifestTests(unittest.TestCase):
    def test_python310(self):
        ast.parse(SOURCE.read_text(), feature_version=(3, 10))

    def test_valid_explicit_new_protocol(self):
        value = manifest()
        self.assertEqual(ops.validate_manifest(value, ops.BASE / ('feedback-' + value['release'])), value)

    def test_no_fake_old_handoff_or_catalog_mutation(self):
        for key in ('source_handoff', 'catalog_sync', 'import_gate_allowlist'):
            value = manifest()
            value[key] = {}
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'fields'):
                ops.validate_manifest(value, ops.BASE / ('feedback-' + value['release']))

    def test_rejects_wrong_baseline_schema_container_or_root(self):
        for key, value in [('target_head', ops.OLD_HEAD), ('old_revision', 'b' * 40),
                           ('old_schema', 'other'), ('old_app', 'itsp-presales3-unknown-app'),
                           ('old_front_id', '2' * 64), ('subnet', '0.0.0.0/0')]:
            candidate = manifest()
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                ops.validate_manifest(candidate, ops.BASE / ('feedback-' + candidate['release']))
        with self.assertRaisesRegex(RuntimeError, 'directory'):
            ops.validate_manifest(manifest(), Path('/tmp/feedback'))

    def test_helper_is_verified_before_code_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'helper.py'
            path.write_text('raise AssertionError("unverified code executed")')
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                ops.load_helper(path, '0' * 64)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.r = Release.__new__(Release)
        self.r.m = manifest()
        self.r.root = self.root
        self.r.path = self.root / 'manifest.json'
        self.r.manifest_bytes = json.dumps(self.r.m).encode()
        self.r.path.write_bytes(self.r.manifest_bytes)
        self.r.sha = self.r.m['release']
        self.r.old_app, self.r.old_front = self.r.m['old_app'], self.r.m['old_front']
        self.r.prefix = 'itsp-feedback-' + self.r.sha[:12]
        self.r.app, self.r.front, self.r.net = (self.r.prefix + suffix for suffix in ('-app', '-frontend', '-net'))
        self.old = container(self.r.old_app, self.r.m['old_app_id'], self.r.m['old_app_image'])
        self.db = container(core.DB, '8' * 64, 'sha256:' + '9' * 64)
        self.db['NetworkSettings']['Networks']['it-spareparts_default'].update(IPAddress='172.20.0.2', Aliases=['db', core.DB])
        self.r.inspect = Mock(side_effect=lambda name, *a: self.db if name == core.DB else self.old)
        self.snapshot = {'batches': [], 'jobs': [], 'active_return_receipts': 0}
        self.r.sql = Mock(side_effect=lambda *a: json.dumps(self.snapshot))

    def review(self):
        at = dt.datetime.now(dt.timezone.utc).isoformat()
        body = {'release': self.r.sha, 'old_revision': ops.OLD_REVISION, 'writers': self.r.writers(),
                'reviewed_at': at, 'snapshot_sha256': ops.digest_json(self.snapshot),
                'no_resume_path_reviewed': True, 'no_surviving_worker_reviewed': True,
                'legacy_preview_without_archives': [item['id'] for item in self.snapshot['batches'] if item.get('has_archives') is False],
                'source_evidence': 'exact old source paths and process lifecycle inspected'}
        raw = json.dumps(body).encode()
        (self.root / 'processing-review.private.json').write_bytes(raw)
        self.r.m['processing_review'] = {'reviewed_at': at, 'snapshot_sha256': body['snapshot_sha256'], 'review_sha256': ops.sha256(raw)}
        return body

    def valid_orphan(self):
        self.snapshot['batches'] = [{'id': 282, 'row_sha256': 'a' * 64, 'preview_shape': True, 'has_archives': False}]
        self.snapshot['jobs'] = [{'id': 137, 'row_sha256': 'b' * 64, 'created_at': '2026-10-20T00:00:00Z',
            'finished_at': None, 'total_files': 1, 'done_files': 1, 'error_files': 0,
            'batches': [{'id': 230, 'row_sha256': 'c' * 64, 'file_type': 'sales', 'status': 'success'}]}]

    def test_zero_imports_default_and_legacy_exceptions_never_implicit(self):
        self.assertEqual(self.r.import_gate()['mode'], 'all_zero')
        self.valid_orphan()
        with self.assertRaisesRegex(RuntimeError, 'new release-specific review'):
            self.r.import_gate()

    def test_exact_fresh_review_admits_only_bound_snapshot(self):
        self.valid_orphan()
        self.review()
        result = self.r.import_gate()
        self.assertFalse(result['job_completion_claimed'])
        self.assertTrue(result['post_import_refresh_completion_unknown'])
        self.snapshot['batches'][0]['row_sha256'] = 'f' * 64
        with self.assertRaisesRegex(RuntimeError, 'snapshot changed'):
            self.r.import_gate()

    def test_historic_missing_archives_requires_explicit_new_review(self):
        self.valid_orphan()
        body = self.review()
        body['legacy_preview_without_archives'] = []
        raw = json.dumps(body).encode()
        (self.root / 'processing-review.private.json').write_bytes(raw)
        self.r.m['processing_review']['review_sha256'] = ops.sha256(raw)
        with self.assertRaisesRegex(RuntimeError, 'legacy preview shape'):
            self.r.import_gate()
        self.assertIn("AS has_archives", ops.import_gate_query(core))

    def test_review_for_another_release_or_missing_worker_audit_fails(self):
        for key, value in [('release', 'f' * 40), ('writers', []), ('no_resume_path_reviewed', False),
                           ('no_surviving_worker_reviewed', False), ('source_evidence', '')]:
            with self.subTest(key=key):
                body = self.review()
                body[key] = value
                raw = json.dumps(body).encode()
                (self.root / 'processing-review.private.json').write_bytes(raw)
                self.r.m['processing_review']['review_sha256'] = ops.sha256(raw)
                with self.assertRaises(RuntimeError):
                    self.r.import_gate()

    def test_stale_review_active_receipts_nonpreview_or_newer_job_fail(self):
        self.valid_orphan()
        self.review()
        self.r.m['processing_review']['reviewed_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).isoformat()
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.r.import_gate()
        self.snapshot['active_return_receipts'] = 1
        with self.assertRaisesRegex(RuntimeError, 'receipt job'):
            self.r.import_gate()
        self.snapshot['active_return_receipts'] = 0
        self.snapshot['batches'][0]['preview_shape'] = False
        self.review()
        with self.assertRaisesRegex(RuntimeError, 'non-preview'):
            self.r.import_gate()
        self.snapshot['batches'][0]['preview_shape'] = True
        self.snapshot['jobs'][0]['created_at'] = '2026-11-01T00:00:00Z'
        self.review()
        with self.assertRaisesRegex(RuntimeError, 'surviving writer'):
            self.r.import_gate()

    def test_writer_alias_and_ip_share_inventory_and_unknown_client_fails(self):
        self.r.sql = Mock(return_value='["172.20.0.3"]')
        self.r.writer_inventory([self.old], True)
        for alias in ('it-spareparts-db-1', '172.20.0.2'):
            hidden = container('hidden', 'f' * 64, 'sha256:' + 'e' * 64, host=alias)
            with self.subTest(alias=alias), self.assertRaisesRegex(RuntimeError, 'writer inventory'):
                self.r.writer_inventory([self.old, hidden], True)
        self.r.sql = Mock(return_value='["172.20.0.99"]')
        with self.assertRaisesRegex(RuntimeError, 'unreviewed database client'):
            self.r.writer_inventory([self.old], True)

    def test_protected_config_catches_env_and_network_changes(self):
        before = self.r.container_config(self.old)
        changed = copy.deepcopy(self.old)
        changed['Config']['Env'].append('UNAPPROVED=yes')
        self.assertNotEqual(self.r.container_config(changed), before)
        changed = copy.deepcopy(self.old)
        changed['NetworkSettings']['Networks'] = {}
        self.assertNotEqual(self.r.container_config(changed), before)

    def test_stop_cleared_operational_network_fields_do_not_change_config(self):
        stopped = copy.deepcopy(self.old)
        stopped['State']['Running'] = False
        network = stopped['NetworkSettings']['Networks']['it-spareparts_default']
        network.update(IPAddress='', EndpointID='', Gateway='')
        self.assertEqual(self.r.container_config(stopped), self.r.container_config(self.old))

    def test_evidence_binds_sha_tag_scope_and_old_schema_compatibility(self):
        value = {'release': self.r.sha, 'old_revision': ops.OLD_REVISION, 'target_head': ops.TARGET_HEAD,
                 'required_ci': 'passed', 'independent_review': 'passed', 'candidate_regression': 'passed',
                 'scope_review': 'passed', 'old_app_on_expanded_schema': 'passed',
                 'release_tag': 'release-feedback-test', 'release_tag_sha': self.r.sha,
                 'references': {'regression': 'exact-source-run-log'}}
        for key, change in [(None, None), ('release', 'a' * 40), ('release_tag_sha', 'a' * 40),
                            ('old_app_on_expanded_schema', 'not-run'), ('scope_review', 'pending'), ('references', {})]:
            body = dict(value)
            if key:
                body[key] = change
            raw = json.dumps(body).encode()
            self.r.m['evidence_sha256'] = ops.sha256(raw)
            (self.root / 'release-evidence.json').write_bytes(raw)
            with self.subTest(key=key):
                if key:
                    with self.assertRaises(RuntimeError):
                        self.r.evidence()
                else:
                    self.assertEqual(self.r.evidence(), value)

    def prepare_rollback(self):
        self.r.evidence = Mock()
        self.r.frozen = Mock()
        self.r.schema = Mock(return_value=ops.TARGET_HEAD)
        self.r.counts = Mock(return_value={'some_fact': 3})
        self.r.marker = Mock(return_value={'counts': {'some_fact': 3}})
        self.r.unchanged_business = Mock()
        self.r.rollback_integrity = Mock(return_value={'frozen_access_audit_unchanged': True, 'current_additional_access_audit_rows': 1})
        self.r.wait_http = Mock()
        self.r.sql = Mock(return_value='0')
        self.old['State']['Running'] = False
        def run(args, **kwargs):
            if args[:2] == ['docker', 'ps']:
                return (self.r.old_app + '\n').encode()
            if args[:2] == ['docker', 'start']:
                self.old['State']['Running'] = True
            return b''
        self.r.run = Mock(side_effect=run)
        self.r.alembic = Mock(side_effect=AssertionError('must never downgrade'))

    def test_application_rollback_keeps_expanded_schema_without_restore(self):
        self.prepare_rollback()
        result = self.r.rollback()
        self.assertEqual(result['schema'], ops.TARGET_HEAD)
        self.assertTrue(result['no_downgrade'] and result['no_database_restore'])
        self.r.alembic.assert_not_called()
        self.assertTrue(self.old['State']['Running'])

    def test_rollback_refuses_uncertain_cutover_or_business_changes(self):
        self.prepare_rollback()
        (self.root / 'cutover.intent.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'cutover may have begun'):
            self.r.rollback()
        self.r.run.assert_not_called()
        (self.root / 'cutover.intent.json').unlink()
        self.r.rollback_integrity.side_effect = RuntimeError('business row counts changed')
        with self.assertRaisesRegex(RuntimeError, 'row counts changed'):
            self.r.rollback()
        self.assertFalse(self.old['State']['Running'])

    def test_rollback_integrity_preserves_new_audits_but_no_other_table_exception(self):
        fingerprints = {name: {'columns': ['id'], 'sha256': 'f' * 64, 'bytes': 65}
                        for name in ('business_fact', 'sys_access_log', 'sys_other')}
        baseline = {'counts': {'business_fact': 1, 'sys_access_log': 1, 'sys_other': 1},
                    'data_fingerprints': fingerprints, 'audit_watermark': 8}
        self.r.marker = Mock(return_value=baseline)
        self.r.counts = Mock(return_value={**baseline['counts'], 'sys_access_log': 3})
        self.r.data_fingerprints = Mock(return_value={k: v for k, v in fingerprints.items() if k != 'sys_access_log'})
        self.r.audit_prefix = Mock(return_value=fingerprints['sys_access_log'])
        self.assertEqual(self.r.rollback_integrity()['current_additional_access_audit_rows'], 2)
        self.r.audit_prefix.assert_called_with(8, ['id'])
        self.r.counts.return_value['sys_other'] = 2
        with self.assertRaisesRegex(RuntimeError, 'row counts changed'):
            self.r.rollback_integrity()
        self.r.counts.return_value['sys_other'] = 1
        self.r.data_fingerprints.return_value['business_fact'] = {'sha256': 'changed'}
        with self.assertRaisesRegex(RuntimeError, 'non-access-audit row content changed'):
            self.r.rollback_integrity()
        self.r.data_fingerprints.return_value = {k: v for k, v in fingerprints.items() if k != 'sys_access_log'}
        self.r.audit_prefix.return_value = {'sha256': 'old-audit-changed'}
        with self.assertRaisesRegex(RuntimeError, 'frozen access audit changed'):
            self.r.rollback_integrity()

    def execute_setup(self):
        self.r.predecessor = Mock()
        self.r.preflight = Mock(return_value={'checked': True})
        self.r.images = Mock(return_value={})
        self.r.marker = Mock()
        return patch.object(ops, 'BASE', self.root)

    def test_intent_prevents_retry_and_owner_rejects_interleaved_release(self):
        with self.execute_setup(), patch('builtins.print'):
            self.r.execute('preflight')
            self.assertTrue((self.root / 'preflight.intent.json').exists())
            with self.assertRaisesRegex(RuntimeError, 'already completed'):
                self.r.execute('preflight')
            owner = self.root / '.feedback-active-owner.json'
            owner.write_text('{"release":"another"}')
            with self.assertRaisesRegex(RuntimeError, 'another release'):
                self.r.execute('images')

    def test_failed_phase_has_immutable_intent_and_cannot_repeat(self):
        with self.execute_setup(), patch('builtins.print'):
            self.r.preflight.side_effect = RuntimeError('synthetic failure')
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                self.r.execute('preflight')
            self.assertTrue((self.root / 'preflight.intent.json').exists())
            self.assertFalse((self.root / 'preflight.json').exists())
            with self.assertRaises(FileExistsError):
                self.r.execute('preflight')
            self.assertEqual(self.r.preflight.call_count, 1)

    def test_phase_order_and_terminal_recovery_cannot_be_bypassed(self):
        with self.execute_setup(), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'missing release ownership'):
                self.r.execute('migrate')
            self.r.execute('preflight')
            self.r.marker.side_effect = RuntimeError('missing prior phase')
            with self.assertRaisesRegex(RuntimeError, 'missing prior phase'):
                self.r.execute('bluegreen')
            (self.root / 'rollback.intent.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'terminal'):
                self.r.execute('images')

    def test_legacy_downgrade_handoff_paths_unavailable(self):
        with self.execute_setup():
            for phase in ('hold-source', 'recover-hold-source', 'seal-handoff'):
                with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, 'unknown single-system'):
                    self.r.execute(phase)

    def test_inherited_migration_backup_clone_and_cas_are_the_reviewed_implementations(self):
        for method in ('restore', 'alembic', 'clone', 'cas', 'cutover', 'bluegreen', 'images'):
            self.assertIs(getattr(Release, method), getattr(core.Release, method))


@unittest.skipUnless(os.environ.get('FEEDBACK_RELEASE_PG_DRILL') == '1', 'explicit isolated jinchen-dev PostgreSQL drill only')
class FeedbackPostgresDrill(unittest.TestCase):
    def test_real_two_head_restore_checks_all_tables_and_rejects_count_drift(self):
        self.assertEqual(socket.gethostname(), 'jinchen-dev', 'real drill is dev-host only')
        root = Path('/home/cloudlay/work/requirements-20261101-01a1976c/release-safety')
        self.assertEqual(root.resolve(), root)
        self.assertTrue(root.is_dir())
        def run(argv, input=None):
            return subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  check=True, timeout=180).stdout
        image = json.loads(run(['docker', 'image', 'inspect', 'postgres:15']))[0]['Id']
        prefix = 'itsp-feedback-synthetic-' + secrets.token_hex(6)
        cid = None
        with tempfile.TemporaryDirectory(prefix=prefix + '-', dir=root) as directory:
            r = Release.__new__(Release)
            r.root, r.sha, r.prefix = Path(directory), 'a' * 40, prefix
            r.marker = Mock(return_value={'db_image': image})
            env = r.root / 'source.env.private'
            core.write_new(env, 'POSTGRES_USER=spareparts\nPOSTGRES_DB=spareparts\nPOSTGRES_PASSWORD=' + secrets.token_hex(32) + '\n')
            try:
                cid = run(['docker', 'run', '-d', '--name', prefix + '-source', '--network', 'none',
                    '--memory', '512m', '--cpus', '1', '--pids-limit', '128',
                    '--tmpfs', '/var/lib/postgresql/data:rw,size=256m', '--security-opt', 'no-new-privileges',
                    '--label', 'itsp.synthetic=' + prefix, '--env-file', str(env), image]).decode().strip()
                row = r.inspect(cid)
                self.assertEqual(row['HostConfig']['NetworkMode'], 'none')
                self.assertFalse(row['HostConfig']['PortBindings'])
                for _ in range(60):
                    probe = subprocess.run(['docker', 'exec', cid, 'pg_isready', '-h', '127.0.0.1', '-U', 'spareparts'], capture_output=True)
                    if probe.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    self.fail('synthetic PostgreSQL not ready')
                def write_sql(query):
                    return run(['docker', 'exec', '-i', cid, 'psql', '-U', 'spareparts', '-d', 'spareparts', '-X', '-v', 'ON_ERROR_STOP=1'], query.encode())
                write_sql('CREATE TABLE alembic_version(version_num text); INSERT INTO alembic_version VALUES (\'' + ops.OLD_HEAD + "'); "
                          'CREATE TABLE arbitrary_fact(id int PRIMARY KEY, original text); '
                          "INSERT INTO arbitrary_fact VALUES (1,'original'),(2,'other'); "
                          'CREATE TABLE "quoted table" (id int); CREATE TABLE zero_fact(id int); '
                          'INSERT INTO "quoted table" VALUES (7); '
                          'CREATE TABLE sys_access_log(id serial PRIMARY KEY, action text); '
                          "INSERT INTO sys_access_log(action) VALUES ('existing');")
                expected = {'arbitrary_fact': 2, 'quoted table': 1, 'zero_fact': 0, 'sys_access_log': 1}
                self.assertEqual(r.counts(cid), expected)
                restored = []
                for label, head in (('old', ops.OLD_HEAD), ('new', ops.TARGET_HEAD)):
                    if label == 'new':
                        write_sql("UPDATE alembic_version SET version_num='" + head + "'; ALTER TABLE arbitrary_fact ADD COLUMN preserved_raw text;")
                    dump = r.root / (label + '.dump')
                    with dump.open('xb') as target:
                        r.run(['docker', 'exec', cid, 'pg_dump', '-U', 'spareparts', '-d', 'spareparts', '-Fc'], stdout=target)
                    result = r.restore(dump, head, expected, label)
                    self.assertTrue(result['actual_restore_performed'])
                    self.assertEqual(result['counts'], expected)
                    self.assertEqual(result['network'], 'none')
                    restored.append(result['container_id'])
                with self.assertRaisesRegex(RuntimeError, 'restored schema/count mismatch'):
                    r.restore(dump, ops.TARGET_HEAD, {**expected, 'arbitrary_fact': 3}, 'wrong-count')
                remaining = run(['docker', 'ps', '-a', '--filter', 'name=' + prefix, '--format', '{{.ID}} {{.Names}}']).decode().splitlines()
                self.assertEqual(len(remaining), 1, 'restored owned containers must be cleaned after success/failure')
                self.assertIn(prefix + '-source', remaining[0])
                self.assertEqual(r.schema(cid), ops.TARGET_HEAD)
                self.assertEqual(r.counts(cid), expected)
                # Real PostgreSQL hashes / rows with synthetic orchestration
                # seams: no Caddy or actual app image is run by this drill.
                original_run, original_inspect = r.run, r.inspect
                old = container('synthetic-old-app', 'e' * 64, 'sha256:' + 'f' * 64, running=False)
                def source_run(argv, **kwargs):
                    if argv[:2] == ['docker', 'start']:
                        self.assertEqual(argv[2], old['Id'])
                        old['State']['Running'] = True
                        return b''
                    return original_run([cid if token == core.DB else token for token in argv], **kwargs)
                r.run = source_run
                r.inspect = lambda name, *args: old if name == 'synthetic-old-app' else original_inspect(name, *args)
                frozen = {'counts': r.counts(), 'data_fingerprints': r.data_fingerprints(),
                          'audit_watermark': int(r.sql('SELECT max(id) FROM sys_access_log'))}
                r.marker = Mock(return_value=frozen)
                r.m, r.app, r.front = manifest(), prefix + '-app', prefix + '-frontend'
                r.evidence, r.frozen, r.wait_http = Mock(), Mock(), Mock()
                r.writers = Mock(return_value=[{'name': 'synthetic-old-app', 'id': old['Id'], 'image': old['Image']}])
                write_sql("INSERT INTO sys_access_log(action) VALUES ('authenticated_read_smoke');")
                result = r.rollback()
                self.assertTrue(old['State']['Running'])
                self.assertEqual(result['current_additional_access_audit_rows'], 1)
                self.assertEqual(r.counts()['sys_access_log'], 2, 'rollback must preserve new access audit')
                write_sql("UPDATE sys_access_log SET action='tampered' WHERE id=1;")
                with self.assertRaisesRegex(RuntimeError, 'frozen access audit changed'):
                    r.rollback_integrity()
                write_sql("UPDATE sys_access_log SET action='existing' WHERE id=1; UPDATE arbitrary_fact SET original='changed' WHERE id=1;")
                with self.assertRaisesRegex(RuntimeError, 'non-access-audit row content changed'):
                    r.rollback_integrity()
                write_sql("UPDATE arbitrary_fact SET original='original' WHERE id=1; DELETE FROM sys_access_log WHERE id=1;")
                with self.assertRaisesRegex(RuntimeError, 'frozen access audit changed'):
                    r.rollback_integrity()
            finally:
                if cid:
                    row = r.inspect(cid)
                    self.assertEqual(row['Id'], cid)
                    self.assertEqual(row['Name'], '/' + prefix + '-source')
                    self.assertEqual(row['Config']['Labels']['itsp.synthetic'], prefix)
                    run(['docker', 'rm', '-f', '-v', cid])


if __name__ == '__main__':
    unittest.main()
