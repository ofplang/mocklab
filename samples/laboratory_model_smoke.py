"""Smoke test for the laboratory model service itself, over plain HTTP.

The only sample that speaks no instrument protocol: it tests the world model as a component, so
it is not part of `run_all_smoke_tests.py` (which is scoped to the five instrument servers)
and is run on its own. What it pins down is the model's rule set, in the order the rules
fire:

* a declared spot that nothing has touched reads as empty and accessible, while a spot the
  seed never declared is a 404 -- the topology is declared, so a location does not come into
  being by being mentioned;
* locking a location blocks reaching into it (add, remove, move) with a 409 and a stable
  error code, while leaving any item that is already there in place;
* an item keeps its `item_id` across a move, and the source is emptied by it;
* accessibility is checked before occupancy, so a locked destination is reported even when
  the source has nothing to give.

Prerequisite: the laboratory model is up (`docker compose up`). The world is wiped at both
ends of the test, so it is safe to run repeatedly, but it is destructive -- do not run it
against a stack that is mid-workflow. Exit code 0 means every rule held.
"""

from __future__ import annotations

import argparse
from urllib.parse import quote

from common import (
    DEFAULT_LABORATORY_MODEL_URL,
    add_item_to_location,
    expect_laboratory_model_error,
    get_laboratory_model_health,
    get_location_state,
    lock_location,
    move_item_between_locations,
    remove_item_from_location,
    reset_laboratory_model,
    unlock_location,
)

# Both ends are the station's two slots: plain holding places that belong to no instrument, so
# nothing here depends on a device being in a particular state. They have to be spots the seed
# declared -- an undeclared location is a 404 now, not an empty one -- which is also what
# UNDECLARED_LOCATION below exists to demonstrate.
DEFAULT_SOURCE_LOCATION = "station.slot2"
DEFAULT_DESTINATION_LOCATION = "station.slot1"
# Well-formed (`device.spot`) but absent from the seed. Reaching for it is the check that the
# world refuses to invent a place on request, which is what makes a workflow's wrong assumption
# about the physical layout visible instead of silently satisfied.
UNDECLARED_LOCATION = "station.slot9"


