# Bundled OPC UA NodeSets

These NodeSet2 files are vendored verbatim from
<https://github.com/OPCFoundation/UA-Nodeset>. The versions are pinned to the
ones that LADS 1.0.0 declares as `RequiredModel`, because the latest Machinery
release additionally requires the IA companion specification.

| File | Model | Source tag |
|---|---|---|
| `Opc.Ua.Di.NodeSet2.xml` | DI 1.04.0 | `DI-1.04.0-2022-11-03` |
| `Opc.Ua.AMB.NodeSet2.xml` | AMB 1.01.0 | `AMB-1.01.0-2022-11-01` |
| `Opc.Ua.Machinery.NodeSet2.xml` | Machinery 1.03.0 | `Machinery-1.03.0-2023-08-01` |
| `Opc.Ua.LADS.NodeSet2.xml` | LADS 1.0.0 | `LADS-1.0.0-2023-11-30` |

Import order matters (dependencies first): DI, AMB, Machinery, LADS.

The LADS file contains six `DataTypeEncoding` objects that are referenced only
from their DataType (forward `HasEncoding`) without the inverse reference.
asyncua needs a parent reference to add a node, so `lads_common.nodesets`
patches the inverse reference in memory at import time. The files on disk are
left untouched.
