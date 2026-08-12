"""Run-level pytest database and raw archive isolation contracts."""

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from tests import run_isolation
from tests.run_isolation import (
    DatabaseRun,
    capture_database_run,
    cleanup_database_run,
    cleanup_raw_run,
    create_database_run,
    create_raw_run,
    validate_database_base,
    validate_raw_base,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("_pytest_run_isolation_probe.py")
GENERATED_DATABASE = re.compile(r"^spareparts_test_(\d+)_([a-z0-9]+)$")
SUBPROCESS_TIMEOUT_SECONDS = 180


def _render(url) -> str:
    return url.render_as_string(hide_password=False)


def _maintenance_engine(url):
    return create_engine(
        url.set(database="postgres"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )


def _database_exists(url, name: str) -> bool:
    engine = _maintenance_engine(url)
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"),
                    {"name": name},
                ).scalar()
            )
    finally:
        engine.dispose()


def _databases_with_prefix(url, prefix: str) -> list[str]:
    engine = _maintenance_engine(url)
    try:
        with engine.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :prefix"),
                    {"prefix": f"{prefix}%"},
                ).scalars()
            )
    finally:
        engine.dispose()


def _create_exact_database(base_url, name: str):
    engine = _maintenance_engine(base_url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return capture_database_run(_render(base_url), name, expected_name=name)


def _cleanup_exact_database(handle, expected_name: str) -> None:
    cleanup_database_run(handle, expected_name=expected_name)


def _cleanup_child_state_if_present(base_url, state: dict) -> None:
    name = state["database_name"]
    match = GENERATED_DATABASE.fullmatch(name)
    if not match or int(match.group(1)) != state["pid"]:
        raise AssertionError(
            "child state does not identify one exact generated database"
        )
    if not _database_exists(base_url, name):
        return
    handle = capture_database_run(_render(base_url), name, expected_name=name)
    cleanup_database_run(handle)


def _probe_environment(tmp_path: Path, run_id: str, *, shared: str | None = None):
    base = make_url(os.environ["DATABASE_URL"]).set(database="spareparts_test")
    suffix = shared or uuid.uuid4().hex[:10]
    inherited_name = f"spareparts_test_issue147_contract_{suffix}"
    inherited_raw = tmp_path / f"inherited-{suffix}"
    marker = tmp_path / f"{run_id}.json"
    release = tmp_path / f"{run_id}.release"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": _render(base.set(database=inherited_name)),
            "PYTEST_DATABASE_BASE_URL": _render(base),
            "RAW_FILE_DIR": str(inherited_raw),
            "PYTEST_RAW_FILE_BASE_DIR": str(tmp_path / "raw-base"),
            "ISOLATION_PROBE_MARKER": str(marker),
            "ISOLATION_PROBE_RELEASE": str(release),
            "ISOLATION_PROBE_RUN_ID": run_id,
        }
    )
    return env, base, inherited_name, inherited_raw, marker, release


def _start_probe(env, mode: str = "pass") -> subprocess.Popen[str]:
    child_env = env.copy()
    child_env["ISOLATION_PROBE_MODE"] = mode
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(PROBE)],
        cwd=BACKEND_DIR,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_state(
    process,
    marker: Path,
    timeout: float = SUBPROCESS_TIMEOUT_SECONDS,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            state = json.loads(marker.read_text(encoding="utf-8"))
            marker.unlink()
            return state
        if process.poll() is not None:
            output = process.communicate()[0]
            pytest.fail(f"probe exited before ready ({process.returncode}):\n{output}")
        time.sleep(0.05)
    process.terminate()
    output = process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)[0]
    pytest.fail(f"probe did not become ready:\n{output}")


def _assert_generated_run_state(
    state: dict, base_url, raw_base: Path
) -> tuple[str, Path]:
    name = state["database_name"]
    url = base_url.set(database=name)
    match = GENERATED_DATABASE.fullmatch(name)
    assert match, f"pytest database must be spareparts_test_<pid>_<token>, got {name!r}"
    assert int(match.group(1)) == state["pid"]
    assert len(name) <= 63
    assert url.set(database=base_url.database) == base_url

    raw_dir = Path(state["raw_file_dir"])
    assert raw_dir.is_relative_to(raw_base)
    assert raw_dir != raw_base
    assert not raw_dir.is_relative_to(BACKEND_DIR / "data" / "raw")
    return name, raw_dir


def _finish(
    process: subprocess.Popen[str],
    timeout: float = SUBPROCESS_TIMEOUT_SECONDS,
) -> str:
    output = process.communicate(timeout=timeout)[0]
    assert process.returncode == 0, output
    return output


def _snapshot_worktree_raw() -> set[Path]:
    root = BACKEND_DIR / "data" / "raw"
    return set(root.rglob("*")) if root.exists() else set()


def test_contract_subprocess_timeout_exceeds_database_maintenance_timeouts():
    assert SUBPROCESS_TIMEOUT_SECONDS == 180
    assert (
        SUBPROCESS_TIMEOUT_SECONDS
        > max(
            run_isolation.MAINTENANCE_STATEMENT_TIMEOUT_MS,
            run_isolation.MAINTENANCE_DROP_STATEMENT_TIMEOUT_MS,
        )
        / 1000
    )
    source = Path(__file__).read_text(encoding="utf-8")
    assert ("timeout=" + "90") not in source
    assert ("timeout: float = " + "90") not in source
    assert not re.search(r"_finish\([^\n]*timeout=" + "90", source)


def test_current_run_derives_bounded_database_and_temporary_raw_directory():
    state = {
        "database_name": make_url(os.environ["DATABASE_URL"]).database,
        "raw_file_dir": os.environ["RAW_FILE_DIR"],
        "pid": os.getpid(),
    }
    database_base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    raw_base = Path(os.environ["PYTEST_RAW_FILE_BASE_DIR"])
    _assert_generated_run_state(state, database_base, raw_base)


def test_database_run_requires_absent_name_and_revalidates_oid_and_owner():
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    collision_token = f"collision{uuid.uuid4().hex[:8]}"
    fresh_token = f"fresh{uuid.uuid4().hex[:8]}"
    collision_name = f"spareparts_test_{os.getpid()}_{collision_token}"
    fresh_name = f"spareparts_test_{os.getpid()}_{fresh_token}"
    maint = _maintenance_engine(base)
    handle = None
    collision_handle = None
    try:
        collision_handle = _create_exact_database(base, collision_name)
        with maint.connect() as conn:
            collision_oid = conn.execute(
                text("SELECT oid FROM pg_database WHERE datname=:name"),
                {"name": collision_name},
            ).scalar_one()
        tokens = iter([collision_token, fresh_token])
        owned = []
        handle = create_database_run(
            _render(base),
            token_factory=lambda: next(tokens),
            on_owned=owned.append,
            max_attempts=2,
        )
        assert owned == [handle]
        assert handle.name == fresh_name
        assert handle.database_oid != collision_oid
        assert handle.owner_oid > 0
        assert handle.owner_role
        with maint.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT oid FROM pg_database WHERE datname=:name"),
                    {"name": collision_name},
                ).scalar_one()
                == collision_oid
            )

        with pytest.raises(RuntimeError, match="OID"):
            cleanup_database_run(replace(handle, database_oid=handle.database_oid + 1))
        assert _database_exists(base, fresh_name)
        with pytest.raises(RuntimeError, match="owner"):
            cleanup_database_run(replace(handle, owner_oid=handle.owner_oid + 1))
        assert _database_exists(base, fresh_name)
        cleanup_database_run(handle)
        assert not _database_exists(base, fresh_name)
        handle = None
    finally:
        if handle is not None:
            cleanup_database_run(handle)
        if collision_handle is not None:
            _cleanup_exact_database(collision_handle, collision_name)
        maint.dispose()


def test_database_run_leaves_published_recovery_to_lifecycle_on_identity_failure(
    monkeypatch,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"identityfailure{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original = run_isolation._database_identity
    calls = 0

    def fail_once(conn, database_name):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("intentional identity lookup failure")
        return original(conn, database_name)

    monkeypatch.setattr(run_isolation, "_database_identity", fail_once)
    owned = []
    try:
        with pytest.raises(RuntimeError) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                on_owned=owned.append,
                max_attempts=1,
            )
        assert str(raised.value) == "pytest database creation failed safely"
        assert "intentional identity lookup failure" not in str(raised.value)
        assert len(owned) == 1
        assert owned[0].name == name
        assert _database_exists(base, name)
    finally:
        if owned and _database_exists(base, name):
            cleanup_database_run(owned[0])


