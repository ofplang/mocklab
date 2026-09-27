"""End-to-end sample: one plate makes a full circuit through every instrument and comes
back to where it started.

Where the per-server smoke tests check one server in isolation, this one checks that the
instruments, the Ardea transporter and the world model agree with each other over a whole
workflow. It is the closest thing here to a real run, and the pattern the labcode SiLA2
flavor is meant to reproduce: seal removal -> sealing -> thermal cycling ->
centrifugation, with Ardea carrying the plate between stations.

Two properties are worth naming because they drive the ordering below:

* **A closed instrument is a closed door.** A lid or door being shut makes that location
  inaccessible in the world model, and the model refuses to move an item into or out of an
  inaccessible location. So every transport is bracketed by the open/close commands of the
  instrument it touches -- those calls are load-bearing, not decoration.
* **Identity survives the circuit.** The plate is minted once at the start and its
  `item_id` is checked again at the end, so a transport that silently recreated the item
  instead of moving it would fail the test.

The world model is used only to set the scene and to check the result; everything in
between happens through SiLA2 commands, exactly as a workflow client would do it.

Prerequisite: the stack is up with the `sila2` profile. Exit code 0 means the circuit completed and the
final world state was as expected.
"""

from __future__ import annotations

from contextlib import ExitStack

from common import (
    DEFAULT_LABORATORY_MODEL_URL,
    add_item_to_location,
    build_parser,
    connect,
    get_location_state,
    print_server_identity,
    request_laboratory_model,
    reset_laboratory_model,
    wait_for_observable,
)

# Host-side ports published by docker-compose, listed in the order the workflow visits the
# instruments (Ardea is used throughout).
ARDEA_PORT = 50057
PLATE_SEAL_REMOVER_PORT = 50054
PLATELOC_PORT = 50053
THERMAL_CYCLER_PORT = 50055
CENTRIFUGE_PORT = 50052

# The locations each server is configured to act on (LABORATORY_MODEL_LOCATION in
# docker-compose.yml). Source and return are deliberately the same spot: the plate ends the
# workflow back where it began, which is what makes "roundabout" a checkable property.
STATION_SOURCE = "station.slot1"
STATION_RETURN = "station.slot1"
SEAL_REMOVER_LOCATION = "seal-remover.stage"
PLATELOC_LOCATION = "plateloc.stage"
THERMAL_CYCLER_LOCATION = "thermal-cycler.block"
CENTRIFUGE_LOCATION = "centrifuge.deck"

# Ardea's own name for each of those places. A transport names STATIONS, not locations, because
# that is what the real machine takes -- the server's station map (ARDEA_STATIONS in
# docker-compose.yml) is what pairs the two. This script needs both: the name to ask for the
# move, and the location to check afterwards that the plate is where it should be. Keeping the
# pairs here rather than asking the server is deliberate -- a map that stopped matching the
# bench then shows up as a plate landing somewhere this script does not expect.
ARDEA_STATION_BY_LOCATION = {
    STATION_SOURCE: "Base1",            # a plain plate-holding station
    "station.slot2": "Base2",           # the other one; unused by this circuit
    SEAL_REMOVER_LOCATION: "Base3",     # peeler
    PLATELOC_LOCATION: "Base4",         # sealer
    CENTRIFUGE_LOCATION: "Base5",       # plate centrifuge
    THERMAL_CYCLER_LOCATION: "Base6",   # thermal cycler
}


def setup_initial_laboratory_state(*, laboratory_model_url: str) -> str:
    """Arrange the world: wipe it, then put a single plate on the station.

    Returns the id the world model minted for that plate, which the final check compares
    against."""
    print("Preparing initial laboratory state via laboratory model.")
    reset_laboratory_model(laboratory_model_url=laboratory_model_url)
    initial_state = add_item_to_location(
        laboratory_model_url=laboratory_model_url,
        location=STATION_SOURCE,
    )
    item_id = str(initial_state["item_id"])
    print(f"Initial state: {initial_state}")
    return item_id


def verify_final_laboratory_state(*, laboratory_model_url: str, expected_item_id: str) -> None:
    """Check the world after the circuit: the same plate is back on the station and no
    instrument is still holding anything."""
    print("Verifying final laboratory state via laboratory model.")
    # The plate must be home, and must be the *same* plate -- an id mismatch would mean a
    # transport recreated it somewhere along the way.
    final_station = get_location_state(laboratory_model_url=laboratory_model_url, location=STATION_RETURN)
    if final_station["occupied"] is not True:
        raise RuntimeError(f"Expected an item at {STATION_RETURN}, but found: {final_station}")
    if str(final_station["item_id"]) != expected_item_id:
        raise RuntimeError(
            f"Expected item {expected_item_id} at {STATION_RETURN}, but found {final_station['item_id']}"
        )

    # Every instrument must be empty. This is what catches a half-completed transport: a
    # plate left behind would otherwise go unnoticed if a duplicate reached the station.
    for location in (
        SEAL_REMOVER_LOCATION,
        PLATELOC_LOCATION,
        THERMAL_CYCLER_LOCATION,
        CENTRIFUGE_LOCATION,
    ):
        state = get_location_state(laboratory_model_url=laboratory_model_url, location=location)
        if state["occupied"] is True:
            raise RuntimeError(f"Expected no item at {location}, but found: {state}")

    # Full snapshot printed for the record -- including the lock states the run left behind,
    # which the assertions above do not cover.
    snapshot = request_laboratory_model(laboratory_model_url=laboratory_model_url, path="/state")
    print(f"Final state snapshot: {snapshot}")


