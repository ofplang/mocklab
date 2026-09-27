# Build-time only: cut this server's section out of the lab-wide command-duration file. Kept in
# its own stage so PyYAML -- needed to read the YAML, and nothing else needs it -- never reaches
# the runtime image, which reads the sliced JSON with the standard library.
#
# DURATIONS_FILE selects the profile; the whole config/ directory is copied in so it can be
# chosen by name at build time (`DURATIONS_FILE=command_durations.realistic.yaml docker compose
# build`). Durations are baked rather than mounted, so changing them is a rebuild.
FROM python:3.14-slim AS durations
ARG DURATIONS_FILE=command_durations.yaml
RUN pip install --no-cache-dir pyyaml
COPY config/ /tmp/config/
COPY tools/slice_durations.py /tmp/slice_durations.py
RUN python /tmp/slice_durations.py --device thermal-cycler --input "/tmp/config/${DURATIONS_FILE}" --output /tmp/command_durations.json

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# NOTE: the build context is the REPOSITORY ROOT, not this directory (see `context: .` in
# docker-compose.yml), because the image needs the shared laboratory_client, the shared instrument
# behaviour and the lab-wide duration file as well as this server's own package. Every COPY path below is therefore
# repo-relative, and .dockerignore is what keeps that wider context small.

# Third-party dependencies first, so editing our own sources does not invalidate this layer.
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir sila2 typer

# Both of our own trees are staged under /tmp and deleted in the same layer as their install, so
# the runtime image carries only what pip put in site-packages: nothing to keep in sync with the
# installed copy, and no source tree left on the import path.

# The shared laboratory-model HTTP client, installed ahead of the server package so the server's
# own `laboratory-client` dependency is already satisfied and pip never goes looking for it
# online.
COPY laboratory-client/ /tmp/laboratory-client/
RUN pip install --no-cache-dir /tmp/laboratory-client && rm -rf /tmp/laboratory-client

# The protocol-independent instrument behaviour -- state, rules, durations, Status and world-model
# effects -- which this server only exposes over SiLA2. Shared with the LADS OPC UA servers.
COPY instruments/ /tmp/instruments/
RUN pip install --no-cache-dir /tmp/instruments && rm -rf /tmp/instruments

# This server's own package.
COPY sila2/servers/automated_thermal_cycler_server/ /tmp/automated_thermal_cycler_server/
RUN pip install --no-cache-dir /tmp/automated_thermal_cycler_server && rm -rf /tmp/automated_thermal_cycler_server

# This server's command durations, sliced out of the lab-wide file above.
COPY --from=durations /tmp/command_durations.json /app/command_durations.json

EXPOSE 50052

# Start the SiLA2 server without TLS for local use; override in compose if needed
CMD ["python", "-m", "automated_thermal_cycler_server", "--ip-address", "0.0.0.0", "--port", "50052", "--insecure"]
