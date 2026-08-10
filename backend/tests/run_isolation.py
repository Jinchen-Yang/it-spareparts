"""Own exactly one pytest run's database and raw archive directory."""

import ctypes
import errno
import os
import re
import secrets
import signal
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool


CONTROLLED_RAW_BASE = Path("/tmp/it-spareparts-pytest-raw")
GENERATED_DATABASE = re.compile(r"^spareparts_test_(\d+)_([a-z0-9]+)$")
_CONTRACT_DATABASE = re.compile(r"^spareparts_test_issue147_contract_[A-Za-z0-9_]+$")
_TEST_DATABASE_BASE = re.compile(r"^spareparts_test(?:_[A-Za-z0-9_]+)?$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TARGET_QUERY_OVERRIDES = {
    "host",
    "hostaddr",
    "port",
    "dbname",
    "database",
    "user",
    "username",
    "password",
    "service",
    "servicefile",
}
_MARKER_NAME = ".pytest-run-owner"
_MARKER_BYTES = 64
_RENAME_NOREPLACE = 1
MAINTENANCE_LOCK_TIMEOUT_MS = 1000
MAINTENANCE_STATEMENT_TIMEOUT_MS = 120000
MAINTENANCE_DROP_STATEMENT_TIMEOUT_MS = 150000

try:
    _LIBC_RENAMEAT2 = ctypes.CDLL(None, use_errno=True).renameat2
    _LIBC_RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _LIBC_RENAMEAT2.restype = ctypes.c_int
except (AttributeError, OSError):
    _LIBC_RENAMEAT2 = None


def _rename_noreplace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    if _LIBC_RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    result = _LIBC_RENAMEAT2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


@dataclass(frozen=True)
class DatabaseRun:
    base_url: str
    database_url: str
    name: str
    database_oid: int
    owner_oid: int
    owner_role: str


@dataclass(frozen=True)
class RawBasePlan:
    root: Path
    existing_ancestors: tuple[tuple[Path, int, int], ...]


@dataclass(frozen=True)
class RawRun:
    root: Path
    run_dir: Path
    root_dev: int
    root_ino: int
    run_dev: int
    run_ino: int
    marker: str
    marker_path: Path
    marker_dev: int
    marker_ino: int


class LifecycleState(Enum):
    ACTIVE = "active"
    CLEANING = "cleaning"
    CLEANED = "cleaned"
    FAILED = "failed"


class _DatabaseOwnershipHandoffError(RuntimeError):
    pass


class RunLifecycle:
    def __init__(
        self,
        *,
        engine_dispose: Callable[[], object] | None = None,
        database_cleanup: Callable[[], object] | None = None,
        raw_cleanup: Callable[[], object] | None = None,
    ) -> None:
        self.state = LifecycleState.ACTIVE
        self.engine_dispose = engine_dispose
        self.database_cleanup = database_cleanup
        self.raw_cleanup = raw_cleanup
        self._operations = {
            "engine": [engine_dispose, engine_dispose is None],
            "database": [database_cleanup, database_cleanup is None],
            "raw": [raw_cleanup, raw_cleanup is None],
        }
        self._failed_once = False

    def cleanup(self) -> bool:
        if self.state is LifecycleState.CLEANED:
            return False
        if self.state is LifecycleState.CLEANING:
            raise RuntimeError("pytest run cleanup unavailable")
        if self.state is LifecycleState.FAILED and not self._failed_once:
            raise RuntimeError("pytest run cleanup unavailable")
        self.state = LifecycleState.CLEANING
        failed = False
        for value in self._operations.values():
            operation, succeeded = value
            if succeeded:
                continue
            try:
                operation()
            except BaseException:
                failed = True
            else:
                value[1] = True
        if failed:
            self._failed_once = True
            self.state = LifecycleState.FAILED
            raise RuntimeError("pytest run cleanup failed safely")
        self.state = LifecycleState.CLEANED
        return True


