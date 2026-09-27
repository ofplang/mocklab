"""Smoke test for the Microplate Centrifuge mock, driven over SiLA2 directly.

Exercises the feature the way an instrument client would: read the static version and
profile readouts, then run the door + spin sequence. This server's physical effects land
in the laboratory model -- OpenDoor/CloseDoor toggle the accessibility of its location,
and SpinCycle refuses to run unless a plate is present there -- so the script seeds that
plate first.

Prerequisite: the compose stack is up. Exit code 0 means the whole sequence passed; any
rejection inside the mock propagates as an exception and a non-zero exit.
"""

from __future__ import annotations

from common import (
    DEFAULT_LABORATORY_MODEL_URL,
    build_parser,
    connect,
    ensure_item_at_location,
    print_server_identity,
    wait_for_observable,
)

# Host-side port published by docker-compose for this server, and the location it is
# configured to act on (LABORATORY_MODEL_LOCATION in docker-compose.yml). The two must
# agree with compose or the seeded plate lands somewhere the server never looks at.
DEFAULT_PORT = 50052
DEFAULT_LOCATION = "centrifuge.deck"


def main() -> int:
    parser = build_parser("Smoke test the Microplate Centrifuge SiLA2 server directly.", DEFAULT_PORT)
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used to seed required test items",
    )
    parser.add_argument(
        "--laboratory-model-location",
        default=DEFAULT_LOCATION,
        help="Laboratory model location required by the centrifuge server",
    )
    args = parser.parse_args()

    # Arrange the world: a clean model with exactly one plate sitting in the centrifuge,
    # which is SpinCycle's precondition.
    seeded_item = ensure_item_at_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.laboratory_model_location,
    )
    print(f"Seeded laboratory model item: {seeded_item}")

    with connect(args.host, args.port, insecure=args.insecure) as client:
        print_server_identity(client, host=args.host, port=args.port)

        # Unobservable reads first: they confirm the feature is reachable and cheap to
        # call before anything actuates.
        feature = client.MicroplateCentrifugeController
        print(f"Hardware version: {feature.HardwareVersion.get()}")
        print(f"Firmware version: {feature.FirmwareVersion.get()}")
        print(f"Profiles: {feature.EnumerateProfiles()}")

        # Open then close the door. Beyond exercising the commands this walks the location
        # through inaccessible/accessible in the world model, mirroring a real load.
        wait_for_observable(
            feature.OpenDoor(BucketNumber=1),
            label="MicroplateCentrifugeController.OpenDoor",
            timeout_seconds=args.timeout,
        )
        wait_for_observable(
            feature.CloseDoor(),
            label="MicroplateCentrifugeController.CloseDoor",
            timeout_seconds=args.timeout,
        )
        # The spin itself, with the door closed. Most parameters exist for signature
        # fidelity with the real instrument and are not simulated; the values here are
        # simply valid ones (Time=1 keeps the run short).
        wait_for_observable(
            feature.SpinCycle(
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
            timeout_seconds=args.timeout,
        )
        # Reset returns the instrument to a known state, leaving the server ready for the
        # next sample run.
        wait_for_observable(
            feature.Reset(),
            label="MicroplateCentrifugeController.Reset",
            timeout_seconds=args.timeout,
        )

        # Expected to read Idle (1) per the feature's status contract.
        print(f"Status after reset: {feature.Status.get()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
