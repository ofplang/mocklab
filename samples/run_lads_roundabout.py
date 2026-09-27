"""End-to-end sample over LADS OPC UA: one plate makes the full circuit and comes back.

The LADS counterpart of `run_roundabout.py`, and deliberately the same circuit -- seal removal ->
sealing -> thermal cycling -> centrifugation and home, Ardea carrying the plate between stations --
with the same world-model setup and the same final check, imported from that script. Only the
middle is different: every step is a LADS call instead of a SiLA2 command.

    SiLA2                               LADS
    Ardea LabwareService.Transfer       Ardea's Transfer program (SourceStation, DestinationStation)
    OpenLid / CloseLid, OpenDoor ...    the Lid / Door cover functions' Open and Close
    Peel, StartCycle, SpinCycle ...     programs on each instrument's functional unit
    StopRun                             the thermal cycler's FunctionalUnitState.Stop
    SetSealingTemperature / Time        TargetValue writes (the time in milliseconds)

The lid and door choreography is as load-bearing as over SiLA2: a closed instrument is an
inaccessible location, and the world model refuses to move a plate into or out of one.

Prerequisite: the stack is up with the `lads` profile. Exit code 0 means the circuit completed and
the final world state was as expected.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack

from common import DEFAULT_LABORATORY_MODEL_URL, get_location_state
from lads_client import DEFAULT_HOST, DEFAULT_TIMEOUT_SECONDS, Unit, connect, expect
from run_roundabout import (
    ARDEA_STATION_BY_LOCATION,
    CENTRIFUGE_LOCATION,
    PLATELOC_LOCATION,
    SEAL_REMOVER_LOCATION,
    STATION_RETURN,
    STATION_SOURCE,
    THERMAL_CYCLER_LOCATION,
    setup_initial_laboratory_state,
    verify_final_laboratory_state,
)

# Host-side ports docker-compose publishes for the LADS servers; `--port-offset` shifts them all.
CENTRIFUGE_PORT = 4841
PLATELOC_PORT = 4842
SEAL_REMOVER_PORT = 4843
THERMAL_CYCLER_PORT = 4844
ARDEA_PORT = 4847

SPIN_CYCLE: dict[str, object] = {
    "VelocityPercent": 50.0,
    "AccelerationPercent": 50.0,
    "DecelerationPercent": 50.0,
    "TimerMode": 1,
    "Time": 1,
    "BucketNumberToLoad": 1,
    "BucketNumberToUnload": 1,
    "GripperOffsetToLoad": 8.0,
    "GripperOffsetToUnload": 8.0,
    "PlateHeightToLoad": 15.0,
    "PlateHeightToUnload": 15.0,
    "SpeedToLoad": 1,
    "SpeedToUnload": 1,
    "OptionsToLoad": 0,
    "OptionsToUnload": 0,
}


async def run_program(unit: Unit, template_id: str, **properties: object) -> dict[str, str]:
    result = await unit.run_program(template_id, **properties)
    expect(
        result["Outcome"] == "Completed",
        f"{unit.name}.{template_id} ended {result['Outcome']}: {await unit.last_error()}",
    )
    return result


async def move_with_ardea(ardea: Unit, *, laboratory_model_url: str, source: str, destination: str) -> None:
    """One transport: Ardea's Transfer program between two stations, named by location here."""
    source_station = ARDEA_STATION_BY_LOCATION[source]
    destination_station = ARDEA_STATION_BY_LOCATION[destination]
    print(f"Moving item with Ardea: {source_station} ({source}) -> {destination_station} ({destination})")
    result = await run_program(ardea, "Transfer", SourceStation=source_station, DestinationStation=destination_station)
    print(f"Transfer finished at {result['CarriagePosition']} mm, at retract pose: {result['AtRetractPose']}")
    after = get_location_state(laboratory_model_url=laboratory_model_url, location=destination)
    expect(after["occupied"] is True, f"no item at {destination} after the transfer")