def move_with_ardea(
    *, ardea_feature, laboratory_model_url: str, source: str, destination: str, timeout_seconds: float
) -> None:
    """One transport: a single `Transfer` from `source` to `destination`, given as locations.

    The locations are translated to Ardea's station names here, so every caller below can go on
    naming the place the plate is going rather than the machine's label for it.

    Ardea drives the whole route in one command -- carriage to the source, pick, carriage to
    the destination, put -- where the trolley arm this replaced needed a Pick and a Place.
    `Transfer` is observable and reports its phase as it goes, so it is polled to completion;
    the reads afterwards are for the log, showing the source emptied and the destination
    filled. Both ends must be accessible or the world model rejects the move -- hence the
    lid/door commands surrounding these calls in the sequence below."""
    source_station = ARDEA_STATION_BY_LOCATION[source]
    destination_station = ARDEA_STATION_BY_LOCATION[destination]
    print(f"Moving item with Ardea: {source_station} ({source}) -> {destination_station} ({destination})")
    responses = wait_for_observable(
        ardea_feature.Transfer(SourceStation=source_station, DestinationStation=destination_station),
        label="LabwareService.Transfer",
        timeout_seconds=timeout_seconds,
    )
    print(f"Transfer finished at {responses.CarriagePosition} mm, at retract pose: {responses.AtRetractPose}")

    pick_source_state = get_location_state(
        laboratory_model_url=laboratory_model_url,
        location=source,
    )
    place_destination_state = get_location_state(
        laboratory_model_url=laboratory_model_url,
        location=destination,
    )
    print(f"State after move: source={pick_source_state} destination={place_destination_state}")


