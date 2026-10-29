FROM docker.io/library/debian:bookworm-slim AS build-ui

# Compile against the same Debian generation as the NAS Python environment.
# Compiler and package caches stay in this disposable stage, never the image.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev python3
WORKDIR /src
COPY scripts/build-ui.py ./scripts/build-ui.py
COPY ui ./ui
COPY runtime ./runtime
COPY native/fan_guard.c ./native/fan_guard.c
RUN gcc -shared -fPIC -O2 -std=c11 -Wall -Wextra -Werror -pthread \
      -o /tmp/fan_guard.so native/fan_guard.c -ldl \
    && python3 scripts/build-ui.py --native-guard /tmp/fan_guard.so

FROM docker.io/library/debian:bookworm-slim

ARG TOOLBOX_VERSION=dev
ENV TOOLBOX_VERSION=${TOOLBOX_VERSION}

LABEL org.opencontainers.image.title="R1 Toolbox" \
      org.opencontainers.image.description="Resident Web, MCP and screen control for Hiksemi R1 NAS" \
      org.opencontainers.image.source="https://github.com/Shinku-Chen/R1-Toolbox" \
      org.opencontainers.image.version="${TOOLBOX_VERSION}"

WORKDIR /opt/r1-toolbox
COPY tools/boot-once/runner.sh ./runner.sh
COPY --from=build-ui /src/build/ui-bundle.tar /opt/r1-toolbox/ui-bundle/ui.tar
COPY --from=build-ui /src/build/ui-bundle.sha256 /opt/r1-toolbox/ui-bundle/ui.sha256
COPY docker/entrypoint.sh /usr/local/bin/r1-toolbox
RUN chmod 755 /usr/local/bin/r1-toolbox /opt/r1-toolbox/runner.sh

ENTRYPOINT ["/usr/local/bin/r1-toolbox"]
CMD ["run"]
