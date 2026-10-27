FROM docker.io/library/debian:bookworm-slim

ARG TOOLBOX_VERSION=dev
ENV TOOLBOX_VERSION=${TOOLBOX_VERSION}

LABEL org.opencontainers.image.title="R1 Toolbox" \
      org.opencontainers.image.description="One-shot host maintenance for Hiksemi R1 NAS" \
      org.opencontainers.image.source="https://github.com/Shinku-Chen/R1-Toolbox" \
      org.opencontainers.image.version="${TOOLBOX_VERSION}"

WORKDIR /opt/r1-toolbox
COPY tools/boot-once/runner.sh ./runner.sh
COPY docker/entrypoint.sh /usr/local/bin/r1-toolbox
RUN chmod 755 /usr/local/bin/r1-toolbox /opt/r1-toolbox/runner.sh

ENTRYPOINT ["/usr/local/bin/r1-toolbox"]
CMD ["run"]
