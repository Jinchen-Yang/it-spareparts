"""Whole signed workbook input, current apply authorization and immutable receipt.

Transient upload selectors never become persisted authorization proof. A replay
resolves its actual successful batch, operations and original bytes anew, then
requires the caller's current complete apply contract.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import service
from app.authz.context import ObjectRef, PolicySubject, create_context_batch, read_snapshot, relation_revision
from app.authz.errors import authz_error
from app.models.maintenance import MaintenanceContractWorkbookState, MaintenanceRoundtripOperation
from app.models.system import SysImportBatch, SysRawFile
from app.services import authorization_artifacts as artifacts
from app.services import maintenance_authorization as foundation
from app.services import maintenance_roundtrip as business
from app.services.maintenance_workflow_authorization import require_complete

ENTRY = 'POST /api/maintenance/roundtrip-import'
RESOURCE = 'maintenance_cost_roundtrip'
KIND = 'maintenance_roundtrip_batch'
_SEAL = object()


def _json(value):
    return json.loads(json.dumps(value, default=str))


def _digest(db, batch):
    files = [list(row) for row in db.execute(select(SysRawFile.id, SysRawFile.file_hash,
        SysRawFile.storage_path, SysRawFile.filename).where(SysRawFile.batch_id == batch.id).order_by(SysRawFile.id))]
    operations = [list(row) for row in db.execute(select(MaintenanceRoundtripOperation.id,
        MaintenanceRoundtripOperation.export_id, MaintenanceRoundtripOperation.sheet_code,
        MaintenanceRoundtripOperation.client_row_id, MaintenanceRoundtripOperation.operation,
        MaintenanceRoundtripOperation.payload_hash, MaintenanceRoundtripOperation.result_json,
        MaintenanceRoundtripOperation.owner_user_id, MaintenanceRoundtripOperation.applied_by,
        MaintenanceRoundtripOperation.applied_at).where(MaintenanceRoundtripOperation.import_batch_id == batch.id)
        .order_by(MaintenanceRoundtripOperation.id))]
    return relation_revision(_json({'batch': [batch.id, batch.owner_user_id, batch.file_type, batch.file_hash,
        batch.filename, batch.uploaded_by, batch.uploaded_at, batch.status, batch.report_json, batch.rows_total, batch.rows_inserted,
        batch.rows_skipped, batch.rows_error, batch.rows_inactive], 'files': files, 'operations': operations}))


def _state(db, target):
    batch = db.get(SysImportBatch, int(target.id), populate_existing=True)
    if (target.generation != 1 or batch is None or batch.file_type != business.ROUNDTRIP_FILE_TYPE
            or batch.status != 'success' or batch.owner_user_id is None):
        raise artifacts.unavailable()
    return artifacts.ArtifactDomainState(target, ObjectRef('roundtrip_import_batch', str(batch.id)),
        _digest(db, batch), batch.owner_user_id)


artifacts.register_artifact_domain(KIND, artifacts.ArtifactDomainAdapter(_state, None, (), private=False))


def _verify_receipt(db, batch_id, *, lock=False):
    query = select(SysImportBatch).where(SysImportBatch.id == batch_id).execution_options(populate_existing=True)
    batch = db.scalar(query.with_for_update() if lock else query)
    if batch is None:
        raise artifacts.unavailable()
    artifacts.load_artifact_binding(db, target=artifacts.ArtifactTarget(KIND, str(batch_id), 1))
    files = list(db.scalars(select(SysRawFile).where(SysRawFile.batch_id == batch_id)))
    if len(files) != 1 or files[0].file_hash != batch.file_hash:
        raise artifacts.unavailable()
    path = Path(files[0].storage_path)
    try:
        if path.is_symlink() or not path.is_file() or business.pipeline.sha256_file(str(path)) != batch.file_hash:
            raise artifacts.unavailable()
    except OSError:
        raise artifacts.unavailable() from None
    return batch


def _input_plan(db, path):
    file_hash = business.pipeline.sha256_file(path)
    workbook = None
    try:
        workbook, metadata, revisions, rows = business._load_and_parse(path)
        business._validate_project_rows(rows['01_项目'])
        business._validate_exported_row_identities(rows, export_id=metadata['export_id'])
        intents = business._operation_intents(rows)
        pending, replayed = business._partition_operation_replays(db, export_id=metadata['export_id'], intents=intents)
        business._validate_replayed_intents(export_id=metadata['export_id'], replayed=replayed)
        batch_ids = set()
        if replayed:
            keys = {(item.row.sheet, item.client_row_id) for item in replayed}
            for row in db.scalars(select(MaintenanceRoundtripOperation).where(
                    MaintenanceRoundtripOperation.export_id == metadata['export_id'])):
                if (row.sheet_code, row.client_row_id) in keys:
                    batch_ids.add(row.import_batch_id)
        duplicate = business.pipeline.successful_batch_ids_by_hash(db, {file_hash},
            file_type=business.ROUNDTRIP_FILE_TYPE).get(file_hash)
        if duplicate is not None:
            batch_ids.add(duplicate)
        receipts = {str(bid): _digest(db, _verify_receipt(db, bid)) for bid in sorted(batch_ids)}
        states = [list(row) for row in db.execute(select(MaintenanceContractWorkbookState.contract_no,
            MaintenanceContractWorkbookState.revision).where(MaintenanceContractWorkbookState.contract_no.in_(sorted(revisions)))
            .order_by(MaintenanceContractWorkbookState.contract_no))]
        # Include the complete global recompute closure; this importer refreshes
        # source costs and confirmed site issues beyond the workbook's edited rows.
        from app.services.maintenance_global_workbook_authorization import _recompute_topology
        plan = {'file_sha256': file_hash, 'metadata': metadata, 'signed_revisions': revisions,
            'states': states, 'replay_receipts': receipts, 'global_topology': _recompute_topology(db),
            'intents': [[item.row.sheet, item.client_row_id, item.payload_hash] for item in intents]}
        return _json(plan)
    finally:
        if workbook is not None:
            workbook.close()


def _context(db, identity, *, entry_id=ENTRY, params, branch=None, read_snapshot=None,
             locked_user=None, principal=None, plan=None):
    if entry_id != ENTRY or branch not in (None, 'default'):
        raise authz_error(403, 'AUTHZ_CONTEXT_REQUIRED', '工作簿回填入口不匹配')
    principal = principal or foundation._named_principal(db, identity,
        read_snapshot=read_snapshot, locked_user=locked_user)
    if plan is None and read_snapshot is not None:
        # This resource has one global all-contracts action, independent of
        # workbook/replay contents. Deny before loading any historical facts.
        admission=_context(db,identity,params=params,principal=principal,read_snapshot=read_snapshot,
            plan={'file_sha256':business.pipeline.sha256_file(params['path']),'signed_revisions':{}})
        require_complete(service.context_accesses(db,identity,admission,read_snapshot=read_snapshot))
    plan = plan if plan is not None else _input_plan(db, params['path'])
    digest = relation_revision(plan)
    carrier = foundation.slot_ref(ENTRY, 'context_1', relation_revision({'actor': principal.id,
        'epoch': principal.authorization_epoch, 'plan': digest}), selection={'command_plan_sha256': digest})
    discovery = foundation.slot_ref(ENTRY, 'context_0', 'maintenance_raw_contracts',
        selection={'file_sha256': plan['file_sha256'], 'command_plan_sha256': digest})
    context = foundation.resolved_context(db, entry_id=ENTRY, branch='default', carrier=carrier,
        slots={'context_0': (discovery,), 'context_1': (carrier,)},
        subjects={RESOURCE: tuple(PolicySubject(value, {}) for value in
            [carrier.id, *sorted(plan['signed_revisions'])])},
        revision=relation_revision({'plan': digest, 'actor': [principal.id, principal.authorization_epoch]}))
    return create_context_batch(db, [context])


foundation.register_entry_resolver(ENTRY, _context)


class RoundtripAuthorization:
    """Internal exact type, sealed to the parsed file, principal and transaction."""
    def __init__(self, identity, principal, path, precondition, initial):
        self.identity, self.principal, self.path = identity, principal, path
        self.precondition, self.initial = precondition, initial
        self._seal = _SEAL
        self.batch = self.transaction = self.actor = None
        self.owned_path = None
        self.result_batch_id = None

    def capture(self, db, file_hash):
        if self._seal is not _SEAL or self.transaction is not None:
            raise ValueError('工作簿回填授权上下文不可复用')
        plan = _input_plan(db, self.path)
        if plan['file_sha256'] != file_hash:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '工作簿输入已变化')
        self.batch = _context(db, self.identity, params={'path': self.path}, principal=self.principal, plan=plan)
        # A lock wait may have changed input topology. This comparison is to the
        # actual fresh RR admission, even when the client omitted a precondition.
        if self.batch.contexts[0] != self.initial.contexts[0]:
            raise authz_error(409, 'AUTHZ_CONTEXT_STALE', '工作簿范围已变化，请重新预览')
        self.transaction = db.get_transaction()
        return self.principal.id

    def finish(self, db, *, replay_batch_ids=()):
        if self._seal is not _SEAL or self.batch is None or self.transaction is not db.get_transaction():
            raise ValueError('工作簿最终授权缺少同事务输入')
        for batch_id in sorted(set(replay_batch_ids)):
            _verify_receipt(db, batch_id, lock=True)
        self.actor = service.lock_actor(db, self.identity)
        accesses = service.context_accesses(db, self.identity, self.batch, locked_user=self.actor)
        require_complete(accesses)
        access = accesses.require(self.batch.contexts[0].ref, RESOURCE, 'apply')
        if access.scope != 'all_contracts':
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '完整工作簿回填需要全部合同范围')
        service.check_context_precondition(self.precondition, accesses, self.batch.contexts[0].ref)

    def archive(self, path, file_hash):
        if self.actor is None or business.pipeline.sha256_file(path) != file_hash:
            raise artifacts.unavailable()
        from app.config import get_settings
        directory = Path(get_settings().raw_file_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # The per-command file is exclusively owned; failed imports cannot
        # delete a shared hash archive referenced by another successful batch.
        descriptor, target = tempfile.mkstemp(prefix='roundtrip-', suffix='.xlsx', dir=directory)
        self.owned_path = target
        with os.fdopen(descriptor, 'wb') as output, open(path, 'rb') as source:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if business.pipeline.sha256_file(target) != file_hash:
            raise artifacts.unavailable()
        return target

    def publish(self, db, batch):
        if self.actor is None or self.transaction is not db.get_transaction():
            raise ValueError('回填回执缺少最终授权')
        self.result_batch_id = batch.id
        db.flush()
        artifacts.register_artifact_binding(db, self.identity,
            target=artifacts.ArtifactTarget(KIND, str(batch.id), 1), input_digest=_digest(db, batch), locked_user=self.actor)

    def cleanup(self, bind):
        if self.owned_path is None:
            return
        # A commit transport failure can leave a durable receipt. Preserve its
        # bytes when the commit outcome cannot be established.
        try:
            with Session(bind) as check:
                durable = check.scalar(select(SysRawFile.id).where(SysRawFile.storage_path == self.owned_path))
            if durable is None:
                Path(self.owned_path).unlink(missing_ok=True)
        except Exception:
            return


def execute(db, identity, *, path, filename, view=None, precondition=None):
    if view not in (None, 'authorization'):
        raise authz_error(422, 'AUTHZ_INVALID_REQUEST', '未知授权视图')
    parsed = foundation.decode_precondition(precondition)
    with read_snapshot(lambda: Session(db.get_bind())) as (snapshot, proof):
        initial = _context(snapshot, identity, params={'path': path}, read_snapshot=proof)
        accesses = service.context_accesses(snapshot, identity, initial, read_snapshot=proof)
        require_complete(accesses)
        access = accesses.require(initial.contexts[0].ref, RESOURCE, 'apply')
        if access.scope != 'all_contracts':
            raise authz_error(403, 'AUTHZ_FORBIDDEN', '完整工作簿回填需要全部合同范围')
        if view == 'authorization':
            return service.context_resolution(accesses, initial.contexts[0].ref)
        service.check_context_precondition(parsed, accesses, initial.contexts[0].ref)
        principal = accesses.principal
    db.rollback()
    artifacts.begin_artifact_protocol(db.get_bind())
    authorization = RoundtripAuthorization(identity, principal, path, parsed, initial)
    try:
        result = business.import_roundtrip_workbook(db, path, filename=filename,
            operated_by=principal.username, _authorization=authorization)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        authorization.cleanup(db.get_bind())
        raise
