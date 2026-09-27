"""
LADS FunctionalUnit runtime: FunctionalUnitState, programs and results -- over an instrument.

Mapping from the SiLA2 servers
------------------------------
A SiLA observable command whose job is a process (SpinCycle, StartCycle, Peel, StartRun, ...)
becomes a LADS *program*: a client calls `FunctionalUnitState.StartProgram(templateId, properties,
...)`, the call returns a DeviceProgramRunId once the command has begun executing, and completion
is observed through `FunctionalUnitState.CurrentState` plus a new entry in
`ProgramManager.ResultSet`. The program body *is* the instrument's command, run on a worker thread
(`bridge.InstrumentBridge`); nothing about what it does is decided here.

FunctionalUnitState is therefore not a state machine with rules of its own. It is derived from
the instrument, so it cannot disagree with what the SiLA2 server of the same instrument reports:

    instrument Status             program running      FunctionalUnitState
    3 Error (or Abort called)     -                    Aborted
    2 Running                     -                    Running
    -                             yes                  Running
    1 Idle / 0 Not Connected      no                   Stopped

(`Stopping` and `Clearing` are shown from the moment Stop or Clear has begun its instrument command
until that command completes; the methods return at the start, like StartProgram.)

Methods:
- StartProgram: only in Stopped, as LADS requires. This is stricter than SiLA2, whose commands are
  refused only while another command is executing -- a SiLA2 client may start a command on an
  instrument in Error. Parameter and precondition failures detected before the command begins are
  answered with a StatusCode and change nothing; failures after it begins end the run as Failed.
- Stop: runs the unit's stop command (e.g. PlateLoc's StopCycle). Like the SiLA2 stop commands,
  it does not interrupt a command already executing -- the mocks cannot -- and it runs even when
  nothing is running, because some stop commands have effects of their own.
- Abort: marks the unit Aborted at once. It cannot interrupt an executing command either.
- Clear: Aborted -> Stopped by running the unit's clear command, the counterpart of SiLA2 Reset.
  So SiLA2 `Reset` is LADS `Abort` + `Clear` (or just `Clear` once the instrument is in Error).

Vendor extension (namespace VENDOR_URI): `LastError` (String), the message of the last rejected or
failed call on this unit, "" after a successful one.

Adapted from the lads-test prototype, whose units kept their own state machine rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from asyncua import Node, ua

from mock_instruments.errors import InvalidArgument
from mock_instruments.runtime import Execution, Observable, Status

from .addressspace import LadsTypes, ModelBuilder
from .bridge import InstrumentBridge
from .methods import Handler, bind_method, bind_not_supported, invalid_state, message_for, not_found, status_for
from .state_machine import StateMachine

if TYPE_CHECKING:
    from .device import LadsDevice

logger = logging.getLogger(__name__)

# Keep the ResultSet bounded; a long-running mock would otherwise grow forever.
MAX_RESULTS = 20
_RUNTIME_TICK_SECONDS = 0.5


# --- Program parameters. StartProgram carries them as KeyValueType[] of strings; each program
# --- converts the ones it takes. A SiLA2 command's parameters are all required and typed, so a
# --- missing or malformed one is refused before the command starts -- on both protocols.


@dataclass(frozen=True)
class ProgramParameters:
    values: Mapping[str, str]

    def _raw(self, key: str) -> str:
        raw = self.values.get(key)
        if raw is None or raw == "":
            raise InvalidArgument(f"Property {key!r} is required")
        return raw

    def as_float(self, key: str) -> float:
        try:
            return float(self._raw(key))
        except ValueError as error:
            raise InvalidArgument(f"Property {key!r} must be a number") from error

    def as_int(self, key: str) -> int:
        value = self.as_float(key)
        if not value.is_integer():
            raise InvalidArgument(f"Property {key!r} must be an integer")
        return int(value)

    def as_str(self, key: str) -> str:
        return self._raw(key)


# What a program runs: the instrument command, given its parameters and the execution hook, on a
# worker thread. It returns the values to record in the Result's Properties (e.g. a warning).
ProgramCommand = Callable[[ProgramParameters, Execution], Mapping[str, object] | None]


@dataclass
class ProgramDefinition:
    """One entry of the ProgramTemplateSet: the instrument command a template id runs."""

    template_id: str
    description: str
    command: ProgramCommand
    # The command's configured duration in seconds, published as EstimatedRuntime; None if unknown.
    estimated_runtime_seconds: float | None = None
    author: str = "ofplang mock lab"
    version: str = "1.0"


@dataclass
class _Run:
    template_id: str
    run_id: str
    properties: dict[str, str]
    supervisory_job_id: str
    supervisory_task_id: str
    samples: list[Any]
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.monotonic)
    steps: int = 0


# A command a unit method runs (stop, clear); it takes the execution hook like any command.
UnitCommand = Callable[[Execution], object]


class FunctionalUnit:
    def __init__(self, builder: ModelBuilder, bridge: InstrumentBridge, node: Node, status: Observable[Status] | None):
        self.builder = builder
        self.server = builder.server
        self.bridge = bridge
        self.node = node
        self.name = node.nodeid.Identifier.rsplit(".", 1)[-1]
        self.state: StateMachine
        self.function_set: Node
        self.program_manager: Node
        self._status = status
        self._template_set: Node
        self._result_set: Node
        self._active_program: dict[str, Node] = {}
        self._last_error: Node
        self._programs: dict[str, ProgramDefinition] = {}
        self._template_nodes: dict[str, Node] = {}
        self._results: list[Node] = []
        self._running: _Run | None = None
        self._aborted = False
        # While Stop or Clear is carrying out its instrument command, CurrentState shows that
        # transition (Stopping, Clearing) instead of the derived state.
        self._transient: str | None = None
        self._stop_command: UnitCommand | None = None
        self._clear_command: UnitCommand | None = None
        self._refreshers: list[Callable[[], Awaitable[None]]] = []
        self._tasks: set[asyncio.Future[None]] = set()

    # -- construction ---------------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        device: LadsDevice,
        bridge: InstrumentBridge,
        name: str,
        *,
        status: Observable[Status] | None,
        stop: UnitCommand | None = None,
        clear: UnitCommand | None = None,
    ) -> FunctionalUnit:
        """Create the unit under the device's FunctionalUnitSet.

        `status` is the instrument's Status (None for an instrument without one, such as Ardea);
        `stop` and `clear` are the instrument commands the Stop and Clear methods run."""
        builder = device.builder
        node = await builder.instantiate(device.functional_unit_set, builder.lads_id(LadsTypes.FUNCTIONAL_UNIT), name)
        unit = cls(builder, bridge, node, status)
        unit._stop_command, unit._clear_command = stop, clear
        await unit._build()
        if status is not None:
            status.subscribe(bridge.listener(lambda _status: unit.refresh_state()))
        return unit

    async def _build(self) -> None:
        b = self.builder
        self._last_error = await b.add_vendor_variable(self.node, "LastError", "", ua.VariantType.String)

        # FunctionalUnitState with the program-oriented methods we implement.
        state_node = await b.lads_child(self.node, "FunctionalUnitState")
        handlers: dict[str, Handler] = {
            "StartProgram": self._start_program,
            "Stop": self._stop,
            "Abort": self._abort,
            "Clear": self._clear,
        }
        for method_name, handler in handlers.items():
            method = await b.add_optional(state_node, method_name)
            bind_method(self.server, method, handler, label=f"{self.name}.{method_name}", on_error=self.set_last_error)
        self.state = await StateMachine.attach(b, state_node)
        await self.state.set("Stopped")

        # DI LockingServices is Mandatory on LADS FunctionalUnits. The mock does not implement
        # client locking, so the methods answer BadNotSupported.
        lock = await b.find_child(self.node, ua.QualifiedName("Lock", b.ns.di))
        if lock is not None:
            for method in await lock.get_children(nodeclassmask=ua.NodeClass.Method):
                bname = await method.read_browse_name()
                bind_not_supported(self.server, method, label=f"{self.name}.Lock.{bname.Name}")
            for prop_name, value, varianttype in (
                ("Locked", False, ua.VariantType.Boolean),
                ("LockingClient", "", ua.VariantType.String),
                ("LockingUser", "", ua.VariantType.String),
                ("RemainingLockTime", 0.0, ua.VariantType.Double),
            ):
                prop = await b.find_child(lock, ua.QualifiedName(prop_name, b.ns.di))
                if prop is not None:
                    await prop.write_value(ua.Variant(value, varianttype))

        self.function_set = await b.add_optional(self.node, "FunctionSet")
        await b.bump_node_version(self.function_set)

        self.program_manager = await b.add_optional(self.node, "ProgramManager")
        self._template_set = await b.lads_child(self.program_manager, "ProgramTemplateSet")
        self._result_set = await b.lads_child(self.program_manager, "ResultSet")
        await b.bump_node_version(self._template_set)
        await b.bump_node_version(self._result_set)
        active_program = await b.lads_child(self.program_manager, "ActiveProgram")
        for name in (
            "DeviceProgramRunId",
            "CurrentRuntime",
            "EstimatedRuntime",
            "CurrentStepName",
            "CurrentStepNumber",
        ):
            self._active_program[name] = await b.add_optional(active_program, name)
        await self._reset_active_program()

    async def add_readout(
        self,
        name: str,
        read: Callable[[], object],
        varianttype: ua.VariantType,
        *,
        observable: Observable[Any] | None = None,
    ) -> Node:
        """Publish an instrument value with no LADS counterpart as a vendor variable on the unit
        (a cycle count, a profile list, a firmware version string, ...).

        `read` is called on the event loop -- instrument readouts are plain attribute reads -- and
        its value republished after every program, stop and clear. If the instrument publishes the
        value as an `Observable` (the thermal cycler's elapsed time), pass it too and every change
        is published as it happens, in order."""
        node = await self.builder.add_vendor_variable(self.node, name, read(), varianttype)

        async def publish(_value: object = None) -> None:
            await node.write_value(ua.Variant(read(), varianttype))

        self.add_refresher(publish)
        if observable is not None:
            observable.subscribe(self.bridge.listener(publish))
        return node

    def add_refresher(self, refresh: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine that republishes instrument values (a function's CurrentValue,
        ...). Every refresher runs after each program, stop and clear finishes."""
        self._refreshers.append(refresh)

    async def refresh_values(self) -> None:
        for refresh in self._refreshers:
            await refresh()

    async def set_last_error(self, message: str) -> None:
        await self._last_error.write_value(ua.Variant(message, ua.VariantType.String))

    # -- the derived state ------------------------------------------------------------------------

    def derived_state(self) -> str:
        status = self._status.value if self._status is not None else Status.IDLE
        if self._aborted or status is Status.ERROR:
            return "Aborted"
        if self._running is not None or status is Status.RUNNING:
            return "Running"
        return "Stopped"

    def current_state(self) -> str:
        """What CurrentState shows: a method's transition while one is in progress, else the
        derived state."""
        return self._transient or self.derived_state()

    async def refresh_state(self) -> None:
        await self.state.set(self.current_state())

    # -- program templates ----------------------------------------------------------------------

    async def add_program(self, program: ProgramDefinition) -> Node:
        """Register a program and publish it as a ProgramTemplate."""
        b = self.builder
        if program.template_id in self._programs:
            raise ValueError(f"Duplicate program template {program.template_id}")
        node = await b.instantiate(self._template_set, b.lads_id(LadsTypes.PROGRAM_TEMPLATE), program.template_id)
        await self._write_template_properties(node, program)
        self._programs[program.template_id] = program
        self._template_nodes[program.template_id] = node
        await b.bump_node_version(self._template_set)
        return node

    async def _write_template_properties(self, node: Node, program: ProgramDefinition) -> None:
        now = datetime.now(UTC)
        values: dict[str, tuple[object, ua.VariantType]] = {
            "Author": (program.author, ua.VariantType.String),
            "Created": (now, ua.VariantType.DateTime),
            "Modified": (now, ua.VariantType.DateTime),
            "Description": (ua.LocalizedText(program.description, "en"), ua.VariantType.LocalizedText),
            "DeviceTemplateId": (program.template_id, ua.VariantType.String),
            "Version": (program.version, ua.VariantType.String),
        }
        for prop_name, (value, varianttype) in values.items():
            prop = await self.builder.lads_child(node, prop_name)
            await prop.write_value(ua.Variant(value, varianttype))

    # -- state machine methods --------------------------------------------------------------------

    async def _start_program(
        self,
        template_id: str,
        properties: list | None,
        supervisory_job_id: str | None,
        supervisory_task_id: str | None,
        samples: list | None,
    ) -> list[ua.Variant]:
        state = self.current_state()
        if state != "Stopped":
            raise invalid_state(f"StartProgram requires state Stopped, current state is {state}")
        program = self._programs.get(template_id)
        if program is None:
            raise not_found(f"Program template {template_id!r} does not exist; available: {sorted(self._programs)}")

        run = _Run(
            template_id=template_id,
            run_id=f"{template_id}-{uuid4().hex[:8]}",
            properties={kv.Key: kv.Value for kv in (properties or [])},
            supervisory_job_id=supervisory_job_id or "",
            supervisory_task_id=supervisory_task_id or "",
            samples=list(samples or []),
        )
        logger.info("%s.StartProgram called: template=%s properties=%s", self.name, template_id, run.properties)
        parameters = ProgramParameters(run.properties)

        async def report(phase: str, progress: float) -> None:
            run.steps += 1
            await self._set_step(run.steps, phase)

        # Begin the command. A failure before it begins -- a bad parameter, the busy guard, a
        # missing item -- propagates to the caller as a StatusCode, and nothing has changed.
        started = await self.bridge.start(lambda execution: program.command(parameters, execution), on_report=report)

        self._running = run
        await self._write_active("DeviceProgramRunId", run.run_id, ua.VariantType.String)
        estimate = (program.estimated_runtime_seconds or 0.0) * 1000.0
        await self._write_active("EstimatedRuntime", estimate, ua.VariantType.Double)
        await self.refresh_state()
        # Finish in the background; the caller has its run id.
        self.server_task(self._finish_program(program, run, started.completion), name=run.run_id)
        return [ua.Variant(run.run_id, ua.VariantType.String)]

    def server_task(self, coroutine: Awaitable[None], *, name: str) -> None:
        task = asyncio.ensure_future(coroutine)
        task.set_name(name)

    # Stop and Clear run instrument commands that take time (StopCycle, Reset, ...), and asyncua
    # answers the requests of one connection in order: a method call that waited for its command
    # would hold up everything else that client sends, including the keep-alive reads it uses to
    # decide the server is still there. So, like StartProgram, they return once their command has
    # begun and show Stopping / Clearing until it completes -- the LADS way of reporting a
    # transition anyway.

    async def _stop(self) -> None:
        logger.info("%s.Stop called", self.name)
        state = self.current_state()
        if state not in ("Stopped", "Running"):
            raise invalid_state(f"Stop is not allowed in state {state}")
        if self._stop_command is None:
            return
        await self._start_transition("Stop", "Stopping", self._stop_command)

    async def _abort(self) -> None:
        # Nothing to interrupt: the mocks cannot stop a command already executing. Abort only
        # marks the unit, which then needs Clear.
        logger.info("%s.Abort called", self.name)
        self._aborted = True
        await self.refresh_state()

    async def _clear(self) -> None:
        logger.info("%s.Clear called", self.name)
        state = self.current_state()
        if state != "Aborted":
            raise invalid_state(f"Clear requires state Aborted, current state is {state}")
        if self._clear_command is None:
            await self._cleared()
            return
        # A clear the instrument refuses before it begins (a command is still executing) is the
        # call's StatusCode; one that fails after it begins leaves the unit Aborted.
        await self._start_transition("Clear", "Clearing", self._clear_command, on_success=self._cleared)

    async def _cleared(self) -> None:
        self._aborted = False
        await self._reset_active_program()
        await self.refresh_state()

    async def _start_transition(
        self,
        method: str,
        transient: str,
        command: UnitCommand,
        *,
        on_success: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        started = await self.bridge.start(command)
        self._transient = transient
        await self.refresh_state()
        self.server_task(self._finish_transition(method, started.completion, on_success), name=f"{self.name}.{method}")

    async def _finish_transition(
        self, method: str, completion: Awaitable[Any], on_success: Callable[[], Awaitable[None]] | None
    ) -> None:
        try:
            await completion
        except Exception as error:
            logger.warning("%s.%s failed: %s", self.name, method, error)
            await self.set_last_error(message_for(f"{self.name}.{method}", error))
        else:
            if on_success is not None:
                await on_success()
        finally:
            self._transient = None
            # Settle what the instrument published meanwhile before deriving the final state.
            await self.bridge.flush()
            await self.refresh_state()
            await self.refresh_values()

    # -- program execution --------------------------------------------------------------------------

    async def _finish_program(self, program: ProgramDefinition, run: _Run, completion: Awaitable[Any]) -> None:
        ticker = asyncio.create_task(self._tick_runtime(run.started_monotonic))
        outcome = "Completed"
        outputs: Mapping[str, object] | None = None
        try:
            outputs = await completion
        except Exception as error:
            outcome = "Failed"
            label = f"{self.name}.{run.template_id}"
            if status_for(error) == ua.StatusCodes.BadInternalError:
                logger.exception("%s program %s failed", self.name, run.run_id)
            else:
                logger.warning("%s program %s failed: %s", self.name, run.run_id, error)
            await self.set_last_error(message_for(label, error))
        finally:
            ticker.cancel()

        runtime_ms = (time.monotonic() - run.started_monotonic) * 1000.0
        await self._write_active("CurrentRuntime", runtime_ms, ua.VariantType.Double)
        logger.info("%s program %s finished: %s", self.name, run.run_id, outcome)
        if outcome == "Completed":
            await self.set_last_error("")
        # Settle everything the run changed -- the state, and the values it moved (a cycle count,
        # a heater temperature) -- before the Result appears: a client takes the Result as the
        # signal that the run is over, and must not then read a value from before it.
        self._running = None
        await self.bridge.flush()
        await self.refresh_state()
        await self.refresh_values()
        result_properties = {key: str(value) for key, value in (outputs or {}).items()}
        try:
            await self._record_result(program, run, runtime_ms, outcome, result_properties)
        except Exception:
            logger.exception("%s failed to record result for %s", self.name, run.run_id)

    async def _tick_runtime(self, started_monotonic: float) -> None:
        while True:
            elapsed_ms = (time.monotonic() - started_monotonic) * 1000.0
            await self._write_active("CurrentRuntime", elapsed_ms, ua.VariantType.Double)
            await asyncio.sleep(_RUNTIME_TICK_SECONDS)

    async def _set_step(self, number: int, name: str) -> None:
        await self._write_active("CurrentStepNumber", number, ua.VariantType.UInt32)
        await self._write_active("CurrentStepName", ua.LocalizedText(name, "en"), ua.VariantType.LocalizedText)

    async def _write_active(self, prop: str, value: object, varianttype: ua.VariantType) -> None:
        await self._active_program[prop].write_value(ua.Variant(value, varianttype))

    async def _reset_active_program(self) -> None:
        await self._write_active("DeviceProgramRunId", "", ua.VariantType.String)
        await self._write_active("CurrentRuntime", 0.0, ua.VariantType.Double)
        await self._write_active("EstimatedRuntime", 0.0, ua.VariantType.Double)
        await self._set_step(0, "")

    async def _record_result(
        self,
        program: ProgramDefinition,
        run: _Run,
        runtime_ms: float,
        outcome: str,
        result_properties: dict[str, str],
    ) -> None:
        """Append a ResultType entry; its Properties carry inputs, outputs and the outcome."""
        b = self.builder
        node = await b.instantiate(self._result_set, b.lads_id(LadsTypes.RESULT), run.run_id)

        merged = {**run.properties, **result_properties, "Outcome": outcome}
        key_values = [ua.KeyValueType(Key=key, Value=value) for key, value in merged.items()]
        description = ua.LocalizedText(f"{program.template_id}: {outcome}", "en")
        values: dict[str, tuple[object, ua.VariantType]] = {
            "ApplicationUri": (await self._application_uri(), ua.VariantType.String),
            "Description": (description, ua.VariantType.LocalizedText),
            "SupervisoryJobId": (run.supervisory_job_id, ua.VariantType.String),
            "SupervisoryTaskId": (run.supervisory_task_id, ua.VariantType.String),
            "Samples": (run.samples, ua.VariantType.ExtensionObject),
            "Started": (run.started, ua.VariantType.DateTime),
            "Stopped": (datetime.now(UTC), ua.VariantType.DateTime),
            "User": ("", ua.VariantType.String),
            "Properties": (key_values, ua.VariantType.ExtensionObject),
        }
        for prop_name, (value, varianttype) in values.items():
            prop = await b.lads_child(node, prop_name)
            await prop.write_value(ua.Variant(value, varianttype))
        for prop_name, (value, varianttype) in {
            "DeviceProgramRunId": (run.run_id, ua.VariantType.String),
            "TotalRuntime": (runtime_ms, ua.VariantType.Double),
        }.items():
            prop = await b.add_optional(node, prop_name)
            await prop.write_value(ua.Variant(value, varianttype))
        await self._write_template_properties(await b.lads_child(node, "ProgramTemplate"), program)

        self._results.append(node)
        while len(self._results) > MAX_RESULTS:
            await self.server.delete_nodes([self._results.pop(0)], recursive=True)
        await b.bump_node_version(self._result_set)

    async def _application_uri(self) -> str:
        uris = await self.server.get_namespace_array()
        return uris[1] if len(uris) > 1 else ""
