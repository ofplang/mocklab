"""Smoke test for the PlateLoc (plate sealer) mock, driven over SiLA2 directly.

The interesting shape here is settings-then-cycle: the sealing temperature and time are
unobservable setters whose effect is only visible through the matching getters, and the
cycle count is the observable side effect of running a seal. The script therefore reads
the defaults, overrides them, runs one cycle, and finally resets -- which restores the
factory defaults and zeroes the cycle count, so repeated runs start from the same place.

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
DEFAULT_PORT = 50053
DEFAULT_LOCATION = "plateloc.stage"


def main() -> int:
    parser = build_parser("Smoke test the PlateLoc SiLA2 server directly.", DEFAULT_PORT)
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used to seed required test items",
    )
    parser.add_argument(
        "--laboratory-model-location",
        default=DEFAULT_LOCATION,
        help="Laboratory model location required by the PlateLoc server",
    )
    args = parser.parse_args()

    # Arrange the world: a clean model with one plate in the sealer, which StartCycle
    # requires to be present.
    seeded_item = ensure_item_at_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.laboratory_model_location,
    )
    print(f"Seeded laboratory model item: {seeded_item}")

    with connect(args.host, args.port, insecure=args.insecure) as client:
        print_server_identity(client, host=args.host, port=args.port)

        # Baseline: the mock's factory defaults, before anything is changed.
        feature = client.PlateLocController
        print(f"Initial sealing temperature: {feature.SealingTemperature.get()}")
        print(f"Initial sealing time: {feature.SealingTime.get()}")
        print(f"Profiles: {feature.EnumerateProfiles()}")

        # Set both parameters and read them back: the setters are unobservable, so the
        # getters are the only evidence the values took effect.
        feature.SetSealingTemperature(SealingTemperature=180)
        feature.SetSealingTime(SealingTime=2.0)
        print(f"Updated sealing temperature: {feature.SealingTemperature.get()}")
        print(f"Updated sealing time: {feature.SealingTime.get()}")

        # Run one seal. The cycle count is expected to have advanced by one afterwards.
        wait_for_observable(
            feature.StartCycle(),
            label="PlateLocController.StartCycle",
            timeout_seconds=args.timeout,
        )
        print(f"Cycle count after StartCycle: {feature.CycleCount.get()}")

        # Reset restores the defaults and clears the counter back to 0, so this sample is
        # repeatable against a long-lived server.
        wait_for_observable(
            feature.Reset(),
            label="PlateLocController.Reset",
            timeout_seconds=args.timeout,
        )
        print(f"Cycle count after reset: {feature.CycleCount.get()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
