"""
Loading of the LADS companion-specification NodeSets into an asyncua server.

The NodeSet files are bundled inside this package (see `nodesets/README.md`) so
that every server image carries exactly the same, version-pinned information
model. They must be imported in dependency order.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from importlib import resources

from asyncua import Server

logger = logging.getLogger(__name__)

DI_URI = "http://opcfoundation.org/UA/DI/"
AMB_URI = "http://opcfoundation.org/UA/AMB/"
MACHINERY_URI = "http://opcfoundation.org/UA/Machinery/"
LADS_URI = "http://opcfoundation.org/UA/LADS/"

# Namespace for everything this mock project adds on top of LADS: the device
# instances themselves and a few vendor-specific nodes (e.g. LastError, Ardea's station names).
VENDOR_URI = "http://ofplang.org/UA/MockLab/"

NODESET_FILES = (
    "Opc.Ua.Di.NodeSet2.xml",
    "Opc.Ua.AMB.NodeSet2.xml",
    "Opc.Ua.Machinery.NodeSet2.xml",
    "Opc.Ua.LADS.NodeSet2.xml",
)

_UANODESET = "http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
_U = "{" + _UANODESET + "}"


@dataclass(frozen=True)
class Namespaces:
    """Resolved namespace indexes of one running server."""

    di: int
    amb: int
    machinery: int
    lads: int
    vendor: int


def _patch_missing_encoding_parents(xml_text: str) -> tuple[str, int]:
    """
    Add inverse `HasEncoding` references that some NodeSets omit.

    LADS 1.0.0 declares a few DataTypeEncoding objects ("Default JSON", ...)
    that are only referenced by their DataType. The OPC UA spec allows that,
    but asyncua's importer requires every non-root node to name its parent, so
    those nodes fail with BadParentNodeIdInvalid. We add the inverse reference
    in memory instead of editing the vendored file.
    """
    ET.register_namespace("", _UANODESET)
    root = ET.fromstring(xml_text)

    encoding_owner: dict[str, str] = {}
    for datatype in root.iter(_U + "UADataType"):
        for ref in datatype.iter(_U + "Reference"):
            if ref.get("ReferenceType") == "HasEncoding" and ref.get("IsForward", "true") != "false":
                encoding_owner[(ref.text or "").strip()] = datatype.get("NodeId", "")

    patched = 0
    for obj in root.iter(_U + "UAObject"):
        node_id = obj.get("NodeId", "")
        refs = obj.find(_U + "References")
        if node_id not in encoding_owner or refs is None:
            continue
        if any(ref.get("IsForward") == "false" for ref in refs):
            continue
        inverse = ET.SubElement(refs, _U + "Reference", {"ReferenceType": "HasEncoding", "IsForward": "false"})
        inverse.text = encoding_owner[node_id]
        patched += 1

    if patched == 0:
        return xml_text, 0
    return ET.tostring(root, encoding="unicode"), patched


async def import_lads_nodesets(server: Server) -> Namespaces:
    """
    Import DI, AMB, Machinery and LADS into `server` and register the vendor namespace.

    Also generates the Python classes for the LADS structures (KeyValueType,
    SampleInfoType, ...) so that method arguments can be encoded/decoded.
    """
    package_dir = resources.files("lads_common") / "nodesets"
    for file_name in NODESET_FILES:
        xml_text = (package_dir / file_name).read_text(encoding="utf-8")
        xml_text, patched = _patch_missing_encoding_parents(xml_text)
        if patched:
            logger.info("Patched %s missing HasEncoding parent references in %s", patched, file_name)
        await server.import_xml(xmlstring=xml_text)
        logger.info("Imported nodeset %s", file_name)

    vendor = await server.register_namespace(VENDOR_URI)
    await server.load_data_type_definitions()

    return Namespaces(
        di=await server.get_namespace_index(DI_URI),
        amb=await server.get_namespace_index(AMB_URI),
        machinery=await server.get_namespace_index(MACHINERY_URI),
        lads=await server.get_namespace_index(LADS_URI),
        vendor=vendor,
    )
