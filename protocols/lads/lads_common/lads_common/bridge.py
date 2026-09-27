"""
The seam between the blocking instruments and the asyncio OPC UA server.

The instruments (`mock_instruments`) are written synchronously -- a command sleeps for its
configured duration and talks to the world model over blocking HTTP -- because the SiLA2 servers
run each command on a thread. asyncua is asyncio, and a blocking call on its event loop would
freeze every client. This module is how the two meet:

- `InstrumentBridge.start` runs one instrument command on a worker thread and returns as soon as
  the command *begins* executing (`execution.begin()`), or finishes, or fails -- whichever comes
  first. That is exactly the split an OPC UA program needs: a failure before execution begins is
  answered to the caller of the method, one after it is a failed run. The same split is where the
  SiLA2 servers call `begin_execution()`, so both protocols refuse and fault on the same checks.
- `InstrumentBridge.post` carries work that a worker thread wants done on the event loop --
  publishing a new Status, a progress step, an observable value -- and does it in the order it
  was posted, so a client never sees Idle overtaken by a Running that was set before it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mock_instruments.runtime import CallbackExecution, Execution

logger = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]
Reporter = Callable[[str, float], Awaitable[None]]


@dataclass
class Started[T]:
    """A command that has begun executing (or has already finished).

    `completion` resolves to the command's return value, or raises what it raised."""

    completion: asyncio.Future[T]


class InstrumentBridge:
    """Bound to one running event loop; create it inside the server's coroutine."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[tuple[Job | None, asyncio.Future[None] | None]] = asyncio.Queue()
        self._writer = asyncio.create_task(self._drain(), name="instrument-bridge")

    # -- work posted from any thread ------------------------------------------------------------

    def post(self, job: Job) -> None:
        """Run `job()` on the event loop after everything posted before it. Callable from any
        thread, including the loop's own."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (job, None))

    def listener[V](self, publish: Callable[[V], Awaitable[None]]) -> Callable[[V], None]:
        """Adapt an async publisher into an `Observable` listener, which instruments call
        synchronously on whatever thread changed the value."""

        def listen(value: V) -> None:
            self.post(lambda: publish(value))

        return listen

    async def flush(self) -> None:
        """Wait until everything posted so far has been done."""
        marker: asyncio.Future[None] = self._loop.create_future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (None, marker))
        await marker

    async def _drain(self) -> None:
        while True:
            job, marker = await self._queue.get()
            if marker is not None:
                marker.set_result(None)
                continue
            assert job is not None
            try:
                await job()
            except Exception:
                # One failed publication must not stop the ones after it.
                logger.exception("Posted job failed")

    # -- running instrument commands ------------------------------------------------------------

    async def call[T](self, function: Callable[..., T], *args: Any) -> T:
        """Run a quick blocking call (a setter, a read) on a worker thread and wait for it."""
        return await asyncio.to_thread(function, *args)

    async def start[T](
        self,
        command: Callable[[Execution], T],
        *,
        on_report: Reporter | None = None,
    ) -> Started[T]:
        """Run `command(execution)` on a worker thread; return once it has begun executing.

        If the command fails before it begins, this raises that failure. If it finishes without
        ever calling `execution.begin()`, the finished command is returned. `on_report` receives
        the command's progress steps, in order, on the event loop."""
        begun: asyncio.Future[None] = self._loop.create_future()

        def begin() -> None:
            self._loop.call_soon_threadsafe(_resolve, begun)

        def report(phase: str, progress: float) -> None:
            if on_report is not None:
                self.post(lambda: on_report(phase, progress))

        execution = CallbackExecution(begin=begin, report=report)
        completion: asyncio.Future[T] = asyncio.ensure_future(asyncio.to_thread(command, execution))
        either: set[asyncio.Future[Any]] = {begun, completion}
        await asyncio.wait(either, return_when=asyncio.FIRST_COMPLETED)
        if not begun.done():
            begun.cancel()
            # Finished or failed without beginning: a failure here is the caller's to answer.
            completion.result()
        return Started(completion)

    async def close(self) -> None:
        self._writer.cancel()


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
