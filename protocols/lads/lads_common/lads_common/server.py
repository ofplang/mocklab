"""
Shared server runtime and command line for the LADS OPC UA servers.

Every LADS server package provides a `ServerSpec` and a `build(context)` coroutine that creates its
device model over its instrument; this module owns everything else, so the servers differ only in
how they present their instrument.

Configuration follows the SiLA2 servers of this repository: the command line carries only how to
serve (address, port, security, verbosity) and everything that describes *this* instance comes from
the environment, so compose can configure each one:

    SiLA2 server                        LADS server
    --ip-address / --port 50052         --ip-address / --port 4840
    --insecure                          --insecure     (SecurityPolicy None, anonymous)
    SILA_SERVER_NAME / SILA_SERVER_TYPE LADS_SERVER_NAME / LADS_SERVER_TYPE
    LABORATORY_MODEL_URL / _LOCATION    same (read by the instrument itself)
    COMMAND_DURATIONS_FILE              same (read by the instrument itself)

Only `--insecure` is supported for now: certificate handling is a separate piece of work and not
needed for a local Docker mock.

Adapted from the lads-test prototype.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from asyncua import Server, ua

from .addressspace import ModelBuilder
from .bridge import InstrumentBridge
from .nodesets import Namespaces, import_lads_nodesets

logger = logging.getLogger(__name__)

SERVER_NAME_VARIABLE = "LADS_SERVER_NAME"
SERVER_TYPE_VARIABLE = "LADS_SERVER_TYPE"
# Prefix of each server's ApplicationUri, which doubles as a stable identity (like the SiLA2
# ServerUUID): built from the configured type and name, so it survives restarts.
APPLICATION_URI_PREFIX = "urn:ofplang:mock-lab"


@dataclass
class ServerContext:
    """Everything a server's build() needs."""

    server: Server
    builder: ModelBuilder
    bridge: InstrumentBridge
    ns: Namespaces
    server_name: str


BuildFunction = Callable[[ServerContext], Awaitable[None]]


@dataclass(frozen=True)
class ServerSpec:
    """Static description of one server: its default type (the counterpart of SILA_SERVER_TYPE)
    and name, and a description for `--help`."""

    server_type: str
    default_name: str
    description: str


def build_parser(spec: ServerSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=spec.description)
    parser.add_argument("-a", "--ip-address", default="0.0.0.0", help="The IP address to bind")
    parser.add_argument("-p", "--port", type=int, default=4840, help="The OPC UA TCP port")
    parser.add_argument("--insecure", action="store_true", help="Start without encryption (SecurityPolicy None)")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true", help="Only log errors")
    verbosity.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    verbosity.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def _initialize_logging(options: argparse.Namespace) -> None:
    level = logging.WARNING
    if options.verbose:
        level = logging.INFO
    if options.debug:
        level = logging.DEBUG
    if options.quiet:
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(asctime)s:%(levelname)s:%(name)s:%(message)s")
    # asyncua is very chatty at INFO (every Call and Browse); keep our own INFO lines -- the
    # "<Method> called" lines, like the SiLA2 servers' -- readable in `docker compose logs`.
    logging.getLogger("asyncua").setLevel(logging.DEBUG if options.debug else logging.WARNING)


async def start_server(spec: ServerSpec, build: BuildFunction, *, endpoint: str) -> tuple[Server, ServerContext]:
    """Create, populate and return a server that has not started listening yet.

    Split from `serve` so tests can run a real server in process."""
    server_name = os.getenv(SERVER_NAME_VARIABLE) or spec.default_name
    server_type = os.getenv(SERVER_TYPE_VARIABLE) or spec.server_type

    server = Server()
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name(server_name)
    await server.set_application_uri(f"{APPLICATION_URI_PREFIX}:{server_type}:{server_name}")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    ns = await import_lads_nodesets(server)
    context = ServerContext(
        server=server,
        builder=ModelBuilder(server, ns),
        bridge=InstrumentBridge(),
        ns=ns,
        server_name=server_name,
    )
    await build(context)
    return server, context


async def serve(spec: ServerSpec, build: BuildFunction, options: argparse.Namespace) -> None:
    if not options.insecure:
        raise SystemExit("Only --insecure is supported by the LADS servers for now")

    endpoint = f"opc.tcp://{options.ip_address}:{options.port}/"
    server, context = await start_server(spec, build, endpoint=endpoint)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # add_signal_handler is unavailable on Windows
            loop.add_signal_handler(sig, stop_event.set)

    async with server:
        logger.info("Server startup complete: %s at %s", context.server_name, endpoint)
        await stop_event.wait()
    await context.bridge.close()
    logger.info("Server shutdown complete")


def run(spec: ServerSpec, build: BuildFunction) -> None:
    """Entry point used by each LADS server package's __main__."""
    options = build_parser(spec).parse_args()
    _initialize_logging(options)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(spec, build, options))
