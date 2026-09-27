"""
Low-level helpers for building LADS instances in an asyncua address space.

Why this module exists (instead of asyncua's `instantiate`)
-----------------------------------------------------------
1. Selective optionals. asyncua instantiates either every Optional member
   (e.g. the full alarm machinery of each function) or none. A realistic LADS
   device needs the Mandatory skeleton plus hand-picked optional members
   (FunctionSet, ProgramManager, the Start/Stop methods of a state machine...).
   `instantiate()` creates Mandatory members only; `add_optional()` adds one
   Optional member at a time.

2. Correct member resolution. OPC UA instance declarations only list what they
   *override*; everything else comes from the declaration's own TypeDefinition.
   Example: `FunctionalUnitType.FunctionalUnitState` does not repeat
   `CurrentState`, which is declared on FiniteStateMachineType. asyncua only
   copies the declaration's own children, so such members would be missing.
   `_collect_members()` merges declaration children (winning on conflicts)
   with the TypeDefinition hierarchy, most derived first.

3. FunctionalGroups. DI FunctionalGroups (e.g. `Operational`) organize nodes
   that live elsewhere in the instance. Instantiation creates copies of them;
   `link_functional_groups()` replaces the copies with Organizes references to
   the real nodes so clients never read a stale duplicate.

Instance NodeIds are string NodeIds in the vendor namespace built from the
browse path ("AutomatedThermalCycler.FunctionalUnitSet.Cycler...."), which
keeps them stable across restarts and readable in generic OPC UA browsers.
"""

from __future__ import annotations

import logging

from asyncua import Node, Server, ua
from asyncua.common.instantiate_util import _read_and_copy_attrs

from .nodesets import Namespaces

logger = logging.getLogger(__name__)


# Numeric identifiers of the LADS 1.0.0 types we use (namespace = LADS).
# They are stable for the pinned NodeSet version, which is why the NodeSet
# files are vendored rather than downloaded at build time.
class LadsTypes:
    LADS_DEVICE = 1002
    FUNCTIONAL_UNIT = 1003
    ANALOG_CONTROL_FUNCTION = 1009
    COVER_FUNCTION = 1011
    TIMER_CONTROL_FUNCTION = 1013
    ANALOG_SCALAR_SENSOR_FUNCTION = 1016
    PROGRAM_TEMPLATE = 1018
    RESULT = 1021


# DI identifiers (namespace = DI).
DI_DEVICE_SET = 5001
DI_FUNCTIONAL_GROUP_TYPE = 1005

_MANDATORY = ua.NodeId(ua.ObjectIds.ModellingRule_Mandatory)
_OPTIONAL = ua.NodeId(ua.ObjectIds.ModellingRule_Optional)
_FINITE_STATE_MACHINE_TYPE = ua.NodeId(ua.ObjectIds.FiniteStateMachineType)

# Member references followed when collecting instance declarations. HasSubtype
# (also hierarchical) must not be followed: it links types, not members.
_MEMBER_REFERENCES = tuple(
    ua.NodeId(identifier)
    for identifier in (
        ua.ObjectIds.HasComponent,
        ua.ObjectIds.HasProperty,
        ua.ObjectIds.Organizes,
        ua.ObjectIds.HasAddIn,
    )
)


def _key(bname: ua.QualifiedName) -> tuple[int, str]:
    return (bname.NamespaceIndex, bname.Name)


