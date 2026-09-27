"""The microplate centrifuge, served over LADS OPC UA.

The instrument is `mock_instruments.centrifuge.Centrifuge` -- the same object the SiLA2 server of
this centrifuge presents. This module only decides where each part of it appears in the LADS model:

    SiLA2 (MicroplateCentrifugeController)   LADS (DeviceSet/Centrifuge, unit "Centrifuge")
    OpenDoor(BucketNumber) / CloseDoor       FunctionSet/Door (CoverFunction) Open / Close
    SpinCycle(15 parameters)                 program "SpinCycle", the same 15 properties
    LoadPlate / UnloadPlate(5 parameters)    programs "LoadPlate" / "UnloadPlate"
    Home / Park                              programs "Home" / "Park"
    StopSpinCycle(BucketNumber)              FunctionalUnitState.Stop
    Reset                                    FunctionalUnitState.Abort + Clear
    Status                                   FunctionalUnitState.CurrentState
    EnumerateProfiles                        vendor variable Centrifuge/Profiles
    FirmwareVersion / HardwareVersion        Identification/SoftwareRevision / HardwareRevision
    ActiveXVersion, CentrifugeActiveX...,    vendor variables of the same names
    CentrifugeHardwareVersion

LADS' Cover Open and FunctionalUnitState Stop take no arguments, so the door is opened to bucket 1
and StopSpinCycle names bucket 1. The mock only records which bucket a door was opened to, and
nothing reads it back, so no client can tell -- but it is listed in `docs/LADS_MAPPING.md` with the
other differences LADS forces.
"""

from __future__ import annotations

from asyncua import ua

from lads_common import (
    CoverFunction,
    DeviceIdentity,
    FunctionalUnit,
    LadsDevice,
    ProgramDefinition,
    ProgramParameters,
    ServerContext,
    ServerSpec,
)
from mock_instruments.centrifuge import Centrifuge
from mock_instruments.runtime import Execution

SPEC = ServerSpec(
    server_type="MicroplateCentrifugeServer",
    default_name="MicroplateCentrifugeServer",
    description="Mock LADS OPC UA server for the Agilent microplate centrifuge",
)

# The bucket LADS' argument-less Open and Stop stand for.
DEFAULT_BUCKET = 1

# SpinCycle's parameters, as SiLA2 declares them. All are required and typed on both protocols;
# only Time is acted on (the others are accepted for signature fidelity, as in SiLA2).
SPIN_CYCLE_FLOATS = (
    "VelocityPercent",
    "AccelerationPercent",
    "DecelerationPercent",
    "GripperOffsetToLoad",
    "GripperOffsetToUnload",
    "PlateHeightToLoad",
    "PlateHeightToUnload",
)
SPIN_CYCLE_INTEGERS = (
    "TimerMode",
    "Time",
    "BucketNumberToLoad",
    "BucketNumberToUnload",
    "SpeedToLoad",
    "SpeedToUnload",
    "OptionsToLoad",
    "OptionsToUnload",
)
# LoadPlate's and UnloadPlate's parameters; only BucketNumber is acted on.
PLATE_FLOATS = ("GripperOffset", "PlateHeight")
PLATE_INTEGERS = ("BucketNumber", "Speed", "Options")


def _require(parameters: ProgramParameters, floats: tuple[str, ...], integers: tuple[str, ...]) -> dict[str, float]:
    """Check every declared parameter is present and well-typed, returning them by name."""
    values: dict[str, float] = {name: parameters.as_float(name) for name in floats}
    values.update({name: parameters.as_int(name) for name in integers})
    return values


async def build(context: ServerContext) -> None:
    # The instrument reads its world-model wiring and durations from the environment; a variable
    # that is set but unusable raises here, so a misconfigured container fails at startup.
    instrument = Centrifuge.from_environment()

    device = await LadsDevice.create(
        context.builder,
        "Centrifuge",
        DeviceIdentity(
            manufacturer="Agilent",
            model="Microplate Centrifuge (mock)",
            serial_number="MOCK-CENTRIFUGE-1",
            software_revision=instrument.firmware_version,
            hardware_revision=instrument.hardware_version,
        ),
        asset_id=context.server_name,
    )
    unit = await FunctionalUnit.create(
        device,
        context.bridge,
        "Centrifuge",
        status=instrument.status,
        stop=lambda execution: instrument.stop_spin_cycle(DEFAULT_BUCKET, execution),
        clear=instrument.reset,
    )

    # --- Programs. ---

    def spin_cycle(parameters: ProgramParameters, execution: Execution) -> None:
        values = _require(parameters, SPIN_CYCLE_FLOATS, SPIN_CYCLE_INTEGERS)
        instrument.spin_cycle(int(values["Time"]), execution)

    def load_plate(parameters: ProgramParameters, execution: Execution) -> None:
        values = _require(parameters, PLATE_FLOATS, PLATE_INTEGERS)
        instrument.load_plate(int(values["BucketNumber"]), execution)

    def unload_plate(parameters: ProgramParameters, execution: Execution) -> None:
        values = _require(parameters, PLATE_FLOATS, PLATE_INTEGERS)
        instrument.unload_plate(int(values["BucketNumber"]), execution)

    plate_properties = ", ".join((*PLATE_INTEGERS, *PLATE_FLOATS))
    for template_id, description, command in (
        ("SpinCycle", "Spin the plate. Properties: the fifteen SpinCycle parameters of the SiLA2 feature.", spin_cycle),
        ("LoadPlate", f"Load a plate into a bucket. Properties: {plate_properties}.", load_plate),
        ("UnloadPlate", f"Unload a plate from a bucket. Properties: {plate_properties}.", unload_plate),
        ("Home", "Return the rotor to its home position.", lambda parameters, execution: instrument.home(execution)),
        ("Park", "Move the rotor to its park position.", lambda parameters, execution: instrument.park(execution)),
    ):
        await unit.add_program(
            ProgramDefinition(
                template_id=template_id,
                description=description,
                command=command,
                estimated_runtime_seconds=instrument.durations.duration_of(template_id),
            )
        )

    # --- The door. It starts open, matching the world's resting state (the seed's `door: open`,
    # --- and the deck is accessible until a CloseDoor locks it).
    await CoverFunction.create(
        unit,
        "Door",
        open=lambda execution: instrument.open_door(DEFAULT_BUCKET, execution),
        close=instrument.close_door,
        initially_open=True,
    )

    # --- Readouts with no LADS counterpart. ---
    await unit.add_readout("Profiles", lambda: list(instrument.profiles), ua.VariantType.String)
    await unit.add_readout("ActiveXVersion", lambda: instrument.active_x_version, ua.VariantType.String)
    await unit.add_readout(
        "CentrifugeActiveXVersion", lambda: instrument.centrifuge_active_x_version, ua.VariantType.String
    )
    await unit.add_readout(
        "CentrifugeHardwareVersion", lambda: instrument.centrifuge_hardware_version, ua.VariantType.String
    )

    await device.mark_operational()
