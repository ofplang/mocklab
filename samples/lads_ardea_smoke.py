"""Smoke test for Ardea, the transporter, over LADS OPC UA.

The LADS counterpart of `sila2_ardea_smoke.py`: the station names and the machine light are vendor
variables, the carriage position a sensor function, and a transfer the Transfer program -- whose
seven phases show as ActiveProgram.CurrentStepName while it runs, and whose responses land in the
Result. A transfer naming a station the machine does not have fails after it has begun, as SiLA2's
InvalidStation does.

Prerequisite: the stack is up with the `lads` profile. Exit code 0 means the sequence passed.
"""

from __future__ import annotations

import argparse
import asyncio

from common import add_item_to_location, get_location_state, reset_laboratory_model
from lads_client import build_parser, connect, expect

DEFAULT_PORT = 4847
# The arm's own holding location (LABORATORY_MODEL_LOCATION of the Ardea service).
DEFAULT_LOCATION = "ardea.gripper"
SOURCE = ("Base1", "station.slot1")
DESTINATION = ("Base2", "station.slot2")


async def run(args: argparse.Namespace) -> None:
    url = args.laboratory_model_url
    reset_laboratory_model(laboratory_model_url=url)
    add_item_to_location(laboratory_model_url=url, location=SOURCE[1])

    async with connect(args.host, args.port, "Ardea", "Transporter", timeout=args.timeout) as ardea:
        stations = await ardea.read("StationNames")
        print(f"Station names: {stations}")
        expect(SOURCE[0] in stations and DESTINATION[0] in stations, "the station map lacks Base1/Base2")
        print(f"Light is on: {await ardea.read('LightIsOn')}")
        print(f"Carriage position: {await ardea.read('FunctionSet.Carriage.SensorValue')} mm")

        # An unknown station: the run fails, the reason names the known stations, nothing moves.
        result = await ardea.run_program("Transfer", SourceStation=SOURCE[0], DestinationStation="Nowhere")
        expect(result["Outcome"] == "Failed", "a transfer to an unknown station did not fail")
        print(f"Unknown station refused as expected: {await ardea.last_error()}")
        expect(get_location_state(laboratory_model_url=url, location=SOURCE[1])["occupied"] is True, "plate moved")

        # A real transfer, watching its phases while it runs (with the default profile it may be
        # over before a phase is seen; with the realistic one each phase lasts seconds).
        run_id = await ardea.start_program("Transfer", SourceStation=SOURCE[0], DestinationStation=DESTINATION[0])
        phases: list[str] = []
        while await ardea.state() == "Running":
            phase = (await ardea.read("ProgramManager.ActiveProgram.CurrentStepName")).Text
            if phase and (not phases or phases[-1] != phase):
                phases.append(phase)
                print(f"  phase: {phase}")
            await asyncio.sleep(0.1)
        properties = {kv.Key: kv.Value for kv in await ardea.read(f"ProgramManager.ResultSet.{run_id}.Properties")}
        print(f"Transfer result: {properties}")
        expect(properties["Outcome"] == "Completed", f"transfer failed: {await ardea.last_error()}")
        expect(properties["AtRetractPose"] == "True", "the arm did not end retracted")

        # The world agrees: the plate left the source, reached the destination, and is not in the arm.
        expect(get_location_state(laboratory_model_url=url, location=SOURCE[1])["occupied"] is False, "source full")
        expect(get_location_state(laboratory_model_url=url, location=DESTINATION[1])["occupied"] is True, "no plate")
        expect(
            get_location_state(laboratory_model_url=url, location=args.laboratory_model_location)["occupied"] is False,
            "the arm is still holding the plate",
        )
        carriage = await ardea.read("FunctionSet.Carriage.SensorValue")
        expect(carriage == float(properties["CarriagePosition"]), "carriage position disagrees with the result")
        print("Ardea LADS smoke test passed.")


def main() -> int:
    parser = build_parser("Smoke test the Ardea LADS server.", DEFAULT_PORT, DEFAULT_LOCATION)
    asyncio.run(run(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
