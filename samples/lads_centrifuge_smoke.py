"""Smoke test for the microplate centrifuge LADS OPC UA server.

The LADS counterpart of `microplate_centrifuge_server_smoke.py`: the door is a cover function
whose Close and Open lock and unlock the centrifuge's deck in the world model, and SpinCycle,
LoadPlate, UnloadPlate, Home and Park are programs taking the SiLA2 parameters as properties.

Prerequisite: the stack is up with the `lads` profile. Exit code 0 means the sequence passed.
"""

from __future__ import annotations

import argparse
import asyncio

from common import ensure_item_at_location, get_location_state
from lads_client import LadsCallError, build_parser, connect, expect

DEFAULT_PORT = 4841
DEFAULT_LOCATION = "centrifuge.deck"

SPIN_CYCLE: dict[str, object] = {
    "VelocityPercent": 80.0,
    "AccelerationPercent": 50.0,
    "DecelerationPercent": 50.0,
    "TimerMode": 0,
    "Time": 30,
    "BucketNumberToLoad": 1,
    "BucketNumberToUnload": 1,
    "GripperOffsetToLoad": 0.0,
    "GripperOffsetToUnload": 0.0,
    "PlateHeightToLoad": 14.0,
    "PlateHeightToUnload": 14.0,
    "SpeedToLoad": 1,
    "SpeedToUnload": 1,
    "OptionsToLoad": 0,
    "OptionsToUnload": 0,
}
PLATE: dict[str, object] = {"BucketNumber": 1, "GripperOffset": 0.0, "PlateHeight": 14.0, "Speed": 1, "Options": 0}


async def run(args: argparse.Namespace) -> None:
    url, location = args.laboratory_model_url, args.laboratory_model_location
    ensure_item_at_location(laboratory_model_url=url, location=location)
    async with connect(args.host, args.port, "Centrifuge", "Centrifuge", timeout=args.timeout) as centrifuge:
        await centrifuge.reset()
        print(f"Profiles: {await centrifuge.read('Profiles')}")

        # The door drives the deck's accessibility in the world model.
        expect(await centrifuge.cover("Door", "Close") == "Closed", "door did not close")
        expect(get_location_state(laboratory_model_url=url, location=location)["accessible"] is False, "deck open")
        expect(await centrifuge.cover("Door", "Open") == "Opened", "door did not open")
        expect(get_location_state(laboratory_model_url=url, location=location)["accessible"] is True, "deck locked")

        # SiLA2 requires every SpinCycle parameter; so does the program.
        try:
            await centrifuge.start_program("SpinCycle", Time=30)
            raise AssertionError("SpinCycle with one parameter was accepted")
        except LadsCallError as error:
            print(f"Incomplete SpinCycle refused as expected: {error}")

        for template_id, properties in (
            ("SpinCycle", SPIN_CYCLE),
            ("LoadPlate", PLATE),
            ("UnloadPlate", PLATE),
            ("Home", {}),
            ("Park", {}),
        ):
            result = await centrifuge.run_program(template_id, **properties)
            print(f"{template_id}: {result['Outcome']}")
            expect(result["Outcome"] == "Completed", f"{template_id} did not complete")

        await centrifuge.stop()
        await centrifuge.reset()
        expect(await centrifuge.state() == "Stopped", "centrifuge not Stopped after reset")
        print("Centrifuge LADS smoke test passed.")


def main() -> int:
    parser = build_parser("Smoke test the centrifuge LADS server.", DEFAULT_PORT, DEFAULT_LOCATION)
    asyncio.run(run(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
