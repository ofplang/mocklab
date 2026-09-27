"""
Binding of Python coroutines to OPC UA Method nodes, and how a failure is reported.

Error reporting is the main difference from SiLA2: a SiLA command error carries a free-text
message, while an OPC UA Call returns only a StatusCode. asyncua additionally turns *any*
exception raised in a callback into BadUnexpectedError. So a bound handler's failure is turned
into two things:

- a StatusCode returned to the caller, chosen from the failure's kind (`status_for`), and
- the message, logged and handed to an `on_error` sink, which the functional units use to
  publish it in their vendor `LastError` variable -- the only place a LADS client can read *why*.

The kinds come from the protocol-independent instruments (`mock_instruments.errors`); the one
failure the LADS layer raises of its own accord -- a call the state machine does not allow right
now, or an unknown program template -- is a `LadsMethodError` carrying its StatusCode directly.

Adapted from the lads-test prototype.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from asyncua import Node, Server, ua

from mock_instruments.errors import InstrumentError, InvalidArgument, InvalidState, UnknownStation, WorldError

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[list[ua.Variant] | None]]
ErrorSink = Callable[[str], Awaitable[None]]


class LadsMethodError(Exception):
    """A refusal decided by the LADS layer itself, with the StatusCode to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def invalid_state(message: str) -> LadsMethodError:
    return LadsMethodError(ua.StatusCodes.BadInvalidState, message)


def invalid_argument(message: str) -> LadsMethodError:
    return LadsMethodError(ua.StatusCodes.BadInvalidArgument, message)


def not_found(message: str) -> LadsMethodError:
    return LadsMethodError(ua.StatusCodes.BadNotFound, message)


# Which StatusCode each instrument failure kind answers with:
#   * a refused parameter (range, emptiness) or an unknown station name is the caller's argument;
#   * a command the instrument cannot start from its current state -- another one executing, a
#     required earlier step missing, no item where it acts -- is an invalid state;
#   * the world model failing to carry out or confirm a physical effect is also reported as an
#     invalid state: the effect could not happen in the world as it is. The message in LastError
#     says whether the world refused or could not be reached.
_STATUS_BY_KIND: tuple[tuple[type[InstrumentError], int], ...] = (
    (InvalidArgument, ua.StatusCodes.BadInvalidArgument),
    (UnknownStation, ua.StatusCodes.BadInvalidArgument),
    (InvalidState, ua.StatusCodes.BadInvalidState),
    (WorldError, ua.StatusCodes.BadInvalidState),
)


def status_for(error: BaseException) -> int:
    """The StatusCode a failure is answered with. Anything unexpected is an internal error."""
    if isinstance(error, LadsMethodError):
        return error.status
    for kind, status in _STATUS_BY_KIND:
        if isinstance(error, kind):
            return status
    return ua.StatusCodes.BadInternalError


def message_for(label: str, error: BaseException) -> str:
    """The text published in LastError: the error's own message, prefixed with the method's label
    unless the message already names it."""
    message = error.message if isinstance(error, LadsMethodError) else str(error)
    return message if message.startswith(label) else f"{label}: {message}"


def bind_method(
    server: Server,
    node: Node,
    handler: Handler,
    *,
    label: str,
    on_error: ErrorSink | None = None,
) -> None:
    """
    Link `handler(*input_values)` to the method `node`.

    The handler receives plain Python values (Variant payloads) and returns a list of output
    Variants or None. `label` names the method in logs, like the "<Feature>.<Command> called"
    lines of the SiLA2 servers. A successful call clears LastError.
    """

    async def callback(parent_nodeid: ua.NodeId, *variants: ua.Variant) -> Any:
        args = [variant.Value for variant in variants]
        try:
            outputs = await handler(*args)
        except Exception as error:
            status = status_for(error)
            if status == ua.StatusCodes.BadInternalError:
                logger.exception("%s failed", label)
            else:
                logger.warning("%s rejected (%s): %s", label, ua.StatusCode(status).name, error)
            if on_error is not None:
                await on_error(message_for(label, error))
            return ua.StatusCode(status)
        if on_error is not None:
            await on_error("")
        return outputs or []

    server.link_method(node, callback)


def bind_not_supported(server: Server, node: Node, *, label: str) -> None:
    """Link a mandatory-but-unimplemented method (e.g. the DI Lock services) to BadNotSupported."""

    async def callback(parent_nodeid: ua.NodeId, *variants: ua.Variant) -> Any:
        logger.info("%s called but is not supported by this mock", label)
        return ua.StatusCode(ua.StatusCodes.BadNotSupported)

    server.link_method(node, callback)
