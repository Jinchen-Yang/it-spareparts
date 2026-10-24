"""Cancellation never abandons a workbook transaction or releases its limiter."""
import asyncio
import threading

import pytest

from app.services.workbook_tasks import run_worker


@pytest.mark.parametrize("worker_fails", [False, True])
def test_repeated_cancellation_waits_for_worker_terminal_before_cleanup(worker_fails):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cleanup = threading.Event()

    def blocking_worker():
        entered.set()
        assert release.wait(5)
        finished.set()
        if worker_fails:
            raise RuntimeError("worker transaction failed")
        return "committed"

    async def request():
        try:
            return await run_worker(blocking_worker)
        finally:
            cleanup.set()

    async def exercise():
        task = asyncio.create_task(request())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert not cleanup.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert finished.is_set() and cleanup.is_set()
            if worker_fails:
                assert isinstance(cancelled.value.__cause__, RuntimeError)
        finally:
            release.set()
            if not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(exercise())
