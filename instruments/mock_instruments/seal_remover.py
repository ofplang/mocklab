"""The automated plate seal remover (peeler): its state, its rules, and what it does to the world.

It tracks two tape spool reserves (supply and take-up), each decremented by one per Peel and
reported by GetTapeLeft, with a LOW_TAPE warning once the supply runs low. Peel requires a plate
at this instrument's location. The peeler has no lid or door.

Status (0=Not Connected, 1=Idle, 2=Running, 3=Error) follows the
AutomatedPlateSealRemoverController Feature XML: Peel and ResetInstrument are Running while
acting then return to Idle; GetTapeLeft keeps the start status (it is a read); Reset keeps its
start status then returns to Idle; every observable command sets Error on a failure during
execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from laboratory_client import LaboratoryModelConfig, LaboratoryModelRequestError, get_location

from .errors import InvalidArgument, InvalidState, WorldError
from .runtime import NO_EXECUTION, CommandDurations, Execution, Status, StatusInstrument, one_at_a_time

logger = logging.getLogger(__name__)

# Arbitrary starting reserve per spool, restored by Reset; one unit is consumed per Peel.
SPOOL_CAPACITY = 1200
# Below this many units left on the supply spool, Peel and GetTapeLeft warn.
LOW_TAPE_THRESHOLD = 25
LOW_TAPE_WARNING = "LOW_TAPE"


@dataclass(frozen=True)
class TapeLeft:
    """What GetTapeLeft reports."""

    supply_spool_remaining: int
    takeup_spool_remaining: int
    warning: str


class SealRemover(StatusInstrument):
    def __init__(self, *, laboratory_model: LaboratoryModelConfig, durations: CommandDurations) -> None:
        super().__init__(laboratory_model=laboratory_model, durations=durations)
        self.supply_spool_remaining = SPOOL_CAPACITY
        self.takeup_spool_remaining = SPOOL_CAPACITY

    def _warning(self) -> str:
        return LOW_TAPE_WARNING if self.supply_spool_remaining < LOW_TAPE_THRESHOLD else ""

    @one_at_a_time("Peel")
    def peel(self, begin_peel_location: int, adhesion_time: int, execution: Execution = NO_EXECUTION) -> str:
        """Peel the seal off the plate and return the instrument's warning ("" if none).

        Inputs are validated and a plate is required before the run starts (a bad input or a
        missing plate rejects without faulting). Running while peeling, Idle on success, Error
        on failure."""
        if not (1 <= begin_peel_location <= 9):
            raise InvalidArgument("BeginPeelLocation must be in range 1..9")
        if not (1 <= adhesion_time <= 4):
            raise InvalidArgument("AdhesionTime must be in range 1..4")
        self.require_item_at_location(command_name="AutomatedPlateSealRemoverController.Peel")

        execution.begin()
        self.status.set(Status.RUNNING)
        try:
            self.sleep_for("Peel")
            self.supply_spool_remaining = max(0, self.supply_spool_remaining - 1)
            self.takeup_spool_remaining = max(0, self.takeup_spool_remaining - 1)
            warning = self._warning()
            self.status.set(Status.IDLE)
            return warning
        except Exception:
            self.status.set(Status.ERROR)
            raise

    def get_tape_left(self, execution: Execution = NO_EXECUTION) -> TapeLeft:
        # A value-returning read of the two spool reserves. Not guarded (it is a read), and
        # Status is unchanged on success; a failure sets Error.
        execution.begin()
        try:
            return TapeLeft(self.supply_spool_remaining, self.takeup_spool_remaining, self._warning())
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("ResetInstrument")
    def reset_instrument(self, execution: Execution = NO_EXECUTION) -> None:
        # Actuating recovery command: Running while acting, Idle on success, Error on failure.
        # (Distinct from Reset below, which keeps its start status.)
        execution.begin()
        self.status.set(Status.RUNNING)
        try:
            self.sleep_for("ResetInstrument")
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Reset")
    def reset(self, execution: Execution = NO_EXECUTION) -> None:
        # Recover to Idle and refill the tape reserves. Keeps its start status while running.
        execution.begin()
        try:
            self.sleep_for("Reset")
            self.supply_spool_remaining = SPOOL_CAPACITY
            self.takeup_spool_remaining = SPOOL_CAPACITY
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    # --- The world. Reading occupancy as "a plate is ready to be peeled" lives here, because
    # --- the interpretation of world state belongs to the instrument that acts on it.

    def require_item_at_location(self, *, command_name: str) -> None:
        """Precondition check: fail unless an item is present at this instrument's location."""
        # No world configured => nothing to enforce; log and allow (mock convenience).
        if not self.laboratory_model_url or not self.laboratory_model_location:
            logger.info("%s skipped laboratory model occupancy check because configuration is missing", command_name)
            return

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