def test_raw_run_cleanup_requires_exact_inodes_and_marker(tmp_path):
    root = (tmp_path / "raw-root").resolve()
    handle = create_raw_run(root)
    assert (handle.root_dev, handle.root_ino) == (
        root.stat().st_dev,
        root.stat().st_ino,
    )
    assert (handle.run_dev, handle.run_ino) == (
        handle.run_dir.stat().st_dev,
        handle.run_dir.stat().st_ino,
    )

    handle.marker_path.write_text("replaced", encoding="ascii")
    with pytest.raises(RuntimeError, match="marker"):
        cleanup_raw_run(handle)
    handle.marker_path.write_text(handle.marker, encoding="ascii")

    parked = root / "parked-original"
    handle.run_dir.rename(parked)
    handle.run_dir.mkdir()
    handle.marker_path.write_text(handle.marker, encoding="ascii")
    try:
        with pytest.raises(RuntimeError, match="identity"):
            cleanup_raw_run(handle)
        assert handle.run_dir.exists()
    finally:
        shutil.rmtree(handle.run_dir)
        parked.rename(handle.run_dir)

    parked = root / "parked-for-symlink"
    handle.run_dir.rename(parked)
    handle.run_dir.symlink_to(root / "missing-target", target_is_directory=True)
    try:
        with pytest.raises(RuntimeError, match="identity"):
            cleanup_raw_run(handle)
    finally:
        handle.run_dir.unlink()
        parked.rename(handle.run_dir)

    parked_root = tmp_path / "parked-root"
    root.rename(parked_root)
    root.symlink_to(parked_root, target_is_directory=True)
    try:
        with pytest.raises(RuntimeError, match="RAW base"):
            cleanup_raw_run(handle)
    finally:
        root.unlink()
        parked_root.rename(root)
    cleanup_raw_run(handle)
    assert not handle.run_dir.exists()


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts",
        "postgresql+psycopg://spareparts:spareparts@db.example.invalid:5433/spareparts_test",
        "postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_test?dbname=production",
    ],
)
def test_unsafe_base_url_is_rejected_before_create_engine(tmp_path, unsafe_url):
    sitecustomize = tmp_path / "sitecustomize.py"
    engine_marker = tmp_path / "create-engine-called"
    sitecustomize.write_text(
        "import os\n"
        "import sqlalchemy\n"
        "_original = sqlalchemy.create_engine\n"
        "def guarded_create_engine(*args, **kwargs):\n"
        "    open(os.environ['CREATE_ENGINE_MARKER'], 'w').close()\n"
        "    return _original(*args, **kwargs)\n"
        "sqlalchemy.create_engine = guarded_create_engine\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": unsafe_url,
            "PYTEST_DATABASE_BASE_URL": unsafe_url,
            "PYTHONPATH": os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]),
            "CREATE_ENGINE_MARKER": str(engine_marker),
            "ALLOW_REMOTE_TEST_DB_REBUILD": "1",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode != 0
    assert not engine_marker.exists(), completed.stdout + completed.stderr


def test_app_import_failure_cleans_resources_created_during_conftest_startup(tmp_path):
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import builtins\n"
        "_original_import = builtins.__import__\n"
        "def fail_app_db(name, *args, **kwargs):\n"
        "    if name == 'app.db':\n"
        "        raise RuntimeError('intentional app.db import failure')\n"
        "    return _original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = fail_app_db\n",
        encoding="utf-8",
    )
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    raw_base = tmp_path / "startup-raw"
    env = os.environ.copy()
    env.update(
        {
            "PYTEST_DATABASE_BASE_URL": _render(base),
            "PYTEST_RAW_FILE_BASE_DIR": str(raw_base),
            "PYTHONPATH": os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)[0]
    assert process.returncode != 0, output
    assert not _databases_with_prefix(base, f"spareparts_test_{process.pid}_")
    assert raw_base.exists()
    assert list(raw_base.iterdir()) == []


@pytest.mark.parametrize(
    "mode, expected_code",
    [
        ("pass", 0),
        ("fail", 1),
        ("keyboard_interrupt", 2),
    ],
)
def test_sub_pytest_cleans_only_its_database_and_raw_directory_on_exit(
    tmp_path,
    mode,
    expected_code,
):
    env, base, inherited_name, inherited_raw, marker, _ = _probe_environment(
        tmp_path,
        mode,
    )
    sentinel = f"spareparts_test_issue147_contract_sentinel_{uuid.uuid4().hex[:8]}"
    maint = _maintenance_engine(base)
    process = None
    state = None
    sentinel_handle = None
    try:
        sentinel_handle = _create_exact_database(base, sentinel)
        process = _start_probe(env, mode)
        state = _wait_for_state(process, marker)
        name, raw_dir = _assert_generated_run_state(state, base, tmp_path / "raw-base")
        output = process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)[0]
        assert process.returncode == expected_code, output
        assert not _database_exists(base, name)
        assert not raw_dir.exists()
        assert _database_exists(base, sentinel)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        if state is not None:
            _cleanup_child_state_if_present(base, state)
        if sentinel_handle is not None:
            _cleanup_exact_database(sentinel_handle, sentinel)
        maint.dispose()


@pytest.mark.skipif(sys.platform != "linux", reason="SIGTERM contract is Linux-only")
def test_sigterm_cleans_the_current_run_resources(tmp_path):
    env, base, inherited_name, inherited_raw, marker, _ = _probe_environment(
        tmp_path,
        "sigterm",
    )
    process = _start_probe(env, "hold")
    state = None
    try:
        state = _wait_for_state(process, marker)
        name, raw_dir = _assert_generated_run_state(state, base, tmp_path / "raw-base")
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        assert process.returncode != 0
        assert not _database_exists(base, name)
        assert not raw_dir.exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        if state is not None:
            _cleanup_child_state_if_present(base, state)


def test_two_concurrent_sub_pytests_are_isolated_and_cleanup_independently(tmp_path):
    raw_before = _snapshot_worktree_raw()
    shared = uuid.uuid4().hex[:10]
    env_a, base, inherited_name, inherited_raw, marker_a, release_a = (
        _probe_environment(
            tmp_path,
            "A",
            shared=shared,
        )
    )
    env_b, _, _, _, marker_b, release_b = _probe_environment(
        tmp_path,
        "B",
        shared=shared,
    )
    process_a = _start_probe(env_a, "hold")
    process_b = _start_probe(env_b, "hold")
    state_a = state_b = None
    try:
        state_a = _wait_for_state(process_a, marker_a)
        state_b = _wait_for_state(process_b, marker_b)
        name_a, raw_a = _assert_generated_run_state(
            state_a, base, tmp_path / "raw-base"
        )
        name_b, raw_b = _assert_generated_run_state(
            state_b, base, tmp_path / "raw-base"
        )
        assert name_a != name_b
        assert raw_a != raw_b

        release_b.touch()
        _finish(process_b)
        assert not _database_exists(base, name_b)
        assert not raw_b.exists()
        assert _database_exists(base, name_a)
        assert raw_a.exists()
        check_a = create_engine(
            base.set(database=state_a["database_name"]), poolclass=NullPool
        )
        try:
            with check_a.connect() as conn:
                assert (
                    conn.execute(
                        text(
                            "SELECT value FROM pytest_run_isolation_probe WHERE run_id='A'"
                        )
                    ).scalar_one()
                    == "A"
                )
        finally:
            check_a.dispose()

        release_a.touch()
        _finish(process_a)
        assert not _database_exists(base, name_a)
        assert not raw_a.exists()
        assert _snapshot_worktree_raw() == raw_before
    finally:
        for process in (process_a, process_b):
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        for state in (state_a, state_b):
            if state is not None:
                _cleanup_child_state_if_present(base, state)


def test_cleanup_refuses_busy_database_without_killing_other_client(tmp_path):
    env, base, inherited_name, inherited_raw, marker, release = _probe_environment(
        tmp_path,
        "busy",
    )
    process = _start_probe(env, "hold")
    state = None
    blocker = blocker_conn = None
    try:
        state = _wait_for_state(process, marker)
        name, raw_dir = _assert_generated_run_state(state, base, tmp_path / "raw-base")
        blocker = create_engine(
            base.set(database=state["database_name"]), poolclass=NullPool
        )
        blocker_conn = blocker.connect()
        blocker_pid = blocker_conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        release.touch()
        output = process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)[0]
        assert process.returncode != 0, output
        assert _database_exists(base, name)
        assert not raw_dir.exists()
        assert (
            blocker_conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            == blocker_pid
        )
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        if blocker_conn is not None:
            blocker_conn.close()
        if blocker is not None:
            blocker.dispose()
        if state is not None:
            _cleanup_child_state_if_present(base, state)


