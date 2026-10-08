# Postgres service recipe

Using this service in a test (accounts, tokens, routes, using your own instance): [docs/SERVICES.md](../../../../docs/SERVICES.md).

How it is built:

- `Dockerfile`: `postgres:16` plus `postgresql-16-wal2json` from the image's PGDG apt repository.
  Striim's `PostgreSQLReader` needs `wal2json` as its logical-decoding output plugin.
- `compose.yaml` starts the server with `wal_level=logical` and enough replication slots and WAL
  senders for parallel CDC tests, and allows `wal2json` as an output plugin (Postgres 16 refuses it
  otherwise).
- `init-qausers.sql` creates the `qasource` and `qatarget` roles (with `REPLICATION`) and schemas on
  first start. `PgAdmin.ensure_setup` (`livetest/pgclient.py`) re-creates them when missing, which is
  what makes an existing instance work the same way.
- The healthcheck is `pg_isready -U postgres -d sltdb`.
- Stale replication slots are swept only during service pre-flight, when no other run is registered
  on the endpoint; each test drops its own slot at teardown.
