"""Plumbing every mock instrument shares, whichever protocol serves it.

Nothing here knows what the world means. It is the machinery around a command -- how long it
takes, that only one runs at a time, how a changing value is published, and how a command tells
its caller that it has started -- which every instrument needs identically. The interpretation of
world state stays in each instrument's own module (`docs/RULES.md`).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import IntEnum
from typing import Any, Protocol, Self, TypeVar

from laboratory_client import LaboratoryModelConfig, load_laboratory_model_config

from .errors import InvalidState

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# --- Command durations. Baked into each server image at build time by slicing that device's
# --- section out of the lab-wide duration file (see `tools/slice_durations.py`); a command that
# --- is not listed there waits for nothing.
COMMAND_DURATIONS_VARIABLE = "COMMAND_DURATIONS_FILE"
DEFAULT_COMMAND_DURATIONS_FILE = "/app/command_durations.json"


def load_command_durations(path: str | None = None) -> dict[str, float]:
    """Read one device's per-command durations, keyed by plain SiLA2 command name.

    The keys are SiLA2 command names for both protocols: that is the vocabulary of the lab-wide
    duration file, and the LADS servers mock the same commands.

    A missing file is not an error: a server started outside its image has nothing baked in, and
    the rule for a command with no entry is already "do not wait"."""
    if path is None:
        path = os.getenv(COMMAND_DURATIONS_VARIABLE, DEFAULT_COMMAND_DURATIONS_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        logger.info("No command duration file at %s; commands will not wait", path)
        return {}

    commands = document.get("commands") or {}
    return {name: float(entry.get("duration", 0.0)) for name, entry in commands.items()}


class CommandDurations:
    """How long each command waits: the nominal time it takes on the instrument being mocked."""

    def __init__(self, durations: Mapping[str, float] | None = None, *, sleep: Callable[[float], None] = time.sleep):
        # `sleep` is a parameter so unit tests can record the waits instead of taking them.
        self._durations = dict(durations or {})
        self._sleep = sleep

    @classmethod
    def from_environment(cls) -> CommandDurations:
        return cls(load_command_durations())

    def duration_of(self, command_name: str) -> float:
        """The configured duration of `command_name` in seconds, 0 if none is configured. For a
        protocol that publishes an estimate before the command runs (LADS EstimatedRuntime)."""
        return self._durations.get(command_name, 0.0)

    def sleep_for(self, command_name: str, *, fraction: float = 1.0) -> None:
        """Wait (part of) the nominal time `command_name` takes.

        A command with no configured duration returns at once -- which also collapses its
        Running window, so a polling client would not observe the Status transition.

        `fraction` exists for Ardea's `Transfer`, the one command that reports progress: it
        spends its configured duration in slices, one per phase. The slices come out of that
        single configured number, so how it is divided does not change the total."""
        duration = self._durations.get(command_name, 0.0) * fraction
        if duration > 0:
            self._sleep(duration)


class ExecutionGuard:
    """Holds an instrument for one command, refusing a second one that arrives meanwhile.

    A real instrument does one thing at a time, and these mocks keep their internal state in
    plain unsynchronised attributes, so two commands running at once would interleave those
    silently. Long durations make that a realistic possibility rather than a theoretical one.

    The check is on "is a command executing", NOT on a status value. The thermal cycler's
    StartRun deliberately leaves Status at Running until StopRun, so a status-based guard would
    refuse the very command meant to end the run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executing_command: str | None = None

    @contextmanager
    def executing(self, command_name: str) -> Iterator[None]:
        with self._lock:
            if self._executing_command is not None:
                raise InvalidState(
                    f"{command_name} cannot start because {self._executing_command} is still "
                    "executing on this server"
                )
            self._executing_command = command_name
        try:
            yield
        finally:
            with self._lock:
                self._executing_command = None


