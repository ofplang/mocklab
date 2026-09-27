"""In-process tests of lads_common over real instruments: a real asyncua server and client.

Each test builds a small LADS device around an instrument from `mock_instruments` (with no world
model wired up, so no HTTP), starts the server on a free local port, and drives it the way a LADS
client would. What they pin down is the mapping itself:

* StartProgram returns once the instrument command has begun, and the run ends in a Result;
* FunctionalUnitState follows the instrument's Status (Error -> Aborted) rather than rules of its
  own, and Clear runs the instrument's reset;
* a refusal before a command begins is a StatusCode with the reason in LastError, and changes
  nothing;
* a TargetValue write goes through the instrument's setter and is refused exactly when the SiLA2
  setter refuses.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from asyncua import Client, Node, ua

from laboratory_client import LaboratoryModelConfig
from lads_common import (
    CELSIUS,
    AnalogControlFunction,
    DeviceIdentity,
    FunctionalUnit,
    LadsDevice,
    ProgramDefinition,
    ServerContext,
    ServerSpec,
    start_server,
)
from lads_common.nodesets import LADS_URI, VENDOR_URI
from mock_instruments.plateloc import PlateLoc
from mock_instruments.runtime import CommandDurations
from mock_instruments.thermal_cycler import ThermalCycler

UNCONFIGURED = LaboratoryModelConfig(url=None, location=None)
SPEC = ServerSpec(server_type="TestServer", default_name="Test", description="test")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def serving(
    build: Callable[[ServerContext], Awaitable[None]], scenario: Callable[[Client, int, int], Awaitable[None]]
) -> None:
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/"
    server, context = await start_server(SPEC, build, endpoint=endpoint)
    async with server:
        client = Client(endpoint)
        async with client:
            await client.load_data_type_definitions()
            namespaces = await client.get_namespace_array()
            await scenario(client, namespaces.index(VENDOR_URI), namespaces.index(LADS_URI))
    await context.bridge.close()


class UnitClient:
    """Just enough of a LADS client to drive one functional unit by its NodeIds."""

    def __init__(self, client: Client, vendor: int, lads: int, path: str) -> None:
        self.client, self.vendor, self.lads, self.path = client, vendor, lads, path

    def node(self, suffix: str) -> Node:
        return self.client.get_node(ua.NodeId(f"{self.path}.{suffix}", self.vendor))

    async def state(self) -> str:
        return (await self.node("FunctionalUnitState.CurrentState").read_value()).Text

    async def last_error(self) -> str:
        return await self.node("LastError").read_value()

    async def call(self, method: str, *args: Any) -> Any:
        state = self.node("FunctionalUnitState")
        return await state.call_method(ua.QualifiedName(method, self.lads), *args)

    async def start_program(self, template_id: str, **properties: object) -> str:
        key_values = [ua.KeyValueType(Key=key, Value=str(value)) for key, value in properties.items()]
        return await self.call(
            "StartProgram",
            ua.Variant(template_id, ua.VariantType.String),
            ua.Variant(key_values, ua.VariantType.ExtensionObject),
            ua.Variant("", ua.VariantType.String),
            ua.Variant("", ua.VariantType.String),
            ua.Variant([], ua.VariantType.ExtensionObject),
        )

    async def wait_for(self, *states: str, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            state = await self.state()
            if state in states:
                return state
            if time.monotonic() > deadline:
                raise AssertionError(f"{self.path} stayed {state}, expected one of {states}")
            await asyncio.sleep(0.02)

    async def result_properties(self, run_id: str) -> dict[str, str]:
        node = self.node(f"ProgramManager.ResultSet.{run_id}.Properties")
        return {kv.Key: kv.Value for kv in await node.read_value()}


# --- PlateLoc: a program, a set-point, Stop and Clear. ---


def plateloc_build(instrument: PlateLoc) -> Callable[[ServerContext], Awaitable[None]]:
    async def build(context: ServerContext) -> None:
        device = await LadsDevice.create(
            context.builder, "PlateLoc", DeviceIdentity(manufacturer="Test", model="PlateLoc", serial_number="1"),
            asset_id="Test-PlateLoc",
        )
        unit = await FunctionalUnit.create(
            device, context.bridge, "Sealer", status=instrument.status, stop=instrument.stop_cycle,
            clear=instrument.reset,
        )
        await unit.add_program(
            ProgramDefinition(
                template_id="StartCycle",
                description="Seal the plate",
                command=lambda parameters, execution: instrument.start_cycle(execution),
            )
        )
        await AnalogControlFunction.create(
            unit,
            "SealingTemperature",
            engineering_units=CELSIUS,
            low=0,
            high=None,
            current=lambda: instrument.actual_temperature,
            target=lambda: instrument.sealing_temperature,
            set_target=lambda value: instrument.set_sealing_temperature(int(value)),
        )
        await device.mark_operational()

    return build


def test_a_program_runs_the_instrument_command_and_records_a_result() -> None:
    instrument = PlateLoc(laboratory_model=UNCONFIGURED, durations=CommandDurations({"StartCycle": 0.3}))

    async def scenario(client: Client, vendor: int, lads: int) -> None:
        unit = UnitClient(client, vendor, lads, "PlateLoc.FunctionalUnitSet.Sealer")
        assert await unit.state() == "Stopped"

        run_id = await unit.start_program("StartCycle")
        # The call returned at begin; the cycle is still sealing.
        assert await unit.state() == "Running"
        assert await unit.wait_for("Stopped") == "Stopped"
        assert (await unit.result_properties(run_id))["Outcome"] == "Completed"
        assert instrument.cycle_count == 1

        # The heater went to the set-point, and the published CurrentValue followed it.
        current = unit.node("FunctionSet.SealingTemperature.CurrentValue")
        assert await current.read_value() == 175.0

        # Stop runs StopCycle, which cools by 5 even though nothing is running.
        await unit.call("Stop")
        assert instrument.actual_temperature == 170
        assert await current.read_value() == 170.0

    asyncio.run(serving(plateloc_build(instrument), scenario))


def test_set_point_writes_go_through_the_instrument_setter() -> None:
    instrument = PlateLoc(laboratory_model=UNCONFIGURED, durations=CommandDurations())

    async def scenario(client: Client, vendor: int, lads: int) -> None:
        unit = UnitClient(client, vendor, lads, "PlateLoc.FunctionalUnitSet.Sealer")
        target = unit.node("FunctionSet.SealingTemperature.TargetValue")

        await target.write_value(ua.Variant(180.0, ua.VariantType.Double))
        assert instrument.sealing_temperature == 180
        assert await target.read_value() == 180.0

        # The SiLA2 setter refuses a negative temperature; so does the write.
        with pytest.raises(ua.UaStatusCodeError) as refused:
            await target.write_value(ua.Variant(-1.0, ua.VariantType.Double))
        assert refused.value.code == ua.StatusCodes.BadInvalidArgument
        assert instrument.sealing_temperature == 180
        await asyncio.sleep(0.05)
        assert await unit.last_error() == "Sealer.SealingTemperature.TargetValue: SealingTemperature must be >= 0"

    asyncio.run(serving(plateloc_build(instrument), scenario))


def test_a_refusal_before_the_command_begins_changes_nothing() -> None:
    instrument = PlateLoc(laboratory_model=UNCONFIGURED, durations=CommandDurations())

    async def scenario(client: Client, vendor: int, lads: int) -> None:
        unit = UnitClient(client, vendor, lads, "PlateLoc.FunctionalUnitSet.Sealer")
        # Another command holds the instrument, as a SiLA2 client's command would.
        with instrument.guard.executing("Reset"), pytest.raises(ua.UaStatusCodeError) as refused:
            await unit.start_program("StartCycle")
        assert refused.value.code == ua.StatusCodes.BadInvalidState
        assert await unit.last_error() == (
            "Sealer.StartProgram: StartCycle cannot start because Reset is still executing on this server"
        )
        assert await unit.state() == "Stopped"
        assert instrument.cycle_count == 0

        with pytest.raises(ua.UaStatusCodeError) as unknown:
            await unit.start_program("NoSuchProgram")
        assert unknown.value.code == ua.StatusCodes.BadNotFound

    asyncio.run(serving(plateloc_build(instrument), scenario))


# --- Thermal cycler: a failure after the command begins, and Clear as Reset. ---


def cycler_build(instrument: ThermalCycler) -> Callable[[ServerContext], Awaitable[None]]:
    async def build(context: ServerContext) -> None:
        device = await LadsDevice.create(
            context.builder, "Cycler", DeviceIdentity(manufacturer="Test", model="Cycler", serial_number="1"),
            asset_id="Test-Cycler",
        )
        unit = await FunctionalUnit.create(
            device, context.bridge, "Cycler", status=instrument.status, stop=instrument.stop_run,
            clear=instrument.reset,
        )
        await unit.add_program(
            ProgramDefinition(
                template_id="Validate",
                description="Validate the loaded protocol",
                command=lambda parameters, execution: instrument.validate(
                    parameters.as_float("MaxSampleVolume"), execution
                ),
            )
        )
        await device.mark_operational()

    return build


def test_a_failure_after_begin_aborts_the_unit_until_clear() -> None:
    instrument = ThermalCycler(laboratory_model=UNCONFIGURED, durations=CommandDurations())

    async def scenario(client: Client, vendor: int, lads: int) -> None:
        unit = UnitClient(client, vendor, lads, "Cycler.FunctionalUnitSet.Cycler")

        # A missing parameter is refused before the command begins.
        with pytest.raises(ua.UaStatusCodeError) as missing:
            await unit.start_program("Validate")
        assert missing.value.code == ua.StatusCodes.BadInvalidArgument
        assert await unit.state() == "Stopped"

        # Validate before Load fails during execution: the instrument goes to Error, so the unit
        # is Aborted and the run is recorded as Failed.
        run_id = await unit.start_program("Validate", MaxSampleVolume=10)
        assert await unit.wait_for("Aborted") == "Aborted"
        assert (await unit.result_properties(run_id))["Outcome"] == "Failed"
        assert await unit.last_error() == "Cycler.Validate: Load must be executed before Validate in this mock"

        # LADS refuses to start from Aborted (stricter than SiLA2); Clear runs Reset.
        with pytest.raises(ua.UaStatusCodeError) as aborted:
            await unit.start_program("Validate", MaxSampleVolume=10)
        assert aborted.value.code == ua.StatusCodes.BadInvalidState
        await unit.call("Clear")
        assert await unit.state() == "Stopped"

        # Abort + Clear is the LADS spelling of SiLA2 Reset from Idle.
        await unit.call("Abort")
        assert await unit.state() == "Aborted"
        await unit.call("Clear")
        assert await unit.state() == "Stopped"

    asyncio.run(serving(cycler_build(instrument), scenario))
