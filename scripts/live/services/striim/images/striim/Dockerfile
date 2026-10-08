# syntax=docker/dockerfile:1.4
FROM amd64/ubuntu:22.04

ARG STRIIM_VERSION
ARG JDK_VERSION
ARG INSTANTCLIENT_VERSION
ARG INSTANTCLIENT_ZIPFILE
ARG INSTANTCLIENT_FOLDER

EXPOSE 1527 9080 9081 5701-5702 7071 9300 49152-65535 54327/udp

WORKDIR /app/

# Preflight: verify EACH bind-mounted dependency is present (non-empty) BEFORE the
# apt-get layers, so a skipped download fails fast — listing exactly which files are
# missing — instead of a cryptic dpkg error deep in the build. (The harness
# auto-downloads via ensure_deps; a manual `docker compose build` does not.) Keep this
# list in sync with the deps consumed below, download-dependencies.sh, and
# REQUIRED_DEPS in striim_provision.py.
RUN --mount=type=bind,source=deps,target=/deps \
    missing=""; \
    for f in \
      striim-dbms-${STRIIM_VERSION}-Linux.deb \
      striim-node-${STRIIM_VERSION}-Linux.deb \
      striim-agent-${STRIIM_VERSION}-Linux.deb \
      striim-samples-${STRIIM_VERSION}-Linux.deb \
      sqljdbc_6.0.8112.200_enu.tar.gz \
      mysql-connector-java-8.0.30.zip \
      vertica-jdbc-25.3.0-0.jar \
      jmx_prometheus_javaagent-0.16.1.jar \
      ${INSTANTCLIENT_ZIPFILE}; do \
      [ -s "/deps/$f" ] || missing="${missing} $f"; \
    done; \
    if [ -n "$missing" ]; then \
      echo "ERROR: missing Striim build dependencies under scripts/live/services/striim/images/striim/deps/:" >&2; \
      for m in $missing; do echo "  - $m" >&2; done; \
      echo "Run  scripts/live/services/striim/download-dependencies.sh  (one-time, ~6.3 GB) to fetch them." >&2; \
      exit 1; \
    fi

# Update and install packages
RUN dpkg --add-architecture i386
RUN apt-get -y update
RUN apt-get -y upgrade
RUN apt-get -y install libaio1
RUN apt-get -y install apt-utils
RUN apt-get -y install iputils-ping
RUN apt-get -y install net-tools
RUN apt-get -y install unzip
RUN apt-get -y install emacs
RUN apt-get -y install vim
RUN apt-get -y install curl
RUN apt-get -y install wget
RUN apt-get -y install netcat
RUN apt-get -y install stress
RUN apt-get -y install openjdk-${JDK_VERSION}-jdk
RUN apt-get -y install libnuma1
RUN apt-get -y install libpam0g:i386
RUN apt-get -y install libstdc++6:i386
RUN apt-get -y install make
RUN apt-get -y install g++
RUN apt-get -y install netbase
RUN apt-get -y install file
RUN apt-get -y install patch
RUN apt-get -y install nfs-common

RUN mkdir -p /mnt/shared

# Install Striim (deps pre-downloaded into ./deps by download-dependencies.sh and
# bind-mounted at /deps during the build, so nothing is re-fetched on rebuild and
# the multi-GB artifacts never land in an image layer). Their presence is verified by
# the preflight right after WORKDIR above.
RUN --mount=type=bind,source=deps,target=/deps \
    dpkg -i /deps/striim-dbms-${STRIIM_VERSION}-Linux.deb
RUN --mount=type=bind,source=deps,target=/deps \
    dpkg -i /deps/striim-node-${STRIIM_VERSION}-Linux.deb
RUN --mount=type=bind,source=deps,target=/deps \
    dpkg -i /deps/striim-agent-${STRIIM_VERSION}-Linux.deb
RUN --mount=type=bind,source=deps,target=/deps \
    dpkg -i /deps/striim-samples-${STRIIM_VERSION}-Linux.deb

# JDBC Drivers
RUN --mount=type=bind,source=deps,target=/deps \
    tar -zxf /deps/sqljdbc_6.0.8112.200_enu.tar.gz -C /tmp && \
    cp /tmp/sqljdbc_6.0/enu/jre8/sqljdbc42.jar /opt/striim/lib/ && \
    cp /tmp/sqljdbc_6.0/enu/jre8/sqljdbc42.jar /opt/striim/agent/lib/ && \
    rm -rf /tmp/sqljdbc_6.0
