"""
LADS Function runtimes, each bound to an instrument's commands and readouts.

Each class instantiates one LADS FunctionType under a FunctionalUnit's FunctionSet, adds the
optional members it implements, binds their methods and finally repairs the `Operational`
FunctionalGroup (see addressspace.py). As with the functional unit, the behaviour is the
instrument's: a Function only carries a LADS-shaped view of it.

- CoverFunction          lid or door; Open/Close run the instrument's open/close commands
- AnalogControlFunction  a set-point (e.g. sealing temperature) and the value it acts on
- TimerControlFunction   a duration set-point (e.g. sealing time), published in milliseconds
- AnalogSensorFunction   a read-only measured value (e.g. tape remaining)

Values carry engineering units (UNECE codes), so clients can render and record them without
device-specific knowledge.

Adapted from the lads-test prototype.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from asyncua import Node, ua
from asyncua.server.address_space import NodeData

from mock_instruments.errors import InstrumentError

from .addressspace import LadsTypes, ModelBuilder
from .functional_unit import FunctionalUnit, UnitCommand
from .methods import bind_method, invalid_state, message_for, status_for
from .state_machine import StateMachine

logger = logging.getLogger(__name__)

_UNECE_NAMESPACE = "http://www.opcfoundation.org/UA/units/un/cefact"


def engineering_unit(code: str, display: str, description: str) -> ua.EUInformation:
    """Build EUInformation from a UNECE Rec. 20 common code (UnitId = code packed into bytes)."""
    unit_id = 0
    for character in code:
        unit_id = (unit_id << 8) | ord(character)
    return ua.EUInformation(
        NamespaceUri=_UNECE_NAMESPACE,
        UnitId=unit_id,
        DisplayName=ua.LocalizedText(display, "en"),
        Description=ua.LocalizedText(description, "en"),
    )


CELSIUS = engineering_unit("CEL", "°C", "degree Celsius")
MILLISECOND = engineering_unit("C26", "ms", "millisecond")
MILLIMETRE = engineering_unit("MMT", "mm", "millimetre")
ONE = engineering_unit("C62", "", "one (dimensionless count)")


async def _configure_analog_item(
    builder: ModelBuilder, variable: Node, unit: ua.EUInformation, low: float, high: float
) -> None:
    """Fill the Mandatory EURange / EngineeringUnits properties of an AnalogUnitRangeType variable."""
    eu_range = await builder.find_child(variable, ua.QualifiedName("EURange", 0))
    if eu_range is not None:
        await eu_range.write_value(ua.Variant(ua.Range(Low=low, High=high), ua.VariantType.ExtensionObject))
    eu = await builder.find_child(variable, ua.QualifiedName("EngineeringUnits", 0))
    if eu is not None:
        await eu.write_value(ua.Variant(unit, ua.VariantType.ExtensionObject))


class _Function:
    """Common part: instance creation under the unit's FunctionSet and IsEnabled."""

    def __init__(self, unit: FunctionalUnit, node: Node) -> None:
        self.unit = unit
        self.builder: ModelBuilder = unit.builder
        self.node = node
        self.name = node.nodeid.Identifier.rsplit(".", 1)[-1]
        self.label = f"{unit.name}.{self.name}"

    @staticmethod
    async def _create_node(unit: FunctionalUnit, type_id: int, name: str) -> Node:
        node = await unit.builder.instantiate(unit.function_set, unit.builder.lads_id(type_id), name)
        is_enabled = await unit.builder.lads_child(node, "IsEnabled")
        await is_enabled.write_value(True)
        return node

    def _bind(self, method: Node, method_name: str, handler) -> None:
        bind_method(
            self.unit.server,
            method,
            handler,
            label=f"{self.label}.{method_name}",
            on_error=self.unit.set_last_error,
        )


# -- Cover ----------------------------------------------------------------------------------------


