[![CI](https://github.com/ofplang/mocklab/actions/workflows/ci.yml/badge.svg)](https://github.com/ofplang/mocklab/actions/workflows/ci.yml)

## Overview

A virtual laboratory: five mock instruments -- four instruments and a transporter -- served over
**SiLA2** or **LADS OPC UA** (or both), plus a shared `laboratory_model` service that simulates
the physical world they act on. It exists so a workflow execution system can be exercised
against the interfaces real instruments expose, before there are real instruments.

Each instrument's behaviour is written once (`instruments/`) and presented by both protocols, so
the two cannot drift apart: what LADS changes is only how a thing is asked, never what happens.
The LADS mapping, and the few differences the LADS conventions force, are in
`docs/LADS_MAPPING.md`.

The transporter, **Ardea**, mocks a machine that exists: it serves that machine's own nine
Feature definitions unchanged, and implements the one command a workflow needs from a
transporter (`LabwareService.Transfer`). Like the machine, it is called with station *names* --
`Base1` and `Base2` are the two plain plate-holding stations, `Base3`-`Base6` the four
instruments -- which its station map turns into places in this world. See `docs/SERVERS.md`.

Docker Compose serves (the protocol is chosen by profile, below):

| Instrument | SiLA2 (profile `sila2`) | LADS OPC UA (profile `lads`) |
|---|---|---|
| shared world state | `laboratory-model` : 8001 (always) | same |
| Microplate Centrifuge | `sila2-server-1` : 50052 | `lads-server-1` : 4841 |
| PlateLoc | `sila2-server-2` : 50053 | `lads-server-2` : 4842 |
| Automated Plate Seal Remover | `sila2-server-3` : 50054 | `lads-server-3` : 4843 |
| Automated Thermal Cycler | `sila2-server-4` : 50055 | `lads-server-4` : 4844 |
| Ardea (arm on a travel carriage) | `ardea-server-1` : 50057 | `ardea-lads-server-1` : 4847 |

The station -- the workflow's entry and exit point -- has no server. It is two slots declared
by the seed, and a rack has nothing commandable about it. 50056 is free because the server that
used to sit there offered only `Reset` and a `Status` that never changed; Ardea keeps 50057 so
anything already pointed at the lab's transporter still finds it.

All servers start with `--insecure --verbose`: plain gRPC for SiLA2, and for LADS OPC UA an
endpoint with security mode `None` and anonymous access. A client therefore connects without
certificates to either.

**The dependency runs one way.** This repository knows nothing about whoever drives it; a
client depends only on "several ordinary SiLA2 (or LADS OPC UA) services are running". `laboratory_model`
stands in for the physical world, so **a workflow client must not talk to it** -- there is no
such interface on a real bench. See `docs/RULES.md`.

## Start and stop

Prerequisites: Docker and Docker Compose.

Choose the protocol with a profile; the laboratory model always starts:

```bash
docker compose --profile sila2 up -d                  # the SiLA2 lab
docker compose --profile lads up -d                   # the LADS OPC UA lab
docker compose --profile sila2 --profile lads up -d   # both, over one shared world
docker compose --profile "*" down                     # stop everything, whichever was started
docker compose --profile "*" ps
docker compose --profile "*" logs --tail=120
```

To make one the default, copy `.env.example` to `.env` (it sets `COMPOSE_PROFILES=sila2`); a
plain `docker compose up -d` then starts the SiLA2 lab. `.env` is not tracked, so each checkout
chooses for itself. **With no profile at all, only the laboratory model starts.** The commands
below assume a profile has been chosen one of these ways.

After changing source, build and recreate explicitly -- `up -d --build` does not reliably pick
a change up:

```bash
docker compose build
docker compose up -d --force-recreate
```

## Project layout

| Directory | Contents |
|---|---|
| `instruments/` | What each mock instrument does -- state, rules, timing, status, world-model effects -- independent of the protocol serving it |
| `protocols/sila2/servers/` | SiLA2 server package per mock instrument (`<instrument>_mock_sila2`): a thin adapter over `instruments/` |
| `protocols/lads/servers/` | LADS OPC UA server package per mock instrument (`<instrument>_mock_lads`): which command becomes which node, over `instruments/` |
| `protocols/lads/lads_common/` | What every LADS server shares: the bundled NodeSets, the state machines, programs and results, functions, StatusCode mapping, server start-up |
| `laboratory_model/` | Shared world-state service (devices, spots, opaque device state) |
| `laboratory-client/` | Shared package the servers use to reach the world model |
| `config/` | The world's seed and the command duration profiles |
| `tools/` | Build-time helpers (the duration slicer) |
| `samples/` | Client scripts that check a running stack, over either protocol |
| `docs/` | The documents listed under *Documentation* below |
| `protocols/sila2/specs/` | Source SiLA Feature XML. **Not edited** -- a mock has to keep the real instrument's Feature to be a drop-in replacement |
| `external/` | Optional, local only (`.gitignore`d): reference sources kept to read, never a development target. Absent from a fresh clone |

## World model

The world is a declared set of devices, each holding a fixed set of spots plus an opaque bag of
state.

- A location is always `device.spot` (`station.slot1`, `centrifuge.deck`). There is no shorthand
  for "the device's only spot".
- **The topology is declared by the seed and does not grow at runtime**: addressing a device or
  spot that was never declared is a 404, so a workflow asking to move a plate somewhere that
  does not physically exist fails where the mistake is.
- A spot holds at most one item and has its own `accessible` flag; the model refuses to reach
  into a spot that is not accessible.
- Device `state` is stored verbatim and read by no rule in the service.

`config/laboratory_model.seed.yaml` describes t=0. `POST /reseed` rereads it; `POST /reset`
empties the world but keeps the topology. Details in `docs/LABORATORY_MODEL.md`.

## Command timing

How long each command takes is configuration, not a literal in the implementation.
`config/command_durations.yaml` describes the whole lab and each server's section is baked into
its image at build time. **The default profile is empty, so by default no command waits at all**;
the realistic profile runs at instrument speed, long enough for a polling client to observe a
dispatch/running/completed transition.

```bash
DURATIONS_FILE=command_durations.realistic.yaml docker compose build
docker compose up -d --force-recreate
```

A command not listed waits for nothing, and a server refuses a command arriving while another
is still executing. Details, and why the realistic profile needs `--timeout` on the samples, in
`docs/TIMING.md`.

## Unit tests

Component tests that need no Docker; they run in process and finish in seconds.

Everything from here down runs through [`uv`](https://docs.astral.sh/uv/), the one prerequisite
besides Docker. The project targets **Python 3.14** (`requires-python = ">=3.14"`, pinned by
`.python-version`); uv provisions that interpreter itself, so no system Python of that version is
needed.

```bash
uv run pytest
```

They cover `laboratory_model` (world rules, HTTP contract, seeding), `laboratory-client` (HTTP
transport and configuration), `instruments` (each instrument's behaviour, protocol aside), the
LADS runtime and servers (a real OPC UA server and client, in process), and `tools` (the
duration slicer, plus a check that the duration files and the instruments agree about which
commands wait). Tests live next to the component they cover; dependencies and configuration are
in the root `pyproject.toml`.

## Lint and type checking

```bash
uv run ruff check .
uv run mypy
```

Both are configured in the root `pyproject.toml` and cover hand-written code only: the
`generated/` trees and each server's `__main__.py` belong to the sila2 code generator.

## Samples

These talk to a running stack over the network, so they check the deployment rather than the
rules. Bring the stack up first. They exit non-zero on failure.

```bash
uv run python samples/run_all_smoke_tests.py                 # all five SiLA2 servers
uv run python samples/run_all_smoke_tests.py --protocol lads # all five LADS servers (or: both)
uv run python samples/laboratory_model_smoke.py              # the world model on its own
uv run python samples/run_roundabout.py                      # one plate around the whole lab, SiLA2
uv run python samples/run_lads_roundabout.py                 # the same circuit over LADS
uv run python samples/run_parity.py                          # both protocols must end the same way
```

The LADS samples share a small asyncio client, `samples/lads_client.py` -- a readable example of
driving a LADS functional unit (`StartProgram` and waiting for the Result, Stop/Abort/Clear,
covers, set-points) without any client framework.

`run_roundabout.py` puts one item at `station.slot1` and moves it through
`seal-remover.stage`, `plateloc.stage`, `thermal-cycler.block`, `centrifuge.deck` and back. Each
leg is one `LabwareService.Transfer` on Ardea. The movement goes through the servers; the world
model is used only to arrange the start and check the end. `run_parity.py` runs that circuit over
SiLA2 and then over LADS (both profiles up) and checks that the world, the instruments' readouts
and the reasons given for the same mistakes all come out the same.

Each sample wipes the world on entry, so do not run them against a stack that is mid-workflow,
and do not run two at once.

## Documentation

| Document | Scope |
|---|---|
| `docs/RULES.md` | Policies, including how this repository relates to whoever drives it |
| `docs/SUMMARY.md` | Repository overview |
| `docs/LABORATORY_MODEL.md` | World model: state, API, seeding, lifecycle |
| `docs/SERVERS.md` | Per-server implementation notes and the Status contract |
| `docs/TIMING.md` | Command durations and the one-at-a-time guard |
| `docs/LADS_MAPPING.md` | Where each SiLA2 command appears in LADS OPC UA, and the differences LADS forces |
| `docs/OPERATIONS.md` | Runbook: start, verify, reset, switch profiles |

## Container networking

From the host, reach each SiLA2 server on its published port (`50052`-`50055` and `50057`), each
LADS server on `opc.tcp://localhost:<port>/` (`4841`-`4844` and `4847`) and the laboratory model on
`8001`. Between containers, use the service's container name with the *internal* port (`50052`
for SiLA2 servers, `4840` for LADS servers, `8001` for `laboratory-model`); the published ports are
for host access and are not usable verbatim from inside another container.

The LADS ports sit next to OPC UA's registered port (4840), so another OPC UA stack on the same
machine may already hold them. Publish them elsewhere with an override file rather than by
editing `docker-compose.yml` -- for example a local `docker-compose.lads-ports.yml` that gives each
LADS service `ports: !override ["14841:4840"]` (and so on), started with
`docker compose -f docker-compose.yml -f docker-compose.lads-ports.yml --profile lads up -d`. A
client then has to be pointed at the new ports.

## License

MIT ([LICENSE](LICENSE)), with one carve-out stated there: the SiLA Feature definitions under
`protocols/sila2/specs/ardea_server/` and the generated code under
`protocols/sila2/servers/ardea_mock_sila2/ardea_mock_sila2/generated/` are verbatim copies from the real machine's
servers, so they carry those projects' terms rather than this repository's.
`protocols/sila2/specs/ardea_server/README.md` records where each file came from.
