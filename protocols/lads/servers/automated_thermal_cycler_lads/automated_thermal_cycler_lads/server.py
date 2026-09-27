"""The automated thermal cycler, served over LADS OPC UA.

The instrument is `mock_instruments.thermal_cycler.ThermalCycler` -- the same object the SiLA2
server of this cycler presents. This module only decides where each part of it appears in the LADS
model:

    SiLA2 (AutomatedThermalCyclerController)   LADS (DeviceSet/ThermalCycler, unit "Cycler")
    Load(ProtocolFileData)                     program "Load", property ProtocolFileData (text)
    Validate(MaxSampleVolume)                  program "Validate", property MaxSampleVolume
    StartRun                                   program "StartRun"
    StopRun                                    FunctionalUnitState.Stop
    OpenLid / CloseLid                         FunctionSet/Lid (CoverFunction) Open / Close
    Reset                                      FunctionalUnitState.Abort + Clear
    Status                                     FunctionalUnitState.CurrentState
    GetInstrumentState                         vendor variable Cycler/InstrumentState
    ElapsedTime / RemainingTime                vendor variables of the same names

Load, Validate and StartRun stay three programs, in the order the instrument enforces, rather than
being folded into one: the instrument's rules -- and the failures a client sees when it breaks the
order -- are then the same on both protocols.

StartRun leaves the instrument Running after the command returns (the run continues until
StopRun), so its program completes while the unit stays Running until Stop. That is the SiLA2
Status contract, shown through the derived FunctionalUnitState.

Differences LADS forces are listed in `docs/LADS_MAPPING.md`.
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
from mock_instruments.errors import InvalidArgument
from mock_instruments.runtime import Execution
from mock_instruments.thermal_cycler import ThermalCycler

SPEC = ServerSpec(
    server_type="AutomatedThermalCyclerServer",
    default_name="AutomatedThermalCyclerServer",
    description="Mock LADS OPC UA server for the Thermo Fisher automated thermal cycler",
)


async def build(context: ServerContext) -> None:
    # The instrument reads its world-model wiring and durations from the environment; a variable
    # that is set but unusable raises here, so a misconfigured container fails at startup.
    instrument = ThermalCycler.from_environment()

    device = await LadsDevice.create(
        context.builder,
        "ThermalCycler",
        DeviceIdentity(
            manufacturer="Thermo Fisher Scientific",
            model="Automated Thermal Cycler (mock)",
            serial_number="MOCK-THERMAL-CYCLER-1",
        ),
        asset_id=context.server_name,
    )
    unit = await FunctionalUnit.create(
        device, context.bridge, "Cycler", status=instrument.status, stop=instrument.stop_run, clear=instrument.reset
    )

    # --- Programs. ---

    def load(parameters: ProgramParameters, execution: Execution) -> None:
        # The protocol file travels as text (a KeyValueType value is a string) and reaches the
        # instrument as its UTF-8 bytes. Absent is refused like any missing parameter; *empty* is
        # passed on, because the instrument checks that during execution -- an empty protocol
        # faults the cycler on both protocols.
        if "ProtocolFileData" not in parameters.values:
            raise InvalidArgument("Property 'ProtocolFileData' is required")
        instrument.load(parameters.values["ProtocolFileData"].encode("utf-8"), execution)

    def validate(parameters: ProgramParameters, execution: Execution) -> None:
        instrument.validate(parameters.as_float("MaxSampleVolume"), execution)

    for template_id, description, command in (
        ("Load", "Load a protocol. Property: ProtocolFileData (the protocol file as text).", load),
        ("Validate", "Validate the loaded protocol. Property: MaxSampleVolume.", validate),
        (
            "StartRun",
            "Start the validated protocol. The unit stays Running until Stop.",
            lambda parameters, execution: instrument.start_run(execution),
        ),
    ):
        await unit.add_program(
            ProgramDefinition(
                template_id=template_id,
                description=description,
                command=command,
                estimated_runtime_seconds=instrument.durations.duration_of(template_id),
            )
        )

    # --- The lid. It starts open, matching the world's resting state (the seed's `lid: open`). ---
    await CoverFunction.create(unit, "Lid", open=instrument.open_lid, close=instrument.close_lid, initially_open=True)

    # --- Readouts with no LADS counterpart. The two times are published as they change. ---
    await unit.add_readout("InstrumentState", lambda: int(instrument.get_instrument_state()), ua.VariantType.Int32)
    await unit.add_readout(
        "ElapsedTime", lambda: instrument.elapsed_time.value, ua.VariantType.String, observable=instrument.elapsed_time
    )
    await unit.add_readout(
        "RemainingTime",
        lambda: instrument.remaining_time.value,
        ua.VariantType.String,
        observable=instrument.remaining_time,
    )

    await device.mark_operational()