def validate_platform_capabilities() -> None:
    required_dir_fd = {os.open, os.mkdir, os.rename, os.rmdir, os.unlink, os.stat}
    if (
        sys.platform != "linux"
        or not hasattr(signal, "pthread_sigmask")
        or not hasattr(os, "O_NOFOLLOW")
        or _LIBC_RENAMEAT2 is None
        or not callable(_rename_noreplace)
        or not required_dir_fd.issubset(os.supports_dir_fd)
        or not Path("/proc/self/fd").is_dir()
    ):
        raise RuntimeError("Linux isolation capabilities required")


def validate_pytest_invocation(
    argv: Sequence[str],
    environ: Mapping[str, str],
) -> None:
    if "PYTEST_XDIST_WORKER" in environ or "PYTEST_XDIST_WORKER_COUNT" in environ:
        raise RuntimeError("pytest-xdist is not supported by run isolation")
    for argument in argv:
        if argument == "-n" or (argument.startswith("-n") and len(argument) > 2):
            raise RuntimeError("pytest-xdist is not supported by run isolation")
        if argument == "--numprocesses" or argument.startswith("--numprocesses="):
            raise RuntimeError("pytest-xdist is not supported by run isolation")


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def validate_database_base(database_url: str) -> URL:
    url = make_url(database_url)
    if url.drivername != "postgresql+psycopg":
        raise RuntimeError("pytest database scheme must be postgresql+psycopg")
    overrides = sorted(set(url.query) & _TARGET_QUERY_OVERRIDES)
    if overrides:
        raise RuntimeError("pytest database target query override is forbidden")
    if not _TEST_DATABASE_BASE.fullmatch(url.database or ""):
        raise RuntimeError("pytest database base name is unsafe")
    if url.host not in _LOCAL_HOSTS:
        raise RuntimeError("pytest database host must be local")
    return url


def validate_raw_base(
    raw_base_dir: str | Path,
    *,
    checkout_root: str | Path | None = None,
) -> RawBasePlan:
    requested = Path(raw_base_dir)
    if not requested.is_absolute():
        raise RuntimeError("pytest RAW base must be an absolute safe /tmp child")
    root = Path(os.path.normpath(requested))
    safe_tmp = Path("/tmp")
    if root == safe_tmp or not root.is_relative_to(safe_tmp):
        raise RuntimeError("pytest RAW base must be an absolute safe /tmp child")
    if checkout_root is not None:
        checkout = Path(os.path.normpath(Path(checkout_root).absolute()))
        if root == checkout or root.is_relative_to(checkout):
            raise RuntimeError("pytest RAW base must not be inside the checkout")

    existing = []
    current = Path("/")
    for part in root.parts[1:]:
        current /= part
        try:
            item = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
            raise RuntimeError("pytest RAW base has an unsafe ancestor")
        existing.append((current, item.st_dev, item.st_ino))
    return RawBasePlan(root=root, existing_ancestors=tuple(existing))


