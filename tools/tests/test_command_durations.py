"""Tests for the build-time duration slicer, and for the duration files themselves.

Two different things are checked here, and the second is the reason this file exists at all.

`slice_device` gets the ordinary treatment: what it accepts, what it refuses, and what it does
with an absent device or command.

The coverage tests are the interesting ones. An unlisted command waits for nothing, which means
a command that gains a wait in the implementation but is never added to a timing profile silently
loses its Running window -- a polling client would then never observe the Status transition, and
nothing would fail. There is no way for the slicer to catch that (it cannot know a server's
command surface), so it is caught here instead, by reading the implementations: every command
whose body waits must appear in every profile that declares durations, and nothing else may.

The default profile is exempt because it declares nothing on purpose: it is the "no waiting at
all" configuration, and a test pins that down rather than asking it for coverage.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

from slice_durations import slice_device

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_DIRECTORY = REPOSITORY_ROOT / "config"
# The default profile declares nothing (see `test_the_default_profile_configures_no_waiting`); the
# coverage rule applies to the profiles that do declare durations.
DEFAULT_PROFILE = "command_durations.yaml"
TIMED_PROFILES = ["command_durations.realistic.yaml"]

INSTRUMENTS_DIRECTORY = REPOSITORY_ROOT / "instruments" / "mock_instruments"

# Which device each instrument module is. The waits live in these modules -- the SiLA2 and LADS
# servers only expose them -- so this is what the coverage tests below read.
DEVICE_BY_MODULE = {
    "centrifuge": "centrifuge",
    "thermal_cycler": "thermal-cycler",
    "plateloc": "plateloc",
    "seal_remover": "seal-remover",
    "ardea": "ardea",
}

# Which instrument module each server package exposes. Every server's Dockerfile slices the
# durations of one device at build time (`--device <id>`), and that has to be the device its
# instrument is, or the server would be timed as something else; `test_each_server_image_slices_
# its_own_device` holds the two together.
MODULE_BY_SERVER = {
    "protocols/sila2/servers/microplate_centrifuge_server": "centrifuge",
    "protocols/sila2/servers/automated_thermal_cycler_server": "thermal_cycler",
    "protocols/sila2/servers/plateloc_server": "plateloc",
    "protocols/sila2/servers/automated_plate_seal_remover_server": "seal_remover",
    "protocols/sila2/servers/ardea_server": "ardea",
    "protocols/lads/servers/microplate_centrifuge_lads": "centrifuge",
    "protocols/lads/servers/automated_thermal_cycler_lads": "thermal_cycler",
    "protocols/lads/servers/plateloc_lads": "plateloc",
    "protocols/lads/servers/automated_plate_seal_remover_lads": "seal_remover",
}

# Calls that make a command wait, each taking the command's name as a string literal: the wait
# itself, and the centrifuge's two helpers which wrap it (its commands share those rather than
# waiting inline).
_WAITING_CALLS = frozenset({"sleep_for", "_begin_running", "_begin_quiet"})


def timed_commands(module: str) -> set[str]:
    """Names of the commands in instrument `module` that wait, as given to the waiting calls.

    Read from the source rather than by importing it, since the question is a syntactic one. The
    name each call passes is the duration file's key, so collecting those literals answers
    "which keys does this device read" directly."""
    tree = ast.parse((INSTRUMENTS_DIRECTORY / f"{module}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not (isinstance(call.func, ast.Attribute) and call.func.attr in _WAITING_CALLS):
            continue
        literals = [arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        # A waiting call without a literal name would make the coverage below meaningless for
        # it, so it is a failure rather than something to skip. The helpers' own internal call
        # (`self.sleep_for(command_name)`) is the one legitimate exception.
        if not literals:
            assert call.args and isinstance(call.args[-1], ast.Name) and call.args[-1].id == "command_name", (
                f"{module}.py waits without naming the command: {ast.unparse(call)}"
            )
            continue
        found.update(literals)
    return found


def load_profile(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIRECTORY / name).read_text(encoding="utf-8"))


# --- Coverage: the duration files and the implementations must agree. ---


@pytest.mark.parametrize("profile", TIMED_PROFILES)
@pytest.mark.parametrize("module", sorted(DEVICE_BY_MODULE))
def test_every_waiting_command_has_a_duration(profile: str, module: str) -> None:
    # The failure this prevents: a command that waits but is unlisted takes no time, so its
    # Running window collapses and a polling client stops being able to see the transition.
    device = DEVICE_BY_MODULE[module]
    configured = set(slice_device(load_profile(profile), device)["commands"])

    missing = timed_commands(module) - configured
    assert not missing, f"{profile} is missing durations for {device}: {sorted(missing)}"


@pytest.mark.parametrize("profile", TIMED_PROFILES)
@pytest.mark.parametrize("module", sorted(DEVICE_BY_MODULE))
def test_no_duration_is_configured_for_a_command_that_does_not_wait(profile: str, module: str) -> None:
    # The other direction, which catches a typo in a command name and an entry left behind after
    # a command stopped waiting. Either way the number would silently do nothing.
    device = DEVICE_BY_MODULE[module]
    configured = set(slice_device(load_profile(profile), device)["commands"])

    unexpected = configured - timed_commands(module)
    assert not unexpected, f"{profile} configures non-waiting commands for {device}: {sorted(unexpected)}"


@pytest.mark.parametrize("profile", TIMED_PROFILES)
def test_profiles_describe_only_devices_that_exist(profile: str) -> None:
    # A device name that matches nothing in the world is a typo or a leftover; either way nothing
    # reads it. The seed file is the authority on which devices exist, so it is what this checks
    # against rather than a list repeated here.
    seed = yaml.safe_load((CONFIG_DIRECTORY / "laboratory_model.seed.yaml").read_text(encoding="utf-8"))
    declared = {entry["id"] for entry in seed["devices"]}

    configured = set(load_profile(profile).get("devices") or {})

    assert configured <= declared, f"{profile} names undeclared devices: {sorted(configured - declared)}"


@pytest.mark.parametrize("profile", TIMED_PROFILES)
def test_profiles_describe_only_devices_that_have_a_server(profile: str) -> None:
    # Tighter than the test above, and not redundant with it: a device can be declared by the
    # seed and still have no server (the station is a plain holding place -- two slots, nothing
    # commandable, so no SiLA2 server exists for it). Durations for such a device would be read
    # by nobody, and the seed-based check above cannot see that.
    configured = set(load_profile(profile).get("devices") or {})
    served = set(DEVICE_BY_MODULE.values())

    assert configured <= served, f"{profile} configures serverless devices: {sorted(configured - served)}"


@pytest.mark.parametrize("server", sorted(MODULE_BY_SERVER))
def test_each_server_image_slices_its_own_device(server: str) -> None:
    # A server image bakes in the durations of whichever device its Dockerfile names. Naming the
    # wrong one would not fail anywhere -- the server would just be timed as another instrument,
    # or not at all -- so the Dockerfile is held to the device its instrument module is.
    dockerfile = (REPOSITORY_ROOT / server / "Dockerfile").read_text(encoding="utf-8")
    device = DEVICE_BY_MODULE[MODULE_BY_SERVER[server]]

    assert f"--device {device} " in dockerfile, f"{server}/Dockerfile does not slice durations for {device}"


def test_every_server_directory_is_covered() -> None:
    # So a server added later cannot escape the check above by not being listed.
    dockerfiles = REPOSITORY_ROOT.glob("protocols/*/servers/*/Dockerfile")
    on_disk = {path.parent.relative_to(REPOSITORY_ROOT).as_posix() for path in dockerfiles}

    assert on_disk == set(MODULE_BY_SERVER)


def test_the_default_profile_configures_no_waiting() -> None:
    # The default is "the lab runs as fast as it can", expressed as a profile that declares
    # nothing. Asserted rather than assumed, because the file existing but being empty is exactly
    # the state that could otherwise be mistaken for an unfinished edit.
    assert load_profile(DEFAULT_PROFILE)["devices"] == {}


@pytest.mark.parametrize("module", sorted(DEVICE_BY_MODULE))
def test_the_default_profile_slices_to_nothing_for_every_server(module: str) -> None:
    # The end a server actually sees: whatever device it asks for, the default profile gives it no
    # durations, so every command returns without waiting.
    sliced = slice_device(load_profile(DEFAULT_PROFILE), DEVICE_BY_MODULE[module])

    assert sliced == {"commands": {}}


# --- The slicer itself. ---


def test_slices_only_the_requested_device() -> None:
    document = {
        "devices": {
            "centrifuge": {"commands": {"SpinCycle": {"duration": 20}}},
            "plateloc": {"commands": {"StartCycle": {"duration": 15}}},
        }
    }

    assert slice_device(document, "centrifuge") == {"commands": {"SpinCycle": {"duration": 20.0}}}


def test_keeps_the_mapping_shape_rather_than_flattening() -> None:
    # Flattening to name -> seconds would read better today and would have to be undone the
    # moment a second per-command setting (jitter) arrives.
    sliced = slice_device({"devices": {"centrifuge": {"commands": {"Home": {"duration": 5}}}}}, "centrifuge")

    assert sliced["commands"]["Home"] == {"duration": 5.0}


def test_preserves_settings_it_does_not_know_about() -> None:
    # So a setting added to the lab-wide file reaches the servers without this script having to
    # learn about it first.
    document = {"devices": {"centrifuge": {"commands": {"Home": {"duration": 5, "jitter": 0.2}}}}}

    assert slice_device(document, "centrifuge")["commands"]["Home"] == {"duration": 5.0, "jitter": 0.2}


def test_an_absent_device_waits_for_nothing() -> None:
    # Not an error: "no timing at all" has to be a configuration rather than a special case.
    assert slice_device({"devices": {"plateloc": {"commands": {}}}}, "centrifuge") == {"commands": {}}


def test_an_empty_document_waits_for_nothing() -> None:
    # An intentionally blank profile parses to None.
    assert slice_device(None, "centrifuge") == {"commands": {}}


def test_a_missing_duration_defaults_to_no_wait() -> None:
    assert slice_device({"devices": {"c": {"commands": {"X": {}}}}}, "c") == {"commands": {"X": {"duration": 0.0}}}


def test_rejects_a_bare_number_instead_of_a_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        slice_device({"devices": {"c": {"commands": {"X": 3}}}}, "c")


def test_rejects_a_non_numeric_duration() -> None:
    with pytest.raises(ValueError, match="non-numeric"):
        slice_device({"devices": {"c": {"commands": {"X": {"duration": "3"}}}}}, "c")


def test_rejects_a_boolean_duration() -> None:
    # bool is an int in Python, so without an explicit check `duration: true` would mean 1 second.
    with pytest.raises(ValueError, match="non-numeric"):
        slice_device({"devices": {"c": {"commands": {"X": {"duration": True}}}}}, "c")


def test_rejects_a_negative_duration() -> None:
    with pytest.raises(ValueError, match="negative"):
        slice_device({"devices": {"c": {"commands": {"X": {"duration": -1}}}}}, "c")


def test_rejects_a_non_mapping_document() -> None:
    with pytest.raises(ValueError, match="top-level mapping"):
        slice_device(["devices"], "c")