RUN --mount=type=bind,source=deps,target=/deps \
    unzip -q /deps/mysql-connector-java-8.0.30.zip -d /tmp && \
    cp /tmp/mysql-connector-java-8.0.30/mysql-connector-java-8.0.30.jar /opt/striim/lib/ && \
    cp /tmp/mysql-connector-java-8.0.30/mysql-connector-java-8.0.30.jar /opt/striim/agent/lib/ && \
    rm -rf /tmp/mysql-connector-java-8.0.30

# Vertica: one jar, pure Java, straight onto the classpath. The same release as services/vertica's server.
RUN --mount=type=bind,source=deps,target=/deps \
    cp /deps/vertica-jdbc-25.3.0-0.jar /opt/striim/lib/ && \
    cp /deps/vertica-jdbc-25.3.0-0.jar /opt/striim/agent/lib/

# Your own JDBC drivers: every jar in deps/extra-lib/ goes onto the classpath, as for Vertica.
RUN --mount=type=bind,source=deps,target=/deps \
    for j in /deps/extra-lib/*.jar; do \
      [ -f "$j" ] || continue; \
      cp "$j" /opt/striim/lib/ && cp "$j" /opt/striim/agent/lib/; \
    done

# JMX Exporter
RUN --mount=type=bind,source=deps,target=/deps \
    mkdir -p /opt/striim/jmx/ && \
    cp /deps/jmx_prometheus_javaagent-0.16.1.jar /opt/striim/jmx/

# Oracle Instant Client
RUN --mount=type=bind,source=deps,target=/deps \
    unzip -q /deps/${INSTANTCLIENT_ZIPFILE} && \
    mv /app/${INSTANTCLIENT_FOLDER}/ /app/instantclient/

# Configure Volumes
RUN mkdir /shared/ && \
    chown -R striim:striim /shared/ && \
    chown -R striim:striim /app/ && \
    chown -R striim:striim /opt/striim/

USER striim

# Configure Striim Server
RUN cp /opt/striim/conf/startUp.properties /opt/striim/conf/startUp.properties.orig && \
    cp /opt/striim/bin/server.sh /opt/striim/bin/server.sh.orig
COPY --chown=striim:striim ./files/jmx_prometheus_config.yaml /opt/striim/jmx/

# Configure Striim Agent
RUN cp /opt/striim/agent/conf/agent.conf /opt/striim/agent/conf/agent.conf.orig && \
    cp /opt/striim/agent/bin/agent.sh /opt/striim/agent/bin/agent.sh.orig

# NOTE: keystores are NOT generated here. entrypoint.sh generates them at runtime on the
# primary (sksConfig -> sks.jks; aksConfig AFTER the node is up -> aks.jks) and shares them
# via /shared for the node + agent to reuse. The documented test-only credential is
# admin/striim (sksConfig -a striim -s striim -k striim). Build-time aksConfig cannot
# authenticate the sys user (no live node at build) — see commit daa678c8 — so both
# keystore steps live in the entrypoint, not here.
COPY --chown=striim:striim ./files/entrypoint.sh /app/
# CMD execs the entrypoint directly, so it must be executable whatever mode the
# checkout gave it (a blob at 100644 failed with `exec: "/app/entrypoint.sh": permission denied`).
RUN chmod 0755 /app/entrypoint.sh

# A copy of this image's build inputs, so the harness can ask the IMAGE what it was built from
# instead of inferring it. `ensure_image` extracts this directory and compares it byte-for-byte
# with the checkout; any difference means the image is stale and gets rebuilt.
#
# Why a copy rather than a hash in the tag or a label: a tag encodes the answer in a NAME, which
# every consumer that parses or builds that name must then agree about (plugin.py, stack-doctor,
# the console, the docs -- each one broke when the format changed), and a label needs a digest
# computed identically in Python AND shell. Bytes need no agreement. ~16 KB, last layer, so it
# costs one cached step on an unchanged rebuild.
# Two COPYs, not one with two sources: `COPY ./files /dest/` copies the CONTENTS of files/,
# flattening it, so the layout inside would not match the checkout's and every image would
# read as stale. Keeping `files/` as a directory makes the comparison a plain tree diff.
COPY ./Dockerfile /slt-build-inputs/Dockerfile
COPY ./files /slt-build-inputs/files

CMD [ "/app/entrypoint.sh" ]