def _open_raw_root(plan: RawBasePlan, *, create_missing: bool) -> int:
    expected = {path: (dev, ino) for path, dev, ino in plan.existing_ancestors}
    parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path("/")
    try:
        for part in plan.root.parts[1:]:
            current /= part
            if create_missing:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            child_stat = os.fstat(child_fd)
            if current in expected and expected[current] != (
                child_stat.st_dev,
                child_stat.st_ino,
            ):
                os.close(child_fd)
                raise RuntimeError("pytest RAW base ancestor changed before creation")
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _maintenance_engine(base: URL):
    return create_engine(
        base.set(database="postgres"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )


def _validate_destructive_name(name: str, expected_name: str | None) -> None:
    if expected_name is not None and name != expected_name:
        raise RuntimeError("pytest database cleanup name mismatch")
    if GENERATED_DATABASE.fullmatch(name):
        return
    if expected_name == name and _CONTRACT_DATABASE.fullmatch(name):
        return
    raise RuntimeError("pytest database destructive allowlist rejected name")


def _current_role(conn):
    return conn.execute(
        text(
            "SELECT oid, rolname, rolsuper, rolcreatedb"
            " FROM pg_roles WHERE rolname=current_user"
        )
    ).one()


def _database_lock_key(name: str) -> str:
    return f"pytest-run-database:{name}"


def _acquire_database_lock(conn, name: str) -> None:
    conn.execute(
        text("SELECT pg_advisory_lock(hashtext(:key))"),
        {"key": _database_lock_key(name)},
    )


def _release_database_lock(conn, name: str) -> None:
    conn.execute(
        text("SELECT pg_advisory_unlock(hashtext(:key))"),
        {"key": _database_lock_key(name)},
    )


def _set_maintenance_timeouts(conn) -> None:
    conn.execute(
        text(f"SET lock_timeout = '{MAINTENANCE_LOCK_TIMEOUT_MS}ms'")
    )
    conn.execute(
        text(
            "SET statement_timeout = "
            f"'{MAINTENANCE_STATEMENT_TIMEOUT_MS}ms'"
        )
    )


def _set_drop_statement_timeout(conn) -> None:
    conn.execute(
        text(
            "SET statement_timeout = "
            f"'{MAINTENANCE_DROP_STATEMENT_TIMEOUT_MS}ms'"
        )
    )


def _database_identity(conn, name: str):
    return conn.execute(
        text(
            "SELECT d.oid AS database_oid, d.datdba AS owner_oid,"
            " r.rolname AS owner_role"
            " FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba"
            " WHERE d.datname=:name"
        ),
        {"name": name},
    ).one()


def _owned_handle(base: URL, name: str, created, role) -> DatabaseRun:
    if created.owner_oid != role.oid or created.owner_role != role.rolname:
        raise RuntimeError("pytest database owner identity mismatch")
    return DatabaseRun(
        base_url=_render(base),
        database_url=_render(base.set(database=name)),
        name=name,
        database_oid=int(created.database_oid),
        owner_oid=int(created.owner_oid),
        owner_role=str(created.owner_role),
    )


def _publish_database_run(
    handle: DatabaseRun,
    on_owned: Callable[[DatabaseRun], None] | None,
) -> None:
    if on_owned is None:
        return
    try:
        on_owned(handle)
    except BaseException:
        cleanup_database_run(handle)
        raise _DatabaseOwnershipHandoffError(
            "pytest database ownership handoff failed safely"
        ) from None


def _discard_database_connection(conn) -> None:
    try:
        conn.invalidate()
    except BaseException:
        pass
    try:
        conn.close()
    except BaseException:
        pass


def _recover_database_run(base: URL, name: str) -> DatabaseRun | None:
    engine = _maintenance_engine(base)
    conn = None
    lock_acquired = False
    handle = None
    release_error = None
    try:
        conn = engine.connect()
        _set_maintenance_timeouts(conn)
        _acquire_database_lock(conn, name)
        lock_acquired = True
        identity = conn.execute(
            text(
                "SELECT d.oid AS database_oid, d.datdba AS owner_oid,"
                " r.rolname AS owner_role"
                " FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba"
                " WHERE d.datname=:name"
            ),
            {"name": name},
        ).one_or_none()
        if identity is not None:
            handle = _owned_handle(base, name, identity, _current_role(conn))
    finally:
        if lock_acquired and conn is not None:
            try:
                _release_database_lock(conn, name)
            except BaseException as exc:
                release_error = exc
                _discard_database_connection(conn)
        if conn is not None:
            try:
                conn.close()
            except BaseException as exc:
                if release_error is None:
                    release_error = exc
        try:
            engine.dispose()
        except BaseException as exc:
            if release_error is None:
                release_error = exc
    if release_error is not None:
        if handle is not None:
            cleanup_database_run(handle)
        raise release_error
    return handle


def _recover_after_primary_failure(
    base: URL,
    name: str,
    primary_error: BaseException,
) -> DatabaseRun | None:
    try:
        return _recover_database_run(base, name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise RuntimeError("pytest database creation failed safely") from None


def _cleanup_unpublished_database(handle: DatabaseRun) -> BaseException | None:
    blocked = {signal.SIGINT, signal.SIGTERM}
    old_mask = None
    failure = None
    try:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    except BaseException as exc:
        failure = exc
    try:
        cleanup_database_run(handle)
    except BaseException as exc:
        failure = exc
    finally:
        if old_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            except BaseException as exc:
                failure = exc
    return failure


def create_database_run(
    database_base_url: str,
    *,
    token_factory: Callable[[], str] | None = None,
    on_owned: Callable[[DatabaseRun], None] | None = None,
    execute_create: Callable[[object, str], object] | None = None,
    max_attempts: int = 8,
) -> DatabaseRun:
    base = validate_database_base(database_base_url)
    token_factory = token_factory or (lambda: secrets.token_hex(8))
    execute_create = execute_create or (
        lambda conn, name: conn.execute(text(f'CREATE DATABASE "{name}"'))
    )
    engine = None
    handle = None
    result = None
    published = False
    pending_failure = None
    try:
        engine = _maintenance_engine(base)
        with engine.connect() as conn:
            _set_maintenance_timeouts(conn)
            role = _current_role(conn)
            if not (role.rolsuper or role.rolcreatedb):
                raise RuntimeError("pytest database role lacks create privilege")
            for _ in range(max_attempts):
                handle = None
                lock_acquired = False
                name = f"spareparts_test_{os.getpid()}_{token_factory()}"
                if not GENERATED_DATABASE.fullmatch(name) or len(name) > 63:
                    raise RuntimeError("pytest generated database name is invalid")
                blocked = {signal.SIGINT, signal.SIGTERM}
                old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
                try:
                    _set_maintenance_timeouts(conn)
                    _acquire_database_lock(conn, name)
                    lock_acquired = True
                    conn.execute(text("SET lock_timeout = '0'"))
                    try:
                        execute_create(conn, name)
                    except DBAPIError as exc:
                        if getattr(exc.orig, "sqlstate", None) == "42P04":
                            _release_database_lock(conn, name)
                            lock_acquired = False
                            continue
                        try:
                            _release_database_lock(conn, name)
                        except BaseException:
                            _discard_database_connection(conn)
                        lock_acquired = False
                        ambiguous = _recover_after_primary_failure(base, name, exc)
                        if ambiguous is not None:
                            cleanup_database_run(ambiguous)
                        raise RuntimeError(
                            "pytest database creation failed safely"
                        ) from None
                    except BaseException as exc:
                        try:
                            _release_database_lock(conn, name)
                        except BaseException:
                            _discard_database_connection(conn)
                        lock_acquired = False
                        uncertain = _recover_after_primary_failure(base, name, exc)
                        if uncertain is not None:
                            cleanup_database_run(uncertain)
                        raise
                    try:
                        created = _database_identity(conn, name)
                        handle = _owned_handle(base, name, created, role)
                    except BaseException as exc:
                        try:
                            _release_database_lock(conn, name)
                        except BaseException:
                            _discard_database_connection(conn)
                        lock_acquired = False
                        handle = _recover_after_primary_failure(base, name, exc)
                        if handle is not None:
                            result = handle
                            try:
                                _publish_database_run(handle, on_owned)
                            except _DatabaseOwnershipHandoffError:
                                result = None
                                raise
                            published = on_owned is not None
                            if not published:
                                cleanup_database_run(handle)
                                result = None
                        raise
                    result = handle
                    try:
                        _release_database_lock(conn, name)
                    except BaseException as exc:
                        _discard_database_connection(conn)
                        lock_acquired = False
                        recovered = _recover_after_primary_failure(base, name, exc)
                        if recovered is not None:
                            cleanup_database_run(recovered)
                        result = None
                        raise
                    lock_acquired = False
                    try:
                        _publish_database_run(handle, on_owned)
                    except _DatabaseOwnershipHandoffError:
                        result = None
                        raise
                    published = on_owned is not None
                    break
                finally:
                    if lock_acquired:
                        try:
                            _release_database_lock(conn, name)
                        except BaseException:
                            _discard_database_connection(conn)
                    try:
                        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                    except BaseException:
                        raise
    except BaseException as exc:
        pending_failure = exc
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except BaseException as exc:
                if pending_failure is None:
                    pending_failure = exc
    if pending_failure is not None:
        cleanup_failure = None
        if result is not None and not published:
            cleanup_failure = _cleanup_unpublished_database(result)
            result = None
        if cleanup_failure is not None:
            raise RuntimeError("pytest database creation failed safely") from None
        if isinstance(pending_failure, (KeyboardInterrupt, SystemExit)):
            raise pending_failure
        if isinstance(pending_failure, _DatabaseOwnershipHandoffError):
            raise pending_failure
        if not isinstance(pending_failure, Exception):
            raise pending_failure
        raise RuntimeError("pytest database creation failed safely") from None
    if result is not None:
        return result
    raise RuntimeError("pytest database name allocation failed safely")


def capture_database_run(
    database_base_url: str,
    name: str,
    *,
    expected_name: str,
) -> DatabaseRun:
    base = validate_database_base(database_base_url)
    _validate_destructive_name(name, expected_name)
    engine = None
    try:
        engine = _maintenance_engine(base)
        with engine.connect() as conn:
            _set_maintenance_timeouts(conn)
            _acquire_database_lock(conn, name)
            try:
                role = _current_role(conn)
                identity = _database_identity(conn, name)
            finally:
                _release_database_lock(conn, name)
        return _owned_handle(base, name, identity, role)
    except (KeyboardInterrupt, SystemExit):
        raise
    except DBAPIError:
        raise RuntimeError("pytest database capture failed safely") from None
    finally:
        if engine is not None:
            prior_exception = sys.exc_info()[0] is not None
            try:
                engine.dispose()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                if not prior_exception:
                    raise RuntimeError(
                        "pytest database capture failed safely"
                    ) from None


def _verify_database_for_cleanup(conn, handle: DatabaseRun) -> bool:
    role = _current_role(conn)
    row = conn.execute(
        text(
            "SELECT d.oid AS database_oid, d.datdba AS owner_oid,"
            " r.rolname AS owner_role"
            " FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba"
            " WHERE d.datname=:name"
        ),
        {"name": handle.name},
    ).one_or_none()
    if row is None:
        return False
    if int(row.database_oid) != handle.database_oid:
        raise RuntimeError("pytest database identity mismatch (OID)")
    if int(row.owner_oid) != handle.owner_oid or row.owner_role != handle.owner_role:
        raise RuntimeError("pytest database owner identity mismatch")
    if int(role.oid) != handle.owner_oid or role.rolname != handle.owner_role:
        raise RuntimeError("pytest database current owner mismatch")
    if not (role.rolsuper or role.rolcreatedb):
        raise RuntimeError("pytest database cleanup privilege mismatch")
    clients = int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_stat_activity"
                " WHERE datname=:name AND pid <> pg_backend_pid()"
                " AND backend_type='client backend'"
            ),
            {"name": handle.name},
        ).scalar()
        or 0
    )
    if clients:
        raise RuntimeError("pytest database has other clients")
    return True


