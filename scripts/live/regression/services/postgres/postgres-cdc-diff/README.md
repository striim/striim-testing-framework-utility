# postgres-cdc-diff

Exercises **PostgreSQLReader (CDC / logical decoding)** → DatabaseWriter within one
Postgres database. Unlike `postgres-diff` (which uses `DatabaseReader`, an
initial-load query), this reads the WAL via a logical replication slot, so the seed
runs **after** the app is RUNNING (`when: post_start`) — the slot only captures
commits made after it exists. The diff tier asserts `tgt` catches up to `src`.

Requires the Postgres service running with `wal_level=logical` and the **wal2json**
plugin (both provided by `services/postgres/`), plus a source role with `REPLICATION`
(`qasource`).

Live-validated (Striim 5.4.0.6): `PostgreSQLReader` needs `ReplicationSlotName` and a
**pre-created wal2json slot** — it does not auto-create one. The slot is created in
`slot.sql` (a separate DDL file, since `pg_create_logical_replication_slot` cannot run
in a transaction that has already written, e.g. the `CREATE TABLE`s).
