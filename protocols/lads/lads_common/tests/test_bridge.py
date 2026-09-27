"""Tests for the bridge between the blocking instruments and asyncio.

The property that matters is *when* `start` returns: at `execution.begin()`, so that a failure
before it is the caller's and a failure after it belongs to the run -- the same split the SiLA2
servers make at `begin_execution()`. And posted work must happen in order, or a client could see a
Status overtaken by an older one.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

import pytest

from lads_common.bridge import InstrumentBridge
from mock_instruments.errors import InvalidArgument, InvalidState
from mock_instruments.runtime import Execution


def run(coroutine_function: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coroutine_function())


def test_start_returns_at_begin_and_completion_carries_the_result() -> None:
    release = threading.Event()

    def command(execution: Execution) -> str:
        execution.begin()
        # Still running when start() returns: it only waits for begin.
        release.wait(timeout=5)
        return "done"

    async def scenario() -> None:
        bridge = InstrumentBridge()
        started = await bridge.start(command)
        assert not started.completion.done()
        release.set()
        assert await started.completion == "done"
        await bridge.close()

    run(scenario)


def test_a_failure_before_begin_is_raised_by_start() -> None:
    def command(execution: Execution) -> None:
        raise InvalidArgument("BucketNumber must be 1 or 2")

    async def scenario() -> None:
        bridge = InstrumentBridge()
        with pytest.raises(InvalidArgument, match="BucketNumber"):
            await bridge.start(command)
        await bridge.close()

    run(scenario)


def test_a_failure_after_begin_belongs_to_the_completion() -> None:
    def command(execution: Execution) -> None:
        execution.begin()
        raise InvalidState("Load must be executed before Validate in this mock")

    async def scenario() -> None:
        bridge = InstrumentBridge()
        started = await bridge.start(command)
        with pytest.raises(InvalidState):
            await started.completion
        await bridge.close()

    run(scenario)


def test_a_command_that_never_begins_is_returned_finished() -> None:
    async def scenario() -> None:
        bridge = InstrumentBridge()
        started = await bridge.start(lambda execution: 42)
        assert await started.completion == 42
        await bridge.close()

    run(scenario)


def test_posted_work_and_reports_keep_their_order() -> None:
    seen: list[str] = []

    async def scenario() -> None:
        bridge = InstrumentBridge()
        publish = bridge.listener(lambda value: _append(seen, f"status {value}"))

        def command(execution: Execution) -> None:
            publish("RUNNING")
            execution.begin()
            for step in range(3):
                execution.report(f"phase {step}", (step + 1) / 3)
            publish("IDLE")

        async def on_report(phase: str, progress: float) -> None:
            seen.append(phase)

        started = await bridge.start(command, on_report=on_report)
        await started.completion
        await bridge.flush()
        await bridge.close()

    run(scenario)
    assert seen == ["status RUNNING", "phase 0", "phase 1", "phase 2", "status IDLE"]


async def _append(seen: list[str], item: str) -> None:
    seen.append(item)
