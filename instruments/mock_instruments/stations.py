"""Which station name means which place in the world: Ardea's station map.

The real Ardea takes station *names* -- `Base1`, `Base2`, ... -- and resolves each one
against its motion configuration, which records where that station sits on the rail and
which robot tasks reach it. A workflow therefore says "carry the plate from `Base1` to
`Base3`", and that is the vocabulary this mock has to accept too, or a workflow written
here could not be pointed at the machine.

The machine's motion configuration is measured geometry and controller task names, none of
which exist in a world model. What does exist is where a plate physically sits, so the
mock's equivalent is a much smaller thing: a name-to-location map.

    ARDEA_STATIONS="Base1=station.slot1,Base2=station.slot2,Base3=seal-remover.stage"

WHY AN ENVIRONMENT VARIABLE rather than a file. Per-server settings arrive through the
environment in this repository (`docs/RULES.md`), and this map is per-server: it pairs
*this* machine's station names with *this* world's spots. Keeping it in the environment
also means it can be changed with a `--force-recreate` rather than a rebuild -- unlike the
command durations, which are baked -- which matters because the map has to agree with the
seed, and the seed is likewise editable between runs. A YAML file would have been nicer to
comment, but reading one would put PyYAML into the runtime image, which the durations were
deliberately baked to JSON to avoid.

The map is validated here, at construction, so a container with a typo in it fails at
startup with the reason rather than at the first transfer.
"""

from __future__ import annotations

import os

STATIONS_VARIABLE = "ARDEA_STATIONS"

# Entries are comma-separated, and whitespace around a name, a location or a separator is
# formatting rather than part of a value -- unlike the world model's own names, where padding
# is rejected outright, because there the padded string *is* the name being addressed.
_ENTRY_SEPARATOR = ","
_PAIR_SEPARATOR = "="


class StationMapError(ValueError):
    """The station map is missing or unusable.

    Raised while the server is being constructed. A transporter with no station map cannot
    do the one thing it exists for, so this is fatal rather than a mode of operation -- which
    is the difference from the laboratory-model settings, where an absent variable legitimately
    means "not wired up".
    """


def load_station_map(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Read and validate `ARDEA_STATIONS`, returning {station name: `device.spot`}.

    `environ` defaults to the process environment and is a parameter so tests need not
    mutate global state."""
    source = os.environ if environ is None else environ

    raw = source.get(STATIONS_VARIABLE)
    if raw is None or not raw.strip():
        raise StationMapError(
            f"{STATIONS_VARIABLE} must be set to this machine's station map, as "
            f"'Base1=station.slot1,Base2=station.slot2,...'. Without it no station name can be "
            f"resolved and no transfer is possible."
        )

    stations: dict[str, str] = {}
    # Track the reverse direction as well: two names for one spot is a mistake worth naming,
    # since on this bench a station *is* one spot.
    station_by_location: dict[str, str] = {}

    for entry in raw.split(_ENTRY_SEPARATOR):
        entry = entry.strip()
        # A trailing separator is harmless formatting, so an empty piece is skipped rather
        # than rejected.
        if not entry:
            continue

        if entry.count(_PAIR_SEPARATOR) != 1:
            raise StationMapError(
                f"{STATIONS_VARIABLE} entry {entry!r} must be exactly one 'name=device.spot' pair"
            )

        name, location = (part.strip() for part in entry.split(_PAIR_SEPARATOR, 1))
        _validate_station_name(name, entry)
        _validate_location(location, entry)

        if name in stations:
            raise StationMapError(f"{STATIONS_VARIABLE} names station {name!r} twice")
        if location in station_by_location:
            raise StationMapError(
                f"{STATIONS_VARIABLE} maps both {station_by_location[location]!r} and {name!r} to "
                f"{location!r}; a station is one spot"
            )

        stations[name] = location
        station_by_location[location] = name

    if not stations:
        raise StationMapError(f"{STATIONS_VARIABLE} contains no station entries")
    return stations


def _validate_station_name(name: str, entry: str) -> None:
    # The name travels in a SiLA2 command parameter and is compared verbatim, so the only
    # rules are that there is one and that it is a single token.
    if not name:
        raise StationMapError(f"{STATIONS_VARIABLE} entry {entry!r} has an empty station name")
    if any(character.isspace() for character in name):
        raise StationMapError(f"{STATIONS_VARIABLE} station name {name!r} must not contain whitespace")


def _validate_location(location: str, entry: str) -> None:
    # The same `device.spot` grammar the world model enforces, checked here so a malformed
    # location is a startup failure rather than a 400 on the first transfer. This is
    # validating *configuration*, not asking the world whether the spot exists -- whether it
    # does is answered by the move itself failing, which is the rule for this repository.
    if not location:
        raise StationMapError(f"{STATIONS_VARIABLE} entry {entry!r} has an empty location")
    if any(character.isspace() for character in location):
        raise StationMapError(f"{STATIONS_VARIABLE} location {location!r} must not contain whitespace")
    if location.count(".") != 1 or location.startswith(".") or location.endswith("."):
        raise StationMapError(
            f"{STATIONS_VARIABLE} location {location!r} must be of the form 'device.spot' with exactly one dot"
        )