def cleanup_database_run(
    handle: DatabaseRun,
    *,
    expected_name: str | None = None,
    before_drop: Callable[[], object] | None = None,
) -> bool:
    base = validate_database_base(handle.base_url)
    _validate_destructive_name(handle.name, expected_name)
    if _render(base.set(database=handle.name)) != handle.database_url:
        raise RuntimeError("pytest database cleanup URL mismatch")
    engine = None
    try:
        engine = _maintenance_engine(base)
        with engine.connect() as conn:
            _set_maintenance_timeouts(conn)
            _acquire_database_lock(conn, handle.name)
            try:
                if not _verify_database_for_cleanup(conn, handle):
                    return False
                if before_drop is not None:
                    before_drop()
                try:
                    still_owned = _verify_database_for_cleanup(conn, handle)
                except RuntimeError as exc:
                    if str(exc) == "pytest database has other clients":
                        raise RuntimeError(
                            "pytest database cleanup failed safely"
                        ) from None
                    raise
                if not still_owned:
                    raise RuntimeError("pytest database identity mismatch (missing)")
                _set_drop_statement_timeout(conn)
                conn.execute(text(f'DROP DATABASE "{handle.name}"'))
                return True
            finally:
                _release_database_lock(conn, handle.name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except DBAPIError:
        raise RuntimeError("pytest database cleanup failed safely") from None
    finally:
        if engine is not None:
            prior_exception = sys.exc_info()[0] is not None
            try:
                engine.dispose()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                if not prior_exception:
                    raise RuntimeError(
                        "pytest database cleanup failed safely"
                    ) from None


def _delete_tree_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(item.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _delete_tree_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _raw_marker_matches(directory_fd: int, handle: RawRun) -> bool:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        marker_fd = os.open(_MARKER_NAME, flags, dir_fd=directory_fd)
    except OSError:
        return False
    try:
        marker_stat = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or stat.S_IMODE(marker_stat.st_mode) != 0o600
            or (marker_stat.st_dev, marker_stat.st_ino)
            != (handle.marker_dev, handle.marker_ino)
        ):
            return False
        data = os.read(marker_fd, _MARKER_BYTES + 1)
        return len(data) == _MARKER_BYTES and data == handle.marker.encode("ascii")
    except OSError:
        return False
    finally:
        os.close(marker_fd)


def _quarantine_and_delete(
    root_fd: int,
    name: str,
    expected_dev: int,
    expected_ino: int,
    *,
    before_quarantine: Callable[[], object] | None = None,
    token_factory: Callable[[], str] | None = None,
    before_parent_create: Callable[[str], object] | None = None,
    before_delete: Callable[[], object] | None = None,
    restore_check: Callable[[int], bool] | None = None,
) -> None:
    token_factory = token_factory or (lambda: secrets.token_hex(16))
    parent_name = None
    parent_fd = None
    for _ in range(8):
        token = token_factory()
        if not re.fullmatch(r"[a-z0-9]+", token):
            raise RuntimeError("pytest RAW cleanup failed safely")
        candidate = f".pytest-quarantine-{os.getpid()}-{token}"
        if before_parent_create is not None:
            before_parent_create(candidate)
        try:
            os.mkdir(candidate, 0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        try:
            parent_fd = os.open(
                candidate,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except OSError:
            try:
                os.rmdir(candidate, dir_fd=root_fd)
            except OSError:
                pass
            continue
        parent_stat = os.fstat(parent_fd)
        if stat.S_IMODE(parent_stat.st_mode) != 0o700:
            os.close(parent_fd)
            parent_fd = None
            try:
                os.rmdir(candidate, dir_fd=root_fd)
            except OSError:
                pass
            continue
        parent_name = candidate
        break
    else:
        raise RuntimeError("pytest RAW cleanup failed safely")
    if parent_fd is None or parent_name is None:
        raise RuntimeError("pytest RAW cleanup failed safely")
    entry = "entry"
    if before_quarantine is not None:
        try:
            before_quarantine()
        except (KeyboardInterrupt, SystemExit):
            os.close(parent_fd)
            os.rmdir(parent_name, dir_fd=root_fd)
            raise
        except Exception:
            os.close(parent_fd)
            os.rmdir(parent_name, dir_fd=root_fd)
            raise RuntimeError("pytest RAW cleanup failed safely") from None
    try:
        _rename_noreplace(root_fd, name, parent_fd, entry)
    except OSError:
        os.close(parent_fd)
        try:
            os.rmdir(parent_name, dir_fd=root_fd)
        except OSError:
            pass
        raise RuntimeError("pytest RAW cleanup failed safely") from None
    try:
        quarantine_fd = os.open(
            entry,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError:
        try:
            _rename_noreplace(parent_fd, entry, root_fd, name)
        except OSError:
            pass
        os.close(parent_fd)
        try:
            os.rmdir(parent_name, dir_fd=root_fd)
        except OSError:
            pass
        raise RuntimeError("pytest RAW cleanup failed safely") from None
    quarantined = os.fstat(quarantine_fd)
    if (quarantined.st_dev, quarantined.st_ino) != (expected_dev, expected_ino):
        os.close(quarantine_fd)
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                _rename_noreplace(parent_fd, entry, root_fd, name)
            except OSError:
                pass
        os.close(parent_fd)
        try:
            os.rmdir(parent_name, dir_fd=root_fd)
        except OSError:
            pass
        raise RuntimeError("pytest RAW cleanup failed safely")
    delete_error = None
    try:
        if before_delete is not None:
            before_delete()
        _delete_tree_fd(quarantine_fd)
    except BaseException as exc:
        delete_error = exc
    if delete_error is not None:
        can_restore = restore_check is not None and restore_check(quarantine_fd)
        os.close(quarantine_fd)
        restored = False
        if can_restore:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    _rename_noreplace(parent_fd, entry, root_fd, name)
                    restored = True
                except OSError:
                    pass
        os.close(parent_fd)
        if restored:
            try:
                os.rmdir(parent_name, dir_fd=root_fd)
            except OSError:
                pass
        if isinstance(delete_error, (KeyboardInterrupt, SystemExit)):
            raise delete_error
        raise RuntimeError("pytest RAW cleanup failed safely") from None
    os.close(quarantine_fd)
    try:
        os.rmdir(entry, dir_fd=parent_fd)
        os.close(parent_fd)
        parent_fd = None
        os.rmdir(parent_name, dir_fd=root_fd)
    except OSError:
        if parent_fd is not None:
            os.close(parent_fd)
        raise RuntimeError("pytest RAW cleanup failed safely") from None


def create_raw_run(
    raw_base: RawBasePlan | str | Path,
    *,
    checkout_root: str | Path | None = None,
    run_token_factory: Callable[[], str] | None = None,
    before_run_dir: Callable[[], object] | None = None,
    before_marker: Callable[[Path], object] | None = None,
    before_rollback_quarantine: Callable[[], object] | None = None,
    on_owned: Callable[[RawRun], object] | None = None,
) -> RawRun:
    plan = (
        raw_base
        if isinstance(raw_base, RawBasePlan)
        else validate_raw_base(
            raw_base,
            checkout_root=checkout_root,
        )
    )
    run_token_factory = run_token_factory or (lambda: secrets.token_hex(8))
    root_fd = _open_raw_root(plan, create_missing=True)
    run_name = None
    run_identity = None
    handle = None
    old_mask = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        {signal.SIGINT, signal.SIGTERM},
    )
    try:
        root_stat = os.fstat(root_fd)
        path_stat = plan.root.lstat()
        if (path_stat.st_dev, path_stat.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise RuntimeError("pytest RAW base changed before creation")
        if before_run_dir is not None:
            before_run_dir()
        stable_root = Path(os.readlink(f"/proc/self/fd/{root_fd}"))
        for _ in range(8):
            token = run_token_factory()
            if not re.fullmatch(r"[a-z0-9]+", token):
                raise RuntimeError("pytest RAW run token is invalid")
            run_name = f"pytest-{os.getpid()}-{token}"
            try:
                os.mkdir(run_name, 0o700, dir_fd=root_fd)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("pytest RAW run allocation failed safely")
        run_dir = stable_root / run_name
        run_identity = os.stat(run_name, dir_fd=root_fd, follow_symlinks=False)
        try:
            run_fd = os.open(
                run_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                run_stat = os.fstat(run_fd)
                if before_marker is not None:
                    before_marker(run_dir)
                marker = secrets.token_hex(_MARKER_BYTES // 2)
                marker_fd = os.open(
                    _MARKER_NAME,
                    os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY,
                    0o600,
                    dir_fd=run_fd,
                )
                try:
                    marker_bytes = marker.encode("ascii")
                    written = 0
                    while written < len(marker_bytes):
                        count = os.write(marker_fd, marker_bytes[written:])
                        if count <= 0:
                            raise RuntimeError("pytest RAW marker write failed safely")
                        written += count
                    if written != _MARKER_BYTES:
                        raise RuntimeError("pytest RAW marker write failed safely")
                    marker_stat = os.fstat(marker_fd)
                    if stat.S_IMODE(marker_stat.st_mode) != 0o600:
                        raise RuntimeError("pytest RAW marker mode mismatch")
                finally:
                    os.close(marker_fd)
            finally:
                os.close(run_fd)
            handle = RawRun(
                root=stable_root,
                run_dir=run_dir,
                root_dev=root_stat.st_dev,
                root_ino=root_stat.st_ino,
                run_dev=run_stat.st_dev,
                run_ino=run_stat.st_ino,
                marker=marker,
                marker_path=run_dir / _MARKER_NAME,
                marker_dev=marker_stat.st_dev,
                marker_ino=marker_stat.st_ino,
            )
            if on_owned is not None:
                try:
                    on_owned(handle)
                except BaseException:
                    cleanup_raw_run(handle)
                    raise RuntimeError(
                        "pytest RAW ownership handoff failed safely"
                    ) from None
            return handle
        except BaseException:
            if run_name is not None and run_identity is not None:
                try:
                    os.stat(run_name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        _quarantine_and_delete(
                            root_fd,
                            run_name,
                            run_identity.st_dev,
                            run_identity.st_ino,
                            before_quarantine=before_rollback_quarantine,
                        )
                    except RuntimeError:
                        raise RuntimeError(
                            "pytest RAW rollback failed safely"
                        ) from None
            raise
    finally:
        os.close(root_fd)
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        except BaseException:
            if handle is not None and on_owned is None:
                cleanup_raw_run(handle)
            raise


def cleanup_raw_run(
    handle: RawRun,
    *,
    before_quarantine: Callable[[], object] | None = None,
    quarantine_token_factory: Callable[[], str] | None = None,
    before_quarantine_parent_create: Callable[[str], object] | None = None,
    before_quarantine_delete: Callable[[], object] | None = None,
) -> bool:
    plan = validate_raw_base(handle.root)
    try:
        root_fd = _open_raw_root(plan, create_missing=False)
    except FileNotFoundError:
        raise RuntimeError("pytest RAW root identity mismatch") from None
    try:
        root_stat = os.fstat(root_fd)
        if (root_stat.st_dev, root_stat.st_ino) != (handle.root_dev, handle.root_ino):
            raise RuntimeError("pytest RAW root identity mismatch")
        try:
            run_fd = os.open(
                handle.run_dir.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return False
        except OSError:
            raise RuntimeError("pytest RAW run identity mismatch") from None
        try:
            run_stat = os.fstat(run_fd)
            if (run_stat.st_dev, run_stat.st_ino) != (handle.run_dev, handle.run_ino):
                raise RuntimeError("pytest RAW run identity mismatch")
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            try:
                marker_fd = os.open(_MARKER_NAME, flags, dir_fd=run_fd)
            except OSError:
                raise RuntimeError("pytest RAW marker identity mismatch") from None
            try:
                marker_stat = os.fstat(marker_fd)
                if (
                    not stat.S_ISREG(marker_stat.st_mode)
                    or stat.S_IMODE(marker_stat.st_mode) != 0o600
                    or (marker_stat.st_dev, marker_stat.st_ino)
                    != (handle.marker_dev, handle.marker_ino)
                ):
                    raise RuntimeError("pytest RAW marker identity mismatch")
                data = os.read(marker_fd, _MARKER_BYTES + 1)
                if len(data) != _MARKER_BYTES or data != handle.marker.encode("ascii"):
                    raise RuntimeError("pytest RAW marker identity mismatch")
            finally:
                os.close(marker_fd)
        finally:
            os.close(run_fd)
        _quarantine_and_delete(
            root_fd,
            handle.run_dir.name,
            handle.run_dev,
            handle.run_ino,
            before_quarantine=before_quarantine,
            token_factory=quarantine_token_factory,
            before_parent_create=before_quarantine_parent_create,
            before_delete=before_quarantine_delete,
            restore_check=lambda directory_fd: _raw_marker_matches(
                directory_fd,
                handle,
            ),
        )
        return True
    finally:
        os.close(root_fd)
