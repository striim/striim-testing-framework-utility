# copy-adds-userdata

The operator in its smallest form: every row read from a Postgres table is copied through
unchanged and stamped with `processed=true`, then fanned out to **two** targets so both halves of
that claim can be checked independently.

## What this sample proves

The operator makes exactly one change to an event, and one target alone cannot show that:

- The **Postgres replica** target receives the copied rows. Comparing it against the source table
  proves `data[]` travelled untouched — same columns, same values, same order.
- The **file** target receives the same stream formatted as JSON. Because that format carries
  userdata, it is where `processed=true` is visible. A database target has no column to put it in
  and silently drops it.

Run only the replica and you cannot see the stamp. Run only the file and you have not shown the
columns are unchanged.

## Files

| File | Purpose |
|---|---|
| `app.tql` | The pipeline: `DatabaseReader` → `ReferenceOpV1` → `DatabaseWriter` + `FileWriter` |
| `source_postgres_ddl.sql` | Creates the source `items` table (`id` integer primary key, `name` varchar) |
| `source_postgres_seed.sql` | Inserts the rows the reader picks up |
| `target_postgres_ddl.sql` | Creates the replica table in the target schema |

## The pipeline

```
DatabaseReader ──▶ ItemsSourceStream ──▶ ReferenceOpV1 ──▶ ProcessedStream ─┬─▶ DatabaseWriter (replica)
                                                                            └─▶ FileWriter + JSONFormatter
```

`EnableLogging` is `'true'` so each event is traced while you are watching it run.
`EnableInspection` is `'false'`, so the per-column presence notes are not written — this sample is
about the passthrough and the stamp, not about column presence.

## Running it

From the framework clone's root, with `STRIIM_HOME` set:

```bash
striim-test run samples/code/op
```

The test (`samples/code/op/referenceop-copy-adds-userdata/test.yaml`) builds the jar, loads it,
fills in the `${...}` tokens in `app.tql` (namespace, connection details, and a `${TID}` prefix that
keeps table names apart between runs), seeds the source table before the app starts, and checks
both targets. The reader performs an initial load, so it emits the rows that exist when it starts.

To deploy the app by hand instead, substitute your own values for the tokens and load the jar once:
upload `ReferenceOpV1-5.4.jar` to the server's `UploadedFiles/`, then
`LOAD OPEN PROCESSOR 'UploadedFiles/ReferenceOpV1-5.4.jar';`.
