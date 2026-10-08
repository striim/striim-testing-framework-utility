# Postgres for the live tier, with the wal2json logical-decoding output plugin —
# Striim's PostgreSQLReader (CDC) attaches to a wal2json replication slot, which the
# stock postgres image does not ship. Vanilla Postgres otherwise (DatabaseReader/
# DatabaseWriter tests are unaffected). The official image already configures the
# PGDG apt repo, so the versioned wal2json package installs directly.
FROM postgres:16
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-wal2json \
    && rm -rf /var/lib/apt/lists/* \
    && zic /usr/share/zoneinfo/tzdata.zi
# Provision the qasource/qatarget data roles + schemas at first cluster init, so a
# bare `docker compose up` has the users a developer/DBeaver expects (the live-test
# harness also ensures these idempotently at test-resolve). Runs against POSTGRES_DB.
COPY init-qausers.sql /docker-entrypoint-initdb.d/10-qausers.sql
