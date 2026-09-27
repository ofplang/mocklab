"""The automated thermal cycler: its state, its rules, and what it does to the world.

Internal state: whether a protocol is loaded and validated, and whether a run is active. The
mock enforces the order Load -> Validate -> StartRun. Physical effects: OpenLid and CloseLid
toggle this instrument's location accessibility; StartRun requires a plate at the location.

Two DISTINCT status enums live in the Feature contract; do not conflate them:
  * `Status` (common to all instruments): 0=Not Connected, 1=Idle, 2=Running, 3=Error.
  * `InstrumentState` (GetInstrumentState's response, thermal-cycler specific):
    0=IDLE, 1=STANDBY, 2=RUNNING, 3=ERROR, 4=DIAGNOSTICS.

Status transitions follow the AutomatedThermalCyclerController Feature XML: non-run commands
keep the start status and only move to Error on failure; StartRun goes to Running and STAYS
there after the command returns (the run continues until StopRun); StopRun ends at Idle.
OpenLid/CloseLid deliberately stay at the start status, unlike the centrifuge's door commands --
an intentional per-feature exception in the XML, so only StartRun ever sets Running.
"""

from __future__ import annotations

import logging
from enum import IntEnum

from laboratory_client import (
    LaboratoryModelConfig,
    LaboratoryModelRequestError,
    get_location,
    request_laboratory_model,
)

from .errors import InvalidArgument, InvalidState, WorldError
from .runtime import NO_EXECUTION, CommandDurations, Execution, Observable, Status, StatusInstrument, one_at_a_time

logger = logging.getLogger(__name__)

# ElapsedTime / RemainingTime are "HH:MM:SS" strings in the Feature; these are the values the
# mock publishes (it runs no clock).
ZERO_TIME = "00:00:00"
RUN_REMAINING_TIME = "00:02:00"
STOPPED_ELAPSED_TIME = "00:00:10"


class InstrumentState(IntEnum):
    """GetInstrumentState's response. The mock only ever reports IDLE or RUNNING."""

    IDLE = 0
    STANDBY = 1
    RUNNING = 2
    ERROR = 3
    DIAGNOSTICS = 4


class ThermalCycler(StatusInstrument):
    def __init__(self, *, laboratory_model: LaboratoryModelConfig, durations: CommandDurations) -> None:
        # The two time readouts are created before the base class brings Status online, the
        # order the SiLA2 server has always published them in.
        self.elapsed_time: Observable[str] = Observable(ZERO_TIME)
        self.remaining_time: Observable[str] = Observable(ZERO_TIME)
        super().__init__(laboratory_model=laboratory_model, durations=durations)
        self.protocol_loaded = False
        self.protocol_validated = False
        self.run_active = False

    def get_instrument_state(self) -> InstrumentState:
        # An unobservable read; leaves Status unchanged.
        return InstrumentState.RUNNING if self.run_active else InstrumentState.IDLE

    @one_at_a_time("Load")
    def load(self, protocol_file_data: bytes, execution: Execution = NO_EXECUTION) -> None:
        # Load a protocol, invalidating any earlier validation. Status stays at its start value;
        # the emptiness check runs after execution has begun, so an empty file faults the
        # instrument (Error), as it always has.
        execution.begin()
        try:
            if not protocol_file_data:
                raise InvalidArgument("ProtocolFileData must not be empty")
            self.sleep_for("Load")
            self.protocol_loaded = True
            self.protocol_validated = False
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Validate")
    def validate(self, max_sample_volume: float, execution: Execution = NO_EXECUTION) -> None:
        # Validate the loaded protocol. Both checks run during execution, so either failure
        # moves Status to Error.
        execution.begin()
        try:
            if max_sample_volume < 0:
                raise InvalidArgument("MaxSampleVolume must be >= 0")
            if not self.protocol_loaded:
                raise InvalidState("Load must be executed before Validate in this mock")
            self.sleep_for("Validate")
            self.protocol_validated = True
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("OpenLid")
    def open_lid(self, execution: Execution = NO_EXECUTION) -> None:
        # An open lid makes this instrument's location accessible. Status stays Idle.
        execution.begin()
        try:
            self.sleep_for("OpenLid")
            self.unlock_location(command_name="AutomatedThermalCyclerController.OpenLid")
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("CloseLid")
    def close_lid(self, execution: Execution = NO_EXECUTION) -> None:
        # Inverse of OpenLid: a closed lid makes the location inaccessible.
        execution.begin()
        try:
            self.sleep_for("CloseLid")
            self.lock_location(command_name="AutomatedThermalCyclerController.CloseLid")
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("StartRun")
    def start_run(self, execution: Execution = NO_EXECUTION) -> None:
        # The occupancy precondition is checked BEFORE execution begins, so a missing plate
        # rejects the command without ever entering Running. The validation-order check comes
        # after, and so faults the instrument.
        self.require_item_at_location(command_name="AutomatedThermalCyclerController.StartRun")
        execution.begin()
        self.status.set(Status.RUNNING)  # stays Running after this command returns
        try:
            if not self.protocol_validated:
                raise InvalidState("Validate must be executed before StartRun in this mock")
            self.run_active = True
            self.elapsed_time.set(ZERO_TIME)
            self.remaining_time.set(RUN_REMAINING_TIME)
            self.sleep_for("StartRun")
        except Exception:
            self.run_active = False
            self.status.set(Status.ERROR)
            raise

    def stop_run(self, execution: Execution = NO_EXECUTION) -> None:
        # End the run. Not guarded: it exists to end what StartRun left running. It does not
        # re-assert Running; normal completion -> Idle.
        execution.begin()
        try:
            self.run_active = False
            self.elapsed_time.set(STOPPED_ELAPSED_TIME)
            self.remaining_time.set(ZERO_TIME)
            self.sleep_for("StopRun")
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Reset")
    def reset(self, execution: Execution = NO_EXECUTION) -> None:
        # Return all internal state to power-on defaults and recover to Idle. Does not assert
        # Running.
        execution.begin()
        try:
            self.protocol_loaded = False
            self.protocol_validated = False
            self.run_active = False
            self.elapsed_time.set(ZERO_TIME)
            self.remaining_time.set(ZERO_TIME)
            self.sleep_for("Reset")
            self.status.set(Status.IDLE)
        except Exception:
            self.status.set(Status.ERROR)
            raise

    # --- The world. That a run needs a plate and an open lid is a reachable location is this
    # --- instrument's interpretation, so it lives here; the HTTP mechanics are the shared
    # --- client's.

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

    def _set_location_accessibility(self, *, command_name: str, accessible: bool) -> None:
        """Push an accessibility change for this instrument's location to the world model
        (lid open => unlock, close => lock). A failure is surfaced as a command error so the
        caller knows the physical effect did not register."""
        if not self.laboratory_model_url or not self.laboratory_model_location:
            logger.info(
                "%s skipped laboratory model accessibility update because configuration is missing",
                command_name,
            )
            return

        endpoint = "/locations/unlock" if accessible else "/locations/lock"
        try:
            request_laboratory_model(
                base_url=self.laboratory_model_url,
                path=endpoint,
                method="POST",
                payload={"location": self.laboratory_model_location},
            )
        except LaboratoryModelRequestError as error:
            raise WorldError(
                f"{command_name} failed to update access state for location '{self.laboratory_model_location}': {error}"
            ) from error

    def unlock_location(self, *, command_name: str) -> None:
        self._set_location_accessibility(command_name=command_name, accessible=True)

    def lock_location(self, *, command_name: str) -> None:
        self._set_location_accessibility(command_name=command_name, accessible=False)
