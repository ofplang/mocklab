"""
Shared building blocks for the LADS OPC UA (OPC 30500) servers of the mock lab.

A LADS server here presents one instrument from `mock_instruments` -- the same object the SiLA2
server of that instrument presents -- in the LADS information model. Everything a server package
would otherwise repeat lives here: NodeSet loading, instantiating LADS types, state machines, the
bridge from the blocking instruments to asyncio, functional units with their programs and results,
functions, method binding with error reporting, and the server runtime. A server package only
decides which instrument command appears as which LADS node (`docs/LADS_MAPPING.md`).
"""

from .addressspace import ModelBuilder
from .bridge import InstrumentBridge
from .device import DeviceIdentity, LadsDevice
from .functional_unit import FunctionalUnit, ProgramDefinition, ProgramParameters
from .functions import (
    CELSIUS,
    MILLIMETRE,
    MILLISECOND,
    ONE,
    AnalogControlFunction,
    AnalogSensorFunction,
    CoverFunction,
    TimerControlFunction,
)
from .methods import LadsMethodError, bind_method, invalid_argument, invalid_state, not_found, status_for
from .server import ServerContext, ServerSpec, run, start_server

__all__ = [
    "CELSIUS",
    "MILLIMETRE",
    "MILLISECOND",
    "ONE",
    "AnalogControlFunction",
    "AnalogSensorFunction",
    "CoverFunction",
    "DeviceIdentity",
    "FunctionalUnit",
    "InstrumentBridge",
    "LadsDevice",
    "LadsMethodError",
    "ModelBuilder",
    "ProgramDefinition",
    "ProgramParameters",
    "ServerContext",
    "ServerSpec",
    "TimerControlFunction",
    "bind_method",
    "invalid_argument",
    "invalid_state",
    "not_found",
    "run",
    "start_server",
    "status_for",
]
