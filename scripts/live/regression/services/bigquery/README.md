# BigQuery legacy REST controls

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
