# 12 Consistent target snapshots

The transaction-session sample writes each row to two source tables in the same
transaction. B commits both id 2 rows, then A commits both id 1 rows after recovery.
A background observer reads both target tables in one repeatable-read transaction
per poll. The declared states are empty/empty, B/B, and full/full. Their CSVs are
hand-derived from the commit schedule; no captured output defines a prefix.

```
striim-test run samples/live/12-snapshot-observer
```

Deliberately broken input: remove the auxiliary insert from b.sql. Once id 2
lands in just one target, a snapshot must fail as snapshot-not-committed-prefix.
Sampling checks each observed state; it cannot prove that an invalid state never
existed between reads. The interval is explicit and every observed prefix is recorded.

Recovery is enabled explicitly; commit evidence records source transaction IDs,
server commit status and both source key sets. Failed completion records target
id values. If completion fails before observer assertions, abandon retains the
full bounded history and the worker error, with UTC and monotonic timestamps.
The observer reports a worker failure when it occurs. Expected rows are unchanged.

The reader passes transaction boundaries (`FilterTransactionBoundaries: false`)
and the writer preserves them (`PreserveSourceTransactionBoundary: true`).
Without both settings, a partial target transaction is expected and can fail the
observer. Undeclared-state evidence lists bounded id values for each target table;
the comparison still checks every declared column and duplicate count.
