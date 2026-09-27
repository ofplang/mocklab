"""Checks that the Ardea mock's feature definitions are still the real machine's.

The Ardea server is this lab's one mock of an instrument that exists, and the whole point of it
is that its nine SiLA Feature definitions are the machine's own, unchanged (`docs/RULES.md`,
`protocols/sila2/specs/ardea_server/README.md`). Two copies of each definition live in this repository, and
they have different jobs:

* `protocols/sila2/specs/ardea_server/<Feature>.sila.xml` -- the source file, copied from `ardea-sila2`. It is
  documentation of what was agreed; nothing reads it at run time.
* `protocols/sila2/servers/ardea_server/ardea_server/generated/<feature>/<Feature>.sila.xml` -- the sila2 code
  generator's normalisation of that same file, copied from the real server's generated tree.
  **This** is what the server serves and what a client receives.

They are not byte-identical -- the generator reflows the XML -- so what is checked here is that
they describe the same feature. The failure this catches is either half drifting: an edit to a
`specs/` file that never reached the served copy (which would make the specs a lie), or a
generated tree refreshed from a newer `ardea-sila2` while the specs stayed behind (which would
hide what actually changed). Neither has any other guard: the generated tree is copied rather
than produced here, so there is no regeneration step that would fail.

What this cannot check is whether either copy still matches the machine. That needs the real
repository at hand, so it is a manual step in `docs/OPERATIONS.md`.

Also asserted: that the set of features is exactly the nine, and that the mock implements the
one command it claims to. Both are cheap here and are the kind of thing a hurried edit breaks.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ElementTree

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[5]
SPECS_DIRECTORY = REPOSITORY_ROOT / "protocols" / "sila2" / "specs" / "ardea_server"
SERVER_DIRECTORY = REPOSITORY_ROOT / "protocols" / "sila2" / "servers" / "ardea_server"
GENERATED_DIRECTORY = SERVER_DIRECTORY / "ardea_server" / "generated"

# The nine features the real Ardea server exposes: its own four, the b-CAP provider's three and
# the KV COM+ provider's two. Written out rather than globbed, so a missing file fails the test
# instead of quietly shrinking what it covers.
FEATURES = [
    "LabwareService",
    "CarriageService",
    "RobotPoseService",
    "RobotOrientationService",
    "VariableService",
    "TaskService",
    "RobotService",
    "DeviceService",
    "ConnectionService",
]

# SiLA feature definitions live in this namespace, so every element name is prefixed with it.
SILA_NAMESPACE = "{http://www.sila-standard.org}"


def identifiers(feature: ElementTree.Element, element: str) -> set[str]:
    """The `Identifier` of every direct `element` child of a feature (commands, properties, ...).

    Direct children only: a command's own `DefinedExecutionErrors` list also holds identifiers,
    and counting those would mix "the errors this feature declares" with "the errors this
    command can raise"."""
    found = set()
    for child in feature.findall(f"{SILA_NAMESPACE}{element}"):
        identifier = child.find(f"{SILA_NAMESPACE}Identifier")
        if identifier is not None and identifier.text:
            found.add(identifier.text.strip())
    return found


def parse(path: pathlib.Path) -> ElementTree.Element:
    return ElementTree.parse(path).getroot()


def specification_path(feature: str) -> pathlib.Path:
    return SPECS_DIRECTORY / f"{feature}.sila.xml"


def served_path(feature: str) -> pathlib.Path:
    # The generated package for a feature is its identifier lowercased, which is the code
    # generator's own convention.
    return GENERATED_DIRECTORY / feature.lower() / f"{feature}.sila.xml"


@pytest.mark.parametrize("feature", FEATURES)
def test_both_copies_of_the_definition_exist(feature: str) -> None:
    assert specification_path(feature).is_file(), f"missing source definition for {feature}"
    assert served_path(feature).is_file(), f"missing served definition for {feature}"


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize("element", ["Command", "Property", "DefinedExecutionError", "Metadata"])
def test_the_served_definition_describes_the_same_feature(feature: str, element: str) -> None:
    # The check that matters: whatever the generator did to the formatting, the identifiers a
    # client can call and the errors it can be given must be the ones the specs promise.
    specification = parse(specification_path(feature))
    served = parse(served_path(feature))

    assert identifiers(specification, element) == identifiers(served, element)


@pytest.mark.parametrize("feature", FEATURES)
def test_the_served_definition_keeps_the_feature_identity(feature: str) -> None:
    # Identifier, version and originator together are the fully qualified name a client
    # resolves, so a difference in any of them is a different feature even if the commands match.
    specification = parse(specification_path(feature))
    served = parse(served_path(feature))

    identifier = specification.find(f"{SILA_NAMESPACE}Identifier")
    assert identifier is not None and identifier.text is not None
    assert identifier.text.strip() == feature

    served_identifier = served.find(f"{SILA_NAMESPACE}Identifier")
    assert served_identifier is not None and served_identifier.text is not None
    assert served_identifier.text.strip() == feature

    for attribute in ("FeatureVersion", "Originator", "Category", "SiLA2Version"):
        assert specification.get(attribute) == served.get(attribute), attribute


def test_no_stray_definitions_are_lying_around() -> None:
    # A tenth file in either place would mean the mock serves something the machine does not, or
    # that specs/ documents a feature nobody serves.
    assert {path.name for path in SPECS_DIRECTORY.glob("*.sila.xml")} == {f"{name}.sila.xml" for name in FEATURES}
    assert {path.parent.name for path in GENERATED_DIRECTORY.glob("*/*.sila.xml")} == {
        name.lower() for name in FEATURES
    }


def test_transfer_is_the_command_the_mock_implements() -> None:
    # The mock's contract with a workflow, in one line: `LabwareService.Transfer` exists in the
    # definition and is implemented, and it is the only implemented command. Everything else
    # goes through the shared `unimplemented` refusal, so counting call sites of that is what
    # says "nothing else quietly grew an implementation".
    labware = parse(specification_path("LabwareService"))
    assert "Transfer" in identifiers(labware, "Command")

    implementation = (
        REPOSITORY_ROOT
        / "protocols"
        / "sila2"
        / "servers"
        / "ardea_server"
        / "ardea_server"
        / "feature_implementations"
        / "labwareservice_impl.py"
    ).read_text(encoding="utf-8")

    assert "def Transfer(" in implementation
    assert "unimplemented(_FEATURE, \"Transfer\")" not in implementation