class CoverFunction(_Function):
    """
    Lid or door (CoverFunctionType + CoverStateMachineType), driven by two instrument commands.

    Open/Close behave like a program: the call returns once the instrument command has begun
    executing, the cover shows Opening/Closing meanwhile, and Opened/Closed once it completed. A
    failure before the command begins (another command executing, ...) is the call's StatusCode
    and leaves the cover as it was; a failure after it begins returns the cover to its previous
    state, with the message in the unit's LastError -- so the published state never claims an
    effect the world model did not record.

    Opening an open cover runs the command again, as the SiLA2 command would.
    """

    def __init__(self, unit: FunctionalUnit, node: Node) -> None:
        super().__init__(unit, node)
        self.state: StateMachine
        self._open: UnitCommand
        self._close: UnitCommand

    @classmethod
    async def create(
        cls, unit: FunctionalUnit, name: str, *, open: UnitCommand, close: UnitCommand, initially_open: bool
    ) -> CoverFunction:
        function = cls(unit, await cls._create_node(unit, LadsTypes.COVER_FUNCTION, name))
        function._open, function._close = open, close
        b = function.builder
        state_node = await b.lads_child(function.node, "CoverState")
        for method_name, handler in (("Open", function.open), ("Close", function.close)):
            function._bind(await b.add_optional(state_node, method_name), method_name, handler)
        function.state = await StateMachine.attach(b, state_node)
        await function.state.set("Opened" if initially_open else "Closed")
        await b.link_functional_groups(function.node)
        return function

    @property
    def is_open(self) -> bool:
        return self.state.current in ("Opened", "Opening")

    async def open(self) -> None:
        logger.info("%s.Open called", self.label)
        await self._transition("Open", self._open, "Opening", "Opened")

    async def close(self) -> None:
        logger.info("%s.Close called", self.label)
        await self._transition("Close", self._close, "Closing", "Closed")

    async def _transition(self, method: str, command: UnitCommand, transient: str, target: str) -> None:
        previous = self.state.current
        if previous in ("Opening", "Closing"):
            # The instrument's own guard would refuse too; this names the cover in the message.
            raise invalid_state(f"{self.label} is busy ({previous})")
        started = await self.unit.bridge.start(command)
        await self.state.set(transient)
        self.unit.server_task(self._finish(method, started.completion, previous, target), name=f"{self.label}.{method}")

    async def _finish(self, method: str, completion, previous: str | None, target: str) -> None:
        try:
            await completion
        except Exception as error:
            logger.warning("%s.%s failed: %s", self.label, method, error)
            await self.unit.set_last_error(message_for(f"{self.label}.{method}", error))
            await self.state.set(previous or "Closed")
        else:
            await self.state.set(target)
        await self.unit.bridge.flush()
        await self.unit.refresh_state()
        await self.unit.refresh_values()


# -- Control functions ------------------------------------------------------------------------------

# Reads a value from the instrument, already in the unit the Function publishes.
Getter = Callable[[], float]
# Applies a client's new set-point to the instrument; raises an instrument error to refuse it.
Setter = Callable[[float], None]


class _ControlFunction(_Function):
    """
    Shared set-point logic for AnalogControlFunction and TimerControlFunction.

    Writing `TargetValue` (the spec makes it writable) is the counterpart of a SiLA2 setter such as
    SetSealingTemperature: the write goes to the instrument's setter, which validates it exactly
    as the SiLA2 command does, and a refused value is a refused write -- it never becomes visible.
    `CurrentValue` and `TargetValue` are republished from the instrument after every program, stop
    and clear on the unit.

    ControlFunctionState is published as Stopped and offers no methods: none of these instruments
    has a command that drives a controller to its set-point on its own (the sealer heats as part of
    its cycle program).
    """

    def __init__(self, unit: FunctionalUnit, node: Node) -> None:
        super().__init__(unit, node)
        self.state: StateMachine
        self.current_node: Node
        self.target_node: Node
        self._current: Getter
        self._target: Getter
        self._set_target: Setter
        # True while this server itself republishes TargetValue: asyncua runs the value setter for
        # server-side writes too, and those must not go back to the instrument as a new set-point.
        self._publishing = False

    async def _setup(
        self,
        *,
        engineering_units: ua.EUInformation,
        low: float,
        high: float | None,
        current: Getter,
        target: Getter,
        set_target: Setter,
        optional_values: bool,
    ) -> None:
        b = self.builder
        self._current, self._target, self._set_target = current, target, set_target
        if optional_values:  # TimerControlFunctionType declares its values Optional
            self.current_node = await b.add_optional(self.node, "CurrentValue")
            self.target_node = await b.add_optional(self.node, "TargetValue")
        else:
            self.current_node = await b.lads_child(self.node, "CurrentValue")
            self.target_node = await b.lads_child(self.node, "TargetValue")
        # EURange is Mandatory; "no upper limit" (as in the SiLA2 servers) is published as float max.
        eu_high = sys.float_info.max if high is None else high
        for variable in (self.current_node, self.target_node):
            await _configure_analog_item(b, variable, engineering_units, low, eu_high)
        await self.target_node.set_writable(True)
        await self.refresh()
        self.unit.server.set_attribute_value_setter(self.target_node.nodeid, self._target_setter)
        self.unit.add_refresher(self.refresh)

        state_node = await b.lads_child(self.node, "ControlFunctionState")
        self.state = await StateMachine.attach(b, state_node)
        await self.state.set("Stopped")
        await b.link_functional_groups(self.node)

    async def refresh(self) -> None:
        await self.current_node.write_value(ua.Variant(float(self._current()), ua.VariantType.Double))
        self._publishing = True
        try:
            await self.target_node.write_value(ua.Variant(float(self._target()), ua.VariantType.Double))
        finally:
            self._publishing = False

    def _target_setter(self, node_data: NodeData, attr: ua.AttributeIds, value: ua.DataValue) -> None:
        """Intercepts client writes to TargetValue (asyncua calls this synchronously, on the event
        loop). The instrument setters are quick, non-blocking assignments, so calling one here is
        safe. Raising a UaStatusCodeError makes asyncua reject the write."""
        if self._publishing:
            node_data.attributes[attr].value = value
            return
        new_value = value.Value.Value if value.Value is not None else None
        if new_value is None:
            raise ua.UaStatusCodeError(ua.StatusCodes.BadTypeMismatch)
        label = f"{self.label}.TargetValue"
        logger.info("%s written: %s", label, new_value)
        try:
            self._set_target(float(new_value))
        except InstrumentError as error:
            logger.warning("%s write rejected: %s", label, error)
            # Taken now: `error` is unbound once this block ends, before the posted job runs.
            message = message_for(label, error)
            self.unit.bridge.post(lambda: self.unit.set_last_error(message))
            raise ua.UaStatusCodeError(status_for(error)) from error
        self.unit.bridge.post(lambda: self.unit.set_last_error(""))
        node_data.attributes[attr].value = ua.DataValue(ua.Variant(float(self._target()), ua.VariantType.Double))


