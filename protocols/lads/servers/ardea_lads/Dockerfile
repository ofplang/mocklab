# Build-time only: cut this server's section out of the lab-wide command-duration file -- the same
# slice its SiLA2 counterpart bakes in, so both protocols wait the same time. Kept in its own stage
# so PyYAML never reaches the runtime image.
FROM python:3.14-slim AS durations
ARG DURATIONS_FILE=command_durations.yaml
RUN pip install --no-cache-dir pyyaml
COPY config/ /tmp/config/
COPY tools/slice_durations.py /tmp/slice_durations.py
RUN python /tmp/slice_durations.py --device ardea --input "/tmp/config/${DURATIONS_FILE}" --output /tmp/command_durations.json

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# NOTE: the build context is the REPOSITORY ROOT (see docker-compose.yml): the image needs the
# shared packages and the lab-wide duration file as well as this server's own package, so every
# COPY path below is repo-relative.

# Third-party dependency first, so editing our own sources does not invalidate this layer. Pinned
# like lads_common's own requirement.
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir "asyncua>=2.0.1,<2.1"

# Our own packages, in dependency order so pip resolves each from the one before it and never looks
# online; each source tree is deleted in the layer that installs it.
COPY laboratory-client/ /tmp/laboratory-client/
RUN pip install --no-cache-dir /tmp/laboratory-client && rm -rf /tmp/laboratory-client
COPY instruments/ /tmp/instruments/
RUN pip install --no-cache-dir /tmp/instruments && rm -rf /tmp/instruments
COPY protocols/lads/lads_common/ /tmp/lads_common/
RUN pip install --no-cache-dir /tmp/lads_common && rm -rf /tmp/lads_common
COPY protocols/lads/servers/ardea_lads/ /tmp/ardea_lads/
RUN pip install --no-cache-dir /tmp/ardea_lads && rm -rf /tmp/ardea_lads

# This server's command durations, sliced out of the lab-wide file above.
COPY --from=durations /tmp/command_durations.json /app/command_durations.json

EXPOSE 4840

CMD ["python", "-m", "ardea_lads", "--ip-address", "0.0.0.0", "--port", "4840", "--insecure"]