def main() -> int:
    # This sample talks only to the world model, so it builds its own parser instead of
    # using common.build_parser (which is about SiLA2 host/port/timeout).
    parser = argparse.ArgumentParser(description="Smoke test the laboratory model service directly.")
    parser.add_argument(
        "--laboratory-model-url",
        default=DEFAULT_LABORATORY_MODEL_URL,
        help="Laboratory model base URL",
    )
    parser.add_argument(
        "--source-location",
        default=DEFAULT_SOURCE_LOCATION,
        help="Source location used by the smoke test",
    )
    parser.add_argument(
        "--destination-location",
        default=DEFAULT_DESTINATION_LOCATION,
        help="Destination location used by the smoke test",
    )
    args = parser.parse_args()

    # Liveness first: everything after this assumes the service is answering.
    health = get_laboratory_model_health(laboratory_model_url=args.laboratory_model_url)
    print(f"Health: {health}")
    if health.get("status") != "healthy":
        raise RuntimeError(f"Unexpected health response: {health}")

    # Start from an empty world so the assertions below are absolute, not relative to
    # whatever a previous run or a running workflow left behind.
    reset_result = reset_laboratory_model(laboratory_model_url=args.laboratory_model_url)
    print(f"Reset before test: {reset_result}")
    if reset_result.get("cleared") is not True:
        raise RuntimeError(f"Unexpected reset response: {reset_result}")

    # A declared spot nothing has touched reads as empty and accessible.
    source_before = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    print(f"Source before add: {source_before}")
    if source_before.get("occupied") is not False or source_before.get("accessible") is not True:
        raise RuntimeError(f"Expected empty source location before add: {source_before}")

    # The other half of that property, and the reason the topology is declared at all: a
    # well-formed location the seed does not contain is absent, not empty. A read of it is a 404
    # rather than a fabricated empty spot, so a workflow that believes in a place the lab does
    # not have finds out here.
    undeclared_status, undeclared_error = expect_laboratory_model_error(
        laboratory_model_url=args.laboratory_model_url,
        path=f"/locations/{quote(UNDECLARED_LOCATION, safe='')}",
        method="GET",
    )
    print(f"Undeclared location error: status={undeclared_status} body={undeclared_error}")
    if undeclared_status != 404 or undeclared_error["error"]["code"] != "unknown_location":
        raise RuntimeError(f"Unexpected undeclared-location error: {undeclared_status}, {undeclared_error}")

    # Lock it (what a closing door/lid does) and confirm the change is both returned by the
    # lock call and visible to a subsequent read.
    locked_source = lock_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    print(f"Locked source: {locked_source}")
    if locked_source.get("accessible") is not False:
        raise RuntimeError(f"Unexpected lock response: {locked_source}")

    source_after_lock = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    print(f"Source after lock: {source_after_lock}")
    if source_after_lock.get("accessible") is not False:
        raise RuntimeError(f"Expected locked source location: {source_after_lock}")

    # First negative case: you cannot put anything into a locked location. The status code
    # and the error code are both checked -- the code is the stable part of the contract.
    add_locked_status, add_locked_error = expect_laboratory_model_error(
        laboratory_model_url=args.laboratory_model_url,
        path="/items/add",
        method="POST",
        payload={"location": args.source_location},
    )
    print(f"Add while locked error: status={add_locked_status} body={add_locked_error}")
    if add_locked_status != 409 or add_locked_error["error"]["code"] != "location_locked":
        raise RuntimeError(f"Unexpected add-while-locked error: {add_locked_status}, {add_locked_error}")

    # Unlock and retry: the same call must now succeed, which is what proves the rejection
    # above was the lock and not something else.
    unlocked_source = unlock_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    print(f"Unlocked source: {unlocked_source}")
    if unlocked_source.get("accessible") is not True:
        raise RuntimeError(f"Unexpected unlock response: {unlocked_source}")

    added_item = add_item_to_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    print(f"Added item: {added_item}")
    if added_item.get("occupied") is not True or not added_item.get("item_id"):
        raise RuntimeError(f"Unexpected add response: {added_item}")
    # Remember the minted id: every later assertion tracks this same item.
    item_id = added_item["item_id"]

    source_after_add = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    print(f"Source after add: {source_after_add}")
    if source_after_add.get("item_id") != item_id or source_after_add.get("accessible") is not True:
        raise RuntimeError(f"Added item was not found at source location: {source_after_add}")

    # Move it. The identity must survive the hop -- the model models a move as the same
    # item changing place, not as a destroy plus a create.
    moved_item = move_item_between_locations(
        laboratory_model_url=args.laboratory_model_url,
        source=args.source_location,
        destination=args.destination_location,
    )
    print(f"Moved item: {moved_item}")
    if moved_item.get("moved") is not True or moved_item.get("item_id") != item_id:
        raise RuntimeError(f"Unexpected move response: {moved_item}")

    # Both ends are checked: the source must be empty afterwards (no duplication) and the
    # destination must hold the same item.
    source_after_move = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.source_location,
    )
    destination_after_move = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    print(f"Source after move: {source_after_move}")
    print(f"Destination after move: {destination_after_move}")
    if source_after_move.get("occupied") is not False:
        raise RuntimeError(f"Expected empty source location after move: {source_after_move}")
    if destination_after_move.get("item_id") != item_id or destination_after_move.get("accessible") is not True:
        raise RuntimeError(f"Moved item was not found at destination location: {destination_after_move}")

    # Lock the destination *with the item inside it*: locking must not evict or hide the
    # item, only make it unreachable. That combination (occupied and inaccessible) is a
    # closed instrument holding a plate, so it has to be expressible.
    locked_destination = lock_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    print(f"Locked destination: {locked_destination}")
    if locked_destination.get("accessible") is not False:
        raise RuntimeError(f"Unexpected destination lock response: {locked_destination}")

    destination_after_lock = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    print(f"Destination after lock: {destination_after_lock}")
    if destination_after_lock.get("item_id") != item_id or destination_after_lock.get("accessible") is not False:
        raise RuntimeError(f"Expected locked destination location with item present: {destination_after_lock}")

    # Second negative case: the item is there, but a lock also blocks taking it out.
    remove_locked_status, remove_locked_error = expect_laboratory_model_error(
        laboratory_model_url=args.laboratory_model_url,
        path="/items/remove",
        method="DELETE",
        payload={"location": args.destination_location},
    )
    print(f"Remove while locked error: status={remove_locked_status} body={remove_locked_error}")
    if remove_locked_status != 409 or remove_locked_error["error"]["code"] != "location_locked":
        raise RuntimeError(f"Unexpected remove-while-locked error: {remove_locked_status}, {remove_locked_error}")

    unlocked_destination = unlock_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    print(f"Unlocked destination: {unlocked_destination}")
    if unlocked_destination.get("accessible") is not True:
        raise RuntimeError(f"Unexpected destination unlock response: {unlocked_destination}")

    # Third negative case, and the reason it is worth a test of its own: at this point the
    # SOURCE is empty as well, so the move violates two rules at once. The expected code is
    # `destination_locked`, which pins down the checking order -- accessibility of both ends
    # is verified before occupancy is even looked at.
    lock_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    move_locked_status, move_locked_error = expect_laboratory_model_error(
        laboratory_model_url=args.laboratory_model_url,
        path="/items/move",
        method="POST",
        payload={"source": args.source_location, "destination": args.destination_location},
    )
    print(f"Move to locked destination error: status={move_locked_status} body={move_locked_error}")
    if move_locked_status != 409 or move_locked_error["error"]["code"] != "destination_locked":
        raise RuntimeError(
            f"Unexpected move-to-locked-destination error: {move_locked_status}, {move_locked_error}"
        )
    unlock_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )

    # Now that the destination is reachable again, the removal that was rejected earlier
    # must succeed, and must report the same item id the world has been tracking throughout.
    removed_item = remove_item_from_location(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    print(f"Removed item: {removed_item}")
    if removed_item.get("removed") is not True or removed_item.get("item_id") != item_id:
        raise RuntimeError(f"Unexpected remove response: {removed_item}")

    # Removing empties the location but leaves it accessible: the item is gone, the spot
    # itself is untouched.
    destination_after_remove = get_location_state(
        laboratory_model_url=args.laboratory_model_url,
        location=args.destination_location,
    )
    print(f"Destination after remove: {destination_after_remove}")
    if destination_after_remove.get("occupied") is not False or destination_after_remove.get("accessible") is not True:
        raise RuntimeError(f"Expected empty destination location after remove: {destination_after_remove}")

    # Leave the world clean for whatever runs next.
    reset_after_test = reset_laboratory_model(laboratory_model_url=args.laboratory_model_url)
    print(f"Reset after test: {reset_after_test}")
    if reset_after_test.get("cleared") is not True:
        raise RuntimeError(f"Unexpected final reset response: {reset_after_test}")

    print("Laboratory model smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
