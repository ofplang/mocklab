"""The ways a mock instrument command can fail, named without reference to any protocol.

The same instrument behaviour is served over SiLA2 and over LADS OPC UA, and each protocol has
its own vocabulary for a failure: SiLA2 reports an undefined execution error carrying the
exception's class name and message, OPC UA answers a method call with a StatusCode. The
instrument cannot pick either, so it raises one of the kinds below and each protocol adapter
translates.

Each kind is also a subclass of the built-in exception the SiLA2 servers raised before this
package existed (`ValueError` or `RuntimeError`), and `builtin_errors` converts back to exactly
that built-in. The conversion is not cosmetic: sila2 builds the client-visible message as
`"<ExceptionClassName> - <message>"`, so raising `InvalidArgument` straight through would change
what a SiLA2 client reads from `ValueError - ...` to `InvalidArgument - ...`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


class InstrumentError(Exception):
    """Base of every failure an instrument command reports on purpose."""

    # The built-in type this kind was before it had a name of its own. `builtin_errors`
    # re-raises as this type; subclasses override it.
    builtin: type[Exception] = RuntimeError


class InvalidArgument(InstrumentError, ValueError):
    """A parameter value the command refuses (out of range, empty, ...)."""

    builtin = ValueError


class InvalidState(InstrumentError, RuntimeError):
    """The instrument, or the world around it, is not in a state the command can start from:
    another command is executing, a required earlier step has not happened, or the item the
    command acts on is not there."""

    builtin = RuntimeError


class WorldError(InstrumentError, RuntimeError):
    """The laboratory model could not be reached or answered unusably, or is not configured
    for a command that cannot run without it. The physical effect therefore did not happen."""

    builtin = RuntimeError


class UnknownStation(InstrumentError, ValueError):
    """A transporter was given a station name it does not serve.

    Kept apart from `InvalidArgument` because the real Ardea declares a dedicated error for it
    (`InvalidStation`), which the SiLA2 adapter raises instead of a built-in."""

    builtin = ValueError


@contextmanager
def builtin_errors() -> Iterator[None]:
    """Re-raise any `InstrumentError` inside the block as its built-in type, same message.

    For the SiLA2 adapters, whose clients must keep seeing the class names they saw before
    (see the module docstring). The original is chained as the cause, so a server log still
    shows which kind it was."""
    try:
        yield
    except InstrumentError as error:
        raise error.builtin(str(error)) from error
