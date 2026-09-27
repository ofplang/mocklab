"""Smoke test for the Automated Thermal Cycler mock, driven over SiLA2 directly.

This server has the most internal state of the six, and the command order below is that
state machine rather than an arbitrary sequence: a protocol must be loaded and validated
before StartRun is allowed, the lid must be closed over a plate that is actually present,
and the run continues until StopRun (StartRun returns while the instrument is still
running). The lid commands are also this server's physical effect on the world model --
open makes its location accessible, closed makes it inaccessible.

Note the two distinct state readouts exercised here: `GetInstrumentState` returns the
InstrumentState enum (0=IDLE, 1=STANDBY, 2=RUNNING, 3=ERROR, 4=DIAGNOSTICS), which is a
different enumeration from the `Status` property the other samples print.

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
DEFAULT_PORT = 50055
DEFAULT_LOCATION = "thermal-cycler.block"


def main() -> int:
    parser = build_parser("Smoke test the Automated Thermal Cycler SiLA2 server directly.", DEFAULT_PORT)
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL used to seed required test items",
    )
    parser.add_argument(
        "--laboratory-model-location",
        default=DEFAULT_LOCATION,
        help="Laboratory model location required by the thermal cycler server",
    )
    args = parser.parse_args()

    # Arrange the world: a clean model with one plate in the cycler. StartRun checks this
    # before it begins, so without the seed the run would be rejected outright.
    seeded_item = ensure_item_at_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.laboratory_model_location,
    )
    print(f"Seeded laboratory model item: {seeded_item}")

    with connect(args.host, args.port, insecure=args.insecure) as client:
        print_server_identity(client, host=args.host, port=args.port)

        # Baseline: expected to report IDLE, since no run is active yet.
        feature = client.AutomatedThermalCyclerController
        print(f"Initial instrument state: {feature.GetInstrumentState()}")

        # Protocol setup. Both steps are required, in this order: the mock rejects StartRun
        # unless a protocol has been loaded and then validated. The payload is a stand-in --
        # the mock does not parse it.
        wait_for_observable(
            feature.Load(ProtocolFileData=b"mock protocol"),
            label="AutomatedThermalCyclerController.Load",
            timeout_seconds=args.timeout,
        )
        wait_for_observable(
            feature.Validate(MaxSampleVolume=10.0),
            label="AutomatedThermalCyclerController.Validate",
            timeout_seconds=args.timeout,
        )
        # Lid open then closed: exercises both commands and leaves the lid shut, which is
        # the configuration a run needs. In the world model this is unlock-then-lock of
        # this server's location.
        wait_for_observable(
            feature.OpenLid(),
            label="AutomatedThermalCyclerController.OpenLid",
            timeout_seconds=args.timeout,
        )
        wait_for_observable(
            feature.CloseLid(),
            label="AutomatedThermalCyclerController.CloseLid",
            timeout_seconds=args.timeout,
        )
        # Start the run. Unlike the other commands this one returns while the instrument
        # keeps running, so the readouts below are taken mid-run: RUNNING plus a non-zero
        # remaining time.
        wait_for_observable(
            feature.StartRun(),
            label="AutomatedThermalCyclerController.StartRun",
            timeout_seconds=args.timeout,
        )
        print(f"Instrument state after StartRun: {feature.GetInstrumentState()}")
        print(f"Remaining time: {feature.RemainingTime.get()}")

        # Ending the run explicitly is what returns the instrument to idle; this also
        # leaves the server ready for a repeat run of this sample.
        wait_for_observable(
            feature.StopRun(),
            label="AutomatedThermalCyclerController.StopRun",
            timeout_seconds=args.timeout,
        )
        print(f"Instrument state after StopRun: {feature.GetInstrumentState()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
