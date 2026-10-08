# 06 MySQL CDC

The MySQL counterpart of `04-lifecycle-check`'s CDC pipeline: `MySQLReader` reads the binary log
and `DatabaseWriter` applies every insert, update and delete to a copy.

- The tables start empty. `changes.sql` runs `when: post_start`, after the app is running, because
  `StartPositionByName: true` starts the reader at the current end of the binary log: it sees only
  changes made after it starts.
- `changes.sql` inserts orders 1001 to 1003, ships 1003 and deletes 1002, so the copy must end with
  exactly two rows. `expected/rows.csv` is written by hand from it.
- `rows: 2` pins the count and `match:` compares the values as text. The check is repeated until
  it passes or `timeout` runs out. `exact:` and `lifecycle:` read Postgres only.

The framework's MySQL container has binary logging in `ROW` format and a `qasource` user with
replication privileges. Your own MySQL needs the same ([docs/SERVICES.md](../../../docs/SERVICES.md)).

```
striim-test run samples/live/06-mysql-cdc
```
