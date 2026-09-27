"""Smoke test for the automated thermal cycler LADS OPC UA server.

The LADS counterpart of `sila2_thermal_cycler_smoke.py`: Load, Validate and StartRun
are three programs in the order the instrument enforces; the run keeps the unit Running until
Stop (StopRun); the lid is a cover function that locks and unlocks the block in the world model.
Breaking the order faults the cycler exactly as over SiLA2, and Clear recovers it.

Prerequisite: the stack is up with the `lads` profile. Exit code 0 means the sequence passed.
"""

from __future__ import annotations

import argparse
import asyncio

from common import ensure_item_at_location, get_location_state
from lads_client import build_parser, connect, expect

DEFAULT_PORT = 4844
DEFAULT_LOCATION = "thermal-cycler.block"


async def run(args: argparse.Namespace) -> None:
    url, location = args.laboratory_model_url, args.laboratory_model_location
    ensure_item_at_location(laboratory_model_url=url, location=location)
    async with connect(args.host, args.port, "ThermalCycler", "Cycler", timeout=args.timeout) as cycler:
        await cycler.reset()

        # Out of order: StartRun before Validate fails during execution and faults the cycler.
        result = await cycler.run_program("StartRun")
        expect(result["Outcome"] == "Failed", "StartRun before Validate did not fail")
        expect(await cycler.state() == "Aborted", "a failed run did not abort the unit")
        print(f"StartRun before Validate failed as expected: {await cycler.last_error()}")
        await cycler.clear()

        # The lid drives the block's accessibility in the world model.
        expect(await cycler.cover("Lid", "Close") == "Closed", "lid did not close")
        expect(get_location_state(laboratory_model_url=url, location=location)["accessible"] is False, "block open")

        for template_id, properties in (
            ("Load", {"ProtocolFileData": "mock protocol"}),
            ("Validate", {"MaxSampleVolume": 20.0}),
            ("StartRun", {}),
        ):
            result = await cycler.run_program(template_id, **properties)
            print(f"{template_id}: {result['Outcome']}")
            expect(result["Outcome"] == "Completed", f"{template_id} did not complete")

        # The run continues after StartRun completes, until Stop.
        expect(await cycler.state() == "Running", "the unit is not Running during the run")
        expect(await cycler.read("InstrumentState") == 2, "InstrumentState is not RUNNING")
        print(f"Remaining time: {await cycler.read('RemainingTime')}")
        await cycler.stop()
        expect(await cycler.state() == "Stopped", "Stop did not end the run")
        expect(await cycler.read("InstrumentState") == 0, "InstrumentState is not IDLE after Stop")

        expect(await cycler.cover("Lid", "Open") == "Opened", "lid did not open")
        expect(get_location_state(laboratory_model_url=url, location=location)["accessible"] is True, "block locked")
        await cycler.reset()
        print("Thermal cycler LADS smoke test passed.")


def main() -> int:
    parser = build_parser("Smoke test the thermal cycler LADS server.", DEFAULT_PORT, DEFAULT_LOCATION)
    asyncio.run(run(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
