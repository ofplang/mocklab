"""
LADS device instance (LADSDeviceType) placed in the DI DeviceSet.

A LADS server exposes one or more devices under `Objects/DeviceSet`. Each
device carries DI/Machinery identification, a DeviceState state machine and a
FunctionalUnitSet. The mock servers expose exactly one device each, mirroring
the one-server-per-instrument layout of the SiLA2 servers in this repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from asyncua import Node, ua

from .addressspace import DI_DEVICE_SET, LadsTypes, ModelBuilder
from .state_machine import StateMachine

logger = logging.getLogger(__name__)

# Prefix of each device's ProductInstanceUri. The URI has to be globally unique and stable across
# restarts, so it is built from this prefix, the device name and its serial number.
PRODUCT_URI_PREFIX = "urn:ofplang:mock-lab"


@dataclass(frozen=True)
class DeviceIdentity:
    """
    Nameplate data. The same values are written both on the device (DI
    DeviceType properties) and on its Machinery `Identification` add-in,
    because generic Machinery clients read the latter and DI clients the former.
    """

    manufacturer: str
    model: str
    serial_number: str
    software_revision: str = "0.1.0"
    hardware_revision: str = "MOCK"
    device_revision: str = "1"
    device_class: str = "LaboratoryInstrument"
    product_code: str = ""


class LadsDevice:
    def __init__(self, builder: ModelBuilder, node: Node, state: StateMachine, functional_unit_set: Node) -> None:
        self.builder = builder
        self.node = node
        self.state = state
        self.functional_unit_set = functional_unit_set

    @classmethod
    async def create(cls, builder: ModelBuilder, name: str, identity: DeviceIdentity, *, asset_id: str) -> LadsDevice:
        device_set = builder.node(ua.NodeId(DI_DEVICE_SET, builder.ns.di))
        node = await builder.instantiate(
            device_set,
            builder.lads_id(LadsTypes.LADS_DEVICE),
            name,
            nodeid=ua.NodeId(name, builder.ns.vendor),
        )

        di = builder.ns.di
        values: dict[str, tuple[object, ua.VariantType]] = {
            "Manufacturer": (ua.LocalizedText(identity.manufacturer, "en"), ua.VariantType.LocalizedText),
            "Model": (ua.LocalizedText(identity.model, "en"), ua.VariantType.LocalizedText),
            "SerialNumber": (identity.serial_number, ua.VariantType.String),
            "SoftwareRevision": (identity.software_revision, ua.VariantType.String),
            "HardwareRevision": (identity.hardware_revision, ua.VariantType.String),
            "DeviceRevision": (identity.device_revision, ua.VariantType.String),
            "DeviceManual": ("", ua.VariantType.String),
            "RevisionCounter": (0, ua.VariantType.Int32),
            "AssetId": (asset_id, ua.VariantType.String),
            "ComponentName": (ua.LocalizedText(name, "en"), ua.VariantType.LocalizedText),
            "ProductInstanceUri": (f"{PRODUCT_URI_PREFIX}:{name}:{identity.serial_number}", ua.VariantType.String),
        }
        identification = await builder.find_child(node, ua.QualifiedName("Identification", di))
        for target in (node, identification):
            if target is None:
                continue
            for prop_name, (value, varianttype) in values.items():
                prop = await builder.find_child(target, ua.QualifiedName(prop_name, di))
                if prop is not None:
                    await prop.write_value(ua.Variant(value, varianttype))

        device_class = await builder.add_optional(node, "DeviceClass", browse_ns=di)
        await device_class.write_value(ua.Variant(identity.device_class, ua.VariantType.String))

        # DeviceState: Initialization -> Operate. The mock has no sleep/shutdown
        # behaviour, so the Goto* methods are intentionally not exposed.
        state = await StateMachine.attach(builder, await builder.lads_child(node, "DeviceState"))
        await state.set("Initialization")

        functional_unit_set = await builder.lads_child(node, "FunctionalUnitSet")
        await builder.bump_node_version(functional_unit_set)
        return cls(builder, node, state, functional_unit_set)

    async def mark_operational(self) -> None:
        await self.state.set("Operate")
