# 04 Lifecycle check

A change-data-capture (CDC) test whose correct result is an empty target. An empty table on its own
proves nothing: the pipeline may simply not have run yet. This sample shows how the `lifecycle:` block
proves that it did.

- `PostgreSQLReader` reads an owned, per-run `wal2json` replication slot (`slot.sql`, kept apart from
  the table DDL because Postgres will not create a slot after a write in the same transaction) and
  `DatabaseWriter` applies every change to the target.
- Once the app is running, `changes.sql` commits three transactions in order: insert ids 101, 102 and
  103; update 102; delete all three. The golden `expected/rows.csv` is therefore a header with no rows.
- Readiness and completion are sentinels: the framework inserts a row with an id unique to this run
  (`sentinel_insert.sql`), waits to see it on the target, deletes it (`sentinel_delete.sql`) and waits
  for it to disappear. Completion repeats this after `changes.sql`, and the target must then stay
  unchanged for five seconds. A sentinel left by another run, or a zero row count alone, satisfies
  neither.

The framework's Docker Postgres is ready for CDC. Your own Postgres (README "Your own database") needs
`wal_level=logical`, the `wal2json` output plugin installed, and the `REPLICATION` attribute on the
source user (`SLT_PG_SOURCE_USER`). Without them, creating the replication slot fails.

```
striim-test run samples/live/04-lifecycle-check
```
