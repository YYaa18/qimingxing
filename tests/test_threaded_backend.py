"""The threaded memory proxy must keep the caller loop responsive and be reusable."""
from __future__ import annotations

import asyncio

import pytest

from brain.memory.threaded import ThreadedMemoryBackend


class _SlowBackend:
    def __init__(self) -> None:
        self.closed = False

    async def work(self) -> int:
        await asyncio.sleep(0.15)
        return 42

    async def boom(self) -> None:
        raise ValueError("backend failure")

    async def close(self) -> None:
        self.closed = True


async def test_threaded_backend_keeps_caller_loop_responsive() -> None:
    inner = _SlowBackend()
    backend = ThreadedMemoryBackend(inner)

    task = asyncio.create_task(backend.work())
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.01)

    assert await task == 42
    assert ticks >= 8, "caller loop was blocked by backend work"
    await backend.close()
    assert inner.closed


async def test_threaded_backend_restarts_after_close_and_propagates_errors() -> None:
    inner = _SlowBackend()
    backend = ThreadedMemoryBackend(inner)
    await backend.close()

    # SQLiteMemoryStore reconnects lazily; the proxy must be reusable too.
    assert await backend.work() == 42
    with pytest.raises(ValueError, match="backend failure"):
        await backend.boom()
    await backend.close()
