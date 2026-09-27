"""The PlateLoc plate sealer, served over LADS OPC UA.

The instrument is `mock_instruments.plateloc.PlateLoc` -- the same object the SiLA2 server of
this sealer presents. This module only decides where each part of it appears in the LADS model:

    SiLA2 (PlateLocController)            LADS (Objects/DeviceSet/PlateLoc, unit "Sealer")
    StartCycle                            program "StartCycle"
    StopCycle                             FunctionalUnitState.Stop
    Reset                                 FunctionalUnitState.Abort + Clear (Clear alone from Error)
    Status                                FunctionalUnitState.CurrentState
    SealingTemperature / SetSealing...    FunctionSet/SealingTemperature (AnalogControlFunction):
    ActualTemperature                       TargetValue is the set-point, CurrentValue the heater
    SealingTime / SetSealingTime          FunctionSet/SealingTime (TimerControlFunction), in ms
    CycleCount                            vendor variable Sealer/CycleCount
    EnumerateProfiles                     vendor variable Sealer/Profiles
    FirmwareVersion                       Identification/SoftwareRevision
    Version                               vendor variable Sealer/Version

Differences LADS forces are listed in `docs/LADS_MAPPING.md`.
"""

from __future__ import annotations

from asyncua import ua

from lads_common import (
    CELSIUS,
    AnalogControlFunction,
    DeviceIdentity,
    FunctionalUnit,
    LadsDevice,
    ProgramDefinition,
    ServerContext,
    ServerSpec,
    TimerControlFunction,
)
from mock_instruments.errors import InvalidArgument
from mock_instruments.plateloc import PlateLoc

SPEC = ServerSpec(
    server_type="PlateLocServer",
    default_name="PlateLocServer",
    description="Mock LADS OPC UA server for the Agilent PlateLoc plate sealer",
)

# OPC UA durations are milliseconds; the instrument keeps seconds, as SiLA2 does.
MILLISECONDS_PER_SECOND = 1000.0


async def build(context: ServerContext) -> None:
    # The instrument reads its world-model wiring and durations from the environment; a variable
    # that is set but unusable raises here, so a misconfigured container fails at startup.
    instrument = PlateLoc.from_environment()

    device = await LadsDevice.create(
        context.builder,
        "PlateLoc",
        DeviceIdentity(
            manufacturer="Agilent",
            model="PlateLoc Thermal Microplate Sealer (mock)",
            serial_number="MOCK-PLATELOC-1",
            software_revision=instrument.firmware_version,
        ),
        asset_id=context.server_name,
    )
    unit = await FunctionalUnit.create(
        device, context.bridge, "Sealer", status=instrument.status, stop=instrument.stop_cycle, clear=instrument.reset
    )

    await unit.add_program(
        ProgramDefinition(
            template_id="StartCycle",
            description="Seal the plate at the current sealing temperature and time.",
            command=lambda parameters, execution: instrument.start_cycle(execution),
            estimated_runtime_seconds=instrument.durations.duration_of("StartCycle"),
        )
    )

    # The heater: the set-point is the sealing temperature, the value it acts on the actual
    # temperature. A write is SetSealingTemperature, which takes whole degrees -- a fractional
    # write is refused rather than silently truncated.
    def set_sealing_temperature(value: float) -> None:
        if not value.is_integer():
            raise InvalidArgument("SealingTemperature must be a whole number of degrees")
        instrument.set_sealing_temperature(int(value))

    await AnalogControlFunction.create(
        unit,
        "SealingTemperature",
        engineering_units=CELSIUS,
        low=0,
        high=None,
        current=lambda: instrument.actual_temperature,
        target=lambda: instrument.sealing_temperature,
        set_target=set_sealing_temperature,
    )
    # The sealing time has no measured counterpart, so CurrentValue shows the set-point too.
    await TimerControlFunction.create(
        unit,
        "SealingTime",
        low_ms=0,
        high_ms=None,
        current_ms=lambda: instrument.sealing_time * MILLISECONDS_PER_SECOND,
        target_ms=lambda: instrument.sealing_time * MILLISECONDS_PER_SECOND,
        set_target_ms=lambda value: instrument.set_sealing_time(value / MILLISECONDS_PER_SECOND),
    )

    await unit.add_readout("CycleCount", lambda: instrument.cycle_count, ua.VariantType.Int32)
    await unit.add_readout("Profiles", lambda: list(instrument.profiles), ua.VariantType.String)
    await unit.add_readout("Version", lambda: instrument.version, ua.VariantType.String)

    await device.mark_operational()
