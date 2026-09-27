"""The automated plate seal remover (peeler), served over LADS OPC UA.

The instrument is `mock_instruments.seal_remover.SealRemover` -- the same object the SiLA2 server
of this peeler presents. This module only decides where each part of it appears in the LADS model:

    SiLA2 (AutomatedPlateSealRemoverController)   LADS (DeviceSet/SealRemover, unit "Peeler")
    Peel(BeginPeelLocation, AdhesionTime)          program "Peel" with those two properties;
      -> InstrumentWarningMessage                    the warning is in the Result's Properties
    ResetInstrument                                program "ResetInstrument"
    Reset                                          FunctionalUnitState.Abort + Clear
    Status                                         FunctionalUnitState.CurrentState
    GetTapeLeft -> SupplySpoolRemaining,           FunctionSet/SupplySpool and TakeUpSpool
                   TakeUpSpoolRemaining,             (AnalogScalarSensorFunction), plus the vendor
                   InstrumentWarningMessage          variable Peeler/InstrumentWarningMessage

Differences LADS forces are listed in `docs/LADS_MAPPING.md`.
"""

from __future__ import annotations

from asyncua import ua

from lads_common import (
    ONE,
    AnalogSensorFunction,
    DeviceIdentity,
    FunctionalUnit,
    LadsDevice,
    ProgramDefinition,
    ProgramParameters,
    ServerContext,
    ServerSpec,
)
from mock_instruments.runtime import Execution
from mock_instruments.seal_remover import SPOOL_CAPACITY, SealRemover

SPEC = ServerSpec(
    server_type="AutomatedPlateSealRemoverServer",
    default_name="AutomatedPlateSealRemoverServer",
    description="Mock LADS OPC UA server for the Azenta automated plate seal remover",
)


async def build(context: ServerContext) -> None:
    # The instrument reads its world-model wiring and durations from the environment; a variable
    # that is set but unusable raises here, so a misconfigured container fails at startup.
    instrument = SealRemover.from_environment()

    device = await LadsDevice.create(
        context.builder,
        "SealRemover",
        DeviceIdentity(
            manufacturer="Azenta",
            model="Automated Plate Seal Remover (mock)",
            serial_number="MOCK-SEAL-REMOVER-1",
        ),
        asset_id=context.server_name,
    )
    # This feature has no stop command, so Stop only reports the state.
    unit = await FunctionalUnit.create(
        device, context.bridge, "Peeler", status=instrument.status, clear=instrument.reset
    )

    # Peel's two parameters are required and whole numbers, as in SiLA2; their ranges are the
    # instrument's to check. The warning it returns is recorded in the Result under its SiLA2 name.
    def peel(parameters: ProgramParameters, execution: Execution) -> dict[str, object]:
        warning = instrument.peel(parameters.as_int("BeginPeelLocation"), parameters.as_int("AdhesionTime"), execution)
        return {"InstrumentWarningMessage": warning}

    await unit.add_program(
        ProgramDefinition(
            template_id="Peel",
            description="Peel the seal off the plate. Properties: BeginPeelLocation (1..9), AdhesionTime (1..4).",
            command=peel,
            estimated_runtime_seconds=instrument.durations.duration_of("Peel"),
        )
    )
    await unit.add_program(
        ProgramDefinition(
            template_id="ResetInstrument",
            description="Reinitialise the instrument's mechanics.",
            command=lambda parameters, execution: instrument.reset_instrument(execution),
            estimated_runtime_seconds=instrument.durations.duration_of("ResetInstrument"),
        )
    )

    # GetTapeLeft as live readouts rather than a command: the two reserves are measured values.
    for name, read in (
        ("SupplySpool", lambda: instrument.get_tape_left().supply_spool_remaining),
        ("TakeUpSpool", lambda: instrument.get_tape_left().takeup_spool_remaining),
    ):
        await AnalogSensorFunction.create(unit, name, engineering_units=ONE, low=0, high=SPOOL_CAPACITY, value=read)
    await unit.add_readout(
        "InstrumentWarningMessage", lambda: instrument.get_tape_left().warning, ua.VariantType.String
    )

    await device.mark_operational()
