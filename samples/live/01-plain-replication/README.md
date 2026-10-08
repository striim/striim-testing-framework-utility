# 01 Plain replication

The simplest live test: copy three rows from a Postgres source table to a target table and check that
the target holds exactly the rows you expect.

- `ddl_source.sql`, `ddl_target.sql` create the two tables (`${TID}` makes the names unique per run).
- `seed.sql` inserts three fixed rows before the app is deployed.
- `app.tql` is the app under test: a `DatabaseReader` initial load into a `DatabaseWriter`.
- `expected/rows.csv` is the golden, written by hand from `seed.sql`. It is never learned from a run.
- `test.yaml` ties it together:
  - `exact:` compares the target with the golden by typed value: `amount` as `decimal:2` and
    `created_at` as `timestamptz`, so formatting differences do not matter and a missing,
    extra, duplicated or changed row fails;
  - `lifecycle:` waits until the source rows have landed and the target count equals the source
    count for two seconds before the comparison runs.

Run it from the framework clone:

```
striim-test run samples/live/01-plain-replication
```