def test_database_url_scheme_is_rejected_before_engine_creation():
    with pytest.raises(RuntimeError, match="postgresql\\+psycopg"):
        validate_database_base(
            "postgresql://spareparts:secret@127.0.0.1:5433/spareparts_test"
        )


def test_drop_race_is_bounded_and_does_not_kill_new_client():
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    blocker = blocker_conn = blocker_tx = None

    def connect_after_precheck():
        nonlocal blocker, blocker_conn, blocker_tx
        blocker = create_engine(handle.database_url, poolclass=NullPool)
        blocker_conn = blocker.connect()
        blocker_tx = blocker_conn.begin()
        blocker_conn.execute(text("SELECT 1"))

    try:
        with pytest.raises(RuntimeError) as raised:
            cleanup_database_run(handle, before_drop=connect_after_precheck)
        assert str(raised.value) == "pytest database cleanup failed safely"
        assert "secret" not in str(raised.value)
        assert _database_exists(base, handle.name)
        assert blocker_conn.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        if blocker_tx is not None:
            blocker_tx.rollback()
        if blocker_conn is not None:
            blocker_conn.close()
        if blocker is not None:
            blocker.dispose()
        cleanup_database_run(handle)


def test_raw_marker_is_exact_private_inode_and_symlink_safe(tmp_path):
    handle = create_raw_run((tmp_path / "secure-marker-root").resolve())
    canary_dir = tmp_path / "outside-canary"
    canary_dir.mkdir()
    canary_file = canary_dir / "keep"
    canary_file.write_text("keep", encoding="ascii")
    (handle.run_dir / "external-link").symlink_to(canary_dir, target_is_directory=True)
    try:
        marker_stat = handle.marker_path.lstat()
        assert stat.S_IMODE(marker_stat.st_mode) == 0o600
        assert (marker_stat.st_dev, marker_stat.st_ino) == (
            handle.marker_dev,
            handle.marker_ino,
        )

        original_marker = handle.marker_path.read_bytes()
        handle.marker_path.unlink()
        replacement = handle.run_dir / "replacement-marker"
        replacement.write_bytes(original_marker)
        handle.marker_path.symlink_to(replacement)
        with pytest.raises(RuntimeError, match="marker"):
            cleanup_raw_run(handle)
        handle.marker_path.unlink()
        replacement.rename(handle.marker_path)
        with pytest.raises(RuntimeError, match="marker"):
            cleanup_raw_run(handle)
    finally:
        if handle.marker_path.exists():
            handle.marker_path.unlink()
        fd = os.open(
            handle.marker_path,
            os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(fd, handle.marker.encode("ascii"))
            restored = os.fstat(fd)
        finally:
            os.close(fd)
        if hasattr(handle, "marker_dev"):
            handle = replace(
                handle,
                marker_dev=restored.st_dev,
                marker_ino=restored.st_ino,
            )
        cleanup_raw_run(handle)
    assert canary_file.read_text(encoding="ascii") == "keep"


def test_raw_creation_interrupt_rolls_back_from_first_post_mkdtemp_statement(tmp_path):
    root = (tmp_path / "interrupt-root").resolve()
    root.mkdir()

    def interrupt(_run_dir):
        raise KeyboardInterrupt("injected before marker")

    with pytest.raises(KeyboardInterrupt, match="before marker"):
        create_raw_run(root, before_marker=interrupt)
    assert list(root.iterdir()) == []

    canary = tmp_path / "rollback-canary"
    canary.mkdir()
    canary_file = canary / "keep"
    canary_file.write_text("keep", encoding="ascii")
    parked_root = tmp_path / "interrupt-root-parked"

    def swap_root_then_interrupt(_run_dir):
        root.rename(parked_root)
        root.symlink_to(canary, target_is_directory=True)
        raise KeyboardInterrupt("injected after root swap")

    try:
        with pytest.raises(KeyboardInterrupt, match="root swap"):
            create_raw_run(root, before_marker=swap_root_then_interrupt)
        assert list(parked_root.iterdir()) == []
        assert canary_file.read_text(encoding="ascii") == "keep"
    finally:
        root.unlink()
        parked_root.rename(root)


def test_run_lifecycle_has_terminal_failure_and_reentry_guards():
    assert hasattr(run_isolation, "RunLifecycle")
    lifecycle = run_isolation.RunLifecycle()
    lifecycle.state = run_isolation.LifecycleState.CLEANING
    with pytest.raises(RuntimeError, match="cleanup unavailable"):
        lifecycle.cleanup()


def test_run_lifecycle_dispose_failure_still_cleans_once_and_stays_failed():
    assert hasattr(run_isolation, "RunLifecycle")
    events = []

    attempts = 0

    def fail_dispose_once():
        nonlocal attempts
        attempts += 1
        events.append("dispose")
        if attempts == 1:
            raise RuntimeError("contains postgresql+psycopg://user:password@host/db")

    lifecycle = run_isolation.RunLifecycle(
        engine_dispose=fail_dispose_once,
        database_cleanup=lambda: events.append("database"),
        raw_cleanup=lambda: events.append("raw"),
    )
    with pytest.raises(RuntimeError) as raised:
        lifecycle.cleanup()
    assert str(raised.value) == "pytest run cleanup failed safely"
    assert events == ["dispose", "database", "raw"]
    assert lifecycle.cleanup() is True
    assert events == ["dispose", "database", "raw", "dispose"]
    assert lifecycle.state is run_isolation.LifecycleState.CLEANED
    assert lifecycle.cleanup() is False


def test_run_lifecycle_cleaned_is_idempotent_and_reentry_fails_closed():
    events = []
    clean = run_isolation.RunLifecycle(
        database_cleanup=lambda: events.append("database"),
        raw_cleanup=lambda: events.append("raw"),
    )
    assert clean.cleanup() is True
    assert clean.cleanup() is False
    assert events == ["database", "raw"]

    holder = {}

    def reenter():
        holder["lifecycle"].cleanup()

    reentrant = run_isolation.RunLifecycle(
        engine_dispose=reenter,
        database_cleanup=lambda: events.append("reentrant-database"),
        raw_cleanup=lambda: events.append("reentrant-raw"),
    )
    holder["lifecycle"] = reentrant
    with pytest.raises(RuntimeError, match="cleanup failed safely"):
        reentrant.cleanup()
    assert reentrant.state is run_isolation.LifecycleState.FAILED
    assert events[-2:] == ["reentrant-database", "reentrant-raw"]


def test_default_raw_base_ignores_inherited_raw_and_tmpdir(tmp_path):
    checkout_raw = BACKEND_DIR / "data" / "raw"
    before = _snapshot_worktree_raw()
    env, base, inherited_name, _, marker, _ = _probe_environment(tmp_path, "default")
    env.pop("PYTEST_RAW_FILE_BASE_DIR", None)
    env["RAW_FILE_DIR"] = str(checkout_raw)
    env["TMPDIR"] = str(checkout_raw)
    process = _start_probe(env)
    state = _wait_for_state(process, marker)
    _finish(process)
    assert Path(state["raw_file_dir"]).is_relative_to(
        Path("/tmp/it-spareparts-pytest-raw")
    )
    assert _snapshot_worktree_raw() == before
    assert not _database_exists(base, state["database_name"])


def test_explicit_checkout_raw_base_fails_before_engine_or_write(tmp_path):
    checkout_raw = BACKEND_DIR / "data" / "raw"
    before = _snapshot_worktree_raw()
    sitecustomize = tmp_path / "sitecustomize.py"
    event_file = tmp_path / "forbidden-event"
    sitecustomize.write_text(
        "import os, sqlalchemy, tempfile\n"
        "_engine = sqlalchemy.create_engine\n"
        "_mkdtemp = tempfile.mkdtemp\n"
        "def event():\n"
        "    open(os.environ['FORBIDDEN_EVENT'], 'w').close()\n"
        "def guarded_engine(*args, **kwargs):\n"
        "    event(); return _engine(*args, **kwargs)\n"
        "def guarded_mkdtemp(*args, **kwargs):\n"
        "    event(); return _mkdtemp(*args, **kwargs)\n"
        "sqlalchemy.create_engine = guarded_engine\n"
        "tempfile.mkdtemp = guarded_mkdtemp\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTEST_RAW_FILE_BASE_DIR": str(checkout_raw),
            "PYTHONPATH": os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]),
            "FORBIDDEN_EVENT": str(event_file),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode != 0
    assert not event_file.exists(), completed.stdout + completed.stderr
    assert _snapshot_worktree_raw() == before


