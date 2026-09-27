"""Parity check: the same scenario over SiLA2 and over LADS OPC UA must end the same way.

Both protocols present the same instrument behaviour (`instruments/`), so a workflow should not be
able to tell them apart by what happens -- only by how it is asked. This script holds them to
that, end to end, against the running stack with both profiles up:

1. **The world.** It runs the full roundabout (`run_roundabout.py`) over SiLA2, snapshots the
   world model, then runs the same circuit over LADS (`run_lads_roundabout.py`) and snapshots
   again. The two snapshots must be identical once the plate's minted `item_id` is set aside
   (each run mints its own plate; each roundabout already checks that its plate came home).
2. **The instruments.** Readouts taken before and after each circuit must move by the same
   amount on both protocols -- one more PlateLoc cycle, one unit of tape per spool.
3. **The failures.** The same mistake -- a cycle with no plate, a run out of order, a transfer to
   a station the machine does not have -- must be refused with the same reason. SiLA2 prefixes the
   exception class and LADS the unit and method, so it is the instrument's message that is compared.

Prerequisite: the stack is up with both profiles (`--profile sila2 --profile lads`). The two
protocols are driven one after the other, never at once: they share one world. Exit code 0 means
every check agreed.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from typing import Any

import run_lads_roundabout
import run_roundabout
from common import (
    DEFAULT_LABORATORY_MODEL_URL,
    add_item_to_location,
    connect,
    request_laboratory_model,
    reset_laboratory_model,
    wait_for_observable,
)
from lads_client import DEFAULT_HOST, LadsCallError, expect
from lads_client import connect as lads_connect

SILA2_PLATELOC_PORT = run_roundabout.PLATELOC_PORT
SILA2_SEAL_REMOVER_PORT = run_roundabout.PLATE_SEAL_REMOVER_PORT
SILA2_THERMAL_CYCLER_PORT = run_roundabout.THERMAL_CYCLER_PORT
SILA2_ARDEA_PORT = run_roundabout.ARDEA_PORT


def normalised_world(laboratory_model_url: str) -> dict[str, Any]:
    """The world snapshot with each minted item id replaced by a marker, so two runs compare."""
    snapshot = copy.deepcopy(request_laboratory_model(laboratory_model_url=laboratory_model_url, path="/state"))
    for device in snapshot["devices"]:
        for spot in device["spots"]:
            if spot.get("item_id") is not None:
                spot["item_id"] = "<item>"
    return snapshot


def instrument_message(message: str) -> str:
    """The instrument's own reason, without the protocol's framing around it."""
    for prefix in ("ValueError - ", "RuntimeError - ", "InvalidStation: "):
        if prefix in message:
            message = message.split(prefix, 1)[1]
    # LADS LastError reads "<Unit>.<Method>: <reason>".
    head, _, tail = message.partition(": ")
    return tail if tail and "." in head and " " not in head else message


# --- Readouts ---------------------------------------------------------------------------------


def sila2_readouts(host: str, timeout: float) -> dict[str, float]:
    with connect(host, SILA2_PLATELOC_PORT, insecure=True) as plateloc:
        cycles = plateloc.PlateLocController.CycleCount.get()
    with connect(host, SILA2_SEAL_REMOVER_PORT, insecure=True) as peeler:
        tape = wait_for_observable(
            peeler.AutomatedPlateSealRemoverController.GetTapeLeft(), label="GetTapeLeft", timeout_seconds=timeout
        )
    return {"cycles": cycles, "supply": tape.SupplySpoolRemaining, "takeup": tape.TakeUpSpoolRemaining}


async def lads_readouts(host: str, offset: int, timeout: float) -> dict[str, float]:
    port = run_lads_roundabout.PLATELOC_PORT + offset
    async with lads_connect(host, port, "PlateLoc", "Sealer", timeout=timeout) as sealer:
        cycles = await sealer.read("CycleCount")
    port = run_lads_roundabout.SEAL_REMOVER_PORT + offset
    async with lads_connect(host, port, "SealRemover", "Peeler", timeout=timeout) as peeler:
        supply = await peeler.read("FunctionSet.SupplySpool.SensorValue")
        takeup = await peeler.read("FunctionSet.TakeUpSpool.SensorValue")
    return {"cycles": cycles, "supply": supply, "takeup": takeup}


def delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {key: after[key] - before[key] for key in before}


# --- Failures -----------------------------------------------------------------------------------


def sila2_failures(host: str, timeout: float, laboratory_model_url: str) -> list[str]:
    reasons: list[str] = []
    # A cycle with no plate in the sealer.
    reset_laboratory_model(laboratory_model_url=laboratory_model_url)
    with connect(host, SILA2_PLATELOC_PORT, insecure=True) as plateloc:
        try:
            wait_for_observable(plateloc.PlateLocController.StartCycle(), label="StartCycle", timeout_seconds=timeout)
            raise AssertionError("SiLA2 StartCycle without a plate succeeded")
        except AssertionError:
            raise
        except Exception as error:
            reasons.append(instrument_message(str(error)))
    # A run out of order: StartRun on a freshly reset cycler (needs a plate to get that far).
    add_item_to_location(laboratory_model_url=laboratory_model_url, location=run_roundabout.THERMAL_CYCLER_LOCATION)
    with connect(host, SILA2_THERMAL_CYCLER_PORT, insecure=True) as cycler_client:
        cycler = cycler_client.AutomatedThermalCyclerController
        wait_for_observable(cycler.Reset(), label="Reset", timeout_seconds=timeout)
        try:
            wait_for_observable(cycler.StartRun(), label="StartRun", timeout_seconds=timeout)
            raise AssertionError("SiLA2 StartRun before Validate succeeded")
        except AssertionError:
            raise
        except Exception as error:
            reasons.append(instrument_message(str(error)))
        wait_for_observable(cycler.Reset(), label="Reset", timeout_seconds=timeout)
    # A transfer to a station the machine does not have.
    with connect(host, SILA2_ARDEA_PORT, insecure=True) as ardea:
        try:
            wait_for_observable(
                ardea.LabwareService.Transfer(SourceStation="Base1", DestinationStation="Nowhere"),
                label="Transfer",
                timeout_seconds=timeout,
            )
            raise AssertionError("SiLA2 Transfer to an unknown station succeeded")
        except AssertionError:
            raise
        except Exception as error:
            reasons.append(instrument_message(str(error)))
    return reasons


async def lads_failures(host: str, offset: int, timeout: float, laboratory_model_url: str) -> list[str]:
    reasons: list[str] = []
    reset_laboratory_model(laboratory_model_url=laboratory_model_url)
    port = run_lads_roundabout.PLATELOC_PORT + offset
    async with lads_connect(host, port, "PlateLoc", "Sealer", timeout=timeout) as sealer:
        try:
            await sealer.start_program("StartCycle")
            raise AssertionError("LADS StartCycle without a plate succeeded")
        except LadsCallError as error:
            reasons.append(instrument_message(error.last_error))
    add_item_to_location(laboratory_model_url=laboratory_model_url, location=run_roundabout.THERMAL_CYCLER_LOCATION)
    port = run_lads_roundabout.THERMAL_CYCLER_PORT + offset
    async with lads_connect(host, port, "ThermalCycler", "Cycler", timeout=timeout) as cycler:
        await cycler.reset()
        result = await cycler.run_program("StartRun")
        expect(result["Outcome"] == "Failed", "LADS StartRun before Validate did not fail")
        reasons.append(instrument_message(await cycler.last_error()))
        await cycler.clear()
    port = run_lads_roundabout.ARDEA_PORT + offset
    async with lads_connect(host, port, "Ardea", "Transporter", timeout=timeout) as ardea:
        result = await ardea.run_program("Transfer", SourceStation="Base1", DestinationStation="Nowhere")
        expect(result["Outcome"] == "Failed", "LADS Transfer to an unknown station did not fail")
        reasons.append(instrument_message(await ardea.last_error()))
    return reasons


# --- The check ----------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that SiLA2 and LADS OPC UA behave the same.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host both stacks are published on")
    parser.add_argument("--lads-port-offset", type=int, default=0, help="Added to every LADS server's default port")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds per step")
    parser.add_argument("--laboratory-model-url", default=DEFAULT_LABORATORY_MODEL_URL, help="Laboratory model URL")
    args = parser.parse_args()
    host, offset, timeout, url = args.host, args.lads_port_offset, args.timeout, args.laboratory_model_url

    # 1 + 2: the roundabout over each protocol, with readouts around it.
    print("== SiLA2 roundabout ==")
    before = sila2_readouts(host, timeout)
    item = run_roundabout.setup_initial_laboratory_state(laboratory_model_url=url)
    run_roundabout.run_roundabout_sequence(host=host, insecure=True, timeout_seconds=timeout, laboratory_model_url=url)
    run_roundabout.verify_final_laboratory_state(laboratory_model_url=url, expected_item_id=item)
    sila2_world = normalised_world(url)
    sila2_delta = delta(before, sila2_readouts(host, timeout))

    print("== LADS roundabout ==")
    before = asyncio.run(lads_readouts(host, offset, timeout))
    item = run_roundabout.setup_initial_laboratory_state(laboratory_model_url=url)
    lads_args = argparse.Namespace(host=host, port_offset=offset, timeout=timeout, laboratory_model_url=url)
    asyncio.run(run_lads_roundabout.run_roundabout_sequence(lads_args))
    run_roundabout.verify_final_laboratory_state(laboratory_model_url=url, expected_item_id=item)
    lads_world = normalised_world(url)
    lads_delta = delta(before, asyncio.run(lads_readouts(host, offset, timeout)))

    print(f"Instrument changes: SiLA2 {sila2_delta}, LADS {lads_delta}")
    expect(sila2_world == lads_world, f"the world differs after the roundabout:\n{sila2_world}\n{lads_world}")
    expect(sila2_delta == lads_delta, "the instruments moved differently")

    # 3: the same mistakes, the same reasons.
    print("== Failures ==")
    sila2_reasons = sila2_failures(host, timeout, url)
    lads_reasons = asyncio.run(lads_failures(host, offset, timeout, url))
    for sila2_reason, lads_reason in zip(sila2_reasons, lads_reasons, strict=True):
        print(f"  SiLA2: {sila2_reason}\n  LADS:  {lads_reason}")
        expect(sila2_reason == lads_reason, "the two protocols gave different reasons")

    print("Parity check passed: SiLA2 and LADS OPC UA behaved the same.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
