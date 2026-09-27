"""In-process test of the four LADS instrument servers' real `build()` functions.

All four devices are built into one asyncua server -- loading the NodeSets is what costs time, and
each server package only adds its own device -- and driven by a real client, with no world model
wired up. The point is the mapping each package chose: the templates it publishes, which property
names its programs take, where readouts appear, and that the instrument's rules come through
(ranges, ordering, the thermal cycler's Running-until-Stop).
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from asyncua import Client, Node, ua

import automated_plate_seal_remover_lads.server as seal_remover
import automated_thermal_cycler_lads.server as thermal_cycler
import microplate_centrifuge_lads.server as centrifuge
import plateloc_lads.server as plateloc
from lads_common import ServerContext, ServerSpec, start_server
from lads_common.nodesets import LADS_URI, VENDOR_URI

SPEC = ServerSpec(server_type="TestServer", default_name="Test", description="test")
# What CurrentState reads once Stop or Clear -- which return when their command begins -- are done.
STOPPED = ua.LocalizedText("Stopped", "en")


@pytest.fixture(autouse=True)
def no_world(monkeypatch: pytest.MonkeyPatch) -> None:
    # The instruments read their wiring from the environment; this test runs without a world.
    for variable in ("LABORATORY_MODEL_URL", "LABORATORY_MODEL_LOCATION", "COMMAND_DURATIONS_FILE"):
        monkeypatch.delenv(variable, raising=False)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class UnitClient:
    """Just enough of a LADS client to drive one functional unit by its NodeIds."""

    def __init__(self, client: Client, vendor: int, lads: int, path: str) -> None:
        self.client, self.vendor, self.lads, self.path = client, vendor, lads, path

    def node(self, suffix: str) -> Node:
        return self.client.get_node(ua.NodeId(f"{self.path}.{suffix}", self.vendor))

    async def read(self, suffix: str) -> Any:
        return await self.node(suffix).read_value()

    async def state(self) -> str:
        return (await self.read("FunctionalUnitState.CurrentState")).Text

    async def call(self, owner: str, method: str, *args: Any) -> Any:
        return await self.node(owner).call_method(ua.QualifiedName(method, self.lads), *args)

    async def templates(self) -> set[str]:
        template_set = self.node("ProgramManager.ProgramTemplateSet")
        children = await template_set.get_children(refs=ua.ObjectIds.HasComponent, nodeclassmask=ua.NodeClass.Object)
        return {(await child.read_browse_name()).Name for child in children}

    async def start_program(self, template_id: str, **properties: object) -> str:
        key_values = [ua.KeyValueType(Key=key, Value=str(value)) for key, value in properties.items()]
        return await self.call(
            "FunctionalUnitState",
            "StartProgram",
            ua.Variant(template_id, ua.VariantType.String),
            ua.Variant(key_values, ua.VariantType.ExtensionObject),
            ua.Variant("", ua.VariantType.String),
            ua.Variant("", ua.VariantType.String),
            ua.Variant([], ua.VariantType.ExtensionObject),
        )

    async def run_program(self, template_id: str, **properties: object) -> dict[str, str]:
        """Start a program, wait for its Result, and return the Result's Properties."""
        run_id = await self.start_program(template_id, **properties)
        deadline = time.monotonic() + 5
        while True:
            try:
                values = await self.read(f"ProgramManager.ResultSet.{run_id}.Properties")
                return {kv.Key: kv.Value for kv in values}
            except ua.UaStatusCodeError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.02)

    async def wait_for(self, suffix: str, expected: object) -> None:
        deadline = time.monotonic() + 5
        while (value := await self.read(suffix)) != expected:
            if time.monotonic() > deadline:
                raise AssertionError(f"{self.path}.{suffix} stayed {value!r}, expected {expected!r}")
            await asyncio.sleep(0.02)


async def refused(call: Awaitable[Any]) -> int:
    with pytest.raises(ua.UaStatusCodeError) as error:
        await call
    return error.value.code


async def check_plateloc(unit: UnitClient) -> None:
    assert await unit.templates() == {"StartCycle"}
    assert (await unit.run_program("StartCycle"))["Outcome"] == "Completed"
    await unit.wait_for("CycleCount", 1)
    # SealingTime is published in milliseconds; SetSealingTime(2.0 s) is a 2000 ms write.
    target = unit.node("FunctionSet.SealingTime.TargetValue")
    assert await target.read_value() == 1500.0
    await target.write_value(ua.Variant(2000.0, ua.VariantType.Double))
    assert await target.read_value() == 2000.0
    assert (
        await refused(target.write_value(ua.Variant(0.0, ua.VariantType.Double))) == ua.StatusCodes.BadInvalidArgument
    )
    # Whole degrees only, as SiLA2's integer parameter.
    temperature = unit.node("FunctionSet.SealingTemperature.TargetValue")
    code = await refused(temperature.write_value(ua.Variant(180.5, ua.VariantType.Double)))
    assert code == ua.StatusCodes.BadInvalidArgument
    assert await unit.read("Profiles") == ["default", "foil", "film"]