class ModelBuilder:
    """Helper bound to one server and its namespace indexes."""

    def __init__(self, server: Server, ns: Namespaces) -> None:
        self.server = server
        self.ns = ns
        # instance NodeId -> the instance declarations it was created from, most
        # derived first. A member may be declared on several levels of the owner's
        # type hierarchy (an override refines its supertype's declaration), and
        # the instance needs the union of their members with the most derived
        # winning. Kept so add_optional() sees the same view, e.g.
        # AnalogControlFunctionType.ControlFunctionState.StartWithTargetValue takes
        # a Double, while the generic state machine type declares a Number.
        self._declarations_of: dict[ua.NodeId, list[ua.NodeId]] = {}

    # -- NodeId helpers -------------------------------------------------------

    def lads_id(self, identifier: int) -> ua.NodeId:
        return ua.NodeId(identifier, self.ns.lads)

    def lads_name(self, name: str) -> ua.QualifiedName:
        return ua.QualifiedName(name, self.ns.lads)

    def vendor_name(self, name: str) -> ua.QualifiedName:
        return ua.QualifiedName(name, self.ns.vendor)

    def node(self, nodeid: ua.NodeId) -> Node:
        return self.server.get_node(nodeid)

    @staticmethod
    def child_id(parent: Node | ua.NodeId, name: str) -> ua.NodeId:
        """Derive a child's string NodeId from the parent's (see module docstring)."""
        parent_id = parent.nodeid if isinstance(parent, Node) else parent
        if parent_id.NodeIdType != ua.NodeIdType.String:
            raise ValueError(f"Parent {parent_id} does not use a string NodeId")
        return ua.NodeId(f"{parent_id.Identifier}.{name}", parent_id.NamespaceIndex)

    # -- type hierarchy -------------------------------------------------------

    async def _supertype(self, type_node: Node) -> Node | None:
        refs = await type_node.get_references(refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse)
        return self.node(refs[0].NodeId) if refs else None

    async def _type_chain(self, type_id: ua.NodeId | None) -> list[Node]:
        chain: list[Node] = []
        current = self.node(type_id) if type_id is not None and not type_id.is_null() else None
        while current is not None:
            chain.append(current)
            current = await self._supertype(current)
        return chain

    async def _member_descriptions(self, source: Node) -> list[ua.ReferenceDescription]:
        members: list[ua.ReferenceDescription] = []
        for ref_type in _MEMBER_REFERENCES:
            members.extend(
                await source.get_references(refs=ref_type, direction=ua.BrowseDirection.Forward, includesubtypes=False)
            )
        return members

    async def _collect_members(
        self, declarations: list[ua.NodeId], type_id: ua.NodeId | None
    ) -> dict[tuple[int, str], list[ua.ReferenceDescription]]:
        """
        Members of an instance, each with all its declarations (most derived first).

        Sources in priority order: the instance's own declarations, then its
        TypeDefinition hierarchy. Keyed by (namespace, name) because asyncua's
        QualifiedName is not hashable.
        """
        sources = [self.node(d) for d in declarations]
        sources.extend(await self._type_chain(type_id))
        members: dict[tuple[int, str], list[ua.ReferenceDescription]] = {}
        for source in sources:
            for desc in await self._member_descriptions(source):
                members.setdefault(_key(desc.BrowseName), []).append(desc)
        return members

    @staticmethod
    def _type_of(descs: list[ua.ReferenceDescription]) -> ua.NodeId | None:
        for desc in descs:
            if not desc.TypeDefinition.is_null():
                return desc.TypeDefinition
        return None

    async def _modelling_rule(self, nodeid: ua.NodeId) -> ua.NodeId | None:
        refs = await self.node(nodeid).get_references(refs=ua.ObjectIds.HasModellingRule)
        return refs[0].NodeId if refs else None

    # -- instantiation --------------------------------------------------------

    async def _create(
        self,
        *,
        parent_id: ua.NodeId,
        reference_type: ua.NodeId,
        source: ua.NodeId,
        type_id: ua.NodeId | None,
        nodeid: ua.NodeId,
        bname: ua.QualifiedName,
        declarations: list[ua.NodeId],
    ) -> Node:
        """
        Create one node copied from `source` (a type or an instance declaration)
        and, recursively, its Mandatory members.
        """
        source_node = self.node(source)
        source_class = await source_node.read_node_class()

        item = ua.AddNodesItem()
        item.RequestedNewNodeId = nodeid
        item.BrowseName = bname
        item.ParentNodeId = parent_id
        item.ReferenceTypeId = reference_type
        if source_class in (ua.NodeClass.Object, ua.NodeClass.ObjectType):
            item.NodeClass = ua.NodeClass.Object
            item.TypeDefinition = type_id if type_id is not None else ua.NodeId()
            await _read_and_copy_attrs(source_node, ua.ObjectAttributes(), item)
        elif source_class in (ua.NodeClass.Variable, ua.NodeClass.VariableType):
            item.NodeClass = ua.NodeClass.Variable
            item.TypeDefinition = type_id if type_id is not None else ua.NodeId()
            await _read_and_copy_attrs(source_node, ua.VariableAttributes(), item)
        elif source_class == ua.NodeClass.Method:
            item.NodeClass = ua.NodeClass.Method
            await _read_and_copy_attrs(source_node, ua.MethodAttributes(), item)
        else:
            raise RuntimeError(f"Cannot instantiate node class {source_class} ({source})")
        if source_class in (ua.NodeClass.ObjectType, ua.NodeClass.VariableType):
            # A type's DisplayName ("FunctionalUnitType") must not leak into instances.
            item.NodeAttributes.DisplayName = ua.LocalizedText(bname.Name)

        result = (await self.server.iserver.isession.add_nodes([item]))[0]
        result.StatusCode.check()
        node = self.node(result.AddedNodeId)
        if declarations:
            self._declarations_of[result.AddedNodeId] = declarations

        # Methods have no TypeDefinition: their members (InputArguments/OutputArguments)
        # come from the declaration only.
        members = await self._collect_members(
            declarations, type_id if source_class != ua.NodeClass.Method else None
        )
        for descs in members.values():
            # The most derived declaration decides the modelling rule, because an
            # override may promote an Optional member to Mandatory (e.g. Lock).
            if await self._modelling_rule(descs[0].NodeId) != _MANDATORY:
                continue
            await self._create_member(result.AddedNodeId, descs)
        return node

    async def _create_member(self, parent_id: ua.NodeId, descs: list[ua.ReferenceDescription]) -> Node:
        first = descs[0]
        return await self._create(
            parent_id=parent_id,
            reference_type=first.ReferenceTypeId,
            source=first.NodeId,
            type_id=self._type_of(descs),
            nodeid=self.child_id(parent_id, first.BrowseName.Name),
            bname=first.BrowseName,
            declarations=[d.NodeId for d in descs],
        )

    async def instantiate(
        self,
        parent: Node,
        type_id: ua.NodeId,
        name: str,
        *,
        browse_ns: int | None = None,
        nodeid: ua.NodeId | None = None,
    ) -> Node:
        """Instantiate the ObjectType `type_id` under `parent` (HasComponent) with its Mandatory members."""
        return await self._create(
            parent_id=parent.nodeid,
            reference_type=ua.NodeId(ua.ObjectIds.HasComponent),
            source=type_id,
            type_id=type_id,
            nodeid=nodeid if nodeid is not None else self.child_id(parent, name),
            bname=ua.QualifiedName(name, self.ns.vendor if browse_ns is None else browse_ns),
            declarations=[],
        )

    async def add_optional(self, instance: Node, name: str, *, browse_ns: int | None = None) -> Node:
        """
        Add one Optional member declared for `instance` (or return it if present).

        `browse_ns` defaults to LADS because nearly all optional members we add
        are LADS members; pass `self.ns.di` etc. for members of other models.
        """
        bname = ua.QualifiedName(name, self.ns.lads if browse_ns is None else browse_ns)
        existing = await self.find_child(instance, bname)
        if existing is not None:
            return existing
        members = await self._collect_members(
            self._declarations_of.get(instance.nodeid, []), await instance.read_type_definition()
        )
        descs = members.get(_key(bname))
        if descs is None:
            raise LookupError(f"No member {bname} declared for {instance.nodeid}")
        if await self._modelling_rule(descs[0].NodeId) != _OPTIONAL:
            logger.debug("add_optional(%s) on %s: member is not Optional", bname, instance.nodeid)
        return await self._create_member(instance.nodeid, descs)

    @staticmethod
    async def find_child(parent: Node, bname: ua.QualifiedName) -> Node | None:
        try:
            return await parent.get_child(bname)
        except ua.UaStatusCodeError:
            return None

    async def lads_child(self, parent: Node, *path: str) -> Node:
        """Resolve a browse path whose elements are all in the LADS namespace."""
        return await parent.get_child([self.lads_name(p) for p in path])

    # -- Set NodeVersion -------------------------------------------------------

    async def bump_node_version(self, set_node: Node) -> None:
        """
        Increment the `NodeVersion` of a LADS Set (FunctionSet, ResultSet, ...).

        OPC UA uses NodeVersion to tell clients that the children of a node
        changed; an orchestrator can subscribe to ResultSet.NodeVersion instead of
        re-browsing to notice new results. The type default is not a counter
        (NaN/None), so the first call starts at "1".
        """
        version = await self.find_child(set_node, ua.QualifiedName("NodeVersion", 0))
        if version is None:
            version = await set_node.add_property(
                self.child_id(set_node, "NodeVersion"), ua.QualifiedName("NodeVersion", 0), "0", ua.VariantType.String
            )
        current = await version.read_value()
        try:
            number = int(current)
        except (TypeError, ValueError):
            number = 0
        await version.write_value(ua.Variant(str(number + 1), ua.VariantType.String))

    # -- vendor-specific additions ------------------------------------------

    async def add_vendor_variable(self, parent: Node, name: str, value, varianttype: ua.VariantType) -> Node:
        return await parent.add_variable(self.child_id(parent, name), self.vendor_name(name), value, varianttype)

    async def add_vendor_method(
        self,
        parent: Node,
        name: str,
        inputs: list[ua.Argument],
        outputs: list[ua.Argument],
    ) -> Node:
        """Add a vendor-specific method (no callback yet; bind it with methods.bind_method)."""
        return await parent.add_method(self.child_id(parent, name), self.vendor_name(name), None, inputs, outputs)

    # -- FunctionalGroup repair ----------------------------------------------

    async def _is_state_machine(self, node: Node) -> bool:
        try:
            type_id = await node.read_type_definition()
        except ua.UaStatusCodeError:
            return False
        return any(t.nodeid == _FINITE_STATE_MACHINE_TYPE for t in await self._type_chain(type_id))

    async def link_functional_groups(self, owner: Node) -> None:
        """
        Replace copied children of DI FunctionalGroups under `owner` with Organizes references.

        Call it after every optional member has been added, because only
        existing nodes can be found. Targets are searched among the owner's
        direct children and the children of its state machines (where LADS puts
        Start/Stop/CurrentState). Copies without a real counterpart are deleted:
        an unimplemented method or a variable nobody updates only misleads clients.
        """
        functional_group_type = ua.NodeId(DI_FUNCTIONAL_GROUP_TYPE, self.ns.di)

        groups: list[Node] = []
        candidates: dict[tuple[int, str], Node] = {}
        for child in await owner.get_children(refs=ua.ObjectIds.HasChild):
            node_class = await child.read_node_class()
            if node_class == ua.NodeClass.Object and await child.read_type_definition() == functional_group_type:
                groups.append(child)
                continue
            candidates.setdefault(_key(await child.read_browse_name()), child)
        for child in list(candidates.values()):
            if await child.read_node_class() == ua.NodeClass.Object and await self._is_state_machine(child):
                for grandchild in await child.get_children(refs=ua.ObjectIds.HasChild):
                    candidates.setdefault(_key(await grandchild.read_browse_name()), grandchild)

        for group in groups:
            for copied in await group.get_children(refs=ua.ObjectIds.Organizes):
                bname = await copied.read_browse_name()
                if not str(copied.nodeid.Identifier).startswith(f"{group.nodeid.Identifier}."):
                    continue  # already a reference to a node outside the group
                target = candidates.get(_key(bname))
                await self.server.delete_nodes([copied], recursive=True)
                if target is None:
                    logger.debug("Dropped unimplemented %s from functional group %s", bname, group.nodeid)
                    continue
                await group.add_reference(target.nodeid, ua.ObjectIds.Organizes, forward=True, bidirectional=False)
