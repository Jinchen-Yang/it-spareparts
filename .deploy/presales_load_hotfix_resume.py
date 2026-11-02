"""Resume one known, pre-exposure failure; Caddy stdin avoids generated hide-file drift."""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback

os.umask(0o077)
root = Path('/data/spareparts/releases/presales-load-e2e335bc72bf8cb95ce72ffae5e0e8e151b6da5c')
control = root / 'presales_load_hotfix_ops.py'
assert hashlib.sha256(control.read_bytes()).hexdigest() == '1ad5c0a8f0d87be8ae64acd3c4cee9fe911129a82534bdf76ac117b4d2dbba18'
assert len(sys.argv) == 2 and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == sys.argv[1]
spec = importlib.util.spec_from_file_location('reviewed_hotfix_control', control)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
h = module.h
release = module.Hotfix(root / 'manifest.json')
locks = [os.open(h.BASE / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
         for name in ('.catalog-source-handoff.lock', '.itsp-production-deploy.lock')]
for lock in locks:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
identity = {'release': release.sha, 'manifest_sha256': h.digest(release.manifest_bytes)}
assert json.loads((h.BASE / '.presales-load-hotfix-owner.json').read_bytes()) == identity
assert (root / 'cutover.intent.json').exists() and not (root / 'cutover.json').exists()
assert not (root / 'exposure-started.json').exists()
try:
    h.save(root / 'cutover-resume-stdin.intent.json', {**identity, 'controller_sha256': sys.argv[1], 'time': h.stamp()})
    release.frozen()
    pre, blue = release.marker('preflight'), release.marker('bluegreen')
    for name, key in ((release.app, 'app'), (release.front, 'frontend')):
        h.need(h.fingerprint(release.inspect(name)) == blue[key], 'candidate fingerprint drift')
    before = (root / 'Caddyfile.baseline').read_bytes()
    old_target, new_target = release.old_front + ':80', release.front + ':80'
    h.need(before.count(old_target.encode()) == 1, 'source upstream is not singular')
    after = before.replace(old_target.encode(), new_target.encode())
    h.need((root / 'Caddyfile.candidate').read_bytes() == after, 'previous candidate differs')
    adapted = []
    for data in (before, after):
        release.run(['docker', 'exec', '-i', h.CADDY, 'caddy', 'validate', '--config', '-', '--adapter', 'caddyfile'], input=data)
        adapted.append(json.loads(release.run(['docker', 'exec', '-i', h.CADDY, 'caddy', 'adapt', '--config', '-', '--adapter', 'caddyfile'], input=data)))
    h.need(h.replace_active(adapted[0], old_target, new_target) == adapted[1], 'stdin adaptation changes other settings')
    desired = h.replace_active(pre['active'], old_target, new_target)
    h.need(release.schema() == release.m['target_head'], 'resume schema drift')
    release.cas(before, pre['active'], pre['inode'])
    h.save(root / 'exposure-started.json', {**identity, 'active_after': desired, 'after_sha256': h.digest(after), 'resume_sha256': sys.argv[1]})
    release.write_caddy(after, pre['inode'])
    release.load_active(desired)
    release.cas(after, desired, pre['inode'])
    receipt = {**identity, 'phase': 'cutover', 'status': 'PASS', 'time': dt.datetime.now(dt.timezone.utc).isoformat(),
               'active': desired, 'data_preserving_rollback': True, 'resume_sha256': sys.argv[1]}
    h.save(root / 'cutover.json', receipt)
    print(json.dumps({'status': 'PASS', 'phase': 'cutover', 'receipt': str(root / 'cutover.json'), 'sha256': h.artifact(root / 'cutover.json')['sha256']}))
except Exception:
    path = root / ('resume-failure-' + h.stamp() + '.private.log')
    h.write_new(path, traceback.format_exc())
    print(json.dumps({'status': 'FAILED', 'private_diagnostic': str(path)}))
    raise SystemExit(1)
