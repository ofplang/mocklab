"""Cut one device's section out of the lab-wide command-duration file.

Run at **image build time** by each server's Dockerfile (see the `durations` builder stage
there), never at run time. The lab-wide file is one document so the whole lab's timing can be
read and edited in one place; a server image has no business carrying another instrument's
numbers, so the build narrows it down to the one section that server needs.

Two format choices are worth knowing:

* the input is YAML because it is meant to be read and commented by people, and the output is
  JSON because it is only ever read by a program -- which is what keeps PyYAML in this builder
  stage instead of in ten runtime images (five per protocol);
* the output keeps the *same shape* as the device's section in the input (a `commands` mapping
  of mappings) rather than flattening to name -> seconds. Flattening would read more nicely
  today and would have to be undone the moment a per-command setting other than `duration`
  (jitter, say) is added.

An absent device is not an error: it yields an empty section, and the server then waits for
nothing. That is the same rule as an absent command, and it is what makes "no timing at all" a
configuration rather than a special case.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", required=True, help="Device id whose section to extract")
    parser.add_argument("--input", required=True, help="Lab-wide duration file (YAML)")
    parser.add_argument("--output", required=True, help="Where to write the one-device section (JSON)")
    return parser.parse_args(argv)


def slice_device(document: Any, device: str) -> dict[str, Any]:
    """Return `device`'s section, validated. Raises ValueError on a malformed document.

    Validation is strict about shape but says nothing about which command names are legitimate:
    this script cannot know a server's command surface. That check lives in
    `tools/tests/test_command_durations.py`, which reads the implementations instead."""
    # An empty file parses to None; treat it as "nothing configured" rather than an error, so an
    # intentionally blank profile is expressible.
    if document is None:
        return {"commands": {}}
    if not isinstance(document, dict):
        raise ValueError("duration file must contain a top-level mapping")

    devices = document.get("devices")
    if devices is None:
        return {"commands": {}}
    if not isinstance(devices, dict):
        raise ValueError("'devices' must be a mapping of device id to its settings")

    section = devices.get(device)
    # An absent device waits for nothing -- the same rule as an absent command.
    if section is None:
        return {"commands": {}}
    if not isinstance(section, dict):
        raise ValueError(f"device '{device}' must be a mapping")

    commands = section.get("commands") or {}
    if not isinstance(commands, dict):
        raise ValueError(f"device '{device}' has a non-mapping 'commands'")

    validated: dict[str, Any] = {}
    for name, entry in commands.items():
        if not isinstance(name, str):
            raise ValueError(f"device '{device}' has a non-string command name")
        # Mappings only, even though every mapping currently holds just `duration`: accepting a
        # bare number as shorthand would mean carrying two input forms for ever.
        if not isinstance(entry, dict):
            raise ValueError(f"command '{device}.{name}' must be a mapping such as {{ duration: 3 }}")
        duration = entry.get("duration", 0)
        # bool is an int in Python, so it has to be excluded explicitly or `duration: true`
        # would silently mean one second.
        if isinstance(duration, bool) or not isinstance(duration, int | float):
            raise ValueError(f"command '{device}.{name}' has a non-numeric duration")
        if duration < 0:
            raise ValueError(f"command '{device}.{name}' has a negative duration")
        # Unknown sibling keys are preserved rather than dropped, so a setting added to the
        # lab-wide file reaches the servers without this script needing to learn about it.
        validated[name] = {**entry, "duration": float(duration)}

    return {"commands": validated}


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    document = yaml.safe_load(Path(arguments.input).read_text(encoding="utf-8"))
    section = slice_device(document, arguments.device)

    output_path = Path(arguments.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so a rebuild of an unchanged profile produces an identical file, which keeps the
    # docker layer cached.
    output_path.write_text(json.dumps(section, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    command_count = len(section["commands"])
    print(f"sliced {command_count} command duration(s) for device '{arguments.device}' into {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