def one_at_a_time(command_name: str) -> Callable[[F], F]:
    """Run the decorated command under its instrument's `ExecutionGuard`.

    A decorator rather than a `with` block inside every command body: the guard is a property of
    the command rather than more lines of it. The name is given explicitly because it is the
    SiLA2 command name (`StartCycle`), which is what the refusal message has always named, while
    the Python method is snake_case.

    Commands whose job is to stop something are deliberately NOT decorated -- making them wait for
    the thing they exist to end would be backwards."""

    def decorate(method: F) -> F:
        @functools.wraps(method)
        def wrapper(self: Instrument, *args: Any, **kwargs: Any) -> Any:
            with self.guard.executing(command_name):
                return method(self, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


class Observable[T]:
    """A value that changes over time, with listeners told of every change.

    This is how a protocol adapter learns of a new Status (or elapsed time, or carriage
    position): the SiLA2 adapter registers its generated `update_<Property>` method, the LADS
    adapter a function that writes an OPC UA variable. A listener is called on the thread that
    made the change, which is the command's own thread."""

    def __init__(self, initial: T) -> None:
        self._value = initial
        self._listeners: list[Callable[[T], None]] = []

    @property
    def value(self) -> T:
        return self._value

    def set(self, value: T) -> None:
        self._value = value
        for listener in list(self._listeners):
            listener(value)

    def subscribe(self, listener: Callable[[T], None]) -> None:
        """Register `listener` and hand it the current value at once, so a listener attached
        after the instrument was constructed does not miss the state it came up in."""
        self._listeners.append(listener)
        listener(self._value)


class Execution(Protocol):
    """What a command tells its caller while it runs.

    `begin` is called at exactly the point where the SiLA2 servers call
    `ObservableCommandInstance.begin_execution()`. That point matters and differs between
    commands: a check made before it rejects the command outright, while a failure after it
    happens during execution and, for most commands, leaves the instrument in Error. Keeping
    the call where it was is what keeps the two protocols' behaviour identical.

    `report` publishes a progress step (Ardea's Transfer phases); `progress` runs 0..1."""

    def begin(self) -> None: ...

    def report(self, phase: str, progress: float) -> None: ...


class CallbackExecution:
    """An `Execution` built from two optional callables -- enough for any adapter."""

    def __init__(
        self,
        begin: Callable[[], None] | None = None,
        report: Callable[[str, float], None] | None = None,
    ) -> None:
        self._begin = begin
        self._report = report

    def begin(self) -> None:
        if self._begin is not None:
            self._begin()

    def report(self, phase: str, progress: float) -> None:
        if self._report is not None:
            self._report(phase, progress)


# For callers with nothing to be told, such as unit tests and unobservable commands.
NO_EXECUTION: Execution = CallbackExecution()


class Status(IntEnum):
    """The `Status` property common to the four instrument features: 0=Not Connected, 1=Idle,
    2=Running, 3=Error. Ardea's features declare no such property."""

    NOT_CONNECTED = 0
    IDLE = 1
    RUNNING = 2
    ERROR = 3


class Instrument:
    """The parts of a mock instrument that are the same for every one of them: where the world
    is, how long commands take, and the one-command-at-a-time guard."""

    def __init__(self, *, laboratory_model: LaboratoryModelConfig, durations: CommandDurations) -> None:
        # `laboratory_model.location` is the single location this instrument acts on; for
        # Ardea it is the arm's own holding location.
        self.laboratory_model_url = laboratory_model.url
        self.laboratory_model_location = laboratory_model.location
        self.durations = durations
        self.guard = ExecutionGuard()

    @classmethod
    def from_environment(cls) -> Self:
        """Build from the container's configuration.

        Both world-model settings come from the environment through the shared loader, which
        rejects a variable that is set but blank or padded. That raises here, during server
        construction, so a misconfigured container fails at startup instead of at its first
        command."""
        laboratory_model = load_laboratory_model_config()
        return cls(laboratory_model=laboratory_model, durations=CommandDurations.from_environment())

    def sleep_for(self, command_name: str, *, fraction: float = 1.0) -> None:
        self.durations.sleep_for(command_name, fraction=fraction)


class StatusInstrument(Instrument):
    """An instrument whose feature has the common `Status` property."""

    def __init__(self, *, laboratory_model: LaboratoryModelConfig, durations: CommandDurations) -> None:
        super().__init__(laboratory_model=laboratory_model, durations=durations)
        # Status goes Not Connected -> Idle to mimic a real server coming online.
        self.status: Observable[Status] = Observable(Status.NOT_CONNECTED)
        self.status.set(Status.IDLE)