async def check_seal_remover(unit: UnitClient) -> None:
    assert await unit.templates() == {"Peel", "ResetInstrument"}
    code = await refused(unit.start_program("Peel", BeginPeelLocation=10, AdhesionTime=1))
    assert code == ua.StatusCodes.BadInvalidArgument
    assert await unit.read("LastError") == "Peeler.StartProgram: BeginPeelLocation must be in range 1..9"
    result = await unit.run_program("Peel", BeginPeelLocation=1, AdhesionTime=1)
    assert (result["Outcome"], result["InstrumentWarningMessage"]) == ("Completed", "")
    await unit.wait_for("FunctionSet.SupplySpool.SensorValue", 1199.0)
    assert await unit.read("InstrumentWarningMessage") == ""


async def check_centrifuge(unit: UnitClient) -> None:
    assert await unit.templates() == {"SpinCycle", "LoadPlate", "UnloadPlate", "Home", "Park"}
    # Every SpinCycle parameter is required, as in SiLA2.
    code = await refused(unit.start_program("SpinCycle", Time=10))
    assert code == ua.StatusCodes.BadInvalidArgument
    spin = dict.fromkeys((*centrifuge.SPIN_CYCLE_FLOATS, *centrifuge.SPIN_CYCLE_INTEGERS), 1)
    assert (await unit.run_program("SpinCycle", **spin))["Outcome"] == "Completed"
    code = await refused(
        unit.start_program("LoadPlate", BucketNumber=3, GripperOffset=0, PlateHeight=0, Speed=1, Options=0)
    )
    assert code == ua.StatusCodes.BadInvalidArgument
    assert await unit.read("LastError") == "Centrifuge.StartProgram: BucketNumber must be 1 or 2"

    door = "FunctionSet.Door.CoverState"
    assert (await unit.read(f"{door}.CurrentState")).Text == "Opened"
    await unit.call(door, "Close")
    await unit.wait_for(f"{door}.CurrentState", ua.LocalizedText("Closed", "en"))


async def check_thermal_cycler(unit: UnitClient) -> None:
    assert await unit.templates() == {"Load", "Validate", "StartRun"}
    # StartRun before Validate faults the cycler, as in SiLA2; Clear is Reset.
    assert (await unit.run_program("StartRun"))["Outcome"] == "Failed"
    assert await unit.state() == "Aborted"
    await unit.call("FunctionalUnitState", "Clear")
    await unit.wait_for("FunctionalUnitState.CurrentState", STOPPED)

    assert (await unit.run_program("Load", ProtocolFileData="protocol"))["Outcome"] == "Completed"
    assert (await unit.run_program("Validate", MaxSampleVolume=10))["Outcome"] == "Completed"
    assert (await unit.run_program("StartRun"))["Outcome"] == "Completed"
    # The run continues after StartRun returns, until Stop (StopRun).
    assert await unit.state() == "Running"
    await unit.wait_for("InstrumentState", 2)
    assert await unit.read("RemainingTime") == "00:02:00"
    await unit.call("FunctionalUnitState", "Stop")
    await unit.wait_for("FunctionalUnitState.CurrentState", STOPPED)
    await unit.wait_for("RemainingTime", "00:00:00")
    assert await unit.read("InstrumentState") == 0


def test_the_four_instrument_servers() -> None:
    builds: list[Callable[[ServerContext], Awaitable[None]]] = [
        plateloc.build,
        seal_remover.build,
        centrifuge.build,
        thermal_cycler.build,
    ]

    async def build_all(context: ServerContext) -> None:
        for build in builds:
            await build(context)

    async def scenario() -> None:
        endpoint = f"opc.tcp://127.0.0.1:{free_port()}/"
        server, context = await start_server(SPEC, build_all, endpoint=endpoint)
        async with server:
            client = Client(endpoint)
            async with client:
                await client.load_data_type_definitions()
                namespaces = await client.get_namespace_array()
                vendor, lads = namespaces.index(VENDOR_URI), namespaces.index(LADS_URI)
                for path, check in (
                    ("PlateLoc.FunctionalUnitSet.Sealer", check_plateloc),
                    ("SealRemover.FunctionalUnitSet.Peeler", check_seal_remover),
                    ("Centrifuge.FunctionalUnitSet.Centrifuge", check_centrifuge),
                    ("ThermalCycler.FunctionalUnitSet.Cycler", check_thermal_cycler),
                ):
                    await check(UnitClient(client, vendor, lads, path))
        await context.bridge.close()

    asyncio.run(scenario())
