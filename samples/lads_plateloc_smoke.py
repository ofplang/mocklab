"""Smoke test for the PlateLoc (plate sealer) LADS OPC UA server.

The LADS counterpart of `plateloc_server_smoke.py`, against the same instrument behaviour: the
sealing set-points are written as the SealingTemperature / SealingTime functions' TargetValue, a
seal is the StartCycle program, and the cycle count is a vendor readout. It also checks the one
world rule the sealer has -- no plate, no cycle -- which LADS reports as BadInvalidState with the
reason in LastError.

Prerequisite: the stack is up with the `lads` profile. Exit code 0 means the sequence passed.
"""

from __future__ import annotations

import argparse
import asyncio

from asyncua import ua

from common import ensure_item_at_location, reset_laboratory_model
from lads_client import LadsCallError, build_parser, connect, expect

DEFAULT_PORT = 4842
DEFAULT_LOCATION = "plateloc.stage"


async def run(args: argparse.Namespace) -> None:
    url, location = args.laboratory_model_url, args.laboratory_model_location
    async with connect(args.host, args.port, "PlateLoc", "Sealer", timeout=args.timeout) as sealer:
        # Start from power-on defaults, whatever an earlier run left behind.
        await sealer.reset()

        # No plate: the cycle is refused before it starts, and nothing changes.
        reset_laboratory_model(laboratory_model_url=url)
        try:
            await sealer.start_program("StartCycle")
            raise AssertionError("StartCycle without a plate was accepted")
        except LadsCallError as error:
            print(f"StartCycle without a plate refused as expected: {error}")
            expect(error.status == "BadInvalidState", f"unexpected status {error.status}")
            expect("requires an item" in error.last_error, f"unexpected LastError {error.last_error!r}")
        expect(await sealer.state() == "Stopped", "a refused program changed the unit's state")

        ensure_item_at_location(laboratory_model_url=url, location=location)
        print(f"Initial sealing temperature: {await sealer.read('FunctionSet.SealingTemperature.TargetValue')}")
        print(f"Initial sealing time (ms): {await sealer.read('FunctionSet.SealingTime.TargetValue')}")
        print(f"Profiles: {await sealer.read('Profiles')}")

        # The setters, as TargetValue writes; a value SiLA2 refuses is a refused write.
        await sealer.write_target("SealingTemperature", 180)
        await sealer.write_target("SealingTime", 2000)
        expect(await sealer.read("FunctionSet.SealingTemperature.TargetValue") == 180.0, "temperature not set")
        try:
            await sealer.write_target("SealingTemperature", -1)
            raise AssertionError("a negative sealing temperature was accepted")
        except ua.UaStatusCodeError as error:
            print(f"Negative temperature refused as expected: {type(error).__name__}")

        result = await sealer.run_program("StartCycle")
        print(f"StartCycle result: {result}")
        expect(result["Outcome"] == "Completed", "StartCycle did not complete")
        expect(await sealer.read("CycleCount") == 1, "cycle count did not advance")
        expect(await sealer.read("FunctionSet.SealingTemperature.CurrentValue") == 180.0, "heater not at set-point")

        # Stop is StopCycle: it cools the heater by 5 degrees even with nothing running.
        await sealer.stop()
        expect(await sealer.read("FunctionSet.SealingTemperature.CurrentValue") == 175.0, "Stop did not cool")

        await sealer.reset()
        expect(await sealer.read("CycleCount") == 0, "reset did not clear the cycle count")
        expect(await sealer.read("FunctionSet.SealingTime.TargetValue") == 1500.0, "reset did not restore the time")
        print("PlateLoc LADS smoke test passed.")


def main() -> int:
    parser = build_parser("Smoke test the PlateLoc LADS server.", DEFAULT_PORT, DEFAULT_LOCATION)
    asyncio.run(run(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
