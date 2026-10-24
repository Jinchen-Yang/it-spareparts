"""Keep worker-owned transactions alive until completion on request cancellation."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

import anyio

T = TypeVar("T")


async def run_worker(callback: Callable[[], T]) -> T:
    """Defer cancellation until the thread finishes, including repeated cancels.

    Callers may then safely release their limiter and temporary input in finally.
    The worker owns its session; no request thread session crosses this boundary.
    """
    worker = asyncio.create_task(anyio.to_thread.run_sync(callback, abandon_on_cancel=False))
    cancellation = None
    caller = asyncio.current_task()
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            if caller is not None and caller.cancelling():
                cancellation = cancellation or exc
                continue
            break
        except BaseException:
            break
    try:
        result = worker.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return result
