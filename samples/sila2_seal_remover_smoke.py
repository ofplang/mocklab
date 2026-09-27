"""Smoke test for the Automated Plate Seal Remover (peeler) mock, over SiLA2 directly.

This server is the one that carries a *consumable*: the peel tape. GetTapeLeft is an
observable command that returns values (the two spool reserves plus a low-tape warning),
which makes this sample the reference case for value-returning commands -- the pattern
labcode's partial-outputs support needs from a real instrument. The script therefore
brackets a Peel with two GetTapeLeft reads so the consumption is visible in the output.

Prerequisite: the compose stack is up. Exit code 0 means the sequence passed.
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
# configured to act on (LABORATORY_MODEL_LOCATION in docker-compose.yml).
DEFAULT_PORT = 50054
DEFAULT_LOCATION = "seal-remover.stage"


def main() -> int:
    parser = build_parser("Smoke test the Automated Plate Seal Remover SiLA2 server directly.", DEFAULT_PORT)
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used to seed required test items",
    )
    parser.add_argument(
        "--laboratory-model-location",
        default=DEFAULT_LOCATION,
        help="Laboratory model location required by the seal remover server",
    )
    args = parser.parse_args()

    # Arrange the world: a clean model with one sealed plate in the peeler, which Peel
    # requires to be present.
    seeded_item = ensure_item_at_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.laboratory_model_location,
    )
    print(f"Seeded laboratory model item: {seeded_item}")

    with connect(args.host, args.port, insecure=args.insecure) as client:
        print_server_identity(client, host=args.host, port=args.port)

        # Baseline reserve reading, taken before the peel so the two values can be compared.
        feature = client.AutomatedPlateSealRemoverController
        tape_before = wait_for_observable(
            feature.GetTapeLeft(),
            label="AutomatedPlateSealRemoverController.GetTapeLeft",
            timeout_seconds=args.timeout,
        )
        print(f"Tape before peel: {tape_before}")

        # The peel itself. It also returns a value (a warning string), so its response is
        # printed rather than discarded.
        peel_result = wait_for_observable(
            feature.Peel(BeginPeelLocation=1, AdhesionTime=1),
            label="AutomatedPlateSealRemoverController.Peel",
            timeout_seconds=args.timeout,
        )
        print(f"Peel result: {peel_result}")

        # Second reading: the reserves are expected to have gone down, i.e. the command
        # really consumed a modelled resource rather than just returning a constant.
        tape_after = wait_for_observable(
            feature.GetTapeLeft(),
            label="AutomatedPlateSealRemoverController.GetTapeLeft",
            timeout_seconds=args.timeout,
        )
        print(f"Tape after peel: {tape_after}")

        # Reset recovers to Idle and refills the tape, leaving the server ready for a
        # repeat run of this sample.
        wait_for_observable(
            feature.Reset(),
            label="AutomatedPlateSealRemoverController.Reset",
            timeout_seconds=args.timeout,
        )
        print(f"Status after reset: {feature.Status.get()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
