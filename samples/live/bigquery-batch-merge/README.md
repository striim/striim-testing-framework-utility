# Bounded batch CDC MERGE

The target starts with `(1,old),(2,keep)`. PostgreSQL CDC sends the literal source
rows and then updates 1 to new, deletes 2, and inserts `(3,fresh)`. The hand-derived
golden is exactly `(1,new),(3,fresh)`. OptimizedMerge is off.

Both readiness and completion insert a fresh sentinel through the same source
and writer, observe it in the final target, delete it at the source, and observe
its removal. Exact comparison then checks every final value and multiplicity.
APPENDONLY cannot meet this insert/delete witness.

For a BigQuery CDC lifecycle, the framework substitutes a fresh per-test dataset
for `${BIGQUERY_DATASET}` before rendering DDL or TQL. It refuses a pre-existing
dataset, records its creation and binding in the ownership ledger, and removes
that exact dataset including generated staging objects after stopping the app.
The shared bootstrap dataset is retained. Kept resources retain this ledger scope.
Never put unrelated data into a dataset allocated to a case.

Build the pinned local service and set `SLT_BIGQUERY_IMAGE`, then run:

```
striim-test doctor --case samples/live/bigquery-batch-merge
striim-test run samples/live/bigquery-batch-merge
```

This proves bounded insert/update/delete completion. Final MERGE state does not
prove applied-operation order, replay multiplicity, crash recovery or cloud
semantics. The installed-client exact-chunk-boundary exclusion also applies.

Streaming MERGE is refused by this lane. The emulator has no streaming-buffer
restriction and does not model cloud quotas or DML concurrency limits. Real
BigQuery can reject DML over recent legacy-streamed rows; this batch fixture does
not qualify that behavior. MERGE without this owned CDC lifecycle is also refused.
