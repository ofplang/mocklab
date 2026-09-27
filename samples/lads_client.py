"""A small LADS OPC UA client for the LADS sample scripts in this directory.

It is the LADS counterpart of the SiLA2 half of `common.py`: connect to one LADS server, address
its single device's functional unit by name, and drive it the way any LADS client would --
`StartProgram` and wait for the Result, Stop/Abort/Clear, open and close a cover, write a
set-point, read readouts. A failed call is raised with the unit's `LastError`, because an OPC UA
StatusCode alone does not say why.

The laboratory-model helpers are shared with the SiLA2 samples and stay in `common.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncua import Client, Node, ua

from common import DEFAULT_LABORATORY_MODEL_URL

LADS_URI = "http://opcfoundation.org/UA/LADS/"
VENDOR_URI = "http://ofplang.org/UA/MockLab/"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 10.0


def build_parser(description: str, default_port: int, default_location: str) -> argparse.ArgumentParser:
    """Base command line shared by every LADS sample."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--host", default=DEFAULT_HOST, help="LADS server host")
    parser.add_argument("--port", type=int, default=default_port, help="LADS server port")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout in seconds per program")
    parser.add_argument(
        "--laboratory-model-url", default=DEFAULT_LABORATORY_MODEL_URL, help="Laboratory model base URL"
    )
    parser.add_argument("--laboratory-model-location", default=default_location, help="Location the server acts on")
    return parser


class LadsCallError(RuntimeError):
    """A LADS method call answered with a Bad StatusCode; carries the unit's LastError."""

    def __init__(self, label: str, status: str, last_error: str) -> None:
        super().__init__(f"{label} failed with {status}: {last_error}")
        self.status = status
        self.last_error = last_error


class Unit:
    """One functional unit of the device a LADS server exposes, addressed by NodeId."""

    def __init__(self, client: Client, vendor: int, lads: int, device: str, unit: str, timeout: float) -> None:
        self.client, self.vendor, self.lads, self.timeout = client, vendor, lads, timeout
        self.path = f"{device}.FunctionalUnitSet.{unit}"
        self.name = unit

    def node(self, suffix: str) -> Node:
        return self.client.get_node(ua.NodeId(f"{self.path}.{suffix}", self.vendor))

    async def read(self, suffix: str) -> Any:
        return await self.node(suffix).read_value()

    async def state(self, owner: str = "FunctionalUnitState") -> str:
        return (await self.read(f"{owner}.CurrentState")).Text

    async def last_error(self) -> str:
        return await self.read("LastError")

    async def call(self, owner: str, method: str, *args: Any) -> Any:
        label = f"{self.name}.{method}"
        try:
            return await self.node(owner).call_method(ua.QualifiedName(method, self.lads), *args)
        except ua.UaStatusCodeError as error:
            raise LadsCallError(label, type(error).__name__, await self.last_error()) from error

    async def wait_for_state(self, *states: str, owner: str = "FunctionalUnitState") -> str:
        deadline = time.monotonic() + self.timeout
        while (state := await self.state(owner)) not in states:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.path}.{owner} stayed {state}, expected one of {states}")
            await asyncio.sleep(0.05)
        return state

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
        """StartProgram, wait for the run's Result, and return its Properties (with `Outcome`)."""
        run_id = await self.start_program(template_id, **properties)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                values = await self.read(f"ProgramManager.ResultSet.{run_id}.Properties")
                return {kv.Key: kv.Value for kv in values}
            except ua.UaStatusCodeError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{template_id} ({run_id}) recorded no result") from None
                await asyncio.sleep(0.05)

    async def stop(self) -> str:
        """Stop, and wait for the stop command to finish (Stop returns once it has begun)."""
        await self.call("FunctionalUnitState", "Stop")
        return await self.wait_for_state("Stopped", "Running", "Aborted")

    async def clear(self) -> str:
        """Clear, and wait for the clear command (the instrument's reset) to finish."""
        await self.call("FunctionalUnitState", "Clear")
        return await self.wait_for_state("Stopped", "Aborted")

    async def reset(self) -> None:
        """The LADS spelling of SiLA2 Reset: Abort, then Clear."""
        await self.call("FunctionalUnitState", "Abort")
        expect(await self.clear() == "Stopped", f"{self.name} did not clear")

    async def cover(self, name: str, method: str) -> str:
        """Open or Close a cover and wait for it to settle; returns the settled state."""
        owner = f"FunctionSet.{name}.CoverState"
        await self.call(owner, method)
        return await self.wait_for_state("Opened", "Closed", owner=owner)

    async def write_target(self, function: str, value: float) -> None:
        await self.node(f"FunctionSet.{function}.TargetValue").write_value(
            ua.Variant(float(value), ua.VariantType.Double)
        )


@asynccontextmanager
async def connect(host: str, port: int, device: str, unit: str, *, timeout: float) -> AsyncIterator[Unit]:
    """Connect to a LADS server and yield its device's unit."""
    client = Client(f"opc.tcp://{host}:{port}/")
    async with client:
        # Generates the LADS structure classes (ua.KeyValueType, ...) the calls encode.
        await client.load_data_type_definitions()
        namespaces = await client.get_namespace_array()
        unit_client = Unit(client, namespaces.index(VENDOR_URI), namespaces.index(LADS_URI), device, unit, timeout)
        print(f"Connected to {device} at opc.tcp://{host}:{port}/")
        yield unit_client


def expect(condition: bool, message: str) -> None:
    """Fail the sample (non-zero exit) unless `condition` holds."""
    if not condition:
        raise AssertionError(message)
