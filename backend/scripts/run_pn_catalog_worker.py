"""Run explicitly in a dedicated process; --once provides a bounded operator invocation."""
import argparse
import json
import signal
import sys
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.pn_catalog.errors import CatalogError
from app.services.pn_catalog_runtime import (
    WorkerSettings,
    build_worker_handlers,
    original_actor_authorize,
    run_worker_once,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 1 <= len(args.worker_id) <= 128:
        parser.error("worker-id must contain 1 to 128 characters")
    settings = WorkerSettings()
    if not settings.enabled:
        parser.error("PN_CATALOG_WORKER_ENABLED must be explicitly enabled")
    # Fail before claiming any work if the authority adapter is not available.
    try:
        from app.services.pn_catalog_access import resolve_job_access  # noqa: F401
    except ImportError:
        parser.error("the shared PN authorization adapter is not installed")
    handlers = build_worker_handlers(SessionLocal, authorize=original_actor_authorize)
    stopped = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    while not stopped.is_set():
        try:
            result = run_worker_once(SessionLocal, worker_id=args.worker_id, settings=settings, handlers=handlers)
            if result is not None:
                print(json.dumps({"job_id": str(result.job_id), "status": result.status,
                                  "error_code": result.error_code}), flush=True)
        except CatalogError as exc:
            print(json.dumps({"error_code": exc.code}), file=sys.stderr, flush=True)
            if args.once:
                return 1
            result = None
        if args.once:
            return 0
        if result is None:
            stopped.wait(settings.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
