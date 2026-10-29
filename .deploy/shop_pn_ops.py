#!/usr/bin/env python3
"""商城 PN 接收端窄发布。候选制品事先审阅；本脚本不构建、不改 Caddy、不写业务测试数据。"""
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
import traceback
from types import ModuleType
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = Path('/data/shop/releases')
APPS = ('it-shop-next-backend', 'it-shop-next-worker', 'it-shop-next-agent', 'it-shop-next-frontend')
BRIDGE = 'it-shop-nocodb-nocodb-bridge-1'
MANAGED = (*APPS, BRIDGE)
DB = 'it-shop-next-db'
CADDY = 'it-spareparts-caddy-1'
OLD_HEAD = '0026_pn_master_follow_merges'
NEW_HEAD = '0027_catalog_delivery_receipt'
SOURCE_BASE = Path('/data/spareparts/releases')


def need(test, message):
    if not test:
        raise RuntimeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def artifact(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'bytes': Path(path).stat().st_size, 'sha256': digest.hexdigest()}


def reviewed_module(path, expected):
    path = Path(path)
    need(not path.is_symlink(), 'reviewed module symlink refused')
    raw = path.read_bytes()
    need(sha(raw) == expected, 'reviewed module checksum differs')
    module = ModuleType(path.stem)
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def save(path, value):
    data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def env_map(row):
    values = row['Config'].get('Env') or []
    result = dict(value.split('=', 1) for value in values)
    need(len(result) == len(values), 'duplicate runtime environment keys')
    return result


def database_target(row):
    value = urlsplit(env_map(row).get('DATABASE_URL', ''))
    return (value.hostname, value.port or 5432, value.path) if value.hostname else None


def fingerprint(row):
    return {'id': row['Id'], 'image': row['Image'], 'running': row['State']['Running'],
            'started': row['State']['StartedAt'], 'restarts': row['RestartCount'], 'oom': row['State']['OOMKilled']}


def candidate_config(old, image, release, catalog_hash, frontend=False):
    cfg, host = copy.deepcopy(old['Config']), copy.deepcopy(old['HostConfig'])
    cfg['Image'] = image
    cfg['Labels'] = {**(cfg.get('Labels') or {}), 'org.opencontainers.image.revision': release, 'shop.pn.release': release}
    if not frontend:
        values = env_map(old)
        need(catalog_hash != values.get('GALLERY_INTEGRATION_TOKEN_SHA256', '').strip().lower(), 'catalog credential reuses gallery')
        for key in ('ITSP_CATALOG_TOKEN', 'ITSP_PN_MASTER_TOKEN'):
            need(not values.get(key) or catalog_hash != sha(values[key].encode()), 'catalog credential reuses outbound credential')
        values.update(CATALOG_SYNC_ENABLED='true', CATALOG_SYNC_TOKEN_SHA256=catalog_hash)
        cfg['Env'] = [key + '=' + value for key, value in values.items()]
    return cfg, host


