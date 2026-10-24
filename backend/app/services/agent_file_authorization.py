"""Private Agent filesystem artifacts bound to actual bytes and stable producers."""
from hashlib import sha256
import json
from pathlib import Path
import re

from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import ObjectRef, canonical_json, plain, read_snapshot
from app.authz.provenance import validate_agent_provenance as validate_provenance, unavailable
from app.config import get_settings
from app.services import authorization_artifacts as artifacts

KIND = 'agent_file'
_KEYS = {'schema_version', 'owner_user_id', 'generation', 'bytes_sha256',
         'filename', 'ext', 'kind', 'created_at', 'operated_by', 'authorization_provenance'}
_OPTIONAL = {'sheets', 'base_file_id', 'report'}
_EXTENSIONS = {'xlsx', 'docx', 'pdf', 'txt', 'csv', 'md', 'jpg', 'jpeg', 'png', 'webp', 'bmp'}


def directory():
    return Path(get_settings().raw_file_dir) / 'agent_files'


def paths(file_id, ext=None):
    if type(file_id) is not str or not re.fullmatch('[0-9a-f]{12}', file_id):
        raise unavailable()
    if ext is not None and ext not in _EXTENSIONS:
        raise unavailable()
    return directory() / (file_id + ('.' + ext if ext else '.meta.json'))


def _header(file_id):
    try:
        path = paths(file_id).with_name(file_id + '.auth.json')
        if path.is_symlink() or path.stat().st_size > 4096:
            raise unavailable()
        header = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise unavailable() from None
    if (type(header) is not dict or set(header) != {'schema_version', 'owner_user_id', 'generation',
            'bytes_sha256', 'metadata_sha256', 'ext', 'kind'}
            or type(header['schema_version']) is not int or header['schema_version'] != 2
            or type(header['generation']) is not int or header['generation'] != 1
            or type(header['owner_user_id']) is not int or header['owner_user_id'] <= 0
            or header['kind'] not in ('upload', 'generated') or header['ext'] not in _EXTENSIONS
            or any(type(header[key]) is not str or not re.fullmatch('[0-9a-f]{64}', header[key])
                   for key in ('bytes_sha256', 'metadata_sha256'))):
        raise unavailable()
    return header, sha256(canonical_json(header).encode()).hexdigest()


def _metadata(file_id, header):
    # Caller has already passed current owner/action policy. These are content facts.
    try:
        path = paths(file_id)
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise unavailable()
        meta = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise unavailable() from None
    if (type(meta) is not dict or not _KEYS <= set(meta) or set(meta) - (_KEYS | _OPTIONAL)
            or sha256(canonical_json(meta).encode()).hexdigest() != header['metadata_sha256']
            or any(meta[key] != header[key] for key in ('schema_version', 'owner_user_id',
                                                       'generation', 'bytes_sha256', 'ext', 'kind'))):
        raise unavailable()
    return meta


def _state(db, target):
    meta, digest = _header(target.id)
    if target.generation != meta['generation']:
        raise unavailable()
    return artifacts.ArtifactDomainState(target, ObjectRef(KIND, target.id), digest, meta['owner_user_id'])


_registered = False


def register():
    global _registered
    if not _registered:
        artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(
            _state, None, (), private=True, protocol='authz_s4'))
        _registered = True


def metadata(db, file_id):
    register()
    meta, digest = _header(file_id)
    binding = artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(KIND, file_id, 1))
    if (binding.input_digest != digest or binding.subject.owner_user_id != meta['owner_user_id']
):
        raise unavailable()
    return meta, digest


def materialize(access, file_id, *, download=False):
    from app.services.agent_authorization import ExecutionContract, PREVIEW, DOWNLOAD
    register()
    with read_snapshot(lambda: Session(access.bind)) as (db, proof):
        contract = ExecutionContract(DOWNLOAD if download else PREVIEW, {'file_id': file_id})
        # This is a real data read. Unlike execution checks, keep its callback.
        accesses, ref = contract.authorize_in(db, access.identity, read_snapshot=proof)
        header, _ = metadata(db, file_id)
        binding = artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(KIND, file_id, 1))
        validate_provenance(db, access.identity, plain(binding.provenance), read_snapshot=proof)
        meta = _metadata(file_id, header)
        if plain(binding.provenance) != meta['authorization_provenance']:
            raise unavailable()
        validate_provenance(db, access.identity, meta['authorization_provenance'], read_snapshot=proof)
        source = paths(file_id, meta['ext'])
        try:
            if source.is_symlink():
                raise unavailable()
            content = source.read_bytes()
        except OSError:
            raise unavailable() from None
        if sha256(content).hexdigest() != meta['bytes_sha256']:
            raise unavailable()
        access.collector.inherit(meta['authorization_provenance'])
        # A nested tool file read must not replace its parent run's ready context.
        if (access.execution_contracts[0].entry_id == contract.entry_id
                and access.execution_contracts[0].params.get('file_id') == file_id):
            access.execution_wire = accesses.wire(ref)
    access.check()
    return meta, content


def publish(access, file_id, meta, content):
    """Exclusive new files, same-transaction binding, compensation only for our writes."""
    register()
    access.check()
    artifacts.begin_artifact_protocol(access.bind, protocol='authz_s4')
    snapshot = access.snapshot()
    meta = {**meta, 'schema_version': 2, 'owner_user_id': snapshot['principal_id'],
            'generation': 1, 'bytes_sha256': sha256(content).hexdigest(),
            'authorization_provenance': snapshot}
    content_path, meta_path = paths(file_id, meta['ext']), paths(file_id)
    header_path = meta_path.with_name(file_id + '.auth.json')
    header = {key: meta[key] for key in ('schema_version', 'owner_user_id', 'generation', 'bytes_sha256', 'ext', 'kind')}
    header['metadata_sha256'] = sha256(canonical_json(meta).encode()).hexdigest()
    directory().mkdir(parents=True, exist_ok=True)
    created = []
    try:
        # A metadata collision can never overwrite or compensate somebody else's file.
        with header_path.open('xb') as stream:
            created.append(header_path)
            stream.write(canonical_json(header).encode())
        with meta_path.open('xb') as stream:
            created.append(meta_path)
            stream.write(canonical_json(meta).encode())
        with content_path.open('xb') as stream:
            created.append(content_path)
            stream.write(content)
        with Session(access.bind) as db, db.begin():
            actor = service.lock_actor(db, access.identity)
            with service.access_recorder(lambda _: None):
                for contract in access.execution_contracts:
                    contract.authorize_in(db, access.identity, locked_user=actor)
            validate_provenance(db, access.identity, snapshot, locked_user=actor)
            artifacts.register_artifact_binding(db, access.identity,
                target=artifacts.ArtifactTarget(KIND, file_id, 1),
                input_digest=sha256(canonical_json(header).encode()).hexdigest(),
                provenance=snapshot, locked_user=actor)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    # If revocation races after commit, retain the actual bound artifact; do not
    # delete a committed file as though it were an unowned temporary candidate.
    access.check()
    return {'file_id': file_id, 'ext': meta['ext']}
