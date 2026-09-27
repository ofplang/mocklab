"""The PlateLoc plate sealer: its state, its rules, and what it does to the world.

A little internal state -- the sealing temperature and time setpoints, the (mock) actual
temperature and a cycle count -- read back by the feature's getters. StartCycle requires a plate
at this instrument's location. The sealer has no lid or door, so it never changes a location's
accessibility.

Status (0=Not Connected, 1=Idle, 2=Running, 3=Error) follows the PlateLocController Feature XML:
StartCycle is Running while sealing then returns to Idle; StopCycle and Reset keep their start
status then return to Idle; the setters do not change Status; every observable command sets
Error on a failure during execution.
"""

from __future__ import annotations

import logging

from laboratory_client import LaboratoryModelConfig, LaboratoryModelRequestError, get_location

from .errors import InvalidArgument, InvalidState, WorldError
from .runtime import NO_EXECUTION, CommandDurations, Execution, Status, StatusInstrument, one_at_a_time

logger = logging.getLogger(__name__)

# Power-on values, restored by Reset.
DEFAULT_SEALING_TEMPERATURE = 175
DEFAULT_SEALING_TIME = 1.5
AMBIENT_TEMPERATURE = 25


class PlateLoc(StatusInstrument):
    def __init__(self, *, laboratory_model: LaboratoryModelConfig, durations: CommandDurations) -> None:
        super().__init__(laboratory_model=laboratory_model, durations=durations)
        # Sealing setpoints, the actual temperature and a cycle counter, all read back through
        # the getters. Fixed mock defaults.
        self.sealing_temperature = DEFAULT_SEALING_TEMPERATURE
        self.sealing_time = DEFAULT_SEALING_TIME
        self.actual_temperature = AMBIENT_TEMPERATURE
        self.cycle_count = 0
        self.firmware_version = "FW-MOCK-1.0"
        self.version = "PLATELOC-MOCK-1"
        self.profiles = ["default", "foil", "film"]

    # --- Setters: unobservable, and Status is unchanged on success ("same as start"). A bad
    # --- value is rejected without faulting the instrument.

    def set_sealing_time(self, sealing_time: float) -> None:
        if sealing_time <= 0:
            raise InvalidArgument("SealingTime must be > 0")
        self.sealing_time = sealing_time

    def set_sealing_temperature(self, sealing_temperature: int) -> None:
        if sealing_temperature < 0:
            raise InvalidArgument("SealingTemperature must be >= 0")
        self.sealing_temperature = sealing_temperature

    # --- Observable commands. ---

    @one_at_a_time("StartCycle")
    def start_cycle(self, execution: Execution = NO_EXECUTION) -> None:
        # The sealing cycle: requires a plate present (checked before the run starts, so a
        # missing plate rejects without faulting). Running while sealing, Idle on success,
        # Error on failure. Heats to the setpoint and bumps the cycle counter.
        self.require_item_at_location(command_name="PlateLocController.StartCycle")
        execution.begin()
        self.status.set(Status.RUNNING)
        try:
            self.actual_temperature = self.sealing_temperature
            self.sleep_for("StartCycle")
            self.cycle_count += 1
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    def stop_cycle(self, execution: Execution = NO_EXECUTION) -> None:
        # Stop the cycle. Not guarded: it exists to end what may be running. Keeps its start
        # status while running, then Idle on success / Error on failure. Cools the mock actual
        # temperature a little, never below ambient.
        execution.begin()
        try:
            self.actual_temperature = max(AMBIENT_TEMPERATURE, self.actual_temperature - 5)
            self.sleep_for("StopCycle")
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Reset")
    def reset(self, execution: Execution = NO_EXECUTION) -> None:
        # Recover to Idle (e.g. from Error) and restore setpoints and counters to their
        # power-on values. Keeps its start status while running.
        execution.begin()
        try:
            self.sealing_temperature = DEFAULT_SEALING_TEMPERATURE
            self.sealing_time = DEFAULT_SEALING_TIME
            self.actual_temperature = AMBIENT_TEMPERATURE
            self.cycle_count = 0
            self.sleep_for("Reset")
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    # --- The world. Reading occupancy as "a plate is ready to be sealed" lives here, because
    # --- the interpretation of world state belongs to the instrument that acts on it; the HTTP
    # --- mechanics are the shared `laboratory_client`'s.

    def require_item_at_location(self, *, command_name: str) -> None:
        """Precondition check: fail unless an item is present at this instrument's location."""
        # No world configured => nothing to enforce; log and allow (mock convenience).
        if not self.laboratory_model_url or not self.laboratory_model_location:
            logger.info("%s skipped laboratory model occupancy check because configuration is missing", command_name)
            return

        # Any lookup failure is a hard error for the command: the precondition cannot be
        # confirmed.
        try:
            payload = get_location(base_url=self.laboratory_model_url, location=self.laboratory_model_location)
        except LaboratoryModelRequestError as error:
            raise WorldError(
                f"{command_name} requires an item at location '{self.laboratory_model_location}', "
                f"but laboratory model lookup failed: {error}"
            ) from error

        if payload.get("occupied") is not True:
            raise InvalidState(
                f"{command_name} requires an item at location '{self.laboratory_model_location}', "
                "but no item is present"
            )
