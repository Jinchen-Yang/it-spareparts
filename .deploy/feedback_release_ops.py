#!/usr/bin/env python3
"""D-0051 single-system release. Execute one reviewed phase, never auto-retry.

The immutable presales3 controller is a hash-bound library, not an entry point:
its D-0049 manifest, ownership, handoff and schema-downgrade paths are not used.
"""
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import traceback
from types import ModuleType

BASE = Path('/data/spareparts/releases')
OLD_REVISION = 'e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e'
OLD_HEAD = 'a3c5e7f9b2d4'
TARGET_HEAD = 'c7e1a3b5d9f2'
PHASES = ('preflight', 'images', 'freeze', 'backup-old', 'migrate',
          'backup-new', 'bluegreen', 'cutover', 'verify')
HASH_FIELDS = ('controller_sha256', 'helper_sha256', 'source_sha256', 'smoke_sha256',
               'nginx_sha256', 'evidence_sha256', 'predecessor_owner_sha256')


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def digest_json(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def hash_ok(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(parsed.tzinfo is not None, 'review timestamp must have timezone')
    return parsed.astimezone(dt.timezone.utc)


def validate_manifest(m, root):
    required = {'release', 'old_revision', 'old_schema', 'target_head', 'old_app', 'old_front',
        'old_app_id', 'old_front_id', 'old_app_image', 'old_front_image', 'app_image',
        'frontend_image', 'port', 'caddyfile', 'subnet', 'additional_writers',
        'nginx_upstream_count', *HASH_FIELDS}
    need(isinstance(m, dict) and required <= set(m) <= required | {'processing_review'}, 'manifest fields differ')
    need(isinstance(m['release'], str) and re.fullmatch('[0-9a-f]{40}', m['release']), 'invalid release SHA')
    need(root == BASE / ('feedback-' + m['release']), 'release directory mismatch')
    need(m['old_revision'] == OLD_REVISION and m['old_schema'] == OLD_HEAD
         and m['target_head'] == TARGET_HEAD, 'D-0051 production/schema baseline differs')
    need(m['release'] != OLD_REVISION, 'candidate is the old release')
    for field, suffix in (('old_app', '-app'), ('old_front', '-frontend')):
        need(m[field] == 'itsp-presales3-' + OLD_REVISION[:12] + suffix, 'old container name differs')
    for field in ('old_app_id', 'old_front_id'):
        need(hash_ok(m[field]), 'invalid frozen container ID')
    for field in ('old_app_image', 'old_front_image', 'app_image', 'frontend_image'):
        need(isinstance(m[field], str) and re.fullmatch('sha256:[0-9a-f]{64}', m[field]), 'invalid image ID')
    for field in HASH_FIELDS:
        need(hash_ok(m[field]), 'reviewed checksum required: ' + field)
    need(type(m['port']) is int and 1024 < m['port'] < 65536, 'invalid loopback port')
    need(m['caddyfile'] in {'/opt/it-spareparts/Caddyfile', '/etc/caddy/Caddyfile'}, 'unknown Caddy path')
    need(type(m['nginx_upstream_count']) is int and m['nginx_upstream_count'] > 0, 'invalid nginx token count')
    need(isinstance(m['additional_writers'], list), 'explicit writer inventory required')
    names, ids = {m['old_app'], m['old_front']}, {m['old_app_id'], m['old_front_id']}
    need(len(ids) == 2, 'old container IDs must differ')
    for row in m['additional_writers']:
        need(isinstance(row, dict) and set(row) == {'name', 'id', 'image'}, 'writer fields differ')
        need(isinstance(row['name'], str) and re.fullmatch(r'(?:it-spareparts-app|itsp-presales3)-[a-z0-9-]{1,100}', row['name']), 'writer namespace differs')
        need(row['name'] not in names and row['id'] not in ids and hash_ok(row['id'])
             and isinstance(row['image'], str) and re.fullmatch('sha256:[0-9a-f]{64}', row['image']), 'duplicate/invalid writer identity')
        names.add(row['name'])
        ids.add(row['id'])
    subnet = ipaddress.ip_network(m['subnet'], strict=True)
    need(subnet.version == 4 and subnet.is_private and 24 <= subnet.prefixlen <= 28, 'unsafe subnet')
    if 'processing_review' in m:
        review = m['processing_review']
        need(isinstance(review, dict) and set(review) == {'review_sha256', 'snapshot_sha256', 'reviewed_at'}, 'processing review fields differ')
        need(hash_ok(review['review_sha256']) and hash_ok(review['snapshot_sha256']), 'processing review hashes missing')
        timestamp(review['reviewed_at'])
    return m


def load_helper(path, expected):
    """Verify bytes before executing; no import cache or unverified import-time code."""
    path = Path(path)
    need(not path.is_symlink(), 'helper symlink refused')
    raw = path.read_bytes()
    need(sha256(raw) == expected, 'helper checksum differs')
    module = ModuleType('feedback_reviewed_presales3')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def import_gate_query(core):
    # Expose missing archives explicitly. The legacy SQL recognises historic
    # batch 282's shape, but this release also requires a fresh explicit review
    # of that exact exception; row/snapshot checks alone never grant it.
    token = "    coalesce(b.file_type='maint_bulk'"
    need(core.IMPORT_GATE_SQL.count(token) == 1, 'reviewed import query shape changed')
    return core.IMPORT_GATE_SQL.replace(token, "    (b.report_json ? 'archives') AS has_archives,\n" + token)


def release_class(core):
    class FeedbackRelease(core.Release):
        def __init__(self, path):
            self.path, self.root = Path(path), Path(path).parent
            need(self.path.name == 'manifest.json' and not self.path.is_symlink(), 'manifest path mismatch')
            need(self.root.resolve() == self.root and self.root.is_dir()
                 and self.root.stat().st_uid == 0 and stat.S_IMODE(self.root.stat().st_mode) == 0o700, 'private root-owned release directory required')
            self.manifest_bytes = self.path.read_bytes()
            self.m = validate_manifest(json.loads(self.manifest_bytes), self.root)
            self.sha = self.m['release']
            self.old_app, self.old_front = self.m['old_app'], self.m['old_front']
            self.prefix = 'itsp-feedback-' + self.sha[:12]
            self.app, self.front, self.net = (self.prefix + suffix for suffix in ('-app', '-frontend', '-net'))
            self.caddyfile = Path(self.m['caddyfile'])

        def reviewed_file(self, name, expected):
            path = self.root / name
            need(not path.is_symlink(), 'review evidence symlink refused')
            raw = path.read_bytes()
            need(sha256(raw) == expected, 'review evidence changed: ' + name)
            return raw

        def evidence(self):
            value = json.loads(self.reviewed_file('release-evidence.json', self.m['evidence_sha256']))
            need(value.get('release') == self.sha and value.get('old_revision') == OLD_REVISION
                 and value.get('target_head') == TARGET_HEAD, 'release evidence identity differs')
            need(value.get('required_ci') == 'passed' and value.get('independent_review') == 'passed'
                 and value.get('candidate_regression') == 'passed' and value.get('scope_review') == 'passed', 'release quality gate incomplete')
            need(value.get('old_app_on_expanded_schema') == 'passed', 'old app / expanded schema compatibility not verified')
            need(isinstance(value.get('release_tag'), str) and value['release_tag'].startswith('release-')
                 and value.get('release_tag_sha') == self.sha, 'exact release tag evidence missing')
            need(isinstance(value.get('references'), dict) and value['references'], 'inspectable test/review references required')
            return value

        def predecessor(self):
            path = BASE / '.presales3-active-owner.json'
            need(not path.is_symlink(), 'predecessor owner symlink refused')
            raw = path.read_bytes()
            need(sha256(raw) == self.m['predecessor_owner_sha256'], 'predecessor ownership changed')
            value = json.loads(raw)
            need(value.get('release') == OLD_REVISION and hash_ok(value.get('manifest_sha256')), 'predecessor owner identity differs')

        def counts(self, container=core.DB):
            return json.loads(self.sql("SELECT coalesce(json_object_agg(table_name, n), '{}') FROM "
                "(SELECT table_name, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) n FROM public.%I', table_name), false, true, '')))[1]::text::bigint n "
                "FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name<>'alembic_version') q", container))

        def container_config(self, row):
            # Docker clears operational endpoint/IP IDs when a writer stops.
            # Preserve network membership and requested configuration, not those
            # transient fields; immutable container identity is checked separately.
            networks = {name: {key: network.get(key) for key in ('IPAMConfig', 'Aliases', 'Links', 'DriverOpts')}
                        for name, network in row['NetworkSettings']['Networks'].items()}
            return digest_json({**{key: row[key] for key in ('Config', 'HostConfig')},
                                'networks': networks})

        def writer_inventory(self, rows, running):
            db = self.inspect(core.DB)
            aliases = {'db', core.DB, db['Config'].get('Hostname')}
            for network in db['NetworkSettings']['Networks'].values():
                aliases.update(network.get('Aliases') or [])
                aliases.add(network.get('IPAddress'))
            wanted = {row['name'] for row in self.writers()} if running else set()
            actual, client_ips = set(), set()
            for row in rows:
                target = self.database_target(row)
                if row['State']['Running'] and target and target[0] in aliases and target[1:] == (5432, '/spareparts'):
                    actual.add(row['Name'].lstrip('/'))
                    client_ips.update(net.get('IPAddress') for net in row['NetworkSettings']['Networks'].values())
            need(actual == wanted, 'same-database writer inventory differs')
            clients = json.loads(self.sql("SELECT coalesce(json_agg(DISTINCT host(client_addr)), '[]') FROM pg_stat_activity "
                "WHERE datname=current_database() AND backend_type='client backend' AND client_addr IS NOT NULL"))
            need(set(clients) <= client_ips, 'unreviewed database client remains')

        def import_gate(self):
            current = json.loads(self.sql(import_gate_query(core)))
            need(current['active_return_receipts'] == 0, 'queued/processing receipt job blocks release')
            spec = self.m.get('processing_review')
            if spec is None:
                need(current['batches'] == [] and current['jobs'] == [], 'processing import requires new release-specific review')
                return {'mode': 'all_zero', **current}
            reviewed = timestamp(spec['reviewed_at'])
            now = dt.datetime.now(dt.timezone.utc)
            need(dt.timedelta(0) <= now - reviewed <= dt.timedelta(hours=6), 'processing review stale or future-dated')
            value = json.loads(self.reviewed_file('processing-review.private.json', spec['review_sha256']))
            need(value.get('release') == self.sha and value.get('old_revision') == OLD_REVISION
                 and value.get('writers') == self.writers() and value.get('reviewed_at') == spec['reviewed_at'], 'processing review belongs to another release/writer')
            need(value.get('no_resume_path_reviewed') is True and value.get('no_surviving_worker_reviewed') is True
                 and isinstance(value.get('source_evidence'), str) and value['source_evidence'].strip(), 'processing worker lifecycle audit missing')
            need(digest_json(current) == spec['snapshot_sha256'] == value.get('snapshot_sha256'), 'processing snapshot changed')
            missing_archives = [item['id'] for item in current['batches'] if item.get('has_archives') is False]
            need(value.get('legacy_preview_without_archives') == missing_archives, 'legacy preview shape requires explicit fresh review')
            for item in current['batches']:
                need(type(item.get('has_archives')) is bool and item['preview_shape'] is True
                     and hash_ok(item['row_sha256']), 'non-preview processing batch blocks release')
            starts = {}
            for writer in self.writers():
                row = self.inspect(writer['name'])
                need(row['Id'] == writer['id'] and row['Image'] == writer['image'], 'processing writer identity changed')
                starts[writer['id']] = row['State']['StartedAt']
            for job in current['jobs']:
                need(hash_ok(job['row_sha256']) and job['finished_at'] is None
                     and job['total_files'] == job['done_files'] == 1 and job['error_files'] == 0, 'active/non-single-file import job blocks release')
                batches = job['batches']
                need(len(batches) == 1 and batches[0]['file_type'] == 'sales' and batches[0]['status'] == 'success'
                     and hash_ok(batches[0]['row_sha256']), 'orphan job linked batch differs')
                need(all(timestamp(job['created_at']) < core.docker_started_at(start) for start in starts.values()), 'job may belong to surviving writer')
            return {'mode': 'fresh_exact_readonly_review', **current, 'review_sha256': spec['review_sha256'],
                    'writer_started_at': starts, 'job_completion_claimed': False, 'post_import_refresh_completion_unknown': True}

        def preflight(self):
            self.evidence()
            self.predecessor()
            names = self.run(['docker', 'ps', '-a', '--format', '{{.Names}}']).decode().splitlines()
            need(names and not any(name.startswith(self.prefix) for name in names), 'candidate resource collision')
            rows = json.loads(self.run(['docker', 'inspect', *names]))
            available = os.statvfs(self.root)
            need(available.f_bavail * available.f_frsize >= 20 * 1024 ** 3, 'release filesystem has less than 20 GiB free')
            for name, key in ((self.old_app, 'app'), (self.old_front, 'front')):
                row = self.inspect(name)
                need(row['Id'] == self.m['old_' + key + '_id'] and row['Image'] == self.m['old_' + key + '_image'] and row['State']['Running'], 'old release drift')
                need(self.inspect(row['Image'], True)['Config']['Labels']['org.opencontainers.image.revision'] == OLD_REVISION, 'old OCI revision differs')
                core.save(self.root / (name + '.inspect.private.json'), row)
            need(self.database_target(self.inspect(self.old_app)) in {('db', 5432, '/spareparts'), (core.DB, 5432, '/spareparts')}, 'old database target differs')
            self.writer_inventory(rows, True)
            for writer in self.writers():
                row = self.inspect(writer['name'])
                need(row['Id'] == writer['id'] and row['Image'] == writer['image'] and row['State']['Running'], 'writer identity drift')
                need('it-spareparts_default' in row['NetworkSettings']['Networks'] and not row['HostConfig'].get('PortBindings'), 'writer network/host exposure differs')
            self.nginx_baseline(self.inspect(self.old_front))
            need(self.schema() == OLD_HEAD, 'old schema differs')
            imports = self.import_gate()
            need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND (wait_event_type='Lock' OR application_name ILIKE '%migration%')") == '0', 'active migration/lock')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', self.m['port']))
            networks = self.run(['docker', 'network', 'ls', '-q']).decode().split()
            candidate = ipaddress.ip_network(self.m['subnet'])
            for row in json.loads(self.run(['docker', 'network', 'inspect', *networks])):
                for block in row.get('IPAM', {}).get('Config', []) or []:
                    if block.get('Subnet'):
                        network = ipaddress.ip_network(block['Subnet'])
                        need(network.version != 4 or not candidate.overlaps(network), 'candidate network overlaps')
            data, active, st = self.caddyfile.read_bytes(), self.active(), self.caddyfile.stat()
            need(all(core.site_dials(active).get(host) == [self.old_front + ':80'] for host in core.HOSTS), 'old live upstream drift')
            need(data.count((self.old_front + ':80').encode()) == 1, 'old Caddy upstream not singular')
            inode = [st.st_dev, st.st_ino]
            self.cas(data, active, inode)
            core.write_new(self.root / 'Caddyfile.baseline', data)
            core.save(self.root / 'all-inspect.private.json', rows)
            return {'containers': {r['Name'].lstrip('/'): core.fingerprint(r) for r in rows},
                    'protected_configs': {r['Name'].lstrip('/'): self.container_config(r) for r in rows},
                    'active': active, 'inode': inode, 'schema': self.schema(), 'counts': self.counts(),
                    'db_image': self.inspect(core.DB)['Image'], 'catalog_sync': None, 'import_gate': imports}

        def protected(self, *, old_running=None, partial_freeze=False):
            super().protected(old_running=old_running, partial_freeze=partial_freeze)
            pre = self.marker('preflight')
            names = set(self.run(['docker', 'ps', '-a', '--format', '{{.Names}}']).decode().splitlines())
            need(names - set(pre['containers']) <= {self.app, self.front}, 'unreviewed container appeared')
            for name, expected in pre['protected_configs'].items():
                need(self.container_config(self.inspect(name)) == expected, 'protected runtime configuration drift: ' + name)
            return True

        def freeze(self):
            rows = json.loads(self.run(['docker', 'inspect', *self.run(['docker', 'ps', '-a', '--format', '{{.Names}}']).decode().splitlines()]))
            self.writer_inventory(rows, True)
            result = super().freeze()
            result['audit_watermark'] = int(self.sql('SELECT coalesce(max(id),0) FROM sys_access_log'))
            need(self.audit_prefix(result['audit_watermark'], result['data_fingerprints']['sys_access_log']['columns'])
                 == result['data_fingerprints']['sys_access_log'], 'access audit changed during freeze')
            return result

        def audit_prefix(self, watermark, columns):
            """Hash frozen audit rows exactly like the shared all-table snapshot."""
            need(type(watermark) is int and watermark >= 0, 'invalid frozen audit watermark')
            need(isinstance(columns, list) and 'id' in columns, 'frozen access audit columns missing')
            projection = ','.join('"' + field.replace('"', '""') + '"' for field in columns)
            query = "COPY (SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex') AS fingerprint FROM "
            query += '(SELECT ' + projection + ' FROM public.sys_access_log WHERE id <= ' + str(watermark) + ') t ORDER BY fingerprint) TO STDOUT'
            with tempfile.TemporaryFile(dir=self.root) as target:
                self.run(['docker', 'exec', '-e', 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=120000',
                    core.DB, 'psql', '-U', 'spareparts', '-d', 'spareparts', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-c', query], stdout=target, timeout=150)
                target.seek(0)
                value, size = hashlib.sha256(), 0
                for chunk in iter(lambda: target.read(1024 * 1024), b''):
                    value.update(chunk)
                    size += len(chunk)
            return {'columns': columns, 'sha256': value.hexdigest(), 'bytes': size}

        def rollback_integrity(self):
            """Only permit additional access audit rows; all other tables strict."""
            before = self.marker('freeze')
            counts = self.counts()
            expected_counts = before['counts']
            audit = 'sys_access_log'
            need(audit in expected_counts and audit in counts, 'access audit table missing')
            need({k: v for k, v in counts.items() if k != audit} == {k: v for k, v in expected_counts.items() if k != audit},
                 'business row counts changed; forward repair required')
            expected = before['data_fingerprints']
            strict = {k: v for k, v in expected.items() if k != audit}
            need(self.data_fingerprints({k: v['columns'] for k, v in strict.items()}) == strict,
                 'pre-existing non-access-audit row content changed; forward repair required')
            need(counts[audit] >= expected_counts[audit] and self.audit_prefix(before['audit_watermark'], expected[audit]['columns']) == expected[audit],
                 'frozen access audit changed; forward repair required')
            return {'frozen_access_audit_unchanged': True, 'current_additional_access_audit_rows': counts[audit] - expected_counts[audit]}

        def backup(self, label):
            result = super().backup(label)
            if label == 'old':
                self.run(['tar', '--compare', '--file', str(self.root / 'uploads.tar'), '--directory', '/data/spareparts'], timeout=3600)
                result['uploads_content_compared'] = True
            return result

        def migrate(self):
            self.unchanged_business()
            result = super().migrate()
            self.unchanged_business()
            return result

        def rollback(self):
            """Pre-exposure application recovery only; never downgrade or restore DB."""
            self.evidence()
            self.frozen()
            need(not (self.root / 'cutover.intent.json').exists(), 'cutover may have begun; application rollback refused')
            head = self.schema()
            need(head in {OLD_HEAD, TARGET_HEAD}, 'unknown schema; manual forward repair required')
            for name in (self.app, self.front):
                names = self.run(['docker', 'ps', '-a', '--format', '{{.Names}}']).decode().splitlines()
                if name in names:
                    row = self.inspect(name)
                    key = 'app' if name == self.app else 'frontend'
                    need(row['Config']['Labels'].get('bg.release') == self.sha and row['Image'] == self.m[key + '_image'], 'candidate stop ownership differs')
                    self.run(['docker', 'stop', '--time', '60', row['Id']])
            need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND client_addr IS NOT NULL") == '0', 'database clients remain before recovery')
            integrity = self.rollback_integrity()
            for writer in self.writers():
                row = self.inspect(writer['name'])
                need(row['Id'] == writer['id'] and row['Image'] == writer['image'] and not row['State']['Running'], 'old writer changed')
                self.run(['docker', 'start', writer['id']])
                row = self.inspect(writer['name'])
                need(row['State']['Running'] and row['Id'] == writer['id'] and row['Image'] == writer['image'], 'old writer recovery failed')
            need(self.schema() == head, 'application recovery changed schema')
            self.wait_http('https://ybwznt.com/health/db')
            return {'schema': head, 'schema_preserved': True, 'no_database_restore': True, 'no_downgrade': True, **integrity}

        def execute(self, phase):
            need(phase in {*PHASES, 'recover-freeze', 'rollback'}, 'unknown single-system phase')
            need(core.artifact(Path(__file__))['sha256'] == self.m['controller_sha256'], 'controller changed after review')
            need(core.artifact(Path(core.__file__))['sha256'] == self.m['helper_sha256'], 'helper changed after review')
            need(self.path.read_bytes() == self.manifest_bytes, 'manifest changed during execution')
            self.predecessor()
            owner = BASE / '.feedback-active-owner.json'
            identity = {'release': self.sha, 'manifest_sha256': sha256(self.manifest_bytes), 'controller_sha256': self.m['controller_sha256']}
            if not owner.exists():
                need(phase == 'preflight', 'missing release ownership')
                core.save(owner, identity)
            need(not owner.is_symlink() and json.loads(owner.read_text()) == identity, 'another release owns production')
            need(not any((self.root / (name + '.intent.json')).exists() for name in ('rollback', 'recover-freeze')), 'recovery already attempted; release is terminal')
            if phase in PHASES and phase != 'preflight':
                self.marker(PHASES[PHASES.index(phase) - 1])
            target = self.root / ((phase + '-' + core.stamp() if phase == 'verify' else phase) + '.json')
            need(not target.exists(), 'phase already completed')
            intent = target.with_name(target.stem + '.intent.json')
            core.save(intent, {**identity, 'phase': phase, 'time': core.stamp()})
            result = self.backup(phase.split('-')[1]) if phase.startswith('backup-') else getattr(self, phase.replace('-', '_'))()
            core.save(target, {**identity, 'phase': phase, 'status': 'PASS', 'time': dt.datetime.now(dt.timezone.utc).isoformat(), **result})
            print(json.dumps({'phase': phase, 'status': 'PASS', 'receipt': str(target), 'sha256': core.artifact(target)['sha256']}))

    return FeedbackRelease


def main():
    import fcntl
    os.umask(0o077)
    need(len(sys.argv) == 3 and os.geteuid() == 0, 'usage: feedback_release_ops.py MANIFEST PHASE (root)')
    path = Path(sys.argv[1])
    need(path.name == 'manifest.json' and not path.is_symlink() and path.parent.resolve() == path.parent
         and path.parent.stat().st_uid == 0 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700, 'private manifest root required')
    manifest = validate_manifest(json.loads(path.read_bytes()), path.parent)
    core = load_helper(Path(__file__).with_name('presales3_ops.py'), manifest['helper_sha256'])
    release = release_class(core)(path)
    locks = []
    try:
        # Same order as the legacy cross-system tooling; never touch shop resources.
        for name in ('.catalog-source-handoff.lock', '.itsp-production-deploy.lock'):
            fd = os.open(BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            locks.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.execute(sys.argv[2])
    except Exception:
        failure = release.root / ('failure-' + core.stamp() + '.private.log')
        core.write_new(failure, traceback.format_exc())
        print(json.dumps({'status': 'FAILED', 'private_diagnostic': str(failure)}))
        raise SystemExit(1)
    finally:
        for fd in reversed(locks):
            os.close(fd)


if __name__ == '__main__':
    main()
