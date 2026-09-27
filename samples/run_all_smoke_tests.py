"""Run every per-server smoke test in one go and report which ones failed.

This is the "is the stack healthy?" entry point: bring the compose stack up, run this, and
each of the five mock servers gets exercised -- over SiLA2 by default, over LADS OPC UA with
`--protocol lads`, or both one after the other with `--protocol both`. Each script is launched as a
separate process so that one server's failure -- or a crash in its script -- cannot take the
rest of the run down with it.

Prerequisite: the compose stack is up. Exit code 0 means every script passed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# The five mock servers, one script each: four instruments and the transporter. Order does
# not matter -- every script wipes the laboratory model on entry and seeds only what it
# needs, so they neither depend on nor disturb each other.
#
# laboratory_model_smoke.py is intentionally NOT listed here. This runner is scoped to the
# servers; the world model is a separate component with no protocol surface of its own, and its
# smoke test is run on its own.
SCRIPTS = {
    "sila2": [
        "microplate_centrifuge_server_smoke.py",
        "plateloc_server_smoke.py",
        "automated_plate_seal_remover_server_smoke.py",
        "automated_thermal_cycler_server_smoke.py",
        "ardea_server_smoke.py",
    ],
    # The same five servers' LADS OPC UA counterparts (the `lads` compose profile).
    "lads": [
        "lads_centrifuge_smoke.py",
        "lads_plateloc_smoke.py",
        "lads_seal_remover_smoke.py",
        "lads_thermal_cycler_smoke.py",
        "lads_ardea_smoke.py",
    ],
}


def main() -> int:
    # Only the timeout is forwarded: the individual scripts already default to the compose
    # stack's published ports, and overriding those per script is not something this runner
    # needs to do (run the script directly for that).
    parser = argparse.ArgumentParser(description="Run all per-server smoke test scripts.")
    parser.add_argument(
        "--protocol",
        choices=["sila2", "lads", "both"],
        default="sila2",
        help="Which servers to exercise (both: SiLA2 first, then LADS -- never at once, they share one world)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout in seconds passed to each smoke test",
    )
    args = parser.parse_args()

    # Resolve scripts relative to this file, so the runner works from any working directory.
    samples_dir = Path(__file__).resolve().parent
    failures: list[str] = []

    protocols = ["sila2", "lads"] if args.protocol == "both" else [args.protocol]
    for script_name in (name for protocol in protocols for name in SCRIPTS[protocol]):
        script_path = samples_dir / script_name
        # The banner is what makes the interleaved child output readable, since the children
        # inherit stdout/stderr rather than having it captured.
        print(f"== Running {script_name} ==")
        # check=False on purpose: a failing script is recorded and the run continues, so one
        # invocation reports every broken server rather than just the first.
        result = subprocess.run(
            [sys.executable, str(script_path), "--timeout", str(args.timeout)],
            check=False,
        )
        if result.returncode != 0:
            failures.append(script_name)

    # Summary at the end, so the verdict is visible without scrolling back through the
    # per-script output.
    if failures:
        print("Smoke test failures:")
        for script_name in failures:
            print(f"- {script_name}")
        return 1

    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