async def run_roundabout_sequence(args: argparse.Namespace) -> None:
    offset, host, timeout, url = args.port_offset, args.host, args.timeout, args.laboratory_model_url
    async with AsyncExitStack() as stack:
        # All five servers are needed across the run, so they are connected up front: a part of
        # the stack that is down fails here, not with a plate in transit.
        centrifuge = await stack.enter_async_context(
            connect(host, CENTRIFUGE_PORT + offset, "Centrifuge", "Centrifuge", timeout=timeout)
        )
        plateloc = await stack.enter_async_context(
            connect(host, PLATELOC_PORT + offset, "PlateLoc", "Sealer", timeout=timeout)
        )
        seal_remover = await stack.enter_async_context(
            connect(host, SEAL_REMOVER_PORT + offset, "SealRemover", "Peeler", timeout=timeout)
        )
        thermal_cycler = await stack.enter_async_context(
            connect(host, THERMAL_CYCLER_PORT + offset, "ThermalCycler", "Cycler", timeout=timeout)
        )
        ardea = await stack.enter_async_context(
            connect(host, ARDEA_PORT + offset, "Ardea", "Transporter", timeout=timeout)
        )

        # Prepare the thermal cycler's protocol long before the plate reaches it: StartRun is
        # refused unless a protocol was loaded and validated.
        await run_program(thermal_cycler, "Load", ProtocolFileData="mock protocol")
        await run_program(thermal_cycler, "Validate", MaxSampleVolume=10.0)

        # Step 1 -- seal removal. Neither the station nor the peeler has a door.
        await move_with_ardea(ardea, laboratory_model_url=url, source=STATION_SOURCE, destination=SEAL_REMOVER_LOCATION)
        await run_program(seal_remover, "Peel", BeginPeelLocation=1, AdhesionTime=1)
        print("Peel completed.")

        # Step 2 -- resealing, with the sealing parameters set first.
        await move_with_ardea(
            ardea, laboratory_model_url=url, source=SEAL_REMOVER_LOCATION, destination=PLATELOC_LOCATION
        )
        await plateloc.write_target("SealingTemperature", 180)
        await plateloc.write_target("SealingTime", 2000)
        await run_program(plateloc, "StartCycle")
        print("PlateLoc cycle completed.")

        # Step 3 -- thermal cycling: open to load, closed for the run, open to unload. The run
        # keeps the cycler Running after StartRun completes; Stop (StopRun) ends it.
        expect(await thermal_cycler.cover("Lid", "Open") == "Opened", "lid did not open")
        await move_with_ardea(
            ardea, laboratory_model_url=url, source=PLATELOC_LOCATION, destination=THERMAL_CYCLER_LOCATION
        )
        expect(await thermal_cycler.cover("Lid", "Close") == "Closed", "lid did not close")
        await run_program(thermal_cycler, "StartRun")
        await thermal_cycler.stop()
        expect(await thermal_cycler.cover("Lid", "Open") == "Opened", "lid did not reopen")
        print("Thermal cycler run completed.")

        # Step 4 -- centrifugation, with the same door choreography.
        expect(await centrifuge.cover("Door", "Open") == "Opened", "door did not open")
        await move_with_ardea(
            ardea, laboratory_model_url=url, source=THERMAL_CYCLER_LOCATION, destination=CENTRIFUGE_LOCATION
        )
        expect(await centrifuge.cover("Door", "Close") == "Closed", "door did not close")
        await run_program(centrifuge, "SpinCycle", **SPIN_CYCLE)
        expect(await centrifuge.cover("Door", "Open") == "Opened", "door did not reopen")
        print("Centrifuge cycle completed.")

        # Step 5 -- back to the station the plate started from.
        await move_with_ardea(ardea, laboratory_model_url=url, source=CENTRIFUGE_LOCATION, destination=STATION_RETURN)
        print("Roundabout completed.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the end-to-end roundabout over the LADS OPC UA servers.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host the LADS servers are published on")
    parser.add_argument("--port-offset", type=int, default=0, help="Added to every LADS server's default port")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout in seconds per step")
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used only for setup and verification",
    )
    args = parser.parse_args()

    # Arrange, run, then check -- exactly the setup and check the SiLA2 roundabout uses.
    expected_item_id = setup_initial_laboratory_state(laboratory_model_url=args.laboratory_model_url)
    asyncio.run(run_roundabout_sequence(args))
    verify_final_laboratory_state(laboratory_model_url=args.laboratory_model_url, expected_item_id=expected_item_id)
    print("LADS roundabout workflow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
