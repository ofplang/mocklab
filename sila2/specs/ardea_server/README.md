# ardea_server specs

Feature definition files for the Ardea server. **Do not edit them.** They are copies of the
real instrument's definitions, and a mock that changes them stops being a drop-in
replacement (`docs/RULES.md`).

## Where they came from

All nine files were copied verbatim from the real Ardea SiLA2 server, `ardea-sila2`, at
branch `feature/split-pick-put-scripts` (`177c618`) -- not `main`, which does not yet carry
`LabwareService.Transfer`. Ardea drives the machine through two providers at once, and its
server exposes both providers' features alongside its own, so all nine belong to one server:

| File | Origin | Role |
|---|---|---|
| `LabwareService.sila.xml` | `ardea-sila2/feature_definitions/` | Ardea-specific: labware handling (this is where `Transfer` lives) |
| `CarriageService.sila.xml` | `ardea-sila2/feature_definitions/` | Ardea-specific: travel carriage |
| `RobotPoseService.sila.xml` | `ardea-sila2/feature_definitions/` | Ardea-specific: named-pose checks |
| `RobotOrientationService.sila.xml` | `ardea-sila2/feature_definitions/` | Ardea-specific: turning and parking the arm |
| `VariableService.sila.xml` | `bcap-sila2` (`fe62718`) | DENSO robot controller over ORiN b-CAP |
| `TaskService.sila.xml` | `bcap-sila2` (`fe62718`) | DENSO robot controller over ORiN b-CAP |
| `RobotService.sila.xml` | `bcap-sila2` (`fe62718`) | DENSO robot controller over ORiN b-CAP |
| `DeviceService.sila.xml` | `kvcomplus-sila2` (`e24c142`) | KEYENCE PLC over the KV COM+ Library |
| `ConnectionService.sila.xml` | `kvcomplus-sila2` (`e24c142`) | KEYENCE PLC over the KV COM+ Library |

The two provider repositories are git submodules of `ardea-sila2` (`third_party/`), not of
this repository: what a mock needs is the definitions and the generated code, and carrying a
submodule for that would tie this repository's checkout to a private hardware driver.

## Relation to the served definitions

These are the **source** files. What the server actually serves is the sila2 code
generator's normalisation of them, which lives beside the generated code in
`sila2/servers/ardea_server/ardea_server/generated/<feature>/<Feature>.sila.xml` and was copied
from the real server's generated tree -- so the served bytes are the real server's bytes.
`sila2/servers/ardea_server/tests/test_feature_definitions.py` checks that the two describe the
same feature, which is what would catch a hand edit here or a stale copy there.

## Regenerating

Not something this repository does: the generated tree is copied from `ardea-sila2` rather
than produced here, so that both sides are byte-identical by construction. If these
definitions change on the real machine, copy both the definitions and the generated tree
again, and re-run the checks in `docs/OPERATIONS.md`.
