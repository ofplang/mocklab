"""The microplate centrifuge: its state, its rules, and what it does to the world.

Physical effects: OpenDoor and CloseDoor toggle this instrument's location accessibility in the
world model (an open door is a reachable location); SpinCycle requires a plate at the location.
Most parameters of the motion commands are accepted for signature fidelity but not simulated.

Status (0=Not Connected, 1=Idle, 2=Running, 3=Error) follows the MicroplateCentrifugeController
Feature XML: the actuating commands (OpenDoor, CloseDoor, SpinCycle, Home, Park, LoadPlate,
UnloadPlate) are Running while they execute and return to Idle; StopSpinCycle and Reset keep the
status they started in; a failure after the motion has started sets Error. Note the door
commands ARE Running here, unlike the thermal cycler's lid commands, which its own XML keeps at
Idle -- a per-feature exception there.
"""

from __future__ import annotations

import logging

from laboratory_client import (
    LaboratoryModelConfig,
    LaboratoryModelRequestError,
    get_location,
    request_laboratory_model,
)

from .errors import InvalidArgument, InvalidState, WorldError
from .runtime import NO_EXECUTION, CommandDurations, Execution, Status, StatusInstrument, one_at_a_time

logger = logging.getLogger(__name__)

# The buckets this mock has; a door is opened to a bucket, and 0 means closed.
BUCKETS = (1, 2)
DOOR_CLOSED = 0


class Centrifuge(StatusInstrument):
    def __init__(self, *, laboratory_model: LaboratoryModelConfig, durations: CommandDurations) -> None:
        super().__init__(laboratory_model=laboratory_model, durations=durations)
        # Static version and profile readouts, plus which bucket's door is currently open.
        self.active_x_version = "13.1.9.1493-mock"
        self.centrifuge_active_x_version = "13.1.9.1493-mock"
        self.centrifuge_hardware_version = "HW-MOCK-1"
        self.firmware_version = "FW-MOCK-1.0"
        self.hardware_version = "CEN-MOCK-1"
        self.profiles = ["default", "gentle_spin", "fast_spin"]
        self.door_bucket = DOOR_CLOSED

    # --- Helpers shared by this instrument's commands. ---

    def _validate_bucket(self, bucket: int) -> None:
        if bucket not in BUCKETS:
            raise InvalidArgument("BucketNumber must be 1 or 2")

    def _begin_running(self, execution: Execution, command_name: str) -> None:
        # Start an actuating command: enter Running for the duration of the motion. The wait
        # belongs to the individual command (a spin is not a door opening), hence the name.
        # A failure in here happens before the command's own try block, so it does not set
        # Error -- exactly as the SiLA2 server has always behaved.
        execution.begin()
        self.status.set(Status.RUNNING)
        self.sleep_for(command_name)

    def _begin_quiet(self, execution: Execution, command_name: str) -> None:
        # Start a command that keeps its start status: Stop/Reset do not assert Running.
        execution.begin()
        self.sleep_for(command_name)

    def _finish(self) -> None:
        self.status.set(Status.IDLE)

    # --- Commands. ---

    @one_at_a_time("OpenDoor")
    def open_door(self, bucket_number: int, execution: Execution = NO_EXECUTION) -> None:
        # An open door makes this instrument's location accessible. BucketNumber is validated
        # before execution starts (a bad value rejects without faulting).
        self._validate_bucket(bucket_number)
        self._begin_running(execution, "OpenDoor")
        try:
            self.door_bucket = bucket_number
            self.unlock_location(command_name="MicroplateCentrifugeController.OpenDoor")
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("CloseDoor")
    def close_door(self, execution: Execution = NO_EXECUTION) -> None:
        # Inverse of OpenDoor: a closed door makes the location inaccessible.
        self._begin_running(execution, "CloseDoor")
        try:
            self.door_bucket = DOOR_CLOSED
            self.lock_location(command_name="MicroplateCentrifugeController.CloseDoor")
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("SpinCycle")
    def spin_cycle(self, time: int, execution: Execution = NO_EXECUTION) -> None:
        # The spin itself: requires a plate present (checked before the run starts). Of its
        # fifteen parameters only Time is checked; the rest are not simulated, so the adapter
        # does not pass them in.
        if time < 0:
            raise InvalidArgument("Time must be >= 0")
        self.require_item_at_location(command_name="MicroplateCentrifugeController.SpinCycle")
        self._begin_running(execution, "SpinCycle")
        try:
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    def stop_spin_cycle(self, bucket_number: int, execution: Execution = NO_EXECUTION) -> None:
        # Stop the spin. Not guarded: it exists to end what may be running. Keeps its start
        # status, then Idle on success / Error on failure.
        self._validate_bucket(bucket_number)
        self._begin_quiet(execution, "StopSpinCycle")
        try:
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Reset")
    def reset(self, execution: Execution = NO_EXECUTION) -> None:
        # Recover to Idle (e.g. from Error) and clear the door state.
        self._begin_quiet(execution, "Reset")
        try:
            self.door_bucket = DOOR_CLOSED
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Home")
    def home(self, execution: Execution = NO_EXECUTION) -> None:
        # Actuating motion: return the rotor to its home position.
        self._begin_running(execution, "Home")
        try:
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("Park")
    def park(self, execution: Execution = NO_EXECUTION) -> None:
        # Actuating motion: move the rotor to its park position.
        self._begin_running(execution, "Park")
        try:
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("LoadPlate")
    def load_plate(self, bucket_number: int, execution: Execution = NO_EXECUTION) -> None:
        # Actuating: load a plate into a bucket. Only BucketNumber is checked; the gripper
        # offset, plate height, speed and options are not simulated.
        self._validate_bucket(bucket_number)
        self._begin_running(execution, "LoadPlate")
        try:
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    @one_at_a_time("UnloadPlate")
    def unload_plate(self, bucket_number: int, execution: Execution = NO_EXECUTION) -> None:
        # Actuating: unload a plate from a bucket. Same parameter treatment as LoadPlate.
        self._validate_bucket(bucket_number)
        self._begin_running(execution, "UnloadPlate")
        try:
            self._finish()
        except Exception:
            self.status.set(Status.ERROR)
            raise

    # --- The world. What it means -- that a spin needs a plate, that an open door is a
    # --- reachable location -- lives here, because the interpretation of world state belongs
    # --- to the instrument that acts on it; the HTTP mechanics are the shared client's.

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
        (door open => unlock, close => lock). A failure is surfaced as a command error so the
        caller knows the physical effect did not register."""
        if not self.laboratory_model_url or not self.laboratory_model_location:
            logger.info(
                "%s skipped laboratory model accessibility update because configuration is missing",
                command_name,
            )
            return

        # The world model has one endpoint per direction; choosing between them is this
        # instrument's call, since "the door is open" is what makes the location reachable.
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