def test_raw_base_rejects_tmp_itself_and_symlink_or_file_ancestors(tmp_path):
    with pytest.raises(RuntimeError, match="safe /tmp child"):
        validate_raw_base("/tmp")
    with pytest.raises(RuntimeError, match="safe /tmp child"):
        validate_raw_base("relative/raw")

    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "linked"
    symlink.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe ancestor"):
        validate_raw_base(symlink / "child")

    file_ancestor = tmp_path / "file"
    file_ancestor.write_text("not a directory", encoding="ascii")
    with pytest.raises(RuntimeError, match="unsafe ancestor"):
        validate_raw_base(file_ancestor / "child")


def test_pytest_worker_environment_is_rejected_before_resources(tmp_path):
    raw_base = tmp_path / "worker-raw"
    env = os.environ.copy()
    env.update(
        {
            "PYTEST_XDIST_WORKER": "gw0",
            "PYTEST_RAW_FILE_BASE_DIR": str(raw_base),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode != 0
    assert "xdist" in (completed.stdout + completed.stderr).lower()
    assert not raw_base.exists()


@pytest.mark.parametrize(
    "xdist_args",
    [
        ["-n", "2"],
        ["-n2"],
        ["--numprocesses", "2"],
        ["--numprocesses=2"],
    ],
)
def test_xdist_cli_is_explicitly_rejected_before_resources(tmp_path, xdist_args):
    raw_base = tmp_path / "xdist-cli-raw"
    env = os.environ.copy()
    env["PYTEST_RAW_FILE_BASE_DIR"] = str(raw_base)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *xdist_args, "--collect-only", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "pytest-xdist is not supported" in combined
    assert not raw_base.exists()


def test_probe_state_contains_no_url_or_secret_and_is_consumed(tmp_path):
    env, base, _, _, marker, _ = _probe_environment(tmp_path, "secret")
    process = _start_probe(env)
    state = _wait_for_state(process, marker)
    output = _finish(process)
    assert set(state) == {"database_name", "pid", "raw_file_dir"}
    assert not marker.exists()
    serialized = json.dumps(state)
    base_url = env["PYTEST_DATABASE_BASE_URL"]
    password = make_url(base_url).password or ""
    secrets = [base_url]
    if password:
        secrets.append(f":{password}@")
    for secret in secrets:
        assert secret not in serialized
        assert secret not in output
    assert not _database_exists(base, state["database_name"])


def test_collection_failure_is_nonzero_and_cleans_created_resources(tmp_path):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    raw_base = tmp_path / "collection-fault-raw"
    env = os.environ.copy()
    env.update(
        {
            "PYTEST_DATABASE_BASE_URL": _render(base),
            "PYTEST_RAW_FILE_BASE_DIR": str(raw_base),
            "ISOLATION_PROBE_COLLECTION_FAIL": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = process.communicate(timeout=SUBPROCESS_TIMEOUT_SECONDS)[0]
    assert process.returncode != 0, output
    assert not _databases_with_prefix(base, f"spareparts_test_{process.pid}_")
    assert raw_base.exists()
    assert list(raw_base.iterdir()) == []


def test_raw_run_creation_is_fd_relative_after_root_path_replacement(tmp_path):
    root = (tmp_path / "fd-root").resolve()
    root.mkdir()
    parked = tmp_path / "fd-root-original"
    canary = "replacement-canary"

    def replace_root_before_run():
        root.rename(parked)
        root.mkdir()
        (root / canary).write_text("keep", encoding="ascii")

    handle = None
    try:
        handle = create_raw_run(root, before_run_dir=replace_root_before_run)
        assert handle.run_dir.parent == parked
        assert (root / canary).read_text(encoding="ascii") == "keep"
        cleanup_raw_run(handle)
        handle = None
        assert list(parked.iterdir()) == []
    finally:
        if handle is not None:
            cleanup_raw_run(handle)
        if root.exists():
            if (root / canary).exists():
                assert (root / canary).read_text(encoding="ascii") == "keep"
                (root / canary).unlink()
            if root != parked:
                root.rmdir()
        if parked.exists():
            parked.rename(root)


def test_raw_run_name_collision_retries_on_the_same_root_fd(tmp_path):
    root = (tmp_path / "collision-root").resolve()
    root.mkdir()
    collision = f"pytest-{os.getpid()}-collision"
    (root / collision).mkdir()
    tokens = iter(["collision", "fresh"])
    handle = create_raw_run(root, run_token_factory=lambda: next(tokens))
    try:
        assert handle.run_dir.name == f"pytest-{os.getpid()}-fresh"
        assert (root / collision).is_dir()
    finally:
        cleanup_raw_run(handle)
        (root / collision).rmdir()


def test_raw_cleanup_does_not_recreate_a_missing_root(tmp_path):
    root = (tmp_path / "missing-cleanup-root").resolve()
    handle = create_raw_run(root)
    parked = tmp_path / "missing-cleanup-root-parked"
    root.rename(parked)
    try:
        with pytest.raises(RuntimeError, match="root identity"):
            cleanup_raw_run(handle)
        assert not root.exists()
    finally:
        parked.rename(root)
        cleanup_raw_run(handle)


def test_raw_marker_write_retries_partial_os_writes(monkeypatch, tmp_path):
    root = (tmp_path / "partial-write-root").resolve()
    original_write = run_isolation.os.write

    def partial_write(fd, data):
        return original_write(fd, data[:7])

    monkeypatch.setattr(run_isolation.os, "write", partial_write)
    handle = create_raw_run(root)
    try:
        assert handle.marker_path.read_bytes() == handle.marker.encode("ascii")
    finally:
        cleanup_raw_run(handle)


def test_ambiguous_create_error_captures_and_cleans_exact_candidate():
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"ambiguous{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"

    def create_then_lose_response(conn, candidate):
        conn.execute(text(f'CREATE DATABASE "{candidate}"'))
        raise DBAPIError("CREATE DATABASE", None, RuntimeError("connection lost"))

    with pytest.raises(RuntimeError) as raised:
        create_database_run(
            _render(base),
            token_factory=lambda: token,
            execute_create=create_then_lose_response,
            max_attempts=1,
        )
    assert str(raised.value) == "pytest database creation failed safely"
    assert not _database_exists(base, name)


def test_expected_name_is_mandatory_even_for_generated_database():
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    try:
        with pytest.raises(RuntimeError, match="name mismatch"):
            cleanup_database_run(handle, expected_name=f"{handle.name}_wrong")
        assert _database_exists(base, handle.name)
    finally:
        cleanup_database_run(handle)


@pytest.mark.parametrize("unsafe_name", ["postgres", "spareparts_dev"])
def test_capture_and_cleanup_reject_destructive_name_before_engine(
    monkeypatch,
    unsafe_name,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        lambda *_args, **_kwargs: pytest.fail("engine must not be constructed"),
    )
    with pytest.raises(RuntimeError, match="allowlist"):
        capture_database_run(_render(base), unsafe_name, expected_name=unsafe_name)

    handle = DatabaseRun(
        base_url=_render(base),
        database_url=_render(base.set(database=unsafe_name)),
        name=unsafe_name,
        database_oid=1,
        owner_oid=1,
        owner_role="unsafe",
    )
    with pytest.raises(RuntimeError, match="allowlist"):
        cleanup_database_run(handle, expected_name=unsafe_name)


def test_database_owned_callback_exception_cleans_before_propagating():
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"callback{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    published = []

    def reject_handoff(handle):
        published.append(handle)
        raise RuntimeError("callback contains secret-url")

    try:
        with pytest.raises(RuntimeError) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                on_owned=reject_handoff,
                max_attempts=1,
            )
        assert str(raised.value) == "pytest database ownership handoff failed safely"
        assert len(published) == 1
        assert not _database_exists(base, name)
    finally:
        if published and _database_exists(base, name):
            cleanup_database_run(published[0])


def test_raw_owned_callback_exception_rolls_back_exact_directory(tmp_path):
    root = (tmp_path / "raw-callback-root").resolve()
    published = []

    def reject_handoff(handle):
        published.append(handle)
        raise RuntimeError("callback contains /secret/path")

    with pytest.raises(RuntimeError) as raised:
        create_raw_run(root, on_owned=reject_handoff)
    assert str(raised.value) == "pytest RAW ownership handoff failed safely"
    assert len(published) == 1
    assert list(root.iterdir()) == []


def test_pending_signal_is_delivered_only_after_database_handoff(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    published = []
    original = run_isolation.signal.pthread_sigmask
    armed = True

    def deliver_after_restore(how, mask):
        nonlocal armed
        result = original(how, mask)
        if how == signal.SIG_SETMASK and armed:
            armed = False
            raise KeyboardInterrupt("pending database signal")
        return result

    monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", deliver_after_restore)
    with pytest.raises(KeyboardInterrupt, match="pending database signal"):
        create_database_run(_render(base), on_owned=published.append)
    assert len(published) == 1
    cleanup_database_run(published[0])


def test_pending_signal_is_delivered_only_after_raw_handoff(monkeypatch, tmp_path):
    root = (tmp_path / "pending-raw-root").resolve()
    published = []
    original = run_isolation.signal.pthread_sigmask
    armed = True

    def deliver_after_restore(how, mask):
        nonlocal armed
        result = original(how, mask)
        if how == signal.SIG_SETMASK and armed:
            armed = False
            raise KeyboardInterrupt("pending RAW signal")
        return result

    monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", deliver_after_restore)
    with pytest.raises(KeyboardInterrupt, match="pending RAW signal"):
        create_raw_run(root, on_owned=published.append)
    assert len(published) == 1
    cleanup_raw_run(published[0])


def test_worktree_path_outside_backend_is_rejected_before_engine_or_write(tmp_path):
    worktree_path = BACKEND_DIR.parent / "frontend"
    before = set(worktree_path.rglob("*"))
    sitecustomize = tmp_path / "sitecustomize.py"
    event_file = tmp_path / "worktree-forbidden-event"
    sitecustomize.write_text(
        "import os, sqlalchemy\n"
        "_engine = sqlalchemy.create_engine\n"
        "def guarded_engine(*args, **kwargs):\n"
        "    open(os.environ['FORBIDDEN_EVENT'], 'w').close()\n"
        "    return _engine(*args, **kwargs)\n"
        "sqlalchemy.create_engine = guarded_engine\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTEST_RAW_FILE_BASE_DIR": str(worktree_path),
            "PYTHONPATH": os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]),
            "FORBIDDEN_EVENT": str(event_file),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode != 0
    assert not event_file.exists(), completed.stdout + completed.stderr
    assert set(worktree_path.rglob("*")) == before


def test_raw_cleanup_quarantine_rejects_same_name_replacement(tmp_path):
    root = (tmp_path / "cleanup-quarantine-root").resolve()
    handle = create_raw_run(root)
    parked = root / "owned-parked"
    canary = handle.run_dir / "canary"

    def replace_at_delete_boundary():
        handle.run_dir.rename(parked)
        handle.run_dir.mkdir()
        canary.write_text("keep", encoding="ascii")

    try:
        with pytest.raises(RuntimeError, match="cleanup failed safely"):
            cleanup_raw_run(handle, before_quarantine=replace_at_delete_boundary)
        assert canary.read_text(encoding="ascii") == "keep"
        assert parked.stat().st_ino == handle.run_ino
    finally:
        if parked.exists():
            if canary.exists():
                canary.unlink()
            if handle.run_dir.exists():
                handle.run_dir.rmdir()
            parked_handle = replace(
                handle,
                run_dir=parked,
                marker_path=parked / handle.marker_path.name,
            )
            cleanup_raw_run(parked_handle)
        elif handle.run_dir.exists():
            cleanup_raw_run(handle)


def test_raw_creation_rollback_quarantine_rejects_replacement(tmp_path):
    root = (tmp_path / "rollback-quarantine-root").resolve()
    owned_path = None
    parked = root / "owned-parked"
    canary = None

    def fail_before_marker(run_dir):
        nonlocal owned_path
        owned_path = run_dir
        raise RuntimeError("injected marker failure")

    def replace_at_rollback_boundary():
        nonlocal canary
        owned_path.rename(parked)
        owned_path.mkdir()
        canary = owned_path / "canary"
        canary.write_text("keep", encoding="ascii")

    try:
        with pytest.raises(RuntimeError, match="rollback failed safely"):
            create_raw_run(
                root,
                before_marker=fail_before_marker,
                before_rollback_quarantine=replace_at_rollback_boundary,
            )
        assert canary.read_text(encoding="ascii") == "keep"
        assert parked.is_dir()
    finally:
        if canary is not None and canary.exists():
            canary.unlink()
        if owned_path is not None and owned_path.exists():
            owned_path.rmdir()
        if parked.exists():
            parked.rmdir()


def test_database_cleanup_rechecks_oid_after_before_drop_swap():
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    replacement_oid = None

    def replace_database_at_drop_boundary():
        nonlocal replacement_oid
        maint = _maintenance_engine(base)
        try:
            with maint.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{handle.name}"'))
                conn.execute(text(f'CREATE DATABASE "{handle.name}"'))
                replacement_oid = conn.execute(
                    text("SELECT oid FROM pg_database WHERE datname=:name"),
                    {"name": handle.name},
                ).scalar_one()
        finally:
            maint.dispose()

    try:
        with pytest.raises(RuntimeError, match="identity mismatch"):
            cleanup_database_run(handle, before_drop=replace_database_at_drop_boundary)
        assert replacement_oid != handle.database_oid
        assert _database_exists(base, handle.name)
        replacement_handle = capture_database_run(
            _render(base),
            handle.name,
            expected_name=handle.name,
        )
        assert replacement_handle.database_oid == replacement_oid
    finally:
        if _database_exists(base, handle.name):
            replacement_handle = capture_database_run(
                _render(base),
                handle.name,
                expected_name=handle.name,
            )
            cleanup_database_run(replacement_handle)


def test_database_cleanup_sets_bounded_statement_timeouts(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    statements = []
    original = run_isolation._maintenance_engine

    def observing_engine(url):
        engine = original(url)
        from sqlalchemy import event

        event.listen(
            engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _params, _context, _many: (
                statements.append(statement)
            ),
        )
        return engine

    monkeypatch.setattr(run_isolation, "_maintenance_engine", observing_engine)
    cleanup_database_run(handle)
    assert run_isolation.MAINTENANCE_LOCK_TIMEOUT_MS == 1000
    assert run_isolation.MAINTENANCE_STATEMENT_TIMEOUT_MS == 120000
    assert run_isolation.MAINTENANCE_DROP_STATEMENT_TIMEOUT_MS == 150000
    lock_timeout = (
        f"SET lock_timeout = '{run_isolation.MAINTENANCE_LOCK_TIMEOUT_MS}ms'"
    )
    statement_timeout = (
        "SET statement_timeout = "
        f"'{run_isolation.MAINTENANCE_STATEMENT_TIMEOUT_MS}ms'"
    )
    drop_statement_timeout = (
        "SET statement_timeout = "
        f"'{run_isolation.MAINTENANCE_DROP_STATEMENT_TIMEOUT_MS}ms'"
    )
    lock_index = next(
        index
        for index, statement in enumerate(statements)
        if "pg_advisory_lock" in statement
    )
    identity_indices = [
        index
        for index, statement in enumerate(statements)
        if "FROM pg_database d JOIN pg_roles" in statement
    ]
    drop_timeout_index = statements.index(drop_statement_timeout)
    drop_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("DROP DATABASE")
    )
    assert statements.index(lock_timeout) < lock_index
    assert statements.index(statement_timeout) < lock_index
    assert len(identity_indices) == 2
    assert identity_indices[-1] < drop_timeout_index < drop_index


def test_database_cleanup_redacts_connect_and_before_drop_dbapi_errors(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    secret = "postgresql+psycopg://user:secret-password@host/secret-db"
    original = run_isolation._maintenance_engine

    class SecretEngine:
        def connect(self):
            raise DBAPIError(secret, None, RuntimeError(secret))

        def dispose(self):
            pass

    try:
        monkeypatch.setattr(
            run_isolation, "_maintenance_engine", lambda _base: SecretEngine()
        )
        with pytest.raises(RuntimeError) as raised:
            cleanup_database_run(handle)
        assert str(raised.value) == "pytest database cleanup failed safely"
        assert secret not in str(raised.value)

        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        with pytest.raises(RuntimeError) as raised:
            cleanup_database_run(
                handle,
                before_drop=lambda: (_ for _ in ()).throw(
                    DBAPIError(secret, None, RuntimeError(secret))
                ),
            )
        assert str(raised.value) == "pytest database cleanup failed safely"
        assert secret not in str(raised.value)
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        cleanup_database_run(handle)


def test_ambiguous_create_recovery_connect_error_is_redacted(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    secret = "secret-password secret-db"
    original = run_isolation._maintenance_engine
    calls = 0

    class SecretEngine:
        def connect(self):
            raise DBAPIError(secret, None, RuntimeError(secret))

        def dispose(self):
            pass

    def engine_sequence(url):
        nonlocal calls
        calls += 1
        return original(url) if calls == 1 else SecretEngine()

    monkeypatch.setattr(run_isolation, "_maintenance_engine", engine_sequence)
    with pytest.raises(RuntimeError) as raised:
        create_database_run(
            _render(base),
            execute_create=lambda _conn, _name: (_ for _ in ()).throw(
                DBAPIError(secret, None, RuntimeError(secret))
            ),
            max_attempts=1,
        )
    assert str(raised.value) == "pytest database creation failed safely"
    assert secret not in str(raised.value)


def test_database_cleanup_does_not_swallow_keyboard_interrupt(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    original = run_isolation._maintenance_engine

    class InterruptEngine:
        def connect(self):
            raise KeyboardInterrupt("stop now")

        def dispose(self):
            pass

    try:
        monkeypatch.setattr(
            run_isolation, "_maintenance_engine", lambda _base: InterruptEngine()
        )
        with pytest.raises(KeyboardInterrupt, match="stop now"):
            cleanup_database_run(handle)
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        cleanup_database_run(handle)


def test_run_lifecycle_retries_only_database_failure():
    events = []
    db_attempts = 0

    def fail_database_once():
        nonlocal db_attempts
        db_attempts += 1
        events.append("database")
        if db_attempts == 1:
            raise RuntimeError("first database cleanup failed")

    lifecycle = run_isolation.RunLifecycle(
        engine_dispose=lambda: events.append("dispose"),
        database_cleanup=fail_database_once,
        raw_cleanup=lambda: events.append("raw"),
    )
    with pytest.raises(RuntimeError, match="cleanup failed safely"):
        lifecycle.cleanup()
    assert events == ["dispose", "database", "raw"]
    assert lifecycle.cleanup() is True
    assert events == ["dispose", "database", "raw", "database"]
    assert lifecycle.state is run_isolation.LifecycleState.CLEANED


def test_platform_capabilities_fail_before_resources(monkeypatch):
    monkeypatch.delattr(run_isolation.signal, "pthread_sigmask")
    with pytest.raises(RuntimeError, match="Linux isolation capabilities required"):
        run_isolation.validate_platform_capabilities()


def test_raw_quarantine_parent_collision_never_overwrites_nonowned_canary(tmp_path):
    root = (tmp_path / "quarantine-parent-collision-root").resolve()
    handle = create_raw_run(root)
    tokens = iter(["collision", "fresh"])
    collision_parent = root / f".pytest-quarantine-{os.getpid()}-collision"
    canary = collision_parent / "keep"
    attacked = False

    def create_nonowned_collision(parent_name):
        nonlocal attacked
        if attacked:
            return
        attacked = True
        assert parent_name == collision_parent.name
        collision_parent.mkdir(mode=0o700)
        canary.write_text("keep", encoding="ascii")

    try:
        cleanup_raw_run(
            handle,
            quarantine_token_factory=lambda: next(tokens),
            before_quarantine_parent_create=create_nonowned_collision,
        )
        assert canary.read_text(encoding="ascii") == "keep"
        assert not handle.run_dir.exists()
    finally:
        if handle.run_dir.exists():
            cleanup_raw_run(handle)
        if canary.exists():
            canary.unlink()
        if collision_parent.exists():
            collision_parent.rmdir()


def test_conftest_checks_platform_before_hooks_validation_and_resources():
    source = (BACKEND_DIR / "tests" / "conftest.py").read_text(encoding="utf-8")
    capability = source.index("validate_platform_capabilities()")
    assert capability < source.index("atexit.register")
    assert capability < source.index("signal.signal")
    assert capability < source.index("validate_pytest_invocation(sys.argv")
    assert capability < source.index("_raw_run = create_raw_run(")
    assert capability < source.index("_database_run = create_database_run(")


def test_missing_platform_capability_fails_without_resource_side_effects(tmp_path):
    raw_base = tmp_path / "capability-raw"
    engine_marker = tmp_path / "engine-created"
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import os, signal, sqlalchemy\n"
        "del signal.pthread_sigmask\n"
        "_engine = sqlalchemy.create_engine\n"
        "def guarded_engine(*args, **kwargs):\n"
        "    open(os.environ['ENGINE_MARKER'], 'w').close()\n"
        "    return _engine(*args, **kwargs)\n"
        "sqlalchemy.create_engine = guarded_engine\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTEST_RAW_FILE_BASE_DIR": str(raw_base),
            "PYTHONPATH": os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]),
            "ENGINE_MARKER": str(engine_marker),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(PROBE)],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert completed.returncode != 0
    assert (
        "Linux isolation capabilities required" in completed.stdout + completed.stderr
    )
    assert not raw_base.exists()
    assert not engine_marker.exists()


def test_database_cleanup_reports_dispose_failure_after_success(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    handle = create_database_run(_render(base))
    original = run_isolation._maintenance_engine
    secret = "dispose-secret-password"

    class DisposeFailEngine:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def connect(self):
            return self.wrapped.connect()

        def dispose(self):
            self.wrapped.dispose()
            raise DBAPIError(secret, None, RuntimeError(secret))

    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        lambda url: DisposeFailEngine(original(url)),
    )
    with pytest.raises(RuntimeError) as raised:
        cleanup_database_run(handle)
    assert str(raised.value) == "pytest database cleanup failed safely"
    assert secret not in str(raised.value)
    assert not _database_exists(base, handle.name)


@pytest.mark.parametrize("entrypoint", ["create", "capture", "cleanup"])
def test_database_entrypoints_set_timeouts_before_advisory_lock(
    monkeypatch, entrypoint
):
    from sqlalchemy import event

    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    original = run_isolation._maintenance_engine
    statements = []
    existing = create_database_run(_render(base)) if entrypoint != "create" else None

    def observing_engine(url):
        engine = original(url)
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _params, _context, _many: (
                statements.append(statement)
            ),
        )
        return engine

    monkeypatch.setattr(run_isolation, "_maintenance_engine", observing_engine)
    created = None
    try:
        if entrypoint == "create":
            created = create_database_run(_render(base))
        elif entrypoint == "capture":
            capture_database_run(
                _render(base),
                existing.name,
                expected_name=existing.name,
            )
        else:
            cleanup_database_run(existing)
            existing = None
        lock_index = next(
            index
            for index, statement in enumerate(statements)
            if "pg_advisory_lock" in statement
        )
        assert run_isolation.MAINTENANCE_LOCK_TIMEOUT_MS == 1000
        assert run_isolation.MAINTENANCE_STATEMENT_TIMEOUT_MS == 120000
        lock_timeout = (
            f"SET lock_timeout = '{run_isolation.MAINTENANCE_LOCK_TIMEOUT_MS}ms'"
        )
        statement_timeout = (
            "SET statement_timeout = "
            f"'{run_isolation.MAINTENANCE_STATEMENT_TIMEOUT_MS}ms'"
        )
        assert statements.index(lock_timeout) < lock_index
        assert statements.index(statement_timeout) < lock_index
        if entrypoint == "create":
            reset_index = statements.index("SET lock_timeout = '0'")
            create_index = next(
                index
                for index, statement in enumerate(statements)
                if statement.startswith("CREATE DATABASE")
            )
            assert lock_index < reset_index < create_index
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        if created is not None and _database_exists(base, created.name):
            cleanup_database_run(created)
        if existing is not None and _database_exists(base, existing.name):
            cleanup_database_run(existing)


@pytest.mark.parametrize(
    ("entrypoint", "expected"),
    [
        ("create", "pytest database creation failed safely"),
        ("capture", "pytest database capture failed safely"),
        ("cleanup", "pytest database cleanup failed safely"),
    ],
)
def test_database_lock_errors_are_bounded_and_redacted(
    monkeypatch,
    entrypoint,
    expected,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    existing = create_database_run(_render(base)) if entrypoint != "create" else None
    secret = "advisory-lock-secret-password"
    monkeypatch.setattr(
        run_isolation,
        "_acquire_database_lock",
        lambda _conn, _name: (_ for _ in ()).throw(
            DBAPIError(secret, None, RuntimeError(secret))
        ),
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            if entrypoint == "create":
                create_database_run(_render(base), max_attempts=1)
            elif entrypoint == "capture":
                capture_database_run(
                    _render(base),
                    existing.name,
                    expected_name=existing.name,
                )
            else:
                cleanup_database_run(existing)
        assert str(raised.value) == expected
        assert secret not in str(raised.value)
    finally:
        monkeypatch.undo()
        if existing is not None and _database_exists(base, existing.name):
            cleanup_database_run(existing)


def test_create_release_lock_error_is_redacted_and_cleans_candidate(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"releasefailure{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    secret = "release-lock-secret-password"
    original = run_isolation._release_database_lock
    calls = 0

    def fail_first_release(conn, candidate):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DBAPIError(secret, None, RuntimeError(secret))
        return original(conn, candidate)

    monkeypatch.setattr(run_isolation, "_release_database_lock", fail_first_release)
    try:
        with pytest.raises(RuntimeError) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                max_attempts=1,
            )
        assert str(raised.value) == "pytest database creation failed safely"
        assert secret not in str(raised.value)
        assert not _database_exists(base, name)
    finally:
        monkeypatch.setattr(run_isolation, "_release_database_lock", original)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


@pytest.mark.parametrize("interrupt", [False, True])
def test_create_outer_dispose_failure_cleans_unpublished_database(
    monkeypatch,
    interrupt,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"createdispose{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original = run_isolation._maintenance_engine
    failed = False

    class DisposeFailOnceEngine:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def connect(self):
            return self.wrapped.connect()

        def dispose(self):
            nonlocal failed
            self.wrapped.dispose()
            if not failed:
                failed = True
                if interrupt:
                    raise KeyboardInterrupt("create dispose interrupt")
                raise DBAPIError(
                    "create-dispose-secret",
                    None,
                    RuntimeError("create-dispose-secret"),
                )

    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        lambda url: DisposeFailOnceEngine(original(url)),
    )
    try:
        expected = KeyboardInterrupt if interrupt else RuntimeError
        with pytest.raises(expected) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                max_attempts=1,
            )
        if interrupt:
            assert str(raised.value) == "create dispose interrupt"
        else:
            assert str(raised.value) == "pytest database creation failed safely"
        assert not _database_exists(base, name)
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


def test_create_dispose_failure_does_not_cleanup_published_database(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    original = run_isolation._maintenance_engine
    published = []
    failed = False

    class DisposeFailOnceEngine:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def connect(self):
            return self.wrapped.connect()

        def dispose(self):
            nonlocal failed
            self.wrapped.dispose()
            if not failed:
                failed = True
                raise DBAPIError(
                    "published-dispose-secret",
                    None,
                    RuntimeError("published-dispose-secret"),
                )

    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        lambda url: DisposeFailOnceEngine(original(url)),
    )
    try:
        with pytest.raises(RuntimeError, match="creation failed safely"):
            create_database_run(_render(base), on_owned=published.append)
        assert len(published) == 1
        assert _database_exists(base, published[0].name)
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        if published and _database_exists(base, published[0].name):
            cleanup_database_run(published[0])


def test_raw_quarantine_callback_error_leaks_no_parent_or_fd(tmp_path):
    root = (tmp_path / "quarantine-callback-root").resolve()
    handle = create_raw_run(root)
    token = "callbackfailure"
    parent = root / f".pytest-quarantine-{os.getpid()}-{token}"
    fd_count = len(list(Path("/proc/self/fd").iterdir()))
    secret = "quarantine-callback-secret"

    try:
        with pytest.raises(RuntimeError) as raised:
            cleanup_raw_run(
                handle,
                quarantine_token_factory=lambda: token,
                before_quarantine=lambda: (_ for _ in ()).throw(RuntimeError(secret)),
            )
        assert str(raised.value) == "pytest RAW cleanup failed safely"
        assert secret not in str(raised.value)
        assert handle.run_dir.stat().st_ino == handle.run_ino
        assert not parent.exists()
        assert len(list(Path("/proc/self/fd").iterdir())) == fd_count
    finally:
        if parent.exists():
            parent.rmdir()
        cleanup_raw_run(handle)


def test_raw_quarantine_unlink_failure_restores_owned_name_for_retry(
    monkeypatch,
    tmp_path,
):
    root = (tmp_path / "quarantine-delete-root").resolve()
    handle = create_raw_run(root)
    token = "deletefailure"
    parent = root / f".pytest-quarantine-{os.getpid()}-{token}"
    original_unlink = run_isolation.os.unlink
    failed = False

    def fail_unlink_once(path, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("quarantine-delete-secret")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(run_isolation.os, "unlink", fail_unlink_once)
    with pytest.raises(RuntimeError) as raised:
        cleanup_raw_run(
            handle,
            quarantine_token_factory=lambda: token,
        )
    assert str(raised.value) == "pytest RAW cleanup failed safely"
    assert "quarantine-delete-secret" not in str(raised.value)
    assert handle.run_dir.stat().st_ino == handle.run_ino
    assert handle.marker_path.read_text(encoding="ascii") == handle.marker
    assert not parent.exists()
    monkeypatch.setattr(run_isolation.os, "unlink", original_unlink)
    cleanup_raw_run(handle)


def test_ambiguous_create_recovers_when_old_connection_unlock_stays_broken(
    monkeypatch,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"brokenunlock{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original_release = run_isolation._release_database_lock
    original_mask = run_isolation.signal.pthread_sigmask
    starting_mask = original_mask(signal.SIG_BLOCK, set())
    old_connection = None
    mask_calls = []

    def create_then_lose_response(conn, candidate):
        nonlocal old_connection
        old_connection = conn
        conn.execute(text(f'CREATE DATABASE "{candidate}"'))
        raise DBAPIError(
            "ambiguous-create-secret",
            None,
            RuntimeError("ambiguous-create-secret"),
        )

    def fail_old_connection_unlock(conn, candidate):
        if conn is old_connection:
            raise DBAPIError(
                "broken-unlock-secret",
                None,
                RuntimeError("broken-unlock-secret"),
            )
        return original_release(conn, candidate)

    def track_mask(how, mask):
        mask_calls.append(how)
        return original_mask(how, mask)

    monkeypatch.setattr(
        run_isolation,
        "_release_database_lock",
        fail_old_connection_unlock,
    )
    monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", track_mask)
    try:
        with pytest.raises(RuntimeError) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                execute_create=create_then_lose_response,
                max_attempts=1,
            )
        assert str(raised.value) == "pytest database creation failed safely"
        assert "secret" not in str(raised.value)
        assert not _database_exists(base, name)
        assert mask_calls[0] == signal.SIG_BLOCK
        assert mask_calls[-1] == signal.SIG_SETMASK
    finally:
        monkeypatch.setattr(run_isolation, "_release_database_lock", original_release)
        monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", original_mask)
        original_mask(signal.SIG_SETMASK, starting_mask)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


class _InjectedCreateBaseException(BaseException):
    pass


class _InjectedCreateException(Exception):
    pass


def _exit_failure_engine_factory(original, injected):
    failed = False

    class ExitFailureConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal failed
            result = self.wrapped.__exit__(exc_type, exc_value, traceback)
            if not failed:
                failed = True
                raise injected
            return result

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    class ExitFailureEngine:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def connect(self):
            return ExitFailureConnection(self.wrapped.connect())

        def dispose(self):
            return self.wrapped.dispose()

    return lambda url: ExitFailureEngine(original(url))


@pytest.mark.parametrize(
    ("injected", "expected_type", "expected_message"),
    [
        (
            KeyboardInterrupt("context exit keyboard interrupt"),
            KeyboardInterrupt,
            "context exit keyboard interrupt",
        ),
        (
            SystemExit("context exit system exit"),
            SystemExit,
            "context exit system exit",
        ),
        (
            _InjectedCreateException("context-exit-secret"),
            RuntimeError,
            "pytest database creation failed safely",
        ),
        (
            _InjectedCreateBaseException("context exit base exception"),
            _InjectedCreateBaseException,
            "context exit base exception",
        ),
    ],
    ids=["keyboard-interrupt", "system-exit", "exception", "base-exception"],
)
def test_connection_context_exit_failure_cleans_unpublished_database(
    monkeypatch,
    injected,
    expected_type,
    expected_message,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"exitfailure{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original_engine = run_isolation._maintenance_engine
    original_mask = run_isolation.signal.pthread_sigmask
    starting_mask = original_mask(signal.SIG_BLOCK, set())
    mask_calls = []

    def track_mask(how, mask):
        mask_calls.append(how)
        return original_mask(how, mask)

    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        _exit_failure_engine_factory(original_engine, injected),
    )
    monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", track_mask)
    try:
        with pytest.raises(expected_type) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                max_attempts=1,
            )
        assert str(raised.value) == expected_message
        if isinstance(injected, Exception) and not isinstance(
            injected, (KeyboardInterrupt, SystemExit)
        ):
            assert "context-exit-secret" not in str(raised.value)
        assert not _database_exists(base, name)
        assert mask_calls[0] == signal.SIG_BLOCK
        assert mask_calls[-1] == signal.SIG_SETMASK
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original_engine)
        monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", original_mask)
        original_mask(signal.SIG_SETMASK, starting_mask)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


def test_connection_context_exit_failure_leaves_published_database_to_lifecycle(
    monkeypatch,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    original = run_isolation._maintenance_engine
    injected = _InjectedCreateException("published-context-exit-secret")
    published = []

    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        _exit_failure_engine_factory(original, injected),
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            create_database_run(_render(base), on_owned=published.append)
        assert str(raised.value) == "pytest database creation failed safely"
        assert "published-context-exit-secret" not in str(raised.value)
        assert len(published) == 1
        assert _database_exists(base, published[0].name)
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        if published and _database_exists(base, published[0].name):
            cleanup_database_run(published[0])


def test_ambiguous_create_base_exception_recovers_before_signal_restore(
    monkeypatch,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"createbase{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original_mask = run_isolation.signal.pthread_sigmask
    starting_mask = original_mask(signal.SIG_BLOCK, set())
    mask_calls = []
    injected = _InjectedCreateBaseException("ambiguous base exception")

    def create_then_interrupt(conn, candidate):
        conn.execute(text(f'CREATE DATABASE "{candidate}"'))
        raise injected

    def track_mask(how, mask):
        mask_calls.append(how)
        return original_mask(how, mask)

    monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", track_mask)
    try:
        with pytest.raises(_InjectedCreateBaseException, match=str(injected)):
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                execute_create=create_then_interrupt,
                max_attempts=1,
            )
        assert not _database_exists(base, name)
        assert mask_calls[0] == signal.SIG_BLOCK
        assert mask_calls[-1] == signal.SIG_SETMASK
    finally:
        monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", original_mask)
        original_mask(signal.SIG_SETMASK, starting_mask)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


def test_create_dispose_base_exception_cleanup_secondary_failure_is_safe(monkeypatch):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"disposebase{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original = run_isolation._maintenance_engine
    injected = _InjectedCreateBaseException("dispose-base-secret")

    class DisposeBaseExceptionEngine:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def connect(self):
            return self.wrapped.connect()

        def dispose(self):
            self.wrapped.dispose()
            raise injected

    monkeypatch.setattr(
        run_isolation,
        "_maintenance_engine",
        lambda url: DisposeBaseExceptionEngine(original(url)),
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                max_attempts=1,
            )
        assert str(raised.value) == "pytest database creation failed safely"
        assert "dispose-base-secret" not in str(raised.value)
        assert not _database_exists(base, name)
    finally:
        monkeypatch.setattr(run_isolation, "_maintenance_engine", original)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


@pytest.mark.parametrize(
    "injected",
    [
        KeyboardInterrupt("unlock keyboard interrupt"),
        SystemExit("unlock system exit"),
        _InjectedCreateBaseException("unlock base exception"),
    ],
    ids=["keyboard-interrupt", "system-exit", "base-exception"],
)
def test_identity_known_unlock_base_exception_cleans_before_publication(
    monkeypatch,
    injected,
):
    base = make_url(os.environ["PYTEST_DATABASE_BASE_URL"])
    token = f"unlockbase{uuid.uuid4().hex[:8]}"
    name = f"spareparts_test_{os.getpid()}_{token}"
    original_release = run_isolation._release_database_lock
    original_mask = run_isolation.signal.pthread_sigmask
    starting_mask = original_mask(signal.SIG_BLOCK, set())
    old_connection = None
    published = []
    mask_calls = []

    def create_and_remember_connection(conn, candidate):
        nonlocal old_connection
        old_connection = conn
        conn.execute(text(f'CREATE DATABASE "{candidate}"'))

    def fail_old_connection_unlock(conn, candidate):
        if conn is old_connection:
            raise injected
        return original_release(conn, candidate)

    def track_mask(how, mask):
        mask_calls.append(how)
        return original_mask(how, mask)

    monkeypatch.setattr(
        run_isolation,
        "_release_database_lock",
        fail_old_connection_unlock,
    )
    monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", track_mask)
    try:
        with pytest.raises(type(injected), match=str(injected)):
            create_database_run(
                _render(base),
                token_factory=lambda: token,
                execute_create=create_and_remember_connection,
                on_owned=published.append,
                max_attempts=1,
            )
        assert published == []
        assert not _database_exists(base, name)
        assert mask_calls[0] == signal.SIG_BLOCK
        assert mask_calls[-1] == signal.SIG_SETMASK
    finally:
        monkeypatch.setattr(run_isolation, "_release_database_lock", original_release)
        monkeypatch.setattr(run_isolation.signal, "pthread_sigmask", original_mask)
        original_mask(signal.SIG_SETMASK, starting_mask)
        if _database_exists(base, name):
            leaked = capture_database_run(_render(base), name, expected_name=name)
            cleanup_database_run(leaked)


def test_rename_noreplace_capability_is_required_early(monkeypatch):
    monkeypatch.setattr(run_isolation, "_rename_noreplace", None, raising=False)
    with pytest.raises(RuntimeError, match="Linux isolation capabilities required"):
        run_isolation.validate_platform_capabilities()


def test_raw_open_failure_restore_never_clobbers_same_name_canary(
    monkeypatch,
    tmp_path,
):
    root = (tmp_path / "open-restore-root").resolve()
    handle = create_raw_run(root)
    token = "openrestore"
    parent = root / f".pytest-quarantine-{os.getpid()}-{token}"
    original_open = run_isolation.os.open
    original_rename = run_isolation._rename_noreplace
    canary_inode = None

    def fail_entry_open(path, flags, *args, **kwargs):
        if path == "entry":
            raise OSError("entry-open-secret")
        return original_open(path, flags, *args, **kwargs)

    def inject_canary_before_restore(src_fd, src, dst_fd, dst):
        nonlocal canary_inode
        if src == "entry" and dst == handle.run_dir.name:
            handle.run_dir.mkdir()
            canary_inode = handle.run_dir.stat().st_ino
        return original_rename(src_fd, src, dst_fd, dst)

    monkeypatch.setattr(run_isolation.os, "open", fail_entry_open)
    monkeypatch.setattr(run_isolation, "_rename_noreplace", inject_canary_before_restore)
    try:
        with pytest.raises(RuntimeError) as raised:
            cleanup_raw_run(handle, quarantine_token_factory=lambda: token)
        assert str(raised.value) == "pytest RAW cleanup failed safely"
        assert handle.run_dir.stat().st_ino == canary_inode
        assert (parent / "entry").stat().st_ino == handle.run_ino
    finally:
        monkeypatch.setattr(run_isolation.os, "open", original_open)
        monkeypatch.setattr(run_isolation, "_rename_noreplace", original_rename)
        if handle.run_dir.exists() and handle.run_dir.stat().st_ino == handle.run_ino:
            cleanup_raw_run(handle)
        else:
            if handle.run_dir.exists():
                handle.run_dir.rmdir()
            if (parent / "entry").exists():
                os.rename(parent / "entry", handle.run_dir)
                parent.rmdir()
                cleanup_raw_run(handle)


def test_raw_delete_failure_retains_quarantine_without_clobbering_canary(
    monkeypatch,
    tmp_path,
):
    root = (tmp_path / "delete-restore-root").resolve()
    handle = create_raw_run(root)
    token = "deleterestore"
    parent = root / f".pytest-quarantine-{os.getpid()}-{token}"
    original_unlink = run_isolation.os.unlink
    original_rename = run_isolation._rename_noreplace
    canary_inode = None
    failed = False

    def fail_first_unlink(path, *args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("delete-secret")
        return original_unlink(path, *args, **kwargs)

    def inject_canary_before_restore(src_fd, src, dst_fd, dst):
        nonlocal canary_inode
        if src == "entry" and dst == handle.run_dir.name:
            handle.run_dir.mkdir()
            canary_inode = handle.run_dir.stat().st_ino
        return original_rename(src_fd, src, dst_fd, dst)

    monkeypatch.setattr(run_isolation.os, "unlink", fail_first_unlink)
    monkeypatch.setattr(run_isolation, "_rename_noreplace", inject_canary_before_restore)
    try:
        with pytest.raises(RuntimeError) as raised:
            cleanup_raw_run(handle, quarantine_token_factory=lambda: token)
        assert str(raised.value) == "pytest RAW cleanup failed safely"
        assert handle.run_dir.stat().st_ino == canary_inode
        assert (parent / "entry").stat().st_ino == handle.run_ino
        assert (parent / "entry" / handle.marker_path.name).exists()
    finally:
        monkeypatch.setattr(run_isolation.os, "unlink", original_unlink)
        monkeypatch.setattr(run_isolation, "_rename_noreplace", original_rename)
        if handle.run_dir.exists() and handle.run_dir.stat().st_ino == handle.run_ino:
            cleanup_raw_run(handle)
        else:
            if handle.run_dir.exists():
                handle.run_dir.rmdir()
            if (parent / "entry").exists():
                os.rename(parent / "entry", handle.run_dir)
                parent.rmdir()
                cleanup_raw_run(handle)
