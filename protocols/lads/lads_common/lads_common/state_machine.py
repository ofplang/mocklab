"""
Minimal driver for OPC UA FiniteStateMachine instances.

LADS models device/unit/function status as FiniteStateMachines
(FunctionalUnitState, ControlFunctionState, CoverState, DeviceState). A client
reads `CurrentState` (LocalizedText, e.g. "Running") and `CurrentState/Id`
(the NodeId of the state object in the type). asyncua does not run state
machines for us, so this class only publishes transitions decided by the
runtime classes. It deliberately does not validate transitions; the runtimes
own the rules because they differ per state machine.
"""

from __future__ import annotations

import logging

from asyncua import Node, ua

from .addressspace import ModelBuilder

logger = logging.getLogger(__name__)

_STATE_TYPES = {ua.NodeId(ua.ObjectIds.StateType), ua.NodeId(ua.ObjectIds.InitialStateType)}


class StateMachine:
    def __init__(self, node: Node, current_state: Node, current_state_id: Node, states: dict[str, ua.NodeId]) -> None:
        self.node = node
        self._current_state = current_state
        self._current_state_id = current_state_id
        self._states = states
        self.current: str | None = None

    @classmethod
    async def attach(cls, builder: ModelBuilder, node: Node) -> StateMachine:
        """Bind to a state machine instance and collect its states from the type hierarchy."""
        states: dict[str, ua.NodeId] = {}
        current = builder.node(await node.read_type_definition())
        while True:
            for desc in await current.get_children_descriptions(refs=ua.ObjectIds.HasComponent):
                if desc.TypeDefinition in _STATE_TYPES:
                    states.setdefault(desc.BrowseName.Name, desc.NodeId)
            supertypes = await current.get_references(
                refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse
            )
            if not supertypes:
                break
            current = builder.node(supertypes[0].NodeId)

        current_state = await node.get_child("0:CurrentState")
        current_state_id = await current_state.get_child("0:Id")

        # AvailableStates is Mandatory for LADS FunctionalStateMachineType and
        # lets generic clients render the possible states without browsing types.
        available = await builder.find_child(node, ua.QualifiedName("AvailableStates", 0))
        if available is not None:
            await available.write_value(ua.Variant(list(states.values()), ua.VariantType.NodeId))

        # Transitions are not modelled by this mock; publish an empty list rather
        # than the null value copied from the type.
        transitions = await builder.find_child(node, ua.QualifiedName("AvailableTransitions", 0))
        if transitions is not None:
            await transitions.write_value(ua.Variant([], ua.VariantType.NodeId))

        return cls(node, current_state, current_state_id, states)

    async def set(self, state: str) -> None:
        if state not in self._states:
            raise ValueError(f"Unknown state {state!r}; known: {sorted(self._states)}")
        await self._current_state.write_value(ua.LocalizedText(state, "en"))
        await self._current_state_id.write_value(ua.Variant(self._states[state], ua.VariantType.NodeId))
        if self.current != state:
            logger.info("%s: %s -> %s", self.node.nodeid.to_string(), self.current, state)
        self.current = state