class Release:
    def __init__(self, manifest):
        self.path = Path(manifest)
        self.root = self.path.parent
        need(self.path.name == 'manifest.json' and not self.path.is_symlink(), 'manifest path invalid')
        need(self.root.resolve() == self.root and self.root.is_dir() and self.root.stat().st_uid == 0
             and stat.S_IMODE(self.root.stat().st_mode) == 0o700, 'private root-owned release directory required')
        self.raw = self.path.read_bytes()
        self.m = json.loads(self.raw)
        self.release = self.m.get('release', '')
        need(re.fullmatch('[0-9a-f]{40}', self.release), 'release must be exact SHA')
        need(self.root == BASE / ('pn-' + self.release), 'release directory mismatch')
        need(self.m.get('old_revision') == '4651d1bf7e032ed0b7d0f298532b271578485149', 'unexpected baseline revision')
        need(self.m.get('old_head') == OLD_HEAD and self.m.get('target_head') == NEW_HEAD, 'migration scope differs')
        need(set(self.m.get('old', {})) == set(MANAGED), 'explicit five-container manifest required')
        for item in self.m['old'].values():
            need(re.fullmatch('[0-9a-f]{64}', item.get('id', '')) and re.fullmatch('sha256:[0-9a-f]{64}', item.get('image', '')), 'baseline identity invalid')
        for key in ('app_image', 'frontend_image'):
            need(re.fullmatch('sha256:[0-9a-f]{64}', self.m.get(key, '')), 'candidate image invalid')
        for key in ('source_sha256', 'controller_sha256', 'catalog_token_sha256'):
            need(re.fullmatch('[0-9a-f]{64}', self.m.get(key, '')), 'reviewed hash required: ' + key)
        hold = self.m.get('source_hold')
        need(isinstance(hold, dict) and set(hold) == {'release', 'controller_sha256', 'gate_sha256', 'writers'}, 'D49 source_hold required')
        need(re.fullmatch('[0-9a-f]{40}', hold.get('release', '')) and all(re.fullmatch('[0-9a-f]{64}', hold.get(k, ''))
             for k in ('controller_sha256', 'gate_sha256')), 'source hold identity invalid')
        need(isinstance(hold['writers'], list) and len(hold['writers']) == 4, 'exactly four source writers required')
        for writer in hold['writers']:
            need(set(writer) == {'name', 'id', 'image', 'started_at'} and re.fullmatch('[0-9a-f]{64}', writer['id'])
                 and re.fullmatch('sha256:[0-9a-f]{64}', writer['image'])
                 and re.fullmatch('(it-spareparts-app|itsp-presales3)-[a-z0-9-]{1,100}', writer['name']), 'source writer identity invalid')
            need(dt.datetime.fromisoformat(writer['started_at'].replace('Z', '+00:00')).tzinfo is not None, 'source StartedAt timezone missing')
        need(len({w['id'] for w in hold['writers']}) == len({w['name'] for w in hold['writers']}) == 4, 'duplicate source writer')
        self.prefix = 'shop-pn-' + self.release[:12]

    def run(self, argv, *, input=None, stdin=None, stdout=None, timeout=180):
        result = subprocess.run(argv, input=input, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode or result.stderr:
            save(self.root / ('command-' + now() + '.private.log'), (result.stdout or b'') + b'\nSTDERR\n' + (result.stderr or b''))
        need(result.returncode == 0, 'command failed; inspect private diagnostic')
        return result.stdout if stdout is None else b''

    def inspect(self, name, image=False):
        result = json.loads(self.run(['docker', *(['image'] if image else []), 'inspect', name]))
        need(len(result) == 1, 'inspect result mismatch')
        return result[0]

    def mark(self, phase):
        row = json.loads((self.root / (phase + '.json')).read_bytes())
        need(row.get('manifest_sha256') == sha(self.raw) and row.get('phase') == phase and row.get('status') == 'PASS', 'phase receipt mismatch')
        return row

    def sql(self, query, container=DB):
        original = self.inspect(DB)
        values = env_map(original)
        return self.run(['docker', 'exec', '-e', 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=3000 -c timezone=UTC',
                         container, 'psql', '-U', values['POSTGRES_USER'], '-d', values['POSTGRES_DB'], '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-c', query]).decode().strip()

    def schema(self, container=DB):
        head = self.sql('SELECT version_num FROM alembic_version', container)
        need(head in {OLD_HEAD, NEW_HEAD}, 'unexpected or multiple schema heads')
        return head

    def counts(self, tables=None, container=DB):
        if tables is None:
            tables = self.sql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename", container).splitlines()
        need(tables and all(re.fullmatch('[a-z_][a-z0-9_]*', table) for table in tables), 'unexpected table identifier')
        query = ' UNION ALL '.join("SELECT '" + table + "',count(*) FROM public.\"" + table + '\"' for table in tables)
        return {line.split('|')[0]: int(line.split('|')[1]) for line in self.sql(query, container).splitlines()}

    def entry(self):
        data = Path('/opt/it-spareparts/Caddyfile').read_bytes()
        active = self.run(['docker', 'exec', CADDY, 'curl', '-fsS', '--max-time', '5', 'http://127.0.0.1:2019/config/'])
        return {'file_sha256': sha(data), 'active_sha256': sha(json.dumps(json.loads(active), sort_keys=True).encode())}

    def protected(self):
        before = self.mark('preflight')
        exempt = {row['id'] for row in self.m['old'].values()}
        for cid, expected in before['containers'].items():
            if cid not in exempt:
                need(fingerprint(self.inspect(cid)) == expected, 'protected container drift')
        need(self.entry() == before['entry'], 'Caddy drift; another deployment may be active')

    def frozen(self):
        receipt = self.mark('freeze')
        if 'source_hold' in self.m:
            need(receipt.get('version_gate', {}).get('status') == 'PASS', 'completed source version gate required')
            source = self.source_release()
            held = source.assert_hold()
            need(receipt['source_hold'] == held and receipt['source_manifest_sha256'] == held['manifest_sha256'], 'frozen source binding differs')
        self.protected()
        for name, expected in self.m['old'].items():
            row = self.inspect(expected['id'])
            need(not row['State']['Running'] and row['Image'] == expected['image'], 'frozen writer changed: ' + name)
            if 'source_hold' in self.m:
                need(fingerprint(row) == receipt['shop_writers'][name], 'completed freeze fingerprint changed: ' + name)

    def source_release(self):
        expected = self.m['source_hold']
        root = SOURCE_BASE / ('presales3-' + expected['release'])
        module = reviewed_module(root / 'presales3_ops.py', expected['controller_sha256'])
        source = module.Release(root / 'manifest.json')
        need(source.m['source_handoff'] == {'release': self.release, 'manifest_sha256': sha(self.raw),
             'controller_sha256': self.m['controller_sha256'], 'gate_sha256': expected['gate_sha256']}, 'reciprocal handoff binding differs')
        need([{k: w[k] for k in ('name', 'id', 'image')} for w in expected['writers']] == source.writers(), 'source writer manifest differs')
        return source

    def both_frozen(self):
        source = self.source_release()
        held = source.assert_hold()
        self.protected()
        need(self.schema() == OLD_HEAD, 'version gate old shop schema differs')
        before = self.mark('preflight')['containers']
        frozen = {}
        for name, expected in self.m['old'].items():
            row = self.inspect(expected['id'])
            need(row['Name'] == '/' + name and row['Id'] == expected['id'] and row['Image'] == expected['image']
                 and fingerprint(row) == {**before[expected['id']], 'running': False}, 'shop freeze identity drift')
            frozen[name] = fingerprint(row)
        need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND client_addr IS NOT NULL") == '0', 'shop TCP clients remain')
        return {'source_hold': held, 'source_writers': source.hold_intent()['writers'], 'shop_writers': frozen,
                'source_manifest_sha256': held['manifest_sha256'], 'shop_manifest_sha256': sha(self.raw)}

    def version_gate(self):
        source = self.source_release()
        gate = reviewed_module(Path(__file__).with_name('catalog_source_version_gate.py'), self.m['source_hold']['gate_sha256'])
        def read(query, controller):
            need(query.lstrip().startswith('SELECT '), 'version gate must be SELECT only')
            # Both controllers force read-only sessions; verify their effective setting.
            settings = json.loads(controller.sql("SELECT json_build_object('read_only', current_setting('transaction_read_only'), 'statement_timeout', current_setting('statement_timeout'), 'lock_timeout', current_setting('lock_timeout'))"))
            need(settings == {'read_only': 'on', 'statement_timeout': '30s', 'lock_timeout': '3s'}, 'read-only SQL settings not active')
            return [json.loads(line) for line in controller.sql(query).splitlines() if line]
        frozen = self.both_frozen()
        result = gate.collect(lambda q: read(q, source), lambda q: read(q, self), assert_frozen=self.both_frozen)
        need(self.both_frozen() == frozen, 'freeze identity changed during version gate')
        return {**frozen, 'version_gate': result}

    def preflight(self):
        held = self.source_release().assert_hold()
        need(artifact(Path(__file__))['sha256'] == self.m['controller_sha256'], 'controller changed')
        need(artifact(self.root / 'source.tar.gz')['sha256'] == self.m['source_sha256'], 'source archive changed')
        for key in ('app_image', 'frontend_image'):
            image = self.inspect(self.m[key], True)
            need(image['Config'].get('Labels', {}).get('org.opencontainers.image.revision') == self.release, 'candidate OCI revision differs')
        names = self.run(['docker', 'ps', '-aq']).decode().split()
        rows = json.loads(self.run(['docker', 'inspect', *names]))
        all_rows = {row['Id']: row for row in rows}
        backend = self.inspect(APPS[0])
        baseline_target = database_target(backend)
        need(baseline_target == ('db', 5432, '/shop_next'), 'unexpected shop database target')
        self.catalog_token()
        for name, expected in self.m['old'].items():
            row = self.inspect(name)
            need(row['Id'] == expected['id'] and row['Image'] == expected['image'] and row['State']['Running'], 'baseline changed: ' + name)
            if name != APPS[3]:
                need(database_target(row) == baseline_target and 'it-shop-next_private' in row['NetworkSettings']['Networks'], 'writer database mismatch')
                need(not row['HostConfig'].get('PortBindings'), 'backend/worker unexpectedly published')
                candidate_config(row, self.m['app_image'], self.release, self.m['catalog_token_sha256'])
        same_db = {row['Id'] for row in rows if row['State']['Running'] and 'it-shop-next_private' in row['NetworkSettings']['Networks']
                   and database_target(row) == baseline_target}
        need(same_db == {self.m['old'][name]['id'] for name in (*APPS[:3], BRIDGE)}, 'unlisted shop writer')
        front = self.inspect(APPS[3])
        need(set(front['NetworkSettings']['Networks']) == {'it-shop-next_private', 'it-shop-nocodb-frontend', 'it-spareparts-ingress'}, 'frontend network drift')
        need(self.schema() == OLD_HEAD, 'baseline schema mismatch')
        db = self.inspect(DB)
        need(db['State']['Running'] and re.search(rb'PostgreSQL\) 15\.', self.run(['docker','exec',DB,'postgres','--version'])), 'Postgres15 required')
        need(os.statvfs(self.root).f_bavail * os.statvfs(self.root).f_frsize > 20 * 1024**3, 'insufficient backup space')
        save(self.root / 'all-inspect.private.json', rows)
        save(self.root / 'Caddyfile.baseline', Path('/opt/it-spareparts/Caddyfile').read_bytes())
        return {'containers': {cid: fingerprint(row) for cid, row in all_rows.items()}, 'entry': self.entry(),
                'schema': OLD_HEAD, 'db_image': db['Image'], 'counts': self.counts(), 'source_hold': held}

    def freeze(self):
        self.protected()
        need(self.source_release().assert_hold() == self.mark('preflight')['source_hold'], 'source hold changed since shop preflight')
        need(self.schema() == OLD_HEAD, 'freeze schema changed')
        for name in (APPS[3], APPS[0], APPS[2], APPS[1], BRIDGE):
            expected = self.m['old'][name]
            row = self.inspect(name)
            need(row['Id'] == expected['id'] and row['Image'] == expected['image'], 'writer identity drift')
            self.run(['docker', 'stop', '--time', '660' if name == APPS[2] else '90', expected['id']], timeout=700)
            save(self.root / ('stopped-' + name + '.json'), {'id': expected['id']})
        self.protected()
        need(self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND client_addr IS NOT NULL") == '0', 'unidentified database clients remain')
        gate = self.version_gate()
        return {'schema': self.schema(), 'counts': self.counts(), **gate}

    def restore(self, dump, head, counts, suffix):
        name = self.prefix + '-restore-' + suffix
        image = self.mark('preflight')['db_image']
        values = env_map(self.inspect(DB))
        envfile = self.root / ('restore-' + suffix + '.env.private')
        save(envfile, ('POSTGRES_USER=' + values['POSTGRES_USER'] + '\nPOSTGRES_DB=' + values['POSTGRES_DB'] + '\nPOSTGRES_PASSWORD=' + secrets.token_hex(32) + '\n').encode())
        cid = self.run(['docker','run','-d','--name',name,'--network','none','--memory','4g','--cpus','2','--pids-limit','256',
                        '--security-opt','no-new-privileges','--label','shop.pn.release='+self.release,'--env-file',str(envfile),image]).decode().strip()
        try:
            row = self.inspect(cid)
            need(row['HostConfig']['NetworkMode'] == 'none' and not row['HostConfig'].get('PortBindings'), 'restore isolation differs')
            for attempt in range(60):
                status = subprocess.run(['docker','exec',cid,'pg_isready','-h','127.0.0.1','-U',values['POSTGRES_USER'],'-d',values['POSTGRES_DB']], capture_output=True)
                if status.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('restore Postgres readiness timeout')
            with dump.open('rb') as source:
                self.run(['docker','exec','-i',cid,'pg_restore','--exit-on-error','--no-owner','--no-acl','-U',values['POSTGRES_USER'],'-d',values['POSTGRES_DB']], stdin=source, timeout=3600)
            need(self.schema(cid) == head and self.counts(list(counts), cid) == counts, 'isolated restore mismatch')
            return {'actual_restore_performed': True, 'head': head, 'counts': counts, 'network': 'none', 'container_id': cid}
        finally:
            row = self.inspect(cid)
            need(row['Id'] == cid and row['Name'] == '/' + name and row['Config']['Labels'].get('shop.pn.release') == self.release, 'restore cleanup ownership differs')
            self.run(['docker','rm','-f','-v',cid])

    def backup(self, suffix):
        self.frozen()
        expected = self.mark('freeze')['counts']
        head = OLD_HEAD if suffix == 'old' else NEW_HEAD
        if suffix == 'new':
            self.mark('migrate')
        need(self.schema() == head and self.counts(list(expected)) == expected, 'backup schema/count drift')
        values = env_map(self.inspect(DB))
        dump = self.root / (suffix + '.dump')
        with dump.open('xb') as target:
            self.run(['docker','exec','-e','PGOPTIONS=-c default_transaction_read_only=on',DB,'pg_dump','-U',values['POSTGRES_USER'],'-d',values['POSTGRES_DB'],'-Fc'], stdout=target, timeout=3600)
            target.flush()
            os.fsync(target.fileno())
        need(dump.stat().st_size > 1024, 'dump unexpectedly small')
        with dump.open('rb') as source:
            save(self.root / (suffix + '-toc.private.txt'), self.run(['docker','exec','-i',DB,'pg_restore','--list'], stdin=source))
        actual = self.counts()
        restored = self.restore(dump, head, actual, suffix)
        need(self.counts() == actual, 'database changed during backup')
        self.frozen()
        return {'dump': artifact(dump), 'restore': restored}

    def migrate(self):
        self.mark('backup-old')
        self.frozen()
        need(self.schema() == OLD_HEAD, 'migration starting schema differs')
        env = env_map(self.inspect(self.m['old'][APPS[0]]['id']))
        env.update(PGAPPNAME='shop_pn_migration', PGOPTIONS='-c lock_timeout=3s -c statement_timeout=600s')
        envfile = self.root / 'migration.env.private'
        save(envfile, ('\n'.join(k+'='+v for k,v in env.items())+'\n').encode())
        prefix = ['docker','run','--rm','--name',self.prefix+'-migration','--network','it-shop-next_private','--env-file',str(envfile),self.m['app_image'],'alembic']
        need(self.run(prefix+['heads']).decode().strip() == NEW_HEAD+' (head)', 'candidate schema not singular')
        self.run(prefix+['upgrade',NEW_HEAD], timeout=1800)
        self.run(prefix+['check'], timeout=1800)
        need(self.schema() == NEW_HEAD and self.counts(list(self.mark('freeze')['counts'])) == self.mark('freeze')['counts'], 'migration affected baseline rows')
        need(self.sql('SELECT count(*) FROM catalog_delivery_receipt') == '0', 'unexpected receipt before receiver activation')
        return {'schema': NEW_HEAD}

    def create(self):
        self.mark('backup-new')
        self.frozen()
        snapshots = {r['Name'][1:]: r for r in json.loads((self.root/'all-inspect.private.json').read_bytes())}
        created = {}
        for name in APPS:
            old = snapshots[name]
            image = self.m['frontend_image'] if name == APPS[3] else self.m['app_image']
            cfg, host = candidate_config(old, image, self.release, self.m['catalog_token_sha256'], name == APPS[3])
            self.run(['docker','rename',old['Id'],name+'-before-'+self.release[:12]])
            networks = {key: {'Aliases': value.get('Aliases') or []} for key,value in old['NetworkSettings']['Networks'].items()}
            body = {**cfg,'HostConfig':host,'NetworkingConfig':{'EndpointsConfig':networks}}
            request = self.root / (name+'.create.private.json')
            save(request, body)
            cid = json.loads(self.run(['curl','-fsS','--unix-socket','/var/run/docker.sock','-H','Content-Type: application/json',
                '--data-binary','@'+str(request),'http://localhost/containers/create?name='+name]))['Id']
            need(re.fullmatch('[0-9a-f]{64}',cid), 'invalid clone ID')
            row = self.inspect(cid)
            need(row['Name']=='/'+name and not row['State']['Running'] and row['Image']==image, 'clone identity mismatch')
            for group, expected in (('Config',cfg),('HostConfig',host)):
                need(set(row[group])==set(expected),'clone fields differ')
                for key,value in expected.items():
                    actual = row[group][key]
                    if group=='HostConfig' and key=='OomKillDisable':
                        value,actual=bool(value),bool(actual)
                    need(value==actual,'clone runtime configuration differs: '+group+'.'+key)
            need(set(row['NetworkSettings']['Networks'])==set(networks),'clone networks differ')
            for key,value in networks.items():
                need(set(row['NetworkSettings']['Networks'][key].get('Aliases') or [])==set(value['Aliases']),'clone aliases differ')
            save(self.root/(name+'.clone.json'), {'id':cid})
            created[name]=cid
        return {'created':created}

    def catalog_token(self):
        path = self.root / 'catalog.token.private'
        need(not path.is_symlink() and path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == 0o600, 'private token file required')
        value = path.read_bytes()
        need(32 <= len(value) <= 256 and all(0x21 <= char <= 0x7e for char in value)
             and sha(value) == self.m['catalog_token_sha256'], 'catalog credential fingerprint differs')
        return value.decode('ascii')

    def http(self, path, statuses=(200,), *, method='GET', data=None, authenticated=False):
        headers={'Content-Type':'application/json'}
        if authenticated:
            headers['Authorization']='Bearer '+self.catalog_token()
        request = Request('https://shop.ybwznt.com'+path, data=data, method=method, headers=headers)
        try:
            with urlopen(request,timeout=20) as response:
                code,body=response.status,response.read()
        except HTTPError as error:
            code,body=error.code,error.read()
        need(code in statuses,'public HTTP check failed: '+path)
        if path in ('/api/health','/api/ready'):
            need(json.loads(body).get('status')=='ok','readiness body differs')
        return {'status':code,'sha256':sha(body)}

    def activate(self):
        created=self.mark('create')['created']
        self.frozen()
        need(self.schema()==NEW_HEAD,'activation schema differs')
        # 保存先于任何候选进程启动：host cron 也可能 docker exec backend 产生正常业务写入。
        save(self.root/'exposure-started.json', {'release':self.release,'at':now(),'forward_only':True})
        self.run(['docker','start',created[APPS[0]]])
        for attempt in range(60):
            row=self.inspect(created[APPS[0]])
            if row['State'].get('Health',{}).get('Status')=='healthy':
                break
            need(row['State']['Running'],'candidate backend stopped')
            time.sleep(1)
        else:
            raise RuntimeError('backend health timeout; forward-only repair required')
        self.run(['docker','start',created[APPS[3]]])
        for attempt in range(30):
            try:
                self.http('/api/ready')
                break
            except Exception:
                if attempt==29:
                    raise
                time.sleep(1)
        # 无认证请求必须在依赖层拒绝，不可能走入目录写入函数。
        self.http('/api/integrations/catalog/parts/sync',(401,),method='POST',data=b'{}')
        # 通过认证但故意缺少全部必填字段；422 由请求校验产生，服务函数不能执行。
        self.http('/api/integrations/catalog/parts/sync',(422,),method='POST',data=b'{}',authenticated=True)
        for name in (APPS[1],APPS[2]):
            self.run(['docker','start',created[name]])
        self.run(['docker','start',self.m['old'][BRIDGE]['id']])
        self.protected()
        return {'started':{name:fingerprint(self.inspect(cid)) for name,cid in created.items()},'bridge':fingerprint(self.inspect(self.m['old'][BRIDGE]['id']))}

    def verify(self):
        active=self.mark('activate')
        self.protected()
        need(self.schema()==NEW_HEAD,'live schema differs')
        for name,expected in active['started'].items():
            need(fingerprint(self.inspect(expected['id']))==expected and expected['running'] and not expected['oom'] and expected['restarts']==0,'live candidate changed')
        need(fingerprint(self.inspect(self.m['old'][BRIDGE]['id']))==active['bridge'],'bridge state changed')
        checks={path:self.http(path) for path in ('/api/health','/api/ready','/healthz','/pn')}
        checks['catalog_auth']=self.http('/api/integrations/catalog/parts/sync',(401,),method='POST',data=b'{}')
        checks['catalog_authenticated_validation']=self.http('/api/integrations/catalog/parts/sync',(422,),method='POST',data=b'{}',authenticated=True)
        return {'schema':NEW_HEAD,'checks':checks,'forward_only':True}

    def seal_handoff(self):
        # Seal only after a completed verify. Recheck the ORIGINAL full protected
        # set one last time; ITSP then takes a new complete baseline.
        receipts = sorted(self.root.glob('verify-*.json'))
        need(receipts, 'completed shop verify required before handoff')
        verified = json.loads(receipts[-1].read_bytes())
        need(verified.get('phase') == 'verify' and verified.get('status') == 'PASS'
             and verified.get('manifest_sha256') == sha(self.raw), 'shop verify receipt differs')
        result = self.verify()
        held = self.source_release().assert_hold()
        freeze = self.mark('freeze')
        need(freeze.get('version_gate', {}).get('status') == 'PASS' and freeze['source_hold'] == held, 'final frozen version gate missing or mismatched')
        self.protected()
        active = self.mark('activate')
        return {**result, 'protected_final': True, 'source_hold': held,
                'freeze_sha256': artifact(self.root / 'freeze.json')['sha256'],
                'verify_sha256': artifact(receipts[-1])['sha256'], 'entry': self.entry(),
                'started': active['started'], 'bridge': active['bridge']}

    def handoff_health(self):
        """Observe after ITSP has legitimately changed Caddy; never reuse old Caddy CAS."""
        sealed = self.mark('seal-handoff')
        need(sealed.get('protected_final') is True and self.schema() == NEW_HEAD, 'shop handoff/schema differs')
        freeze = self.mark('freeze')
        need(sealed['freeze_sha256'] == artifact(self.root / 'freeze.json')['sha256']
             and freeze.get('version_gate', {}).get('status') == 'PASS'
             and sealed['source_hold'] == freeze['source_hold'], 'sealed version gate binding differs')
        for expected in (*sealed['started'].values(), sealed['bridge']):
            need(expected['running'] and not expected['oom']
                 and fingerprint(self.inspect(expected['id'])) == expected, 'sealed shop runtime drift')
        checks = {path: self.http(path) for path in ('/api/health', '/api/ready', '/healthz')}
        checks['catalog_auth'] = self.http('/api/integrations/catalog/parts/sync', (401,), method='POST', data=b'{}')
        checks['catalog_authenticated_validation'] = self.http('/api/integrations/catalog/parts/sync', (422,), method='POST', data=b'{}', authenticated=True)
        return {'seal_sha256': artifact(self.root / 'seal-handoff.json')['sha256'], 'checks': checks}

    def recover_freeze(self):
        # 只处理尚未迁移/创建/暴露的部分停服。不会恢复 dump 或删除 receipt。
        self.mark('preflight')
        need((self.root/'freeze.intent.json').exists() and not (self.root/'freeze.json').exists(),
             'not an incomplete freeze')
        need(not any((self.root/(name+'.intent.json')).exists() for name in ('migrate','create','activate'))
             and not (self.root/'exposure-started.json').exists(),'migration/activation may have begun; forward-only')
        need(self.entry()==self.mark('preflight')['entry'],'entry changed during partial freeze')
        need(self.schema()==OLD_HEAD,'recovery schema differs')
        originals=[]
        for name in (APPS[0],APPS[3],APPS[1],APPS[2],BRIDGE):
            row=self.inspect(self.m['old'][name]['id'])
            need(row['Id']==self.m['old'][name]['id'] and row['Name']=='/'+name
                 and row['Image']==self.m['old'][name]['image'],'recovery identity differs')
            originals.append(row)
        for row in originals:
            if not row['State']['Running']:
                self.run(['docker','start',row['Id']])
        def recovered():
            current={}
            for name,expected in self.m['old'].items():
                row=self.inspect(expected['id'])
                need(row['Id']==expected['id'] and row['Name']=='/'+name and row['Image']==expected['image']
                     and row['State']['Running'],'original process failed after restart: '+name)
                current[name]=row
            return current
        deadline=time.monotonic()+90
        for attempt in range(60):
            current=recovered()
            healthy=all(current[name]['State'].get('Health',{}).get('Status')=='healthy' for name in (APPS[0],APPS[3]))
            if healthy:
                try:
                    checks={path:self.http(path) for path in ('/api/ready','/healthz')}
                except Exception:
                    if attempt==59 or time.monotonic()>=deadline:
                        raise RuntimeError('original public readiness failed after restart') from None
                else:
                    final=recovered()
                    need(all(final[name]['State'].get('Health',{}).get('Status')=='healthy' for name in (APPS[0],APPS[3])),
                         'original health changed during recovery verification')
                    return {'schema':OLD_HEAD,'restored_original_processes':True,'database_restored':False,
                            'containers':{name:fingerprint(row) for name,row in final.items()},'checks':checks}
            if attempt==59 or time.monotonic()>=deadline:
                raise RuntimeError('original container health timeout after restart')
            time.sleep(1)

    def execute(self,phase):
        need(phase in {'preflight','freeze','backup-old','migrate','backup-new','create','activate','verify','recover-freeze','seal-handoff'},'unknown phase')
        need(artifact(Path(__file__))['sha256']==self.m['controller_sha256'],'controller changed after review')
        if 'source_hold' in self.m:
            need(artifact(Path(__file__).with_name('catalog_source_version_gate.py'))['sha256'] == self.m['source_hold']['gate_sha256'], 'source version gate changed after review')
        need(not (self.root / 'seal-handoff.json').exists(), 'shop handoff sealed; ITSP now owns protected baseline')
        owner=BASE/'.shop-pn-active-owner.json'
        if not owner.exists():
            need(phase=='preflight','missing release owner')
            save(owner,{'release':self.release,'manifest_sha256':sha(self.raw)})
        need(not owner.is_symlink() and json.loads(owner.read_bytes())=={'release':self.release,'manifest_sha256':sha(self.raw)},'other release owns shop')
        receipt=self.root/((phase+'-'+now() if phase=='verify' else phase)+'.json')
        if phase!='verify':
            need(not receipt.exists(),'phase already completed')
            save(self.root/(phase+'.intent.json'),{'at':now(),'phase':phase,'manifest_sha256':sha(self.raw)})
        result=self.backup(phase.split('-')[1]) if phase.startswith('backup-') else getattr(self,phase.replace('-','_'))()
        save(receipt,{'phase':phase,'status':'PASS','manifest_sha256':sha(self.raw),'at':now(),**result})
        print(json.dumps({'phase':phase,'status':'PASS','receipt':str(receipt),'sha256':artifact(receipt)['sha256']}))


def main():
    os.umask(0o077)
    need(len(sys.argv)==3 and os.geteuid()==0,'usage: shop_pn_ops.py MANIFEST PHASE (root)')
    release=Release(sys.argv[1])
    joint=os.open(SOURCE_BASE/'.catalog-source-handoff.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(joint,fcntl.LOCK_EX|fcntl.LOCK_NB)
    lock=os.open(BASE/'.shop-production-deploy.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        release.execute(sys.argv[2])
    except Exception:
        log=release.root/('failure-'+now()+'.private.log')
        save(log,traceback.format_exc().encode())
        print(json.dumps({'status':'FAILED','private_diagnostic':str(log)}))
        raise SystemExit(1)


if __name__=='__main__':
    main()
