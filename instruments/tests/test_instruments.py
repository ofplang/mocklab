"""Tests for the protocol-independent instrument behaviour.

These pin down what both the SiLA2 and the LADS OPC UA servers expose, so they are written
against the behaviour a client can observe rather than against internals:

* where execution begins relative to each check -- a check before `execution.begin()` rejects
  a command without faulting the instrument, one after it leaves the instrument in Error;
* how Status moves, including the per-feature exceptions (the thermal cycler's StartRun stays
  Running; its lid commands never leave Idle);
* which kind of error each refusal is, and its exact message (SiLA2 clients read the message);
* what each command does to the world model, and how long it waits.

Nothing here talks HTTP: the world model is replaced by a fake installed over the functions each
instrument module imports from `laboratory_client`.
"""

from __future__ import annotations

from typing import Any

import pytest

from laboratory_client import LaboratoryModelConfig, LaboratoryModelRequestError
from mock_instruments import ardea, centrifuge, plateloc, seal_remover, thermal_cycler
from mock_instruments.errors import InvalidArgument, InvalidState, UnknownStation, WorldError, builtin_errors
from mock_instruments.runtime import CommandDurations, Status

UNCONFIGURED = LaboratoryModelConfig(url=None, location=None)


class RecordingExecution:
    """An `Execution` that records the order of events it is told about."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.phases: list[tuple[str, float]] = []

    def begin(self) -> None:
        self.log.append("begin")

    def report(self, phase: str, progress: float) -> None:
        self.phases.append((phase, progress))


class FakeWorld:
    """Stands in for the laboratory model, recording every request."""

    def __init__(self, *, occupied: bool = True, fail: bool = False, state: Any = None) -> None:
        self.occupied = occupied
        self.fail = fail
        self.state = {"light": True} if state is None else state
        self.requests: list[tuple[str, str, object]] = []

    def get_location(self, *, base_url: str, location: str) -> dict[str, object]:
        self.requests.append(("GET", f"/locations/{location}", None))
        if self.fail:
            raise LaboratoryModelRequestError("boom")
        return {"occupied": self.occupied}

    def request(self, *, base_url: str, path: str, method: str, payload: object = None) -> dict[str, object]:
        self.requests.append((method, path, payload))
        if self.fail:
            raise LaboratoryModelRequestError("boom")
        return {"state": self.state}


def configured(location: str) -> LaboratoryModelConfig:
    return LaboratoryModelConfig(url="http://world", location=location)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeWorld:
    fake = FakeWorld()
    for module in (centrifuge, plateloc, seal_remover, thermal_cycler):
        monkeypatch.setattr(module, "get_location", fake.get_location)
    for module in (centrifuge, thermal_cycler, ardea):
        monkeypatch.setattr(module, "request_laboratory_model", fake.request)
    return fake


def durations(log: list[str], **seconds: float) -> CommandDurations:
    # Record each wait in the shared event log instead of sleeping.
    return CommandDurations(seconds, sleep=lambda duration: log.append(f"sleep {duration:g}"))


def status_log(instrument: Any, log: list[str]) -> None:
    # Record every Status change after construction in the shared event log.
    instrument.status.subscribe(lambda status: log.append(f"status {status.name}"))
    log.clear()


# --- Plumbing. ---


def test_builtin_errors_restores_the_class_sila2_clients_see() -> None:
    # sila2 puts the exception's class name into the message a client reads, so the adapters
    # must raise the built-in type, with the message unchanged.
    with pytest.raises(ValueError) as raised, builtin_errors():
        raise InvalidArgument("SealingTime must be > 0")
    assert type(raised.value) is ValueError
    assert str(raised.value) == "SealingTime must be > 0"

    for kind in (InvalidState, WorldError):
        with pytest.raises(RuntimeError) as raised_runtime, builtin_errors():
            raise kind("x")
        assert type(raised_runtime.value) is RuntimeError


def test_a_second_command_is_refused_while_one_executes() -> None:
    instrument = plateloc.PlateLoc(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    with (
        instrument.guard.executing("StartCycle"),
        pytest.raises(InvalidState, match="^Reset cannot start because StartCycle is still executing on this server$"),
    ):
        instrument.reset()


def test_stop_commands_are_not_guarded() -> None:
    # They exist to end what may be running.
    instrument = thermal_cycler.ThermalCycler(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    with instrument.guard.executing("StartRun"):
        instrument.stop_run()


def test_a_configured_duration_can_be_read_before_waiting_it() -> None:
    durations = CommandDurations({"Peel": 8.0})
    assert (durations.duration_of("Peel"), durations.duration_of("Reset")) == (8.0, 0.0)


def test_an_instrument_comes_up_idle() -> None:
    instrument = seal_remover.SealRemover(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    seen: list[Status] = []
    instrument.status.subscribe(seen.append)
    assert seen == [Status.IDLE]


# --- PlateLoc. ---


def test_plateloc_cycle(world: FakeWorld) -> None:
    log: list[str] = []
    instrument = plateloc.PlateLoc(
        laboratory_model=configured("plateloc.stage"), durations=durations(log, StartCycle=15)
    )
    status_log(instrument, log)
    instrument.set_sealing_temperature(180)

    instrument.start_cycle(RecordingExecution(log))

    assert log == ["begin", "status RUNNING", "sleep 15", "status IDLE"]
    assert (instrument.actual_temperature, instrument.cycle_count) == (180, 1)
    assert world.requests == [("GET", "/locations/plateloc.stage", None)]


def test_plateloc_without_a_plate_rejects_before_execution(world: FakeWorld) -> None:
    world.occupied = False
    log: list[str] = []
    instrument = plateloc.PlateLoc(laboratory_model=configured("plateloc.stage"), durations=durations(log))
    status_log(instrument, log)

    with pytest.raises(
        InvalidState,
        match=r"^PlateLocController\.StartCycle requires an item at location 'plateloc\.stage', "
        r"but no item is present$",
    ):
        instrument.start_cycle(RecordingExecution(log))
    assert log == []


def test_plateloc_world_failure_is_a_world_error(world: FakeWorld) -> None:
    world.fail = True
    instrument = plateloc.PlateLoc(laboratory_model=configured("plateloc.stage"), durations=CommandDurations())
    with pytest.raises(WorldError, match="but laboratory model lookup failed: boom$"):
        instrument.start_cycle()


def test_plateloc_setters_validate() -> None:
    instrument = plateloc.PlateLoc(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    with pytest.raises(InvalidArgument, match="^SealingTime must be > 0$"):
        instrument.set_sealing_time(0)
    with pytest.raises(InvalidArgument, match="^SealingTemperature must be >= 0$"):
        instrument.set_sealing_temperature(-1)
    assert instrument.status.value is Status.IDLE


def test_plateloc_stop_cools_and_reset_restores() -> None:
    instrument = plateloc.PlateLoc(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    instrument.start_cycle()
    instrument.stop_cycle()
    assert instrument.actual_temperature == 170
    instrument.set_sealing_time(3)
    instrument.reset()
    assert (instrument.sealing_temperature, instrument.sealing_time) == (175, 1.5)
    assert (instrument.actual_temperature, instrument.cycle_count) == (25, 0)


# --- Seal remover. ---


def test_peel_consumes_tape_and_warns_when_low(world: FakeWorld) -> None:
    log: list[str] = []
    instrument = seal_remover.SealRemover(
        laboratory_model=configured("seal-remover.stage"), durations=durations(log, Peel=8)
    )
    status_log(instrument, log)

    assert instrument.peel(1, 1, RecordingExecution(log)) == ""
    assert log == ["begin", "status RUNNING", "sleep 8", "status IDLE"]
    assert instrument.get_tape_left().supply_spool_remaining == 1199

    instrument.supply_spool_remaining = 25
    assert instrument.peel(1, 1) == "LOW_TAPE"
    assert instrument.get_tape_left() == seal_remover.TapeLeft(24, 1198, "LOW_TAPE")

    instrument.reset()
    assert instrument.get_tape_left() == seal_remover.TapeLeft(1200, 1200, "")


@pytest.mark.parametrize(
    ("location", "adhesion", "message"),
    [(0, 1, "BeginPeelLocation must be in range 1..9"), (1, 5, "AdhesionTime must be in range 1..4")],
)
def test_peel_rejects_bad_input_before_touching_the_world(
    world: FakeWorld, location: int, adhesion: int, message: str
) -> None:
    instrument = seal_remover.SealRemover(
        laboratory_model=configured("seal-remover.stage"), durations=CommandDurations()
    )
    with pytest.raises(InvalidArgument, match=f"^{message}$"):
        instrument.peel(location, adhesion)
    assert world.requests == []
    assert instrument.status.value is Status.IDLE


def test_reset_instrument_runs() -> None:
    log: list[str] = []
    instrument = seal_remover.SealRemover(laboratory_model=UNCONFIGURED, durations=durations(log))
    status_log(instrument, log)
    instrument.reset_instrument()
    assert log == ["status RUNNING", "status IDLE"]


# --- Centrifuge. ---


def test_door_toggles_accessibility(world: FakeWorld) -> None:
    log: list[str] = []
    instrument = centrifuge.Centrifuge(
        laboratory_model=configured("centrifuge.deck"), durations=durations(log, OpenDoor=3, CloseDoor=3)
    )
    status_log(instrument, log)

    instrument.open_door(2, RecordingExecution(log))
    assert log == ["begin", "status RUNNING", "sleep 3", "status IDLE"]
    assert instrument.door_bucket == 2
    instrument.close_door()
    assert instrument.door_bucket == 0
    assert world.requests == [
        ("POST", "/locations/unlock", {"location": "centrifuge.deck"}),
        ("POST", "/locations/lock", {"location": "centrifuge.deck"}),
    ]


def test_door_failure_in_the_world_faults_the_centrifuge(world: FakeWorld) -> None:
    world.fail = True
    instrument = centrifuge.Centrifuge(laboratory_model=configured("centrifuge.deck"), durations=CommandDurations())
    with pytest.raises(
        WorldError,
        match=r"^MicroplateCentrifugeController\.OpenDoor failed to update access state for location "
        r"'centrifuge\.deck': boom$",
    ):
        instrument.open_door(1)
    assert instrument.status.value is Status.ERROR


@pytest.mark.parametrize("command", ["open_door", "stop_spin_cycle", "load_plate", "unload_plate"])
def test_bucket_is_validated_before_execution(command: str) -> None:
    log: list[str] = []
    instrument = centrifuge.Centrifuge(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    with pytest.raises(InvalidArgument, match="^BucketNumber must be 1 or 2$"):
        getattr(instrument, command)(3, RecordingExecution(log))
    assert log == []


def test_spin_needs_a_plate_and_a_non_negative_time(world: FakeWorld) -> None:
    instrument = centrifuge.Centrifuge(laboratory_model=configured("centrifuge.deck"), durations=CommandDurations())
    with pytest.raises(InvalidArgument, match="^Time must be >= 0$"):
        instrument.spin_cycle(-1)
    world.occupied = False
    with pytest.raises(InvalidState, match="SpinCycle requires an item"):
        instrument.spin_cycle(0)
    assert instrument.status.value is Status.IDLE


def test_stop_and_reset_keep_the_start_status() -> None:
    log: list[str] = []
    instrument = centrifuge.Centrifuge(laboratory_model=UNCONFIGURED, durations=durations(log, Reset=1))
    status_log(instrument, log)
    instrument.stop_spin_cycle(1)
    instrument.reset()
    assert log == ["status IDLE", "sleep 1", "status IDLE"]


# --- Thermal cycler. ---


def test_run_order_and_status(world: FakeWorld) -> None:
    log: list[str] = []
    instrument = thermal_cycler.ThermalCycler(
        laboratory_model=configured("thermal-cycler.block"), durations=durations(log)
    )
    status_log(instrument, log)
    remaining: list[str] = []
    instrument.remaining_time.subscribe(remaining.append)

    instrument.load(b"protocol")
    instrument.validate(10.0)
    instrument.start_run()
    # StartRun leaves Status at Running after it returns: the run continues until StopRun.
    assert instrument.status.value is Status.RUNNING
    assert instrument.get_instrument_state() is thermal_cycler.InstrumentState.RUNNING
    instrument.stop_run()
    assert log == ["status RUNNING", "status IDLE"]
    assert remaining == ["00:00:00", "00:02:00", "00:00:00"]
    assert instrument.elapsed_time.value == "00:00:10"


def test_lid_commands_never_leave_idle(world: FakeWorld) -> None:
    log: list[str] = []
    instrument = thermal_cycler.ThermalCycler(
        laboratory_model=configured("thermal-cycler.block"), durations=durations(log, OpenLid=4)
    )
    status_log(instrument, log)
    instrument.open_lid(RecordingExecution(log))
    instrument.close_lid()
    assert log == ["begin", "sleep 4"]
    assert [path for _, path, _ in world.requests] == ["/locations/unlock", "/locations/lock"]


def test_an_empty_protocol_faults_the_cycler() -> None:
    # The emptiness check runs after execution has begun.
    instrument = thermal_cycler.ThermalCycler(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    with pytest.raises(InvalidArgument, match="^ProtocolFileData must not be empty$"):
        instrument.load(b"")
    assert instrument.status.value is Status.ERROR


def test_validate_before_load_faults_the_cycler() -> None:
    instrument = thermal_cycler.ThermalCycler(laboratory_model=UNCONFIGURED, durations=CommandDurations())
    with pytest.raises(InvalidState, match="^Load must be executed before Validate in this mock$"):
        instrument.validate(1.0)
    assert instrument.status.value is Status.ERROR


def test_start_run_without_a_plate_is_rejected_but_unvalidated_faults(world: FakeWorld) -> None:
    instrument = thermal_cycler.ThermalCycler(
        laboratory_model=configured("thermal-cycler.block"), durations=CommandDurations()
    )
    world.occupied = False
    with pytest.raises(InvalidState, match="StartRun requires an item"):
        instrument.start_run()
    assert instrument.status.value is Status.IDLE

    world.occupied = True
    with pytest.raises(InvalidState, match="^Validate must be executed before StartRun in this mock$"):
        instrument.start_run()
    assert instrument.status.value is Status.ERROR
    assert instrument.run_active is False

    instrument.reset()
    assert instrument.status.value is Status.IDLE


# --- Ardea. ---

STATIONS = {"Base1": "station.slot1", "Base2": "station.slot2", "Base5": "centrifuge.deck"}


def make_ardea(log: list[str], **seconds: float) -> ardea.Ardea:
    return ardea.Ardea(
        laboratory_model=configured("ardea.gripper"), durations=durations(log, **seconds), stations=STATIONS
    )


def test_transfer_moves_through_the_arm_and_reports_seven_phases(world: FakeWorld) -> None:
    log: list[str] = []
    transporter = make_ardea(log, Transfer=7)
    positions: list[float] = []
    transporter.carriage_position.subscribe(positions.append)
    execution = RecordingExecution(log)

    result = transporter.transfer(" Base1 ", "Base5", execution)

    assert result == ardea.TransferResult(carriage_position=200, at_retract_pose=True)
    assert [phase for phase, _ in execution.phases] == [
        "transfer Base1 -> Base5",
        "carriage -> 0 mm",
        "start (station Base1)",
        "chuck: closing hand",
        "carriage -> 200 mm",
        "unchuck: opening hand",
        "verify retract pose",
    ]
    assert execution.phases[-1][1] == 1.0
    assert log == ["begin"] + ["sleep 1"] * 7
    assert positions == [0.0, 0.0, 200.0]
    assert world.requests == [
        ("POST", "/items/move", {"source": "station.slot1", "destination": "ardea.gripper"}),
        ("POST", "/items/move", {"source": "ardea.gripper", "destination": "centrifuge.deck"}),
    ]


def test_transfer_to_an_unknown_station_is_refused_after_execution_begins(world: FakeWorld) -> None:
    log: list[str] = []
    transporter = make_ardea(log)
    with pytest.raises(
        UnknownStation, match=r"^Base9: not a station of this machine\. Known stations: Base1, Base2, Base5\.$"
    ):
        transporter.transfer("Base1", "Base9", RecordingExecution(log))
    assert log == ["begin"]
    assert world.requests == []


def test_transfer_needs_the_world(world: FakeWorld) -> None:
    transporter = ardea.Ardea(laboratory_model=UNCONFIGURED, durations=CommandDurations(), stations=STATIONS)
    with pytest.raises(WorldError, match=r"^LabwareService\.Transfer requires LABORATORY_MODEL_URL to be configured$"):
        transporter.transfer("Base1", "Base2")


def test_light_is_read_from_the_device_state(world: FakeWorld) -> None:
    transporter = make_ardea([])
    assert transporter.light_is_on(command_name="LabwareService.LightIsOn") is True
    assert world.requests == [("GET", "/devices/ardea/state", None)]

    world.state = "not a map"
    with pytest.raises(WorldError, match="without a state map$"):
        transporter.light_is_on(command_name="LabwareService.LightIsOn")


def test_station_positions_follow_the_sorted_names() -> None:
    transporter = make_ardea([])
    assert transporter.station_names() == ["Base1", "Base2", "Base5"]
    assert [transporter.station_position_mm(name) for name in ("Base1", "Base2", "Base5", "Nope")] == [
        0.0,
        100.0,
        200.0,
        0.0,
    ]
