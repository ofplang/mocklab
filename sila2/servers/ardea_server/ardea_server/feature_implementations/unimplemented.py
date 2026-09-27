# Shared refusal for the commands this mock does not perform.
#
# The Ardea mock serves the real machine's nine Feature definitions unchanged but implements
# one command out of twenty-three (`LabwareService.Transfer`) plus three read-only properties.
# The other twenty-two all refuse in the same way, and they refuse from one place so the
# wording is identical wherever it is read -- someone meeting two of them should not have to
# wonder whether a difference in phrasing means a difference in kind.
#
# WHY NotImplementedError AND NOT A DECLARED SiLA ERROR. sila2 translates NotImplementedError
# into the standard "not implemented" error, which is exactly the right answer here: "this
# server does not do that" is not one of the failures the real machine's Feature definitions
# describe. Declaring a new error for it would mean editing those definitions, and then the
# mock would no longer be swappable with the machine (`docs/RULES.md`). So the refusal
# deliberately lands outside the declared error space.
#
# WHAT THE CLIENT ACTUALLY SEES: `UndefinedExecutionError: Method is not implemented by the
# server`. sila2 supplies that wording itself and **discards the message built below**, so the
# explanation is logged rather than transmitted -- which is why this function logs at all. A
# client gets an unambiguous "not implemented"; an operator reading `docker compose logs` gets
# the reason.

from __future__ import annotations

import logging
from typing import NoReturn

logger = logging.getLogger(__name__)


def unimplemented(feature: str, command: str) -> NoReturn:
    """Refuse `feature.command`, naming the one command this mock does perform.

    Both halves are passed in rather than derived, because the caller is the generated method
    override and the fully qualified name is what identifies the refusal in a log."""
    message = (
        f"{feature}.{command} is not implemented by this Ardea mock. It drives real hardware "
        "that the laboratory model has no counterpart for; LabwareService.Transfer is the only "
        "command this server performs."
    )
    # Logged as well as raised: the client receives sila2's own "not implemented" wording, so
    # this line is the only place the reason appears. INFO rather than WARNING -- a refusal is
    # this server working as designed, not a fault.
    logger.info(message)
    raise NotImplementedError(message)
