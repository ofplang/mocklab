"""Ardea, the lab's transporter: a robot arm on a travel carriage that carries labware between
stations. The mock of a real machine (`ardea-sila2`).

WHAT IS MOCKED. Only `Transfer` performs work, because it is the one command a workflow needs
from a transporter: carry a labware from one station to another. It is two world-model moves
through the arm's own location (`laboratory_model_location`): source -> arm, then arm ->
destination. The arm holding the plate mid-route is a real state on the machine, so it is a real
state here too. The world model has to be configured for this instrument to work at all: a
transfer that does not reach it has not happened.

Three read-only values also answer, because the world model already holds what they report: the
station names, the carriage position, and the machine light. Each one's derivation is explained
where it is computed, below. Everything else the real machine offers drives hardware this world
has no counterpart for; refusing those is the protocol adapter's business, since what a refusal
looks like is protocol vocabulary.

Unlike the four instruments, Ardea's features declare no Status property, so there is no
Idle/Running bracket: a transfer's progress is reported through its phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from laboratory_client import (
    LaboratoryModelConfig,
    LaboratoryModelRequestError,
    load_laboratory_model_config,
    request_laboratory_model,
)

from .errors import UnknownStation, WorldError
from .runtime import NO_EXECUTION, CommandDurations, Execution, Instrument, Observable, one_at_a_time
from .stations import load_station_map

# Spacing between two neighbouring stations on the mock's synthetic rail, in millimetres. The
# real machine's stations sit at measured positions taken from its motion configuration, which
# is geometry this world does not model, so the position reported here is the station's index in
# the map times this pitch. See `station_position_mm` for why that is worth reporting at all.
STATION_PITCH_MM = 100.0

# The name of the machine light in the world model: a key in the Ardea device's opaque state
# bag. On the real machine the light is a boolean variable on the robot controller, which has
# no counterpart here, so the world model's device state is where it lives instead -- declared
# by the seed and changeable by an operator (`docs/RULES.md`).
LIGHT_STATE_KEY = "light"

# The phases a transfer reports, in order. Each one is announced and then waited out for an
# equal slice of the command's configured duration, so a subscriber sees them spread across
# the transfer instead of all at once at the end.
#
# The wording follows the real server's phases, but only as far as the mock can honestly go:
# the machine names the PacScript it is running (`approach: RunTask(xxxApproachPick2)`) and
# which way the arm is turned, and this mock knows neither -- there are no task names and no
# arm to turn. Inventing them would put strings in front of a client that look like they came
# from a controller. What is left is the shape of the route, which is the part that is true --
# and the station names, which are the machine's own now that there is a station map.
PHASE_START = "transfer {source} -> {destination}"
PHASE_TO_SOURCE = "carriage -> {position:g} mm"
PHASE_AT_SOURCE = "start (station {station})"
PHASE_PICK = "chuck: closing hand"
PHASE_TO_DESTINATION = "carriage -> {position:g} mm"
PHASE_PUT = "unchuck: opening hand"
PHASE_VERIFY = "verify retract pose"
TRANSFER_PHASE_COUNT = 7


@dataclass(frozen=True)
class TransferResult:
    """What Transfer returns: where the carriage ended up, and whether the arm is retracted."""

    carriage_position: int
    at_retract_pose: bool


class Ardea(Instrument):
    def __init__(
        self,
        *,
        laboratory_model: LaboratoryModelConfig,
        durations: CommandDurations,
        stations: dict[str, str],
    ) -> None:
        super().__init__(laboratory_model=laboratory_model, durations=durations)
        # The station map: which of this machine's station names means which place in the
        # world. See `stations.py`.
        self.stations = stations
        # Zero is "wherever it was left": the mock does not know where the carriage physically
        # sits until a transfer puts it somewhere, and the real machine would have read the PLC.
        # It is also what an unknown station reports, which keeps the two indistinguishable
        # rather than inventing a third meaning.
        self.carriage_position: Observable[float] = Observable(0.0)

    @classmethod
    def from_environment(cls) -> Self:
        # Same order the SiLA2 server has always read these in: world wiring, then the station
        # map (not optional -- a transporter that cannot resolve a station name cannot do
        # anything, so a typo fails at startup), then durations.
        laboratory_model = load_laboratory_model_config()
        stations = load_station_map()
        return cls(laboratory_model=laboratory_model, durations=CommandDurations.from_environment(), stations=stations)

    # --- The one command this mock performs. ---

    @one_at_a_time("Transfer")
    def transfer(self, source: str, destination: str, execution: Execution = NO_EXECUTION) -> TransferResult:
        # One transfer at a time, for the same reason every command in this lab is guarded; on
        # the machine the same lock keeps two motions from colliding.
        source = str(source).strip()
        destination = str(destination).strip()
        command = "LabwareService.Transfer"
        execution.begin()

        # Resolve both station names before touching the world. The real server does the same
        # thing first (`station_by_name` -> `InvalidStation`), because a name it does not know is
        # a mistake to report rather than a motion to attempt -- so this is the machine's own
        # behaviour being mocked, not a pre-check invented here.
        source_location = self.station_location(source)
        destination_location = self.station_location(destination)
        unknown = [
            name
            for name, location in ((source, source_location), (destination, destination_location))
            if location is None
        ]
        if unknown:
            raise UnknownStation(
                f"{', '.join(unknown)}: not a station of this machine. Known stations: "
                f"{', '.join(self.station_names())}."
            )
        # mypy: the two are not None once `unknown` is empty, but it cannot see that through
        # the list comprehension above.
        assert source_location is not None and destination_location is not None

        source_position = self.station_position_mm(source)
        destination_position = self.station_position_mm(destination)

        phases_entered = 0

        def phase(text: str) -> None:
            # Announce, then spend this phase's slice of the configured duration. Announcing
            # first is what makes the phase visible for as long as it is supposed to take.
            # Progress counts phases entered, so the last one leaves it at exactly 1.0.
            nonlocal phases_entered
            phases_entered += 1
            execution.report(text, phases_entered / TRANSFER_PHASE_COUNT)
            self.sleep_for("Transfer", fraction=1.0 / TRANSFER_PHASE_COUNT)

        # The route, in the order the machine drives it: to the source, pick, to the destination,
        # put. The carriage position is published as the carriage arrives, so a client watching
        # it sees the two legs of the journey.
        phase(PHASE_START.format(source=source, destination=destination))

        phase(PHASE_TO_SOURCE.format(position=source_position))
        self.carriage_position.set(source_position)

        phase(PHASE_AT_SOURCE.format(station=source))

        phase(PHASE_PICK)
        self.pick_item(command_name=command, location=source_location)

        phase(PHASE_TO_DESTINATION.format(position=destination_position))
        self.carriage_position.set(destination_position)

        phase(PHASE_PUT)
        self.place_item(command_name=command, location=destination_location)

        phase(PHASE_VERIFY)

        # The mock's arm always ends at the destination's retract pose, because there is no
        # motion that could leave it anywhere else -- on the machine this is a genuine readback
        # of the joint angles after the retract task.
        return TransferResult(carriage_position=int(destination_position), at_retract_pose=True)

    # --- Stations. ---

    def station_names(self) -> list[str]:
        """The station names this machine serves, sorted.

        These are the names `transfer` accepts -- the machine's own vocabulary (`Base1`,
        `Base2`, ...), not world-model locations. Fixed for the instrument's lifetime, as the
        real property promises, because the map is read once at startup.

        Sorted so every caller sees one ordering: `station_position_mm` turns a station's index
        in this list into a millimetre position, and an index that varied would not be
        reproducible."""
        return sorted(self.stations)

    def station_location(self, station: str) -> str | None:
        """The `device.spot` a station name means, or None if this machine has no such station."""
        return self.stations.get(station)

    def station_position_mm(self, station: str) -> float:
        """The synthetic rail position of `station`.

        The mock has no measured station positions, so the value is derived rather than
        configured: the station's index in `station_names()`, times `STATION_PITCH_MM`. It is
        not the machine's geometry and is not meant to be read as distance. What it gives a
        client is a number that is stable while nothing moves and that *changes when the
        carriage does*, which is the observable part of the real property. An unknown station
        reports 0.0, the same as the position before the first transfer.

        If real positions are ever wanted here, the station map is where they belong, beside
        the location each name already carries."""
        names = self.station_names()
        try:
            return names.index(station) * STATION_PITCH_MM
        except ValueError:
            return 0.0

    # --- The world. What it means -- that a transfer is two moves through the arm, that the
    # --- light is a key in the device's state -- lives here; the HTTP mechanics are the shared
    # --- client's.

    def light_is_on(self, *, command_name: str) -> bool:
        """Read the machine light out of the Ardea device's opaque state in the world model.

        Which device to read is decided by the arm's own location, so the light needs no
        configuration of its own: `ardea.gripper` means the light is `ardea`'s `light` key. An
        absent key means off -- the seed is expected to declare it, but a world that never did
        is dark rather than broken."""
        arm_location = self._require_move_configuration(command_name=command_name)
        device = arm_location.split(".", 1)[0]
        response = self._request(command_name=command_name, path=f"/devices/{device}/state", method="GET")

        state = response.get("state")
        if not isinstance(state, dict):
            raise WorldError(f"{command_name} got a laboratory model device-state response without a state map")
        return bool(state.get(LIGHT_STATE_KEY, False))

    def pick_item(self, *, command_name: str, location: str) -> None:
        # Pick = move the labware from `location` onto the arm's own holding location.
        arm_location = self._require_move_configuration(command_name=command_name)
        self.move_item(command_name=command_name, source=location, destination=arm_location)

    def place_item(self, *, command_name: str, location: str) -> None:
        # Place = move the labware from the arm's holding location to `location`.
        arm_location = self._require_move_configuration(command_name=command_name)
        self.move_item(command_name=command_name, source=arm_location, destination=location)

    def move_item(self, *, command_name: str, source: str, destination: str) -> None:
        # Apply a world-model move source -> destination (POST /items/move).
        self._require_move_configuration(command_name=command_name)
        self._request(
            command_name=command_name,
            path="/items/move",
            method="POST",
            payload={"source": source, "destination": destination},
        )

    def _require_move_configuration(self, *, command_name: str) -> str:
        """Check the world-model wiring and hand back the arm's own holding location.

        Returning it rather than just validating is what lets the callers pass a
        definitely-configured location on: the attribute itself is optional, because an
        instrument may run without the world model wired up."""
        if not self.laboratory_model_url:
            raise WorldError(f"{command_name} requires LABORATORY_MODEL_URL to be configured")
        if not self.laboratory_model_location:
            raise WorldError(f"{command_name} requires LABORATORY_MODEL_LOCATION to be configured")
        return self.laboratory_model_location

    def _request(
        self, *, command_name: str, path: str, method: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Send one request to the world model, naming the command in any failure."""
        if not self.laboratory_model_url:
            raise WorldError(f"{command_name} requires LABORATORY_MODEL_URL to be configured")

        try:
            return request_laboratory_model(
                base_url=self.laboratory_model_url,
                path=path,
                method=method,
                payload=payload,
            )
        except LaboratoryModelRequestError as error:
            raise WorldError(f"{command_name} failed to access laboratory model: {error}") from error