def run_roundabout_sequence(*, host: str, insecure: bool, timeout_seconds: float, laboratory_model_url: str) -> None:
    """Drive the whole circuit over SiLA2."""
    # All five servers are needed across the run, so their clients are opened together and
    # held for its duration; ExitStack closes them all on the way out, including on failure.
    with ExitStack() as stack:
        centrifuge_client = stack.enter_context(connect(host, CENTRIFUGE_PORT, insecure=insecure))
        plateloc_client = stack.enter_context(connect(host, PLATELOC_PORT, insecure=insecure))
        seal_remover_client = stack.enter_context(connect(host, PLATE_SEAL_REMOVER_PORT, insecure=insecure))
        thermal_cycler_client = stack.enter_context(connect(host, THERMAL_CYCLER_PORT, insecure=insecure))
        ardea_client = stack.enter_context(connect(host, ARDEA_PORT, insecure=insecure))

        # Identify every server up front: this fails fast and clearly if part of the stack
        # is not up, rather than midway through the workflow with a plate in transit.
        print_server_identity(centrifuge_client, host=host, port=CENTRIFUGE_PORT)
        print_server_identity(plateloc_client, host=host, port=PLATELOC_PORT)
        print_server_identity(seal_remover_client, host=host, port=PLATE_SEAL_REMOVER_PORT)
        print_server_identity(thermal_cycler_client, host=host, port=THERMAL_CYCLER_PORT)
        print_server_identity(ardea_client, host=host, port=ARDEA_PORT)

        centrifuge = centrifuge_client.MicroplateCentrifugeController
        plateloc = plateloc_client.PlateLocController
        seal_remover = seal_remover_client.AutomatedPlateSealRemoverController
        thermal_cycler = thermal_cycler_client.AutomatedThermalCyclerController
        ardea = ardea_client.LabwareService

        # Prepare the thermal cycler's protocol now, long before the plate reaches it: the
        # instrument rejects StartRun unless a protocol was loaded and validated, and doing
        # it here mirrors real preparation while the instrument is otherwise idle.
        wait_for_observable(
            thermal_cycler.Load(ProtocolFileData=b"mock protocol"),
            label="AutomatedThermalCyclerController.Load",
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            thermal_cycler.Validate(MaxSampleVolume=10.0),
            label="AutomatedThermalCyclerController.Validate",
            timeout_seconds=timeout_seconds,
        )

        # Step 1 -- seal removal. Neither the station nor the seal remover has a door, so
        # this transport needs no bracketing commands.
        move_with_ardea(
            ardea_feature=ardea,
            laboratory_model_url=laboratory_model_url,
            source=STATION_SOURCE,
            destination=SEAL_REMOVER_LOCATION,
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            seal_remover.Peel(BeginPeelLocation=1, AdhesionTime=1),
            label="AutomatedPlateSealRemoverController.Peel",
            timeout_seconds=timeout_seconds,
        )
        print("Peel completed.")

        # Step 2 -- resealing. The sealing parameters are set on the instrument first; the
        # cycle then runs on whatever plate is present at its location.
        move_with_ardea(
            ardea_feature=ardea,
            laboratory_model_url=laboratory_model_url,
            source=SEAL_REMOVER_LOCATION,
            destination=PLATELOC_LOCATION,
            timeout_seconds=timeout_seconds,
        )
        plateloc.SetSealingTemperature(SealingTemperature=180)
        plateloc.SetSealingTime(SealingTime=2.0)
        wait_for_observable(
            plateloc.StartCycle(),
            label="PlateLocController.StartCycle",
            timeout_seconds=timeout_seconds,
        )
        print("PlateLoc cycle completed.")

        # Step 3 -- thermal cycling. The lid choreography is required: open before the plate
        # can be placed inside, closed for the run, and open again afterwards so the plate
        # can be picked back out.
        wait_for_observable(
            thermal_cycler.OpenLid(),
            label="AutomatedThermalCyclerController.OpenLid",
            timeout_seconds=timeout_seconds,
        )
        move_with_ardea(
            ardea_feature=ardea,
            laboratory_model_url=laboratory_model_url,
            source=PLATELOC_LOCATION,
            destination=THERMAL_CYCLER_LOCATION,
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            thermal_cycler.CloseLid(),
            label="AutomatedThermalCyclerController.CloseLid",
            timeout_seconds=timeout_seconds,
        )
        # StartRun returns while the instrument keeps running, so StopRun is what actually
        # ends the run (this sample does not wait out a protocol).
        wait_for_observable(
            thermal_cycler.StartRun(),
            label="AutomatedThermalCyclerController.StartRun",
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            thermal_cycler.StopRun(),
            label="AutomatedThermalCyclerController.StopRun",
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            thermal_cycler.OpenLid(),
            label="AutomatedThermalCyclerController.OpenLid",
            timeout_seconds=timeout_seconds,
        )
        print("Thermal cycler run completed.")

        # Step 4 -- centrifugation. Same door choreography as the lid above: open to load,
        # closed to spin, open again to unload.
        wait_for_observable(
            centrifuge.OpenDoor(BucketNumber=1),
            label="MicroplateCentrifugeController.OpenDoor",
            timeout_seconds=timeout_seconds,
        )
        move_with_ardea(
            ardea_feature=ardea,
            laboratory_model_url=laboratory_model_url,
            source=THERMAL_CYCLER_LOCATION,
            destination=CENTRIFUGE_LOCATION,
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            centrifuge.CloseDoor(),
            label="MicroplateCentrifugeController.CloseDoor",
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            centrifuge.SpinCycle(
                VelocityPercent=50.0,
                AccelerationPercent=50.0,
                DecelerationPercent=50.0,
                TimerMode=1,
                Time=1,
                BucketNumberToLoad=1,
                BucketNumberToUnload=1,
                GripperOffsetToLoad=8.0,
                GripperOffsetToUnload=8.0,
                PlateHeightToLoad=15.0,
                PlateHeightToUnload=15.0,
                SpeedToLoad=1,
                SpeedToUnload=1,
                OptionsToLoad=0,
                OptionsToUnload=0,
            ),
            label="MicroplateCentrifugeController.SpinCycle",
            timeout_seconds=timeout_seconds,
        )
        wait_for_observable(
            centrifuge.OpenDoor(BucketNumber=1),
            label="MicroplateCentrifugeController.OpenDoor",
            timeout_seconds=timeout_seconds,
        )
        print("Centrifuge cycle completed.")

        # Step 5 -- back to the station the plate started from, closing the circuit.
        move_with_ardea(
            ardea_feature=ardea,
            laboratory_model_url=laboratory_model_url,
            source=CENTRIFUGE_LOCATION,
            destination=STATION_RETURN,
            timeout_seconds=timeout_seconds,
        )
        print("Roundabout completed.")


def main() -> int:
    # Ardea's port is the parser default because it is the one server involved in every
    # step; the rest are fixed constants (see above) since this sample only makes sense
    # against the compose stack as a whole.
    parser = build_parser("Run an end-to-end roundabout workflow using the SiLA2 servers directly.", ARDEA_PORT)
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used only for setup and verification",
    )
    args = parser.parse_args()

    # Arrange, run, then check -- the three phases are kept apart so it is clear that
    # nothing between setup and verification touches the world model directly.
    expected_item_id = setup_initial_laboratory_state(laboratory_model_url=args.laboratory_model_url)
    run_roundabout_sequence(
        host=args.host,
        insecure=args.insecure,
        timeout_seconds=args.timeout,
        laboratory_model_url=args.laboratory_model_url,
    )
    verify_final_laboratory_state(
        laboratory_model_url=args.laboratory_model_url,
        expected_item_id=expected_item_id,
    )
    print("Roundabout workflow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
