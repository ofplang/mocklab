"""Smoke test for the automated plate seal remover (peeler) LADS OPC UA server.

The LADS counterpart of `sila2_seal_remover_smoke.py`: Peel is a program whose
two parameters are properties and whose warning lands in the Result; the tape reserves that SiLA2
reads with GetTapeLeft are two sensor functions here.

Prerequisite: the stack is up with the `lads` profile. Exit code 0 means the sequence passed.
"""

from __future__ import annotations

import argparse
import asyncio

from common import ensure_item_at_location
from lads_client import LadsCallError, build_parser, connect, expect

DEFAULT_PORT = 4843
DEFAULT_LOCATION = "seal-remover.stage"


async def run(args: argparse.Namespace) -> None:
    ensure_item_at_location(laboratory_model_url=args.laboratory_model_url, location=args.laboratory_model_location)
    async with connect(args.host, args.port, "SealRemover", "Peeler", timeout=args.timeout) as peeler:
        await peeler.reset()
        expect(await peeler.read("FunctionSet.SupplySpool.SensorValue") == 1200.0, "reset did not refill the tape")

        # Out-of-range parameters are the instrument's to refuse, before the peel starts.
        try:
            await peeler.start_program("Peel", BeginPeelLocation=10, AdhesionTime=1)
            raise AssertionError("BeginPeelLocation=10 was accepted")
        except LadsCallError as error:
            print(f"BeginPeelLocation=10 refused as expected: {error}")
            expect(error.status == "BadInvalidArgument", f"unexpected status {error.status}")

        result = await peeler.run_program("Peel", BeginPeelLocation=1, AdhesionTime=1)
        print(f"Peel result: {result}")
        expect(result["Outcome"] == "Completed", "Peel did not complete")
        expect(result["InstrumentWarningMessage"] == "", "unexpected tape warning")
        supply = await peeler.read("FunctionSet.SupplySpool.SensorValue")
        takeup = await peeler.read("FunctionSet.TakeUpSpool.SensorValue")
        print(f"Tape after peel: supply={supply} take-up={takeup}")
        expect((supply, takeup) == (1199.0, 1199.0), "Peel did not consume tape")

        result = await peeler.run_program("ResetInstrument")
        expect(result["Outcome"] == "Completed", "ResetInstrument did not complete")
        await peeler.reset()
        print("Seal remover LADS smoke test passed.")


def main() -> int:
    parser = build_parser("Smoke test the seal remover LADS server.", DEFAULT_PORT, DEFAULT_LOCATION)
    asyncio.run(run(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
