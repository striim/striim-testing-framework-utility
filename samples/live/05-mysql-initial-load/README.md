# 05 MySQL initial load

The same shape as `01-plain-replication`, on MySQL: `DatabaseReader` reads five rows that exist
before the app starts, and `DatabaseWriter` copies them to a target table.

- `ddl_source.sql` and `ddl_target.sql` create the tables in the `qasource` and `qatarget` schemas,
  with `${TID}` at the start of each name.
- `seed.sql` inserts five rows before deploy.
- `DatabaseWriter`'s `Tables` is a source,target pair.
- `expected/rows.csv` is written by hand from `seed.sql`.

`exact:` and `lifecycle:` read Postgres only, so this sample checks MySQL another way: `rows: 5`
pins the count, so a missing or duplicated row fails, and `match:` compares the rows' values as
text. The check is repeated until it passes or `timeout` runs out.

```
striim-test run samples/live/05-mysql-initial-load
```
