"""Smoke test for the Ardea mock, driven over SiLA2 directly.

Ardea is the only server that *moves* items, so this sample is where transport is checked in
isolation. It is also this lab's only mock of an instrument that really exists, which makes the
second half as interesting as the first: what a client can and cannot get from it.

Four things are exercised:

* the two `CarriageService` properties -- `StationNames` lists what `Transfer` will accept, and
  `CarriagePosition` is read before and after so the carriage is seen to move;
* `LabwareService.Transfer`, which carries the plate from one station to another. It is called
  with the machine's own station names (`Base1`, `Base2`, ...), which the server's station map
  resolves to places in the world. In the world model that is two hops rather than one (source
  -> the arm's own location -> destination), but unlike the trolley arm this replaced, the
  machine does both inside a single command, so a client makes one call where it made two;
* `LabwareService.LightIsOn`, the one reading here that comes from the world model's opaque
  device state rather than from where anything sits;
* a refusal. Every command but `Transfer` is unimplemented by design, and this checks that one
  of them says so promptly instead of hanging or quietly succeeding -- a mock that pretends to
  have picked a plate is worse than one that admits it cannot.

`Transfer` is observable and reports its phase as it goes, so it is polled to completion with a
subscription collecting the phases. With the realistic duration profile they arrive spread
across the transfer, which is what a polling workflow client needs to be able to see; that
profile also makes the transfer longer than the default 10 s ceiling, so pass `--timeout 120`
when running against it.

Prerequisite: the compose stack is up. Exit code 0 means the sequence passed.
"""

from __future__ import annotations

from typing import Any

from common import (
    DEFAULT_LABORATORY_MODEL_URL,
    add_item_to_location,
    build_parser,
    connect,
    get_location_state,
    print_server_identity,
    reset_laboratory_model,
    wait_for_observable,
)

# Host-side port published by docker-compose for this server.
#
# Ardea takes STATION NAMES, not locations: `Base1`, `Base2`, ... are the machine's own names
# for its stations, and the server's station map (ARDEA_STATIONS in docker-compose.yml) is what
# turns each one into a place in the world. This sample therefore needs both -- the names to
# call `Transfer` with, and the locations to check the world afterwards -- so it keeps its own
# copy of the pair. That the copies must agree is the point: a map that stopped matching the
# bench would show up here as a transfer landing somewhere this script does not expect.
#
# The two chosen stations are the plain plate-holding ones, so the transfer depends on no
# instrument being in any particular state. ARM_LOCATION is Ardea's own location
# (LABORATORY_MODEL_LOCATION), i.e. the spot a plate occupies while it is being carried.
DEFAULT_PORT = 50057
TRANSFER_SOURCE_STATION = "Base1"
TRANSFER_SOURCE = "station.slot1"
TRANSFER_DESTINATION_STATION = "Base2"
TRANSFER_DESTINATION = "station.slot2"
ARM_LOCATION = "ardea.gripper"

LABEL = "LabwareService.Transfer"


def transfer(labware: Any, *, source: str, destination: str, timeout_seconds: float) -> tuple[Any, list[str]]:
    """Run one transfer to completion, collecting the phases it reported on the way.

    The phases arrive on a separate subscription, which is opened before the polling starts and
    closed after it ends. An empty list is not a failure: with the default duration profile a
    transfer can finish before the subscription is even established, and the phases are for the
    log rather than something to assert on. What is asserted is the outcome -- the response, and
    where the plate ended up."""
    phases: list[str] = []
    instance = labware.Transfer(SourceStation=source, DestinationStation=destination)

    try:
        with instance.subscribe_to_intermediate_responses() as stream:
            stream.add_callback(lambda response: phases.append(str(response.Phase)))
            return wait_for_observable(instance, label=LABEL, timeout_seconds=timeout_seconds), phases
    except TimeoutError:
        # A transfer that never finishes is a real failure and must not be swallowed by the
        # subscription handling below.
        raise
    except Exception as error:
        # The subscription itself failed -- most likely the command had already finished. The
        # transfer is still the thing being tested, so fall back to polling it without phases.
        print(f"Intermediate responses unavailable ({type(error).__name__}: {error})")
        return wait_for_observable(instance, label=LABEL, timeout_seconds=timeout_seconds), phases


