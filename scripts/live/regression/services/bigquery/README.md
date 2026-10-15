# BigQuery writer controls

These finite initial-load fixtures require the pinned local `bigquery` service and Postgres.
`legacy-scalar` derives every expected row directly from its literal INSERT, preserving NULL,
empty text, pipe, quoted text, a newline, signed INT64 boundaries and decimal values at four
places. No target output is used to generate a golden.

`queue-size-2` is the independent positive arm for `queue-invalid`: capacity two must start
and deliver both literal rows exactly once. Capacity one must fail with the hidden property
name in the startup diagnostic. The negative arm must not pass for an authentication failure.
These do not prove queue saturation, backpressure, recovery, ordering or cloud equivalence.

Run each case separately with `striim-test doctor --case <directory>` and
`striim-test run <directory>`. Source and target tables are owned and scoped to the run.

The bounded batch gallery uses `StreamingUpload=false`, direct uploads and no GCS
staging. `batch-one` is the independent first-row canary; `batch-rollover` derives
five expected `(n,row-n)` rows from its literal inserts with eventCount two,
including the trailing single row. `batch-scalar` keeps the legacy scalar input
and hand-written golden unchanged, with quoted newlines explicitly enabled.

The installed Java 2.59.0 exact-chunk-boundary defect is excluded from bounded L2.
The [disabled positive client regression](../../../services/bigquery/tests/test_java_boundary.py)
still expects one exact row. Re-enable it with a corrected client and the original
payload; a skip does not qualify that boundary. No padding or chunk-size workaround
is used by these writer fixtures.
