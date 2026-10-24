"""Legacy Agent file publication: independent filesystem and cutover counterexamples."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.authz import runtime
from app.authz.provenance import AgentAccess, execution_access
from app.config import get_settings
from app.models.authorization import SysAuthorizationCutover
from app.services import agent_files


@pytest.fixture
def legacy_files(db, monkeypatch, tmp_path):
    settings = get_settings()
    for key, value in dict(authorization_mode='legacy_compat', authz_enabled_resources=[],
                          authz_suspended_resources=[], authz_sales_analysis_enabled=False,
                          authz_sales_analysis_suspended=False, raw_file_dir=str(tmp_path)).items():
        monkeypatch.setattr(settings, key, value)
    access = AgentAccess(db.get_bind(), None)
    assert access.authoritative is False
    return access


def upload(access, content=b'original'):
    with execution_access(access):
        return agent_files.save_upload(content, 'synthetic.txt', 'legacy-owner')


def test_legacy_file_id_collision_cannot_overwrite_or_compensate_existing_files(db, legacy_files, monkeypatch):
    monkeypatch.setattr(agent_files, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='a' * 32)))
    first = upload(legacy_files)
    assert first['file_id'] == 'a' * 12
    before = {p.name: p.read_bytes() for p in agent_files._dir().iterdir()}
    with pytest.raises(FileExistsError):
        upload(legacy_files, b'replacement')
    assert {p.name: p.read_bytes() for p in agent_files._dir().iterdir()} == before


def test_legacy_metadata_failure_removes_only_this_operations_new_bytes(db, legacy_files, monkeypatch):
    upload(legacy_files)
    before = {p.name: p.read_bytes() for p in agent_files._dir().iterdir()}
    original = Path.open
    def fail_meta(path, mode='r', *args, **kwargs):
        if '.meta.json' in path.name and any(flag in mode for flag in ('w', 'x', 'a')):
            raise OSError('synthetic metadata publication failure')
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_meta)
    with pytest.raises(OSError, match='metadata publication failure'):
        upload(legacy_files, b'must be compensated')
    assert {p.name: p.read_bytes() for p in agent_files._dir().iterdir()} == before


def test_non_agent_first_cutover_waits_for_legacy_file_metadata_publication(db, legacy_files, monkeypatch):
    reached, release, attempting = Event(), Event(), Event()
    original = Path.open
    observed = {}
    def paused_meta(path, mode='r', *args, **kwargs):
        if '.meta.json' in path.name and any(flag in mode for flag in ('w', 'x', 'a')):
            reached.set()
            assert release.wait(10)
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', paused_meta)
    def sql(conn, cursor, statement, parameters, context, executemany):
        if 'pg_advisory_xact_lock_shared' in statement:
            observed['publisher_pid'] = conn.connection.driver_connection.info.backend_pid
        elif 'pg_advisory_xact_lock(' in statement:
            observed['activation_pid'] = conn.connection.driver_connection.info.backend_pid
            attempting.set()
    event.listen(db.get_bind(), 'before_cursor_execute', sql)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            publisher = workers.submit(upload, legacy_files)
            try:
                assert reached.wait(5)
                monkeypatch.setattr(get_settings(), 'authorization_mode', 'unified_modules')
                monkeypatch.setattr(get_settings(), 'authz_enabled_resources', ['sales_analysis'])
                activation = workers.submit(runtime.activate_configured_resources, db.get_bind())
                assert attempting.wait(5)
                deadline = time.monotonic() + 5
                blockers = []
                while time.monotonic() < deadline:
                    with Session(db.get_bind()) as observer:
                        blockers = observer.scalar(text('SELECT pg_blocking_pids(:pid)'), {'pid': observed['activation_pid']})
                    if observed.get('publisher_pid') in blockers or activation.done():
                        break
                    Event().wait(0.01)
                assert observed.get('publisher_pid') in blockers, 'unrelated first cutover passed active file publication'
                with Session(db.get_bind()) as observer:
                    assert observer.get(SysAuthorizationCutover, 'sales_analysis') is None
            finally:
                release.set()
            # Post-publication response recheck may already see the newly configured
            # authority; the publication transaction still precedes its marker.
            try:
                publisher.result(timeout=10)
            except HTTPException:
                pass
            activation.result(timeout=10)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', sql)
    assert db.get(SysAuthorizationCutover, 'sales_analysis') is not None
    published = list(agent_files._dir().iterdir())
    assert len(published) == 2
    assert next(path for path in published if path.suffix == '.txt').read_bytes() == b'original'
    assert len([path for path in published if path.name.endswith('.meta.json')]) == 1
