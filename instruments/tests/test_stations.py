"""Tests for Ardea's station map: what `ARDEA_STATIONS` accepts and what it refuses.

The map is the one piece of this mock a workflow's arguments depend on -- it decides which
station names `Transfer` will take -- and it is read from the environment at startup, where a
typo has no other chance of being noticed. So the refusals matter as much as the successes:
each one below is a mistake that would otherwise reach a client as a puzzling `InvalidStation`,
or worse, as a transfer to the wrong place.

`load_station_map` takes the environment as a parameter, so nothing here mutates global state.
"""

from __future__ import annotations

import pytest

from mock_instruments.stations import STATIONS_VARIABLE, StationMapError, load_station_map

# The real bench, as configured in docker-compose.yml: two plain plate-holding stations and one
# station per instrument, each a single spot.
REAL_MAP = (
    "Base1=station.slot1,"
    "Base2=station.slot2,"
    "Base3=seal-remover.stage,"
    "Base4=plateloc.stage,"
    "Base5=centrifuge.deck,"
    "Base6=thermal-cycler.block"
)


def load(value: str | None) -> dict[str, str]:
    environ = {} if value is None else {STATIONS_VARIABLE: value}
    return load_station_map(environ)


def test_reads_the_real_bench_map() -> None:
    # Pinned deliberately: this is the mapping the machine has, so a change here should be a
    # change someone meant to make.
    assert load(REAL_MAP) == {
        "Base1": "station.slot1",
        "Base2": "station.slot2",
        "Base3": "seal-remover.stage",
        "Base4": "plateloc.stage",
        "Base5": "centrifuge.deck",
        "Base6": "thermal-cycler.block",
    }


def test_whitespace_around_entries_is_formatting() -> None:
    # A map written across a long compose line gets padded; padding around a separator is not
    # part of a name. (Contrast `laboratory_client`, which rejects padding, because there the
    # padded string *is* the location being addressed.)
    assert load(" Base1 = station.slot1 ,  Base2=station.slot2 ") == {
        "Base1": "station.slot1",
        "Base2": "station.slot2",
    }


def test_a_trailing_separator_is_harmless() -> None:
    assert load("Base1=station.slot1,") == {"Base1": "station.slot1"}


@pytest.mark.parametrize("value", [None, "", "   ", ","])
def test_an_absent_or_empty_map_is_fatal(value: str | None) -> None:
    # Not a mode of operation: a transporter that cannot resolve a station name cannot do the
    # one thing it exists for, so this must stop the server rather than wait for a transfer.
    with pytest.raises(StationMapError):
        load(value)


def test_rejects_an_entry_that_is_not_a_pair() -> None:
    with pytest.raises(StationMapError, match="one 'name=device.spot' pair"):
        load("Base1")


def test_rejects_an_entry_with_two_equals_signs() -> None:
    with pytest.raises(StationMapError, match="one 'name=device.spot' pair"):
        load("Base1=station.slot1=extra")


def test_rejects_an_empty_station_name() -> None:
    with pytest.raises(StationMapError, match="empty station name"):
        load("=station.slot1")


def test_rejects_an_empty_location() -> None:
    with pytest.raises(StationMapError, match="empty location"):
        load("Base1=")


@pytest.mark.parametrize("location", ["station", "station.slot.1", ".slot1", "station."])
def test_rejects_a_location_that_is_not_device_dot_spot(location: str) -> None:
    # The world model's own grammar. Checked here so a malformed location fails at startup
    # rather than as a 400 on the first transfer -- this is validating configuration, not asking
    # the world whether the spot exists (that answer comes from the move itself).
    with pytest.raises(StationMapError, match="device.spot"):
        load(f"Base1={location}")


def test_rejects_a_name_with_internal_whitespace() -> None:
    with pytest.raises(StationMapError, match="must not contain whitespace"):
        load("Base 1=station.slot1")


def test_rejects_the_same_station_named_twice() -> None:
    with pytest.raises(StationMapError, match="twice"):
        load("Base1=station.slot1,Base1=station.slot2")


def test_rejects_two_stations_at_one_spot() -> None:
    # On this bench a station *is* one spot, so two names for it is a mistake -- and a silent
    # one, since both transfers would appear to work while meaning the same place.
    with pytest.raises(StationMapError, match="a station is one spot"):
        load("Base1=station.slot1,Base2=station.slot1")
