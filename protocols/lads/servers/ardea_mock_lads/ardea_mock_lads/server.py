"""Ardea, the lab's transporter, served over LADS OPC UA.

The transporter is `mock_instruments.ardea.Ardea` -- the same object the SiLA2 server of Ardea
presents. This module only decides where each part of it appears in the LADS model:

    SiLA2 (Ardea's nine features)              LADS (DeviceSet/Ardea, unit "Transporter")
    LabwareService.Transfer(SourceStation,     program "Transfer" with those two properties;
      DestinationStation)                        its phases are ActiveProgram.CurrentStepName,
      -> CarriagePosition, AtRetractPose         its responses are in the Result's Properties
    CarriageService.CarriagePosition           FunctionSet/Carriage (AnalogScalarSensorFunction, mm),
                                                 updated as the carriage moves mid-transfer
    CarriageService.StationNames               vendor variable Transporter/StationNames
    LabwareService.LightIsOn                   vendor variable Transporter/LightIsOn, re-read from the
                                                 world model twice a second
    InvalidStation (a declared SiLA2 error)    the run fails; the reason is in Transporter/LastError

Only what the mock actually implements is served. The SiLA2 server declares the real machine's
other twenty-two commands and refuses them, because a SiLA2 mock has to carry the machine's own
Feature definitions to be swappable with it; there is no LADS Ardea to be swappable with, so here
they are simply absent.

Like the SiLA2 features, the transporter has no Status: FunctionalUnitState is Running while a
transfer runs and Stopped otherwise. A transfer to an unknown station fails after it has begun, as
on SiLA2 (the station map is consulted inside the command), so it is a Failed run rather than a
refused call. Differences LADS forces are listed in `docs/LADS_MAPPING.md`.
"""

from __future__ import annotations

from asyncua import ua

from lads_common import (
    MILLIMETRE,
    AnalogSensorFunction,
    DeviceIdentity,
    FunctionalUnit,
    LadsDevice,
    ProgramDefinition,
    ProgramParameters,
    ServerContext,
    ServerSpec,
)
from mock_instruments.ardea import STATION_PITCH_MM, Ardea
from mock_instruments.runtime import Execution

SPEC = ServerSpec(
    server_type="ArdeaLadsServer",
    default_name="Ardea",
    description="Mock LADS OPC UA server for Ardea, a robot arm on a travel carriage",
)


async def build(context: ServerContext) -> None:
    # The transporter reads its world-model wiring, its station map (ARDEA_STATIONS) and its
    # durations from the environment; anything set but unusable -- a typo in the station map
    # included -- raises here, so a misconfigured container fails at startup.
    instrument = Ardea.from_environment()

    device = await LadsDevice.create(
        context.builder,
        "Ardea",
        DeviceIdentity(
            # The real machine is an integration (a DENSO arm on a KEYENCE-controlled carriage) with
            # no single catalogue vendor, so the mock names itself rather than guess one.
            manufacturer="ofplang mock lab",
            model="Ardea labware transporter (mock)",
            serial_number="MOCK-ARDEA-1",
        ),
        asset_id=context.server_name,
    )
    # No Status, and no stop or reset command: Stop has nothing to run, and Clear only lifts an
    # Abort.
    unit = await FunctionalUnit.create(device, context.bridge, "Transporter", status=None)

    def transfer(parameters: ProgramParameters, execution: Execution) -> dict[str, object]:
        result = instrument.transfer(
            parameters.as_str("SourceStation"), parameters.as_str("DestinationStation"), execution
        )
        # Recorded under the SiLA2 response names.
        return {"CarriagePosition": result.carriage_position, "AtRetractPose": result.at_retract_pose}

    stations = ", ".join(instrument.station_names())
    await unit.add_program(
        ProgramDefinition(
            template_id="Transfer",
            description=(
                "Carry a labware from one station to another. Properties: SourceStation, "
                f"DestinationStation (one of {stations})."
            ),
            command=transfer,
            estimated_runtime_seconds=instrument.durations.duration_of("Transfer"),
        )
    )

    # The carriage position is synthetic (a station's index times a fixed pitch), so its range is
    # exactly the stations' span.
    await AnalogSensorFunction.create(
        unit,
        "Carriage",
        engineering_units=MILLIMETRE,
        low=0.0,
        high=(len(instrument.station_names()) - 1) * STATION_PITCH_MM,
        value=lambda: instrument.carriage_position.value,
        observable=instrument.carriage_position,
    )
    await unit.add_readout("StationNames", instrument.station_names, ua.VariantType.String)
    await unit.add_polled_readout(
        "LightIsOn",
        lambda: instrument.light_is_on(command_name="LabwareService.LightIsOn"),
        ua.VariantType.Boolean,
        initial=False,
    )

    await device.mark_operational()