class AnalogControlFunction(_ControlFunction):
    @classmethod
    async def create(
        cls,
        unit: FunctionalUnit,
        name: str,
        *,
        engineering_units: ua.EUInformation,
        low: float,
        high: float | None,
        current: Getter,
        target: Getter,
        set_target: Setter,
    ) -> AnalogControlFunction:
        function = cls(unit, await cls._create_node(unit, LadsTypes.ANALOG_CONTROL_FUNCTION, name))
        await function._setup(
            engineering_units=engineering_units,
            low=low,
            high=high,
            current=current,
            target=target,
            set_target=set_target,
            optional_values=False,
        )
        return function


class TimerControlFunction(_ControlFunction):
    """A duration set-point. OPC UA's Duration is a Double of milliseconds, so the getters and the
    setter given here speak milliseconds; converting from the instrument's own unit is the
    caller's business."""

    @classmethod
    async def create(
        cls,
        unit: FunctionalUnit,
        name: str,
        *,
        low_ms: float,
        high_ms: float | None,
        current_ms: Getter,
        target_ms: Getter,
        set_target_ms: Setter,
    ) -> TimerControlFunction:
        function = cls(unit, await cls._create_node(unit, LadsTypes.TIMER_CONTROL_FUNCTION, name))
        await function._setup(
            engineering_units=MILLISECOND,
            low=low_ms,
            high=high_ms,
            current=current_ms,
            target=target_ms,
            set_target=set_target_ms,
            optional_values=True,
        )
        return function


# -- Sensors ----------------------------------------------------------------------------------------


class AnalogSensorFunction(_Function):
    """AnalogScalarSensorFunctionType: SensorValue (and RawValue, kept equal in the mock), read from
    the instrument and republished after every program, stop and clear on the unit."""

    def __init__(self, unit: FunctionalUnit, node: Node) -> None:
        super().__init__(unit, node)
        self._sensor_value: Node
        self._raw_value: Node
        self._read: Getter

    @classmethod
    async def create(
        cls,
        unit: FunctionalUnit,
        name: str,
        *,
        engineering_units: ua.EUInformation,
        low: float,
        high: float,
        value: Getter,
    ) -> AnalogSensorFunction:
        function = cls(unit, await cls._create_node(unit, LadsTypes.ANALOG_SCALAR_SENSOR_FUNCTION, name))
        function._read = value
        b = function.builder
        function._sensor_value = await b.lads_child(function.node, "SensorValue")
        function._raw_value = await b.lads_child(function.node, "RawValue")
        for variable in (function._sensor_value, function._raw_value):
            await _configure_analog_item(b, variable, engineering_units, low, high)
        await function.refresh()
        unit.add_refresher(function.refresh)
        await b.link_functional_groups(function.node)
        return function

    async def refresh(self) -> None:
        value = float(self._read())
        for variable in (self._sensor_value, self._raw_value):
            await variable.write_value(ua.Variant(value, ua.VariantType.Double))