def main() -> int:
    parser = build_parser("Smoke test the Ardea SiLA2 server directly.", DEFAULT_PORT)
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used to seed and inspect the transferred item",
    )
    args = parser.parse_args()

    with connect(args.host, args.port, insecure=args.insecure) as client:
        print_server_identity(client, host=args.host, port=args.port)

        # Arrange the world: wipe it, then place one item at the source so the transfer has
        # something to carry.
        labware = client.LabwareService
        carriage = client.CarriageService
        reset_laboratory_model(laboratory_model_url=args.laboratory_model_url)
        add_item_to_location(laboratory_model_url=args.laboratory_model_url, location=TRANSFER_SOURCE)

        # The stations, as the server reports them: the machine's own names, which on the real
        # Ardea come from its motion configuration and here from the station map. Checking the
        # two this script uses are among them is what turns a misconfigured map into a clear
        # failure rather than a puzzling InvalidStation later.
        stations = carriage.StationNames.get()
        print(f"Stations: {stations}")
        for station in (TRANSFER_SOURCE_STATION, TRANSFER_DESTINATION_STATION):
            if station not in stations:
                raise RuntimeError(f"Expected {station} among the reported stations, got: {stations}")
        # A location is not a station name. If one appears here the map is being bypassed, and a
        # workflow written against this mock would not run on the machine.
        if any("." in station for station in stations):
            raise RuntimeError(f"Station names must be the machine's own, not world locations: {stations}")

        # The carriage position and the machine light, before the transfer. The light is read
        # from the world model's device state, which the seed declares off at t=0.
        print(f"Carriage position before transfer: {carriage.CarriagePosition.get()}")
        print(f"Light is on: {labware.LightIsOn.get()}")

        responses, phases = transfer(
            labware,
            source=TRANSFER_SOURCE_STATION,
            destination=TRANSFER_DESTINATION_STATION,
            timeout_seconds=args.timeout,
        )
        print(f"Transfer phases: {phases}")
        print(f"Transfer response: carriage at {responses.CarriagePosition} mm, retract {responses.AtRetractPose}")

        # All three locations are printed, because it is the set that shows the plate actually
        # moved: the source emptied, the destination filled, and nothing left on the arm -- a
        # plate still on the gripper would mean a half-completed transfer.
        for location in (TRANSFER_SOURCE, ARM_LOCATION, TRANSFER_DESTINATION):
            state = get_location_state(laboratory_model_url=args.laboratory_model_url, location=location)
            print(f"After transfer, {location}: {state}")
            expected_occupied = location == TRANSFER_DESTINATION
            if state["occupied"] is not expected_occupied:
                raise RuntimeError(
                    f"Expected occupied={expected_occupied} at {location} after the transfer, but found: {state}"
                )

        # The carriage should now report the destination's position. Not compared against a
        # literal: the value is derived from the station list, so what is checked is that the
        # property agrees with the response the transfer just gave.
        position = carriage.CarriagePosition.get()
        print(f"Carriage position after transfer: {position}")
        if position != responses.CarriagePosition:
            raise RuntimeError(
                f"Carriage property says {position} but the transfer reported {responses.CarriagePosition}"
            )

        # A refusal, checked rather than assumed. `MoveCarriage` is one of the twenty-two
        # commands this mock does not implement; it must fail, and fail rather than hang.
        try:
            wait_for_observable(
                carriage.MoveCarriage(StationId="0"),
                label="CarriageService.MoveCarriage",
                timeout_seconds=args.timeout,
            )
        except TimeoutError:
            raise
        except Exception as error:
            print(f"MoveCarriage refused as expected: {type(error).__name__}: {error}")
        else:
            raise RuntimeError("CarriageService.MoveCarriage succeeded, but this mock does not implement it")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
