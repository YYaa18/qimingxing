from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any, Optional


class ThreadedMemoryBackend:
    """Run every backend coroutine on a dedicated event loop thread.

    The SQLite backend uses synchronous ``sqlite3`` calls guarded by an asyncio
    lock. Awaiting it from the Brain loop blocks that loop for the duration of
    every query (and the lock wait). This proxy keeps the same async interface
    but moves all backend work to its own thread, so streaming, TTS, heartbeats
    and approvals keep running while memory reads/writes happen.

    The thread is started lazily and restarted after ``close()`` so callers can
    still use the object after a lifecycle (the SQLite store also reconnects
    lazily).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None and loop.is_running():
            return loop
        with self._start_lock:
            loop = self._loop
            if loop is not None and loop.is_running():
                return loop

            ready = threading.Event()

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                # Signal only once the loop is actually running, otherwise a
                # waiter could observe is_running()==False and start a second
                # thread against the same SQLite connection.
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    loop.close()

            thread = threading.Thread(
                target=_runner,
                name="qmx-memory-backend",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            if not ready.wait(5.0):
                raise RuntimeError("memory backend thread failed to start")
            assert self._loop is not None
            return self._loop

    async def close(self) -> None:
        # Detach under the start lock so a concurrent _ensure_loop() cannot
        # reuse the loop/thread that is being shut down.
        with self._start_lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._inner.close(), loop)
            await asyncio.wrap_future(future)
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            if thread is not None:
                await asyncio.to_thread(thread.join, 5.0)

    # --------------------------------------------------------------- proxying
    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        loop = self._ensure_loop()

        async def _invoke() -> Any:
            return await getattr(self._inner, name)(*args, **kwargs)

        future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
        return await asyncio.wrap_future(future)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if inspect.iscoroutinefunction(attr):
            async def proxy(*args: Any, **kwargs: Any) -> Any:
                return await self._call(name, *args, **kwargs)

            return proxy
        return attr

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"ThreadedMemoryBackend({self._inner!r})"
